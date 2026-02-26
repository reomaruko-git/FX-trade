"""
auto_trader.py — FX Trade Luce 司令塔
==========================================
H&S（4時間足）を3通貨ペアで監視し、
シグナルが出たら LINE 通知 → OANDA 発注（または DRY RUN ログ）。

戦略:
  USD/JPY: H&S（4時間足）  バックテスト: 70回, 34.3%, +1333 pips
  EUR/JPY: H&S（4時間足）  バックテスト: 64回, 28.1%, +1023 pips
  AUD/JPY: H&S（4時間足）  バックテスト: 60回, 55.0%, +2895 pips
  ※ GBP/JPY は成績不良（17.6%, -22pips）のため除外
  ※ EMAクロスは廃止（過剰シグナル・低勝率のため）

H&S検出: backtest.pyと同ロジック統一
  - 直近100本窓・全組み合わせ探索（distance=5, tol=0.020）
  - TP: ネックライン倍返し（head - neckline の距離をネックラインから延長）
  - SL: 右肩高値（低値）+ 1pip バッファ
  - MAX_SL_PIPS=80（4h足の自然なSL幅に対応）

テクニカル計算: pandas_ta
H&S検知      : scipy.signal.find_peaks

起動方法:
    # ドライランモード（デフォルト）
    python3 auto_trader.py

    # 本番モード（.env に DRY_RUN=false 設定済みの場合）
    python3 auto_trader.py

停止: Ctrl+C
"""

from __future__ import annotations

import os
import sys
import json
import time
import logging
import logging.handlers
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import contextlib
import io

import pandas as pd
import numpy as np
import pandas_ta as ta
from scipy.signal import find_peaks


def _quiet(func, *args, **kwargs):
    """pandas_ta の verbose 出力を抑制して関数を実行する"""
    with contextlib.redirect_stdout(io.StringIO()):
        return func(*args, **kwargs)

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

ROOT = Path(__file__).parent
JST  = ZoneInfo("Asia/Tokyo")

# ─────────────────────────────────────────────────────────────
# ▌ 設定
# ─────────────────────────────────────────────────────────────
DRY_RUN       = os.environ.get("DRY_RUN", "true").lower() != "false"
LOOP_SEC      = int(os.environ.get("LOOP_SEC", "300"))
LOT           = int(os.environ.get("LOT", "20000"))
SPREAD_PIPS   = float(os.environ.get("SPREAD_PIPS", "0.4"))
MAX_RETRY     = int(os.environ.get("MAX_RETRY", "3"))
SMA_PERIOD    = int(os.environ.get("SMA_PERIOD", "200"))

# 監視ペア: yfinance ticker → 表示名
# ※ GBP/JPY は除外（バックテスト: 17.6%勝率, -22pips で成績不良）
PAIRS = {
    "USDJPY=X": "USD/JPY",
    "EURJPY=X": "EUR/JPY",
    "AUDJPY=X": "AUD/JPY",
}

# H&S 戦略パラメータ（backtest.pyと同値）
HS_DISTANCE    = 5      # find_peaks の distance
HS_TOL         = 0.020  # 肩の対称性許容誤差（感度テストで最適化済み）
HS_BUFFER_PIPS = 0.05   # H&S 右肩からのバッファ距離（5pips）
ADX_PERIOD     = 14     # ADX 計算期間（参考情報としてログ出力）
MAX_POSITIONS  = 2      # 最大同時保有ポジション数

# ── SL / TP ──────────────────────────────────────────────────
# EMA用（互換性のため残す）
SL_MULT        = 2.0
TP_MULT        = 4.0
MAX_SL_PIPS    = 80    # SL上限（pips）。50→80に拡張（4h足基準に合わせる）

# ─────────────────────────────────────────────────────────────
# ▌ ロガー
# ─────────────────────────────────────────────────────────────
def _setup_logger() -> logging.Logger:
    log = logging.getLogger("FXTradeLuce")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    # ローテーション: 1MB で切り替え、最大5世代保持（合計 ~6MB）
    fh = logging.handlers.RotatingFileHandler(
        ROOT / "auto_trader.log",
        maxBytes=1 * 1024 * 1024,   # 1MB
        backupCount=5,
        encoding="utf-8",
    )
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
INITIAL_BALANCE = int(os.environ.get("INITIAL_BALANCE", "1000000"))


