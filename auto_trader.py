"""
auto_trader.py — CatHack AI Trader 司令塔
==========================================
H&S（4時間足）+ BB逆張り（1時間足）を監視し、
シグナルが出たら LINE 通知 → OANDA 発注（または DRY RUN ログ）。

起動方法:
    # ドライランモード（OANDAキーなしで動作確認）
    python3 auto_trader.py

    # 本番モード（.env に OANDA_API_KEY を設定後）
    DRY_RUN=false python3 auto_trader.py

停止: Ctrl+C
"""

from __future__ import annotations

import os
import sys
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np

# ─────────────────────────────────────────────────────────────
# ▌ .env 読み込み
# ─────────────────────────────────────────────────────────────
def _load_env():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())

_load_env()

ROOT    = Path(__file__).parent
JST     = ZoneInfo("Asia/Tokyo")

# ─────────────────────────────────────────────────────────────
# ▌ 設定
# ─────────────────────────────────────────────────────────────
DRY_RUN       = os.environ.get("DRY_RUN", "true").lower() != "false"
LOOP_SEC      = int(os.environ.get("LOOP_SEC", "300"))    # 5分ごとにチェック
LOT           = int(os.environ.get("LOT", "1000"))         # 通貨単位
SPREAD_PIPS   = float(os.environ.get("SPREAD_PIPS", "0.4"))
MAX_RETRY     = int(os.environ.get("MAX_RETRY", "3"))      # APIリトライ回数
SMA_PERIOD    = int(os.environ.get("SMA_PERIOD", "200"))   # トレンドフィルター用SMA期間

# 監視ペア設定
# yfinance ticker → 表示名 の辞書
PAIRS = {
    "USDJPY=X": "USD/JPY",   # BB逆張り(1h) + H&S(4h)
    "GBPJPY=X": "GBP/JPY",   # H&S(4h) バックテスト成績◎
}

# BB 設定
BB_PERIOD     = 14
BB_SIGMA      = 2.5
RSI_PERIOD    = 14
RSI_OS        = 30    # RSI 売られ過ぎ閾値
RSI_OB        = 70    # RSI 買われ過ぎ閾値

# ── 変動型 SL / TP（固定pips廃止） ────────────────────────────
# BB逆張り: ATRベース
BB_SL_MULT     = 3.0   # SL = エントリー ± ATR × BB_SL_MULT
BB_TP_MULT     = 6.0   # TP = エントリー ∓ ATR × BB_TP_MULT  (RR 1:2)
# H&S: テクニカル形状ベース
HS_BUFFER_PIPS = 0.05  # 右肩からのバッファ距離（5pips）
HS_RR          = 2.0   # H&S戦略のリスクリワード比
# ブレークイーブン（含み益がこの pips を超えたら建値にSL移動）
BREAKEVEN_PIPS = 20