def _current_week() -> str:
    d = datetime.now(JST)
    return f"{d.isocalendar().year}-W{d.isocalendar().week:02d}"


def load_stats() -> dict:
    today   = datetime.now(JST).strftime("%Y-%m-%d")
    week    = _current_week()
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
        if data.get("last_date") != today:
            data["daily_pips"] = 0.0
            data["daily_jpy"]  = 0
            data["last_date"]  = today
        if data.get("last_week") != week:
            data["weekly_pips"]   = 0.0
            data["weekly_jpy"]    = 0
            data["weekly_trades"] = 0
            data["weekly_wins"]   = 0
            data["last_week"]     = week
        for k, v in default.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return default


def save_stats(stats: dict):
    STATS_FILE.write_text(json.dumps(stats, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def update_stats(pnl_pips: float, lot: int = LOT) -> dict:
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
POSITION_FILE  = ROOT / "position.json"
POSITIONS_FILE = ROOT / "positions.json"   # 複数ポジション用


def load_positions() -> list[dict]:
    """全アクティブポジションをリストで返す"""
    if not POSITIONS_FILE.exists():
        return []
    try:
        data = json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
        return [p for p in data if p.get("active")]
    except Exception:
        return []


def save_positions(positions: list[dict]):
    POSITIONS_FILE.write_text(
        json.dumps(positions, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def add_position(pos: dict):
    positions = load_positions()
    pos["active"] = True
    positions.append(pos)
    save_positions(positions)


def remove_position(pos: dict):
    positions = load_positions()
    positions = [p for p in positions
                 if not (p.get("pair") == pos.get("pair") and
                         p.get("entry_time") == pos.get("entry_time"))]
    save_positions(positions)


# 後方互換（旧コードとの互換性維持）
def load_position() -> dict | None:
    positions = load_positions()
    return positions[0] if positions else None


def save_position(pos: dict | None):
    if pos is None:
        return
    add_position(pos)


def clear_position():
    pass   # remove_position で個別削除するため不要


# ─────────────────────────────────────────────────────────────
# ▌ データ取得
# ─────────────────────────────────────────────────────────────
def fetch_data(ticker: str, pair_name: str = "") -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    価格データ取得。
    OANDA 接続済み : oandapyV20 でリアルタイム取得（本番・DRY_RUN 共通）
    OANDA 未接続   : yfinance でフォールバック（テスト・オフライン用）

    H1 (10本)  → 現在価格取得用（include_incomplete=True で最新値を含む）
    H4 (350本) → H&S 検出 + SMA200 計算用（確定足のみ）
    """
    # ── OANDA リアルタイム取得 ──────────────────────────────────
    if OANDA_OK and pair_name:
        instrument = pair_name.replace("/", "_")
        try:
            df_1h = oanda.get_candles(instrument, granularity="H1",
                                      count=10, include_incomplete=True)
            df_4h = oanda.get_candles(instrument, granularity="H4",
                                      count=350, include_incomplete=False)
            if not df_1h.empty and not df_4h.empty:
                return df_1h, df_4h
            logger.warning(f"[OANDA] {pair_name} ローソク足が空 — yfinance にフォールバック")
        except Exception as e:
            logger.warning(f"[OANDA] {pair_name} ローソク足取得失敗: {e} — yfinance にフォールバック")

    # ── yfinance フォールバック ────────────────────────────────
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
# ▌ テクニカル計算（pandas_ta 使用）
# ─────────────────────────────────────────────────────────────
def calc_ema(df: pd.DataFrame, period: int) -> pd.Series:
    """EMA（pandas_ta）"""
    return _quiet(df.ta.ema, length=period, append=False)


def calc_atr(df: pd.DataFrame, period: int = 14) -> float:
    """ATR の最新値（pandas_ta）"""
    atr_s = _quiet(df.ta.atr, length=period, append=False)
    val   = atr_s.iloc[-1] if atr_s is not None and not atr_s.empty else np.nan
    return float(val) if not np.isnan(val) else 0.001


def calc_adx(df: pd.DataFrame, period: int = 14) -> float:
    """ADX の最新値（pandas_ta）"""
    adx_df = _quiet(df.ta.adx, length=period, append=False)
    col    = f"ADX_{period}"
    if adx_df is None or col not in adx_df.columns:
        return 0.0
    val = adx_df[col].iloc[-1]
    return float(val) if not np.isnan(val) else 0.0


def calc_sma200(df: pd.DataFrame, period: int = SMA_PERIOD) -> float:
    """SMA の最新値。データ不足時は全期間平均で補完"""
    if len(df) < period:
        return float(df["Close"].mean())
    val = df["Close"].rolling(period).mean().iloc[-1]
    return float(val) if not np.isnan(val) else float(df["Close"].mean())


# ─────────────────────────────────────────────────────────────
# ▌ H&S パターン検知（backtest.py と完全統一ロジック）
# ─────────────────────────────────────────────────────────────
def detect_hs(df: pd.DataFrame,
              distance: int = HS_DISTANCE,
              tol: float = HS_TOL) -> dict | None:
    """
    直近100本窓・全組み合わせ探索でH&Sパターンを検知する。
    backtest.py の detect_hs_at と同ロジック。

    戻り値:
        {
          "pattern":             "HEAD_AND_SHOULDERS" | "INV_HEAD_AND_SHOULDERS",
          "right_shoulder_high": float,
          "right_shoulder_low":  float,   # INV_H&S のみ
          "head":                float,
          "neckline":            float,
        }
        or None
    """
    # 直近100本を使用（backtest.pyと同じ窓サイズ）
    window = df.tail(100)
    if len(window) < distance * 3:
        return None

    highs = window["High"].values
    lows  = window["Low"].values
    n     = len(highs)

    # ── 天井 H&S（3つのピーク：左肩 < 頭 > 右肩、左肩 ≈ 右肩） ─────
    peak_idx, _ = find_peaks(highs, distance=distance)
    if len(peak_idx) >= 3:
        # 全組み合わせを新しい順に探索（backtest.pyと同じ）
        for k in range(len(peak_idx) - 3, -1, -1):
            ls_i = int(peak_idx[k])
            hd_i = int(peak_idx[k + 1])
            rs_i = int(peak_idx[k + 2])
            ls, head, rs = highs[ls_i], highs[hd_i], highs[rs_i]

            # 右肩が古すぎる場合はスキップ
            if rs_i < n - distance * 4:
                continue
            # 頭が両肩より高くないとNG
            if head <= max(ls, rs):
                continue
            # 肩の対称性チェック
            if abs(ls - rs) / (head + 1e-9) > tol:
                continue

            # ネックライン: 左肩〜頭、頭〜右肩 間の最安値の平均
            neck1    = float(lows[ls_i:hd_i].min()) if hd_i > ls_i else float(lows[ls_i])
            neck2    = float(lows[hd_i:rs_i].min()) if rs_i > hd_i else float(lows[hd_i])
            neckline = round((neck1 + neck2) / 2, 3)

            # 右肩の高値（ピーク周辺の最高値）
            buf      = max(1, distance // 2)
            rs_high  = float(highs[max(0, rs_i - buf): rs_i + buf + 1].max())

            return {
                "pattern":             "HEAD_AND_SHOULDERS",
                "right_shoulder_high": rs_high,
                "head":                float(head),
                "neckline":            neckline,
            }

    # ── 逆 H&S（3つのトラフ：左肩 > 頭 < 右肩、左肩 ≈ 右肩） ──────
    trough_idx, _ = find_peaks(-lows, distance=distance)
    if len(trough_idx) >= 3:
        for k in range(len(trough_idx) - 3, -1, -1):
            ls_i = int(trough_idx[k])
            hd_i = int(trough_idx[k + 1])
            rs_i = int(trough_idx[k + 2])
            ls, head, rs = lows[ls_i], lows[hd_i], lows[rs_i]

            if rs_i < n - distance * 4:
                continue
            if head >= min(ls, rs):
                continue
            if abs(ls - rs) / (abs(head) + 1e-9) > tol:
                continue

            neck1    = float(highs[ls_i:hd_i].max()) if hd_i > ls_i else float(highs[ls_i])
            neck2    = float(highs[hd_i:rs_i].max()) if rs_i > hd_i else float(highs[hd_i])
            neckline = round((neck1 + neck2) / 2, 3)

            buf      = max(1, distance // 2)
            rs_low   = float(lows[max(0, rs_i - buf): rs_i + buf + 1].min())
            rs_high  = float(highs[rs_i])

            return {
                "pattern":             "INV_HEAD_AND_SHOULDERS",
                "right_shoulder_high": rs_high,
                "right_shoulder_low":  rs_low,
                "head":                float(head),
                "neckline":            neckline,
            }

    return None


# ─────────────────────────────────────────────────────────────
# ▌ シグナル判定
# ─────────────────────────────────────────────────────────────
def check_ema_signal(df_1h: pd.DataFrame) -> dict | None:
    """
    EMAクロス（1h）シグナルチェック。
    EMA9 が EMA21 を上抜け → BUY（SMA200より上）
    EMA9 が EMA21 を下抜け → SELL（SMA200より下）
    SL = ATR × SL_MULT、TP = ATR × TP_MULT（RR 1:2）
    """
    if len(df_1h) < max(EMA_SLOW, SMA_PERIOD) + 5:
        return None

    ema_fast  = calc_ema(df_1h, EMA_FAST)
    ema_slow  = calc_ema(df_1h, EMA_SLOW)
    if ema_fast is None or ema_slow is None:
        return None

    close        = float(df_1h["Close"].iloc[-1])
    atr          = calc_atr(df_1h)
    atr_pips     = round(atr * 100, 1)
    sma200       = calc_sma200(df_1h)
    above_sma200 = close > sma200
    adx          = calc_adx(df_1h, ADX_PERIOD)

    # ADXフィルター：トレンドが弱い横ばい相場はスキップ
    if adx < ADX_MIN:
        logger.info(f"[ADXフィルター] ADX={adx:.1f} < {ADX_MIN} — 横ばい相場のためスキップ")
        return None

    # 現在足と1本前でクロス判定
    ef_now  = float(ema_fast.iloc[-1])
    es_now  = float(ema_slow.iloc[-1])
    ef_prev = float(ema_fast.iloc[-2])
    es_prev = float(ema_slow.iloc[-2])

    sl_basis = f"ATR×{SL_MULT} (ATR={atr_pips:.1f}pips)"
    tp_basis = f"ATR×{TP_MULT} (RR 1:{int(TP_MULT / SL_MULT)})"

    # ── ゴールデンクロス → BUY ───────────────────────────────
    golden_cross = (ef_prev <= es_prev) and (ef_now > es_now)
    if golden_cross:
        if not above_sma200:
            logger.info(f"[SMAフィルター] EMA/BUYシグナル → SMA200({sma200:.3f})より下のためスキップ")
            return None
        sl      = round(close - atr * SL_MULT, 3)
        tp      = round(close + atr * TP_MULT, 3)
        sl_pips = abs(close - sl) * 100
        if sl_pips > MAX_SL_PIPS:
            logger.info(f"[SLキャップ] EMA/BUY SL={sl_pips:.1f}pips > 上限{MAX_SL_PIPS}pipsのためスキップ")
            return None
        return {
            "action":      "BUY",
            "strategy":    f"EMAクロス 1h (EMA{EMA_FAST}/EMA{EMA_SLOW})",
            "reason":      (
                f"ゴールデンクロス EMA{EMA_FAST}({ef_now:.3f})>EMA{EMA_SLOW}({es_now:.3f}), "
                f"SMA200={sma200:.3f}より上 | "
                f"🛑 SL根拠: {sl_basis} | 🎯 TP根拠: {tp_basis}"
            ),
            "price":       close,
            "stop_loss":   sl,
            "take_profit": tp,
            "sl_basis":    sl_basis,
            "tp_basis":    tp_basis,
        }

    # ── デッドクロス → SELL ──────────────────────────────────
    dead_cross = (ef_prev >= es_prev) and (ef_now < es_now)
    if dead_cross:
        if above_sma200:
            logger.info(f"[SMAフィルター] EMA/SELLシグナル → SMA200({sma200:.3f})より上のためスキップ")
            return None
        sl      = round(close + atr * SL_MULT, 3)
        tp      = round(close - atr * TP_MULT, 3)
        sl_pips = abs(close - sl) * 100
        if sl_pips > MAX_SL_PIPS:
            logger.info(f"[SLキャップ] EMA/SELL SL={sl_pips:.1f}pips > 上限{MAX_SL_PIPS}pipsのためスキップ")
            return None
        return {
            "action":      "SELL",
            "strategy":    f"EMAクロス 1h (EMA{EMA_FAST}/EMA{EMA_SLOW})",
            "reason":      (
                f"デッドクロス EMA{EMA_FAST}({ef_now:.3f})<EMA{EMA_SLOW}({es_now:.3f}), "
                f"SMA200={sma200:.3f}より下 | "
                f"🛑 SL根拠: {sl_basis} | 🎯 TP根拠: {tp_basis}"
            ),
            "price":       close,
            "stop_loss":   sl,
            "take_profit": tp,
            "sl_basis":    sl_basis,
            "tp_basis":    tp_basis,
        }

    return None


def check_hs_signal(df_4h: pd.DataFrame) -> dict | None:
    """
    H&S（4h）シグナルチェック。
    SL: 右肩高値（低値）+ 5pips バッファ
    TP: ネックライン倍返し（backtest.py と統一）
    フィルター: 200SMA（大局トレンド確認）
    """
    if len(df_4h) < 30:
        return None

    hs = detect_hs(df_4h)
    if hs is None:
        return None

    close        = float(df_4h["Close"].iloc[-1])
    adx          = calc_adx(df_4h)
    pattern      = hs["pattern"]
    sma200       = calc_sma200(df_4h)
    above_sma200 = close > sma200

    # ── 天井 H&S → SELL ──────────────────────────────────────
    if pattern == "HEAD_AND_SHOULDERS":
        if above_sma200:
            logger.info(f"[SMAフィルター] H&S/SELLシグナル → SMA200({sma200:.3f})より上のためスキップ")
            return None
        rs_high   = hs["right_shoulder_high"]
        neckline  = hs["neckline"]
        head_val  = hs["head"]
        sl        = round(rs_high + HS_BUFFER_PIPS, 3)
        # SL方向チェック: SELLのSLはエントリーより上でなければならない
        # 現在値が右肩より上にある場合 → パターンが古く逆方向SLになるのでスキップ
        if sl <= close:
            logger.info(f"[SL方向エラー] H&S/SELL SL({sl:.3f}) ≤ 現在値({close:.3f}) — パターン古いためスキップ")
            return None
        sl_pips   = abs(close - sl) * 100
        if sl_pips > MAX_SL_PIPS:
            logger.info(f"[SLキャップ] H&S/SELL SL={sl_pips:.1f}pips > 上限{MAX_SL_PIPS}pipsのためスキップ")
            return None
        # TP: ネックライン倍返し（backtest.pyと統一）
        depth     = head_val - neckline
        tp        = round(neckline - depth, 3)
        if tp >= close:
            tp = round(close - sl_pips / 100 * 2, 3)   # フォールバック: RR 1:2
        sl_basis  = f"右肩高値({rs_high:.3f})+5pips"
        tp_basis  = f"ネックライン倍返し({neckline:.3f} - {depth:.3f} = {tp:.3f})"
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

    # ── 逆 H&S → BUY ─────────────────────────────────────────
    if pattern == "INV_HEAD_AND_SHOULDERS":
        if not above_sma200:
            logger.info(f"[SMAフィルター] 逆H&S/BUYシグナル → SMA200({sma200:.3f})より下のためスキップ")
            return None
        rs_low    = hs["right_shoulder_low"]
        neckline  = hs["neckline"]
        head_val  = hs["head"]
        sl        = round(rs_low - HS_BUFFER_PIPS, 3)
        # SL方向チェック: BUYのSLはエントリーより下でなければならない
        # 現在値が右肩より下にある場合 → パターンが古く逆方向SLになるのでスキップ
        if sl >= close:
            logger.info(f"[SL方向エラー] 逆H&S/BUY SL({sl:.3f}) ≥ 現在値({close:.3f}) — パターン古いためスキップ")
            return None
        sl_pips   = abs(close - sl) * 100
        if sl_pips > MAX_SL_PIPS:
            logger.info(f"[SLキャップ] 逆H&S/BUY SL={sl_pips:.1f}pips > 上限{MAX_SL_PIPS}pipsのためスキップ")
            return None
        # TP: ネックライン倍返し（backtest.pyと統一）
        depth     = neckline - head_val
        tp        = round(neckline + depth, 3)
        if tp <= close:
            tp = round(close + sl_pips / 100 * 2, 3)   # フォールバック: RR 1:2
        sl_basis  = f"右肩安値({rs_low:.3f})-5pips"
        tp_basis  = f"ネックライン倍返し({neckline:.3f} + {depth:.3f} = {tp:.3f})"
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
    DRY_RUN または OANDA未接続 の場合はスキップ。
    """
    if not OANDA_OK or DRY_RUN:
        logger.info("[sync] DRY_RUN または OANDA未接続のためスキップ")
        return
    try:
        oanda_positions = oanda.get_open_positions()
        local_positions = load_positions()

        if not oanda_positions and local_positions:
            logger.warning("[sync] OANDA にポジションなし → positions.json をクリア")
            save_positions([])

        elif oanda_positions and not local_positions:
            logger.warning("[sync] OANDA にポジションあり → positions.json を復元")
            for p in oanda_positions:
                pair     = p.get("instrument", "USD_JPY").replace("_", "/")
                units    = float(p.get("units", 0))
                instrument = pair.replace("/", "_")
                trade_id = ""
                try:
                    trade_id = oanda.get_open_trade_id(instrument) or ""
                except Exception:
                    pass
                add_position({
                    "active":         True,
                    "pair":           pair,
                    "direction":      "BUY" if units > 0 else "SELL",
                    "entry_price":    float(p.get("entry_price", 0)),
                    "stop_loss":      0.0,
                    "take_profit":    0.0,
                    "strategy":       "restored_from_oanda",
                    "entry_time":     datetime.now(JST).isoformat(),
                    "breakeven_done": False,
                    "trade_id":       trade_id,
                })
                logger.info(f"[sync] ポジション復元: {pair} {'BUY' if units > 0 else 'SELL'}"
                            f" tradeID={trade_id}")
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
    成功したらポジション dict を返す（trade_id を含む）。
    """
    action     = signal["action"]
    price      = signal["price"]
    sl         = signal.get("stop_loss")
    tp         = signal.get("take_profit")
    instrument = pair.replace("/", "_")
    trade_id   = ""

    if sl is None or tp is None:
        logger.warning("[place_order] SL/TPがシグナルに含まれていません。ATRフォールバックを使用。")
        import yfinance as yf
        ticker = pair.replace("/", "").replace("_", "") + "=X"
        try:
            _raw = yf.download(ticker, period="30d", interval="1h",
                               progress=False, auto_adjust=True)
            if isinstance(_raw.columns, pd.MultiIndex):
                _raw.columns = _raw.columns.get_level_values(0)
            atr_fb = calc_atr(_raw)
        except Exception:
            atr_fb = 0.5
        sl = round(price - atr_fb * BB_SL_MULT if action == "BUY"
                   else price + atr_fb * BB_SL_MULT, 3)
        tp = round(price + atr_fb * BB_TP_MULT if action == "BUY"
                   else price - atr_fb * BB_TP_MULT, 3)

    if DRY_RUN:
        logger.info(f"[DRY RUN] {pair} {action} @ {price:.3f}  SL:{sl:.3f}  TP:{tp:.3f}  "
                    f"戦略:{signal['strategy']}")
    else:
        if oanda:
            try:
                resp = _with_retry(
                    oanda.place_order,
                    instrument=instrument,
                    units=LOT if action == "BUY" else -LOT,
                    stop_loss=sl,
                    take_profit=tp,
                    label="place_order",
                )
                # tradeID を取得（建値移動で使用）
                trade_id = (
                    str(resp.get("orderFillTransaction", {})
                              .get("tradeOpened", {})
                              .get("tradeID", ""))
                    or str(resp.get("relatedTransactionIDs", [""])[0])
                )
                logger.info(f"[OANDA] 発注成功: {pair} {action} @ {price:.3f}  tradeID={trade_id}")
            except Exception as e:
                logger.error(f"[OANDA] 発注失敗（{MAX_RETRY}回リトライ済み）: {e}")
                if LINE_OK:
                    notify_error(f"{pair} 発注失敗: {e}")
                return None

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
        "trade_id":       trade_id,
    }
    add_position(pos)   # 複数ポジションリストに追加
    return pos


# ─────────────────────────────────────────────────────────────
# ▌ ポジション管理
# ─────────────────────────────────────────────────────────────
def manage_position(pos: dict, current_price: float, now_jst: datetime) -> dict | None:
    """
    ポジション管理:
      - SL / TP 到達チェック
      - 週末強制クローズ（金曜 23:00 JST）
    ※ 建値移動（ブレークイーブン）は廃止
       → バックテストで「含み益トレードを0pipsで強制終了→負け計上」の悪影響が判明
    """
    direction   = pos["direction"]
    entry_price = pos["entry_price"]
    sl          = pos["stop_loss"]
    tp          = pos["take_profit"]

    pnl_pips = (current_price - entry_price) * 100 if direction == "BUY" \
               else (entry_price - current_price) * 100

    # ── 週末強制クローズ ──────────────────────────────────────
    if now_jst.weekday() == 4 and now_jst.hour >= 23:
        _close_position(pos, current_price, pnl_pips, "週末強制クローズ（金曜 23:00 JST）", now_jst)
        return None

    # ── TP 到達 ────────────────────────────────────────────────
    tp_hit = (direction == "BUY"  and current_price >= tp) or \
             (direction == "SELL" and current_price <= tp)
    if tp_hit:
        _close_position(pos, current_price, pnl_pips, "TP到達 (利確)", now_jst)
        return None

    # ── SL 到達 ────────────────────────────────────────────────
    sl_hit = (direction == "BUY"  and current_price <= sl) or \
             (direction == "SELL" and current_price >= sl)
    if sl_hit:
        _close_position(pos, current_price, pnl_pips, "SL到達 (損切り)", now_jst)
        return None

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
    remove_position(pos)   # 該当ポジションのみ削除（複数ポジション対応）


# ─────────────────────────────────────────────────────────────
# ▌ メインループ
# ─────────────────────────────────────────────────────────────
def run():
    mode_label = "🔵 DRY RUN" if DRY_RUN else "🔴 本番"
    logger.info("=" * 60)
    logger.info(f"  FX Trade Luce 起動  [{mode_label}]")
    logger.info(f"  戦略: H&S 4時間足 × {len(PAIRS)}ペア")
    logger.info(f"  監視ペア: {', '.join(PAIRS.values())}")
    logger.info(f"  チェック間隔: {LOOP_SEC}秒  ロット: {LOT:,}通貨")
    logger.info(f"  H&S: distance={HS_DISTANCE}  tol={HS_TOL}  buffer={HS_BUFFER_PIPS}pips")
    logger.info(f"  TP: ネックライン倍返し  SL上限: {MAX_SL_PIPS}pips")
    logger.info(f"  200SMA期間: {SMA_PERIOD}  リトライ: {MAX_RETRY}回")
    logger.info(f"  ※ ブレイクイーブン廃止（バックテストで悪影響確認済み）")
    logger.info("=" * 60)

    sync_position()

    while True:
        try:
            now_utc = datetime.now(tz=timezone.utc)
            now_jst = now_utc.astimezone(JST)
            logger.info(f"--- チェック開始: {now_jst.strftime('%Y/%m/%d %H:%M JST')} ---")

            # ── 保有ポジションの管理（複数対応） ─────────────
            active_positions = load_positions()
            for pos in list(active_positions):
                pair_name = pos.get("pair", "USD/JPY")
                ticker    = next((t for t, n in PAIRS.items() if n == pair_name), "USDJPY=X")
                try:
                    df_1h, _ = _with_retry(fetch_data, ticker, pair_name,
                                           label=f"fetch {pair_name}")
                    current_price = float(df_1h["Close"].iloc[-1])
                except Exception as e:
                    logger.error(f"{pair_name} データ取得失敗（{MAX_RETRY}回リトライ済み）: {e}")
                    continue

                pnl_pips = (current_price - pos["entry_price"]) * 100 \
                           if pos["direction"] == "BUY" \
                           else (pos["entry_price"] - current_price) * 100
                logger.info(
                    f"保有: {pair_name} {pos['direction']} @ {pos['entry_price']:.3f}  "
                    f"現在:{current_price:.3f}  含み損益:{pnl_pips:+.1f}pips"
                )
                updated = manage_position(pos, current_price, now_jst)
                if updated is None:
                    remove_position(pos)
                    logger.info(f"[{pair_name}] ポジション決済完了")
                else:
                    # 建値移動などで更新された場合は保存し直す
                    positions_all = load_positions()
                    for p in positions_all:
                        if (p.get("pair") == pos.get("pair") and
                                p.get("entry_time") == pos.get("entry_time")):
                            p.update(updated)
                    save_positions(positions_all)

            # 最大ポジション数に達していたら新規エントリーをスキップ
            active_positions = load_positions()
            if len(active_positions) >= MAX_POSITIONS:
                logger.info(f"[ポジション上限] {len(active_positions)}/{MAX_POSITIONS} 保有中 — 新規エントリースキップ")
                time.sleep(LOOP_SEC)
                continue

            # ── トレードフィルター ─────────────────────────────
            if tf:
                filter_ok, filter_msg = tf.is_tradeable(now_jst, SPREAD_PIPS)
                if not filter_ok:
                    logger.info(f"フィルター: {filter_msg}")
                    time.sleep(LOOP_SEC)
                    continue

            # ── 各ペアのシグナルチェック ──────────────────────
            found_signal = False
            for ticker, pair_name in PAIRS.items():
                try:
                    df_1h, df_4h = _with_retry(fetch_data, ticker, pair_name,
                                               label=f"fetch {pair_name}")
                    current_price = float(df_1h["Close"].iloc[-1])
                    logger.info(f"{pair_name}: {current_price:.3f}")
                except Exception as e:
                    logger.error(f"{pair_name} データ取得失敗（{MAX_RETRY}回リトライ済み）: {e}")
                    continue

                # 全ペア H&S 戦略のみ（EMAクロスは廃止）
                # バックテスト結果: USD/JPY +1333p, EUR/JPY +1023p, AUD/JPY +2895p
                signal = check_hs_signal(df_4h)
                if signal:
                    logger.info(f"[H&S/{pair_name}] {signal['action']}  {signal['reason']}")

                if signal:
                    active_positions = load_positions()
                    if len(active_positions) < MAX_POSITIONS:
                        place_order(signal, now_jst, pair=pair_name)
                        found_signal = True
                    else:
                        logger.info(f"[ポジション上限] {pair_name} シグナルあり — "
                                    f"最大{MAX_POSITIONS}ポジション保有中のためスキップ")

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