# ─────────────────────────────────────────────────────────────
# ▌ ロガー
# ─────────────────────────────────────────────────────────────
def _setup_logger() -> logging.Logger:
    log = logging.getLogger("AutoTrader")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    fh = logging.FileHandler(ROOT / "auto_trader.log", encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    return log

logger = _setup_logger()

# ─────────────────────────────────────────────────────────────
# ▌ 外部モジュール読み込み
# ─────────────────────────────────────────────────────────────
sys.path.insert(0, str(ROOT))

try:
    from line_notify import notify_entry, notify_close, notify_error
    LINE_OK = True
except ImportError:
    LINE_OK = False
    logger.warning("line_notify.py が見つかりません。LINE通知は無効です。")

try:
    from trade_filter import TradeFilter
    tf = TradeFilter(max_spread_pips=1.5, news_buffer_min=30)
    news_path = ROOT / "news_events.json"
    if news_path.exists():
        tf.load_news_from_json(str(news_path))
    FILTER_OK = True
except ImportError:
    tf = None
    FILTER_OK = False
    logger.warning("trade_filter.py が見つかりません。フィルターは無効です。")

# OANDA executor（キーが来たら有効化）
try:
    from oanda_executor import OandaExecutor
    oanda = OandaExecutor()
    OANDA_OK = True
except ImportError:
    oanda = None
    OANDA_OK = False
    if not DRY_RUN:
        logger.warning("oanda_executor.py が見つかりません。DRY_RUN に切り替えます。")
        DRY_RUN = True

# ─────────────────────────────────────────────────────────────
# ▌ 戦績トラッカー（trade_stats.json に永続化）
# ─────────────────────────────────────────────────────────────
STATS_FILE      = ROOT / "trade_stats.json"
INITIAL_BALANCE = int(os.environ.get("INITIAL_BALANCE", "1000000"))  # 初期資金（円）


def _current_week() -> str:
    """ISO週番号文字列（例: "2026-W08"）"""
    d = datetime.now(JST)
    return f"{d.isocalendar().year}-W{d.isocalendar().week:02d}"


def load_stats() -> dict:
    """戦績を読み込む。なければ初期値を返す"""
    today = datetime.now(JST).strftime("%Y-%m-%d")
    week  = _current_week()
    default = {
        "daily_pips":    0.0,
        "daily_jpy":     0,
        "weekly_pips":   0.0,
        "weekly_jpy":    0,
        "weekly_trades": 0,
        "weekly_wins":   0,
        "total_pips":    0.0,
        "balance":       INITIAL_BALANCE,
        "last_date":     today,
        "last_week":     week,
    }
    if not STATS_FILE.exists():
        return default
    try:
        data = json.loads(STATS_FILE.read_text(encoding="utf-8"))
        # 日付が変わっていたら日次リセット
        if data.get("last_date") != today:
            data["daily_pips"] = 0.0
            data["daily_jpy"]  = 0
            data["last_date"]  = today
        # 週が変わっていたら週次リセット
        if data.get("last_week") != week:
            data["weekly_pips"]   = 0.0
            data["weekly_jpy"]    = 0
            data["weekly_trades"] = 0
            data["weekly_wins"]   = 0
            data["last_week"]     = week
        # 旧フォーマット互換（キーがなければデフォルト値で補完）
        for k, v in default.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return default


def save_stats(stats: dict):
    STATS_FILE.write_text(json.dumps(stats, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def update_stats(pnl_pips: float, lot: int = LOT) -> dict:
    """決済後に戦績を更新して返す"""
    pnl_jpy = int(pnl_pips * lot / 100)
    stats = load_stats()
    stats["daily_pips"]    = round(stats["daily_pips"]  + pnl_pips, 1)
    stats["daily_jpy"]     = stats["daily_jpy"]  + pnl_jpy
    stats["weekly_pips"]   = round(stats["weekly_pips"] + pnl_pips, 1)
    stats["weekly_jpy"]    = stats["weekly_jpy"] + pnl_jpy
    stats["weekly_trades"] = stats["weekly_trades"] + 1
    if pnl_pips >= 0:
        stats["weekly_wins"] = stats["weekly_wins"] + 1
    stats["total_pips"]    = round(stats["total_pips"]  + pnl_pips, 1)
    stats["balance"]       = stats["balance"] + pnl_jpy
    save_stats(stats)
    return stats

# ─────────────────────────────────────────────────────────────
# ▌ ポジション管理（position.json に永続化）
# ─────────────────────────────────────────────────────────────
POSITION_FILE = ROOT / "position.json"

def load_position() -> dict | None:
    """保存済みポジションを読み込む。なければ None"""
    if not POSITION_FILE.exists():
        return None
    try:
        data = json.loads(POSITION_FILE.read_text(encoding="utf-8"))
        return data if data.get("active") else None
    except Exception:
        return None

def save_position(pos: dict | None):
    """ポジションを保存（None で削除）"""
    if pos is None:
        POSITION_FILE.write_text(json.dumps({"active": False}), encoding="utf-8")
    else:
        pos["active"] = True
        POSITION_FILE.write_text(json.dumps(pos, ensure_ascii=False, indent=2),
                                 encoding="utf-8")

def clear_position():
    save_position(None)

# ─────────────────────────────────────────────────────────────
# ▌ データ取得
# ─────────────────────────────────────────────────────────────
def fetch_data(ticker: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """yfinance から指定ペアの 1h / 4h 足を取得"""
    import yfinance as yf
    end   = datetime.now(tz=timezone.utc)
    start = end - timedelta(days=90)
    raw   = yf.download(ticker, start=start, end=end,
                        interval="1h", progress=False, auto_adjust=True)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df_1h = raw[["Open","High","Low","Close","Volume"]].dropna()
    df_4h = raw.resample("4h").agg(
        {"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}
    ).dropna()
    return df_1h, df_4h

# ─────────────────────────────────────────────────────────────
# ▌ テクニカル計算
# ─────────────────────────────────────────────────────────────
def calc_bb(df: pd.DataFrame, period: int = BB_PERIOD, sigma: float = BB_SIGMA):
    """ボリンジャーバンド計算。(upper, mid, lower) を返す"""
    mid   = df["Close"].rolling(period).mean()
    std   = df["Close"].rolling(period).std()
    return mid + sigma * std, mid, mid - sigma * std

def calc_rsi(df: pd.DataFrame, period: int = RSI_PERIOD) -> pd.Series:
    """RSI 計算"""
    delta = df["Close"].diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def calc_adx(df: pd.DataFrame, period: int = 14) -> float:
    """ADX 計算（最終値のみ）"""
    high  = df["High"]
    low   = df["Low"]
    close = df["Close"]
    tr    = pd.concat([high - low,
                       (high - close.shift()).abs(),
                       (low  - close.shift()).abs()], axis=1).max(axis=1)
    dm_p  = (high.diff()).clip(lower=0)
    dm_m  = (-low.diff()).clip(lower=0)
    atr_  = tr.ewm(alpha=1/period, adjust=False).mean()
    di_p  = 100 * dm_p.ewm(alpha=1/period, adjust=False).mean() / atr_.replace(0, np.nan)
    di_m  = 100 * dm_m.ewm(alpha=1/period, adjust=False).mean() / atr_.replace(0, np.nan)
    dx    = (100 * (di_p - di_m).abs() / (di_p + di_m).replace(0, np.nan))
    adx   = dx.ewm(alpha=1/period, adjust=False).mean()
    return float(adx.iloc[-1]) if not adx.empty else 0.0

def calc_atr(df: pd.DataFrame, period: int = 14) -> float:
    """ATR（Average True Range）の最新値を返す"""
    high  = df["High"]
    low   = df["Low"]
    close = df["Close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    return float(atr.iloc[-1]) if not atr.empty else 0.001


def calc_sma200(df: pd.DataFrame, period: int = SMA_PERIOD) -> float:
    """SMA計算（最終値のみ）。データ不足時は全期間平均で補完"""
    if len(df) < period:
        return float(df["Close"].mean())
    val = df["Close"].rolling(period).mean().iloc[-1]
    return float(val) if not np.isnan(val) else float(df["Close"].mean())


def detect_hs(df: pd.DataFrame, window: int = 5, tol: float = 0.015) -> dict | None:
    """
    H&S パターン検知。右肩・頭・ネックライン情報を含む辞書を返す。

    戻り値:
        {
          "pattern":             "HEAD_AND_SHOULDERS" | "INV_HEAD_AND_SHOULDERS",
          "right_shoulder_high": float,   # 右肩高値（天井H&S の SL 基準）
          "right_shoulder_low":  float,   # 右肩安値（逆H&S の SL 基準）
          "head":                float,   # 頭の極値
          "neckline":            float,   # ネックライン（簡易）
        }
        or None
    """
    if len(df) < window * 5:
        return None

    roll_highs = df["High"].rolling(window).max()
    roll_lows  = df["Low"].rolling(window).min()

    head_h = roll_highs.iloc[-1]
    head_l = roll_lows.iloc[-1]

    prev3_h = roll_highs.iloc[-window * 3: -window * 2]
    prev1_h = roll_highs.iloc[-window * 2: -window]
    if prev3_h.empty or prev1_h.empty:
        return None

    left_h  = float(prev3_h.max())
    right_h = float(prev1_h.max())   # 右肩高値

    # ── 天井 H&S ─────────────────────────────────────────────
    if abs(left_h - right_h) / (head_h + 1e-9) < tol and \
            head_h > max(left_h, right_h) * 1.005:
        # ネックライン: 右肩〜頭のゾーンの最安値（簡易近似）
        neck_zone  = df["Low"].iloc[-window * 2:]
        neckline   = float(neck_zone.min())
        rs_lows    = df["Low"].iloc[-window * 2: -window]
        right_low  = float(rs_lows.min()) if not rs_lows.empty \
                     else float(df["Low"].iloc[-1])
        return {
            "pattern":             "HEAD_AND_SHOULDERS",
            "right_shoulder_high": right_h,
            "right_shoulder_low":  right_low,
            "head":                float(head_h),
            "neckline":            neckline,
        }

    # ── 逆 H&S ──────────────────────────────────────────────
    prev3_l = roll_lows.iloc[-window * 3: -window * 2]
    prev1_l = roll_lows.iloc[-window * 2: -window]
    if prev3_l.empty or prev1_l.empty:
        return None

    left_l  = float(prev3_l.min())
    right_l = float(prev1_l.min())   # 右肩安値

    if abs(left_l - right_l) / (head_l + 1e-9) < tol and \
            head_l < min(left_l, right_l) * 0.995:
        neck_zone   = df["High"].iloc[-window * 2:]
        neckline    = float(neck_zone.max())
        rs_highs    = df["High"].iloc[-window * 2: -window]
        right_high  = float(rs_highs.max()) if not rs_highs.empty \
                      else float(df["High"].iloc[-1])
        return {
            "pattern":             "INV_HEAD_AND_SHOULDERS",
            "right_shoulder_high": right_high,
            "right_shoulder_low":  right_l,
            "head":                float(head_l),
            "neckline":            neckline,
        }

    return None

# ─────────────────────────────────────────────────────────────
# ▌ シグナル判定
# ─────────────────────────────────────────────────────────────
def check_bb_signal(df_1h: pd.DataFrame) -> dict | None:
    """
    BB逆張り（1h）シグナルチェック。
    SL/TP は ATR ベースの変動値:
      SL = エントリー ± ATR × BB_SL_MULT (×3.0)
      TP = エントリー ∓ ATR × BB_TP_MULT (×6.0 / RR 1:2)
    200SMAトレンドフィルター付き。
    """
    if len(df_1h) < BB_PERIOD + 5:
        return None

    upper, mid, lower = calc_bb(df_1h)
    rsi               = calc_rsi(df_1h)
    close             = float(df_1h["Close"].iloc[-1])
    rsi_val           = float(rsi.iloc[-1])
    upper_val         = float(upper.iloc[-1])
    lower_val         = float(lower.iloc[-1])

    # ATR / SMA200
    atr          = calc_atr(df_1h)
    atr_pips     = round(atr * 100, 1)
    sma200       = calc_sma200(df_1h)
    above_sma200 = close > sma200

    if close <= lower_val and rsi_val < RSI_OS:
        if not above_sma200:
            logger.info(f"[SMAフィルター] BB/BUYシグナル → SMA200({sma200:.3f})より下のためスキップ")
            return None
        sl       = round(close - atr * BB_SL_MULT, 3)
        tp       = round(close + atr * BB_TP_MULT, 3)
        sl_basis = f"ATR×{BB_SL_MULT} (ATR={atr_pips:.1f}pips)"
        tp_basis = f"ATR×{BB_TP_MULT} (RR 1:{int(BB_TP_MULT / BB_SL_MULT)})"
        return {
            "action":      "BUY",
            "strategy":    f"BB逆張り 1h (σ={BB_SIGMA})",
            "reason":      (
                f"RSI={rsi_val:.1f}（売られ過ぎ）, BB下限={lower_val:.3f}タッチ, "
                f"SMA200={sma200:.3f}より上 | "
                f"🛑 SL根拠: {sl_basis} | 🎯 TP根拠: {tp_basis}"
            ),
            "price":       close,
            "rsi":         rsi_val,
            "stop_loss":   sl,
            "take_profit": tp,
            "sl_basis":    sl_basis,
            "tp_basis":    tp_basis,
        }

    if close >= upper_val and rsi_val > RSI_OB:
        if above_sma200:
            logger.info(f"[SMAフィルター] BB/SELLシグナル → SMA200({sma200:.3f})より上のためスキップ")
            return None
        sl       = round(close + atr * BB_SL_MULT, 3)
        tp       = round(close - atr * BB_TP_MULT, 3)
        sl_basis = f"ATR×{BB_SL_MULT} (ATR={atr_pips:.1f}pips)"
        tp_basis = f"ATR×{BB_TP_MULT} (RR 1:{int(BB_TP_MULT / BB_SL_MULT)})"
        return {
            "action":      "SELL",
            "strategy":    f"BB逆張り 1h (σ={BB_SIGMA})",
            "reason":      (
                f"RSI={rsi_val:.1f}（買われ過ぎ）, BB上限={upper_val:.3f}タッチ, "
                f"SMA200={sma200:.3f}より下 | "
                f"🛑 SL根拠: {sl_basis} | 🎯 TP根拠: {tp_basis}"
            ),
            "price":       close,
            "rsi":         rsi_val,
            "stop_loss":   sl,
            "take_profit": tp,
            "sl_basis":    sl_basis,
            "tp_basis":    tp_basis,
        }
    return None


def check_hs_signal(df_4h: pd.DataFrame) -> dict | None:
    """
    H&S（4h）シグナルチェック。
    SL/TP はテクニカル形状ベースの変動値:
      天井H&S SELL: SL = 右肩高値 + HS_BUFFER_PIPS, TP = RR 1:HS_RR
      逆H&S  BUY : SL = 右肩安値 - HS_BUFFER_PIPS, TP = RR 1:HS_RR
    200SMAトレンドフィルター付き。
    """
    if len(df_4h) < 30:
        return None

    hs = detect_hs(df_4h)
    if hs is None:
        return None

    close   = float(df_4h["Close"].iloc[-1])
    adx     = calc_adx(df_4h)
    pattern = hs["pattern"]

    sma200       = calc_sma200(df_4h)
    above_sma200 = close > sma200

    # ── 天井 H&S → SELL ───────────────────────────────────────
    if pattern == "HEAD_AND_SHOULDERS":
        if above_sma200:
            logger.info(f"[SMAフィルター] H&S/SELLシグナル → SMA200({sma200:.3f})より上のためスキップ")
            return None

        rs_high  = hs["right_shoulder_high"]
        neckline = hs["neckline"]
        head_val = hs["head"]

        sl   = round(rs_high + HS_BUFFER_PIPS, 3)  # 右肩高値 + 5pips
        risk = sl - close
        tp   = round(close - risk * HS_RR, 3)       # RR 1:HS_RR

        # ネックライン倍返し目安（参考）
        neck_proj = round(neckline - (head_val - neckline), 3)

        sl_basis = f"右肩高値({rs_high:.3f})+5pips"
        tp_basis = f"RR 1:{HS_RR:.0f} / ネックライン倍返し目安 {neck_proj:.3f}"

        return {
            "action":      "SELL",
            "strategy":    "H&S 4h",
            "reason":      (
                f"天井H&Sパターン検知, ADX={adx:.1f}, SMA200={sma200:.3f}より下 | "
                f"🛑 SL根拠: {sl_basis} | 🎯 TP根拠: {tp_basis}"
            ),
            "price":       close,
            "adx":         adx,
            "stop_loss":   sl,
            "take_profit": tp,
            "sl_basis":    sl_basis,
            "tp_basis":    tp_basis,
        }

    # ── 逆 H&S → BUY ────────────────────────────────────────
    if pattern == "INV_HEAD_AND_SHOULDERS":
        if not above_sma200:
            logger.info(f"[SMAフィルター] 逆H&S/BUYシグナル → SMA200({sma200:.3f})より下のためスキップ")
            return None

        rs_low   = hs["right_shoulder_low"]
        neckline = hs["neckline"]
        head_val = hs["head"]

        sl   = round(rs_low - HS_BUFFER_PIPS, 3)   # 右肩安値 - 5pips
        risk = close - sl
        tp   = round(close + risk * HS_RR, 3)       # RR 1:HS_RR

        # ネックライン倍返し目安（参考）
        neck_proj = round(neckline + (neckline - head_val), 3)

        sl_basis = f"右肩安値({rs_low:.3f})-5pips"
        tp_basis = f"RR 1:{HS_RR:.0f} / ネックライン倍返し目安 {neck_proj:.3f}"

        return {
            "action":      "BUY",
            "strategy":    "逆H&S 4h",
            "reason":      (
                f"底逆H&Sパターン検知, ADX={adx:.1f}, SMA200={sma200:.3f}より上 | "
                f"🛑 SL根拠: {sl_basis} | 🎯 TP根拠: {tp_basis}"
            ),
            "price":       close,
            "adx":         adx,
            "stop_loss":   sl,
            "take_profit": tp,
            "sl_basis":    sl_basis,
            "tp_basis":    tp_basis,
        }

    return None

# ─────────────────────────────────────────────────────────────
# ▌ リトライヘルパー
# ─────────────────────────────────────────────────────────────
def _with_retry(func, *args, max_retry: int = MAX_RETRY,
                wait_sec: int = 60, label: str = "", **kwargs):
    """
    func を最大 max_retry 回リトライする。
    失敗のたびに wait_sec 秒待機し、最後も失敗したら例外を再送出する。
    """
    for attempt in range(1, max_retry + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt >= max_retry:
                raise
            logger.warning(
                f"[retry] {label or func.__name__} 試行 {attempt}/{max_retry} 失敗: {e}"
                f" — {wait_sec}秒後に再試行"
            )
            time.sleep(wait_sec)


# ─────────────────────────────────────────────────────────────
# ▌ 起動時ポジション照合
# ─────────────────────────────────────────────────────────────
def sync_position():
    """
    起動時に OANDA API と position.json を照合。
    ズレがあれば API 側の情報を正として position.json を上書きする。
    DRY_RUN または OANDA未接続 の場合はスキップ。
    """
    if not OANDA_OK or DRY_RUN:
        logger.info("[sync] DRY_RUN または OANDA未接続のためスキップ")
        return
    try:
        oanda_positions = oanda.get_open_positions()   # list of dicts
        local_pos       = load_position()

        if not oanda_positions and local_pos:
            logger.warning("[sync] OANDA にポジションなし → position.json をクリア")
            clear_position()

        elif oanda_positions and not local_pos:
            logger.warning("[sync] OANDA にポジションあり → position.json を復元")
            p     = oanda_positions[0]   # 1ポジションのみ管理
            pair  = p.get("instrument", "USD_JPY").replace("_", "/")
            units = float(p.get("units", 0))
            save_position({
                "active":         True,
                "pair":           pair,
                "direction":      "BUY" if units > 0 else "SELL",
                "entry_price":    float(p.get("averagePrice", 0)),
                "stop_loss":      0.0,
                "take_profit":    0.0,
                "strategy":       "restored_from_oanda",
                "entry_time":     datetime.now(JST).isoformat(),
                "breakeven_done": False,
            })
            logger.info(f"[sync] ポジション復元: {pair} {'BUY' if units > 0 else 'SELL'}")

        else:
            logger.info("[sync] ポジション照合OK（ズレなし）")

    except Exception as e:
        logger.error(f"[sync] ポジション照合エラー: {e}")


# ─────────────────────────────────────────────────────────────
# ▌ 発注処理
# ─────────────────────────────────────────────────────────────
def place_order(signal: dict, now_jst: datetime, pair: str = "USD/JPY") -> dict | None:
    """
    エントリー発注。DRY_RUN の場合はログのみ。
    成功したらポジション dict を返す。
    """
    action = signal["action"]
    price  = signal["price"]

    # ── 変動型 SL / TP（シグナルから取得） ──────────────────
    sl = signal.get("stop_loss")
    tp = signal.get("take_profit")
    if sl is None or tp is None:
        # フォールバック: ATRベースで再計算
        logger.warning("[place_order] SL/TPがシグナルに含まれていません。ATRフォールバックを使用。")
        import yfinance as yf
        ticker = pair.replace("/", "_").replace("_", "") + "=X"
        try:
            import pandas as _pd
            from datetime import timedelta as _td
            _raw = yf.download(ticker, period="30d", interval="1h",
                               progress=False, auto_adjust=True)
            if isinstance(_raw.columns, _pd.MultiIndex):
                _raw.columns = _raw.columns.get_level_values(0)
            atr_fb = calc_atr(_raw)
        except Exception:
            atr_fb = 0.5  # 最終フォールバック 50pips
        sl = round(price - atr_fb * BB_SL_MULT if action == "BUY"
                   else price + atr_fb * BB_SL_MULT, 3)
        tp = round(price + atr_fb * BB_TP_MULT if action == "BUY"
                   else price - atr_fb * BB_TP_MULT, 3)

    # OANDA instrument 形式に変換（例: "GBP/JPY" → "GBP_JPY"）
    instrument = pair.replace("/", "_")

    if DRY_RUN:
        logger.info(f"[DRY RUN] {pair} {action} @ {price:.3f}  SL:{sl:.3f}  TP:{tp:.3f}  "
                    f"戦略:{signal['strategy']}")
    else:
        if oanda:
            try:
                _with_retry(
                    oanda.place_order,
                    instrument=instrument,
                    units=LOT if action == "BUY" else -LOT,
                    stop_loss=sl,
                    take_profit=tp,
                    label="place_order",
                )
                logger.info(f"[OANDA] 発注成功: {pair} {action} @ {price:.3f}")
            except Exception as e:
                logger.error(f"[OANDA] 発注失敗（{MAX_RETRY}回リトライ済み）: {e}")
                if LINE_OK:
                    notify_error(f"{pair} 発注失敗: {e}")
                return None

    # LINE 通知
    if LINE_OK:
        notify_entry(
            direction=action,
            price=price,
            stop_loss=sl,
            take_profit=tp,
            strategy=signal["strategy"],
            reason=signal["reason"],
            lot=LOT,
            pair=pair,
        )

    pos = {
        "active":         True,
        "pair":           pair,
        "direction":      action,
        "entry_price":    price,
        "stop_loss":      sl,
        "take_profit":    tp,
        "strategy":       signal["strategy"],
        "entry_time":     now_jst.isoformat(),
        "breakeven_done": False,
    }
    save_position(pos)
    return pos

# ─────────────────────────────────────────────────────────────
# ▌ ポジション管理
# ─────────────────────────────────────────────────────────────
def manage_position(pos: dict, current_price: float, now_jst: datetime) -> dict | None:
    """
    ポジション管理。
    - SL / TP 到達チェック
    - 建値移動（含み益 BREAKEVEN_PIPS 超）
    - 週末強制クローズ（金曜 23:00 JST）
    戻り値: 更新後ポジション / None（決済済み）
    """
    direction   = pos["direction"]
    entry_price = pos["entry_price"]
    sl          = pos["stop_loss"]
    tp          = pos["take_profit"]

    pnl_pips = (current_price - entry_price) * 100 if direction == "BUY" \
               else (entry_price - current_price) * 100

    # ── 週末強制クローズ ────────────────────────────────────
    is_friday_close = (now_jst.weekday() == 4 and now_jst.hour >= 23)
    if is_friday_close:
        reason = "週末強制クローズ（金曜 23:00 JST）"
        _close_position(pos, current_price, pnl_pips, reason, now_jst)
        return None

    # ── TP 到達 ────────────────────────────────────────────
    tp_hit = (direction == "BUY"  and current_price >= tp) or \
             (direction == "SELL" and current_price <= tp)
    if tp_hit:
        _close_position(pos, current_price, pnl_pips, "TP到達 (利確)", now_jst)
        return None

    # ── SL 到達 ────────────────────────────────────────────
    sl_hit = (direction == "BUY"  and current_price <= sl) or \
             (direction == "SELL" and current_price >= sl)
    if sl_hit:
        _close_position(pos, current_price, pnl_pips, "SL到達 (損切り)", now_jst)
        return None

    # ── 建値移動（ブレークイーブン） ──────────────────────
    if not pos.get("breakeven_done") and pnl_pips >= BREAKEVEN_PIPS:
        new_sl = entry_price + 0.01 / 100 if direction == "BUY" \
                 else entry_price - 0.01 / 100
        pos["stop_loss"]     = new_sl
        pos["breakeven_done"] = True
        save_position(pos)
        logger.info(f"[建値移動] SL を {new_sl:.3f} に移動 (含み益 {pnl_pips:.1f} pips)")

    return pos

def _close_position(pos: dict, close_price: float,
                    pnl_pips: float, reason: str, now_jst: datetime):
    """決済処理（DRY RUN / OANDA 共通）"""
    direction   = pos["direction"]
    entry_price = pos["entry_price"]
    pair        = pos.get("pair", "USD/JPY")
    instrument  = pair.replace("/", "_")

    if DRY_RUN:
        logger.info(f"[DRY RUN] {pair} 決済: {direction}  entry:{entry_price:.3f}"
                    f"  close:{close_price:.3f}  PnL:{pnl_pips:+.1f}pips  理由:{reason}")
    else:
        if oanda:
            try:
                _with_retry(oanda.close_position, instrument=instrument,
                            label="close_position")
                logger.info(f"[OANDA] {pair} 決済成功: {pnl_pips:+.1f} pips")
            except Exception as e:
                logger.error(f"[OANDA] {pair} 決済失敗（{MAX_RETRY}回リトライ済み）: {e}")

    # 戦績を更新
    stats = update_stats(pnl_pips, LOT)

    if LINE_OK:
        notify_close(
            direction=direction,
            entry_price=entry_price,
            close_price=close_price,
            pnl_pips=pnl_pips,
            reason=reason,
            lot=LOT,
            pair=pair,
            daily_pips=stats["daily_pips"],
            daily_jpy=stats["daily_jpy"],
            total_pips=stats["total_pips"],
            balance=stats["balance"],
        )
    clear_position()

# ─────────────────────────────────────────────────────────────
# ▌ メインループ
# ─────────────────────────────────────────────────────────────
def run():
    mode_label = "🔵 DRY RUN" if DRY_RUN else "🔴 本番"
    logger.info("=" * 55)
    logger.info(f"  CatHack AI Trader 起動  [{mode_label}]")
    logger.info(f"  チェック間隔: {LOOP_SEC}秒  ロット: {LOT:,}通貨")
    logger.info(f"  BB: period={BB_PERIOD}, σ={BB_SIGMA}  SL:ATR×{BB_SL_MULT}  TP:ATR×{BB_TP_MULT}")
    logger.info(f"  200SMA期間: {SMA_PERIOD}  リトライ: {MAX_RETRY}回")
    logger.info("=" * 55)

    # 起動時ポジション照合（OANDA本番時のみ有効）
    sync_position()

    while True:
        try:
            now_utc = datetime.now(tz=timezone.utc)
            now_jst = now_utc.astimezone(JST)
            logger.info(f"--- チェック開始: {now_jst.strftime('%Y/%m/%d %H:%M JST')} ---")

            # ── ポジション管理（全ペア共通：1ポジションのみ保有） ──
            pos = load_position()
            if pos:
                pair_name = pos.get("pair", "USD/JPY")
                ticker    = next((t for t, n in PAIRS.items() if n == pair_name), "USDJPY=X")
                try:
                    df_1h, _ = _with_retry(fetch_data, ticker,
                                           label=f"fetch {pair_name}")
                    current_price = float(df_1h["Close"].iloc[-1])
                except Exception as e:
                    logger.error(f"{pair_name} データ取得失敗（{MAX_RETRY}回リトライ済み）: {e}")
                    time.sleep(60)
                    continue
                logger.info(f"保有: {pair_name} {pos['direction']} @ {pos['entry_price']:.3f}  現在:{current_price:.3f}")
                pos = manage_position(pos, current_price, now_jst)
                if pos is None:
                    logger.info("ポジション決済完了")
                time.sleep(LOOP_SEC)
                continue   # ポジションがあれば新規エントリーしない

            # ── トレードフィルター ─────────────────────────
            if tf:
                filter_ok, filter_msg = tf.is_tradeable(now_jst, SPREAD_PIPS)
                if not filter_ok:
                    logger.info(f"フィルター: {filter_msg}")
                    time.sleep(LOOP_SEC)
                    continue

            # ── 各ペアのシグナルチェック ──────────────────
            found_signal = False
            for ticker, pair_name in PAIRS.items():
                try:
                    df_1h, df_4h = _with_retry(fetch_data, ticker,
                                               label=f"fetch {pair_name}")
                    current_price = float(df_1h["Close"].iloc[-1])
                    logger.info(f"{pair_name}: {current_price:.3f}")
                except Exception as e:
                    logger.error(f"{pair_name} データ取得失敗（{MAX_RETRY}回リトライ済み）: {e}")
                    continue

                # BB逆張り（1h）→ H&S（4h）の順でチェック（SMAフィルター内蔵）
                signal = check_bb_signal(df_1h)
                if signal:
                    logger.info(f"[BB/{pair_name}] {signal['action']}  {signal['reason']}")
                else:
                    signal = check_hs_signal(df_4h)
                    if signal:
                        logger.info(f"[H&S/{pair_name}] {signal['action']}  {signal['reason']}")

                if signal:
                    place_order(signal, now_jst, pair=pair_name)
                    found_signal = True
                    break   # 1ペアでシグナルが出たら他はスキップ

            if not found_signal:
                logger.info("全ペア シグナルなし — 様子見")

        except KeyboardInterrupt:
            logger.info("\n停止しました（Ctrl+C）")
            break
        except Exception as e:
            logger.error(f"予期しないエラー: {e}", exc_info=True)
            if LINE_OK:
                notify_error(str(e))
            logger.info("60秒後に再試行します…")
            time.sleep(60)
            continue

        time.sleep(LOOP_SEC)


# ─────────────────────────────────────────────────────────────
# ▌ 起動
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run()
