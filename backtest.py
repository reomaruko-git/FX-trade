"""
backtest.py — FX Trade Luce バックテスト
=========================================
過去データ（最大2年・1時間足）で EMAクロス + H&S 戦略を検証する。

使い方:
    # 全ペア・全戦略
    python3 backtest.py

    # ペア指定
    python3 backtest.py --pair USDJPY

    # 戦略指定
    python3 backtest.py --strategy ema
    python3 backtest.py --strategy hs

結果は backtest_results/ フォルダに保存される（git管理外）。
"""

from __future__ import annotations

import argparse
import warnings
from datetime import datetime, timezone, timedelta
from pathlib import Path

import contextlib
import io

import numpy as np
import pandas as pd
import pandas_ta as ta
from scipy.signal import find_peaks

warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────
# ▌ 設定（auto_trader.py と同じ値）
# ─────────────────────────────────────────────────────────────
EMA_FAST       = 21    # 短期EMA（12→21: 1h足の過剰シグナルを抑制）
EMA_SLOW       = 55    # 中期EMA（21→55: より信頼性の高いトレンドフォロー）
ADX_PERIOD     = 14    # ADX 計算期間
ADX_MIN        = 20    # これ以上のADX値のみエントリー（25→20: 4h足なので少し緩める）
MAX_POSITIONS  = 2     # 最大同時保有ポジション数
SL_MULT        = 2.0   # SL = エントリー ± ATR × SL_MULT
TP_MULT        = 4.0   # TP = エントリー ∓ ATR × TP_MULT (RR 1:2)
HS_BUFFER_PIPS = 0.05
HS_RR          = 2.0
SMA_PERIOD     = 200
BREAKEVEN_PIPS = 20    # ブレイクイーブン移動を少し遅らせる（15→20）
MAX_SL_PIPS    = 80    # SL上限（pips）。50→80に拡張（4h足基準に合わせる）
LOT            = 20000
INITIAL_BALANCE = 500000

PAIRS = {
    "USDJPY=X": "USD/JPY",
    "GBPJPY=X": "GBP/JPY",
    "EURJPY=X": "EUR/JPY",
    "AUDJPY=X": "AUD/JPY",
}

RESULTS_DIR = Path(__file__).parent / "backtest_results"


# ─────────────────────────────────────────────────────────────
# ▌ ユーティリティ
# ─────────────────────────────────────────────────────────────
def _quiet(func, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return func(*args, **kwargs)


def _pips(price_diff: float) -> float:
    return round(price_diff * 100, 1)


# ─────────────────────────────────────────────────────────────
# ▌ データ取得
# ─────────────────────────────────────────────────────────────
def fetch_data(ticker: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """yfinance から 1h / 4h 足を取得（最大730日）"""
    import yfinance as yf
    end   = datetime.now(tz=timezone.utc)
    start = end - timedelta(days=728)   # yfinance 1h の上限
    print(f"  データ取得中: {ticker} ({start.date()} 〜 {end.date()})")
    raw = yf.download(ticker, start=start, end=end,
                      interval="1h", progress=False, auto_adjust=True)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df_1h = raw[["Open","High","Low","Close","Volume"]].dropna()
    df_4h = raw.resample("4h").agg(
        {"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}
    ).dropna()
    print(f"  1h: {len(df_1h)}本  4h: {len(df_4h)}本")
    return df_1h, df_4h


# ─────────────────────────────────────────────────────────────
# ▌ テクニカル計算
# ─────────────────────────────────────────────────────────────
def calc_ema(df, period) -> pd.Series:
    return _quiet(df.ta.ema, length=period, append=False)


def calc_atr(df, period=14) -> pd.Series:
    return _quiet(df.ta.atr, length=period, append=False)


def calc_adx(df, period=14) -> pd.Series:
    adx_df = _quiet(df.ta.adx, length=period, append=False)
    col = f"ADX_{period}"
    if adx_df is None or col not in adx_df.columns:
        return pd.Series([0.0] * len(df), index=df.index)
    return adx_df[col].fillna(0.0)


def calc_sma200(df) -> pd.Series:
    return df["Close"].rolling(SMA_PERIOD).mean()


def detect_hs_at(df, i, distance=5, tol=0.020):
    """
    i番目の足時点でのH&Sパターン検知
    ── run_backtest.py と同じロジックで統一（100本窓・全組み合わせ探索）
    """
    window_start = max(0, i - 100)
    window = df.iloc[window_start: i + 1]
    if len(window) < distance * 3:
        return None
    highs = window["High"].values
    lows  = window["Low"].values
    n     = len(highs)

    # 天井 H&S → SELL
    peak_idx, _ = find_peaks(highs, distance=distance)
    if len(peak_idx) >= 3:
        for k in range(len(peak_idx) - 3, -1, -1):
            ls_i = int(peak_idx[k])
            hd_i = int(peak_idx[k + 1])
            rs_i = int(peak_idx[k + 2])
            ls, head, rs = highs[ls_i], highs[hd_i], highs[rs_i]

            if rs_i < n - distance * 4:
                continue
            if head <= max(ls, rs):
                continue
            if abs(ls - rs) / (head + 1e-9) > tol:
                continue

            neck1    = float(lows[ls_i:hd_i].min()) if hd_i > ls_i else float(lows[ls_i])
            neck2    = float(lows[hd_i:rs_i].min()) if rs_i > hd_i else float(lows[hd_i])
            neckline = (neck1 + neck2) / 2
            buf      = max(1, distance // 2)
            rs_high  = float(highs[max(0, rs_i - buf): rs_i + buf + 1].max())
            return {"pattern": "HEAD_AND_SHOULDERS",
                    "right_shoulder_high": rs_high, "head": float(head),
                    "neckline": neckline, "rs_idx": window_start + rs_i}

    # 逆 H&S → BUY
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
            neckline = (neck1 + neck2) / 2
            buf      = max(1, distance // 2)
            rs_low   = float(lows[max(0, rs_i - buf): rs_i + buf + 1].min())
            return {"pattern": "INV_HEAD_AND_SHOULDERS",
                    "right_shoulder_high": float(highs[rs_i]), "right_shoulder_low": rs_low,
                    "head": float(head), "neckline": neckline, "rs_idx": window_start + rs_i}
    return None


# ─────────────────────────────────────────────────────────────
# ▌ EMAクロスバックテスト
# ─────────────────────────────────────────────────────────────
def backtest_ema(df_1h: pd.DataFrame, pair_name: str) -> pd.DataFrame:
    """EMAクロス + ADXフィルター戦略をバックテストして取引ログを返す（複数ポジション対応）"""
    ema_fast_s = calc_ema(df_1h, EMA_FAST)
    ema_slow_s = calc_ema(df_1h, EMA_SLOW)
    atr_s      = calc_atr(df_1h)
    sma_s      = calc_sma200(df_1h)
    adx_s      = calc_adx(df_1h, ADX_PERIOD)

    trades    = []
    positions = []   # 複数ポジション管理（最大 MAX_POSITIONS）

    start = max(SMA_PERIOD, EMA_SLOW) + 5
    for i in range(start, len(df_1h)):
        close  = float(df_1h["Close"].iloc[i])
        high   = float(df_1h["High"].iloc[i])
        low    = float(df_1h["Low"].iloc[i])
        ts     = df_1h.index[i]
        atr    = float(atr_s.iloc[i])   if not pd.isna(atr_s.iloc[i])      else 0.001
        sma200 = float(sma_s.iloc[i])   if not pd.isna(sma_s.iloc[i])      else close
        adx    = float(adx_s.iloc[i])   if not pd.isna(adx_s.iloc[i])      else 0.0
        ef_now  = float(ema_fast_s.iloc[i])   if not pd.isna(ema_fast_s.iloc[i])  else close
        es_now  = float(ema_slow_s.iloc[i])   if not pd.isna(ema_slow_s.iloc[i])  else close
        ef_prev = float(ema_fast_s.iloc[i-1]) if not pd.isna(ema_fast_s.iloc[i-1]) else close
        es_prev = float(ema_slow_s.iloc[i-1]) if not pd.isna(ema_slow_s.iloc[i-1]) else close

        # ── 保有ポジションの管理（全件チェック） ────────────
        to_close = []
        for pos in positions:
            direction   = pos["direction"]
            entry_price = pos["entry_price"]
            pnl_now     = _pips(close - entry_price) if direction == "BUY" \
                          else _pips(entry_price - close)

            if not pos["breakeven_done"] and pnl_now >= BREAKEVEN_PIPS:
                pos["sl"]             = entry_price
                pos["breakeven_done"] = True

            tp_hit = (direction == "BUY"  and high >= pos["tp"]) or \
                     (direction == "SELL" and low  <= pos["tp"])
            sl_hit = (direction == "BUY"  and low  <= pos["sl"]) or \
                     (direction == "SELL" and high >= pos["sl"])

            if tp_hit or sl_hit:
                exit_price = pos["tp"] if tp_hit else pos["sl"]
                pnl_pips   = _pips(exit_price - entry_price) if direction == "BUY" \
                             else _pips(entry_price - exit_price)
                trades.append({
                    "pair":        pair_name,
                    "strategy":    "EMA",
                    "direction":   direction,
                    "entry_time":  pos["entry_time"],
                    "exit_time":   ts,
                    "entry_price": entry_price,
                    "exit_price":  exit_price,
                    "sl":          pos["sl_orig"],
                    "tp":          pos["tp"],
                    "pnl_pips":    pnl_pips,
                    "pnl_jpy":     int(pnl_pips * LOT / 100),
                    "result":      "WIN" if pnl_pips > 0 else "LOSS",
                    "exit_reason": "TP" if tp_hit else "SL",
                })
                to_close.append(pos)

        for pos in to_close:
            positions.remove(pos)

        # ── エントリー判定（最大ポジション数未満のみ） ──────
        if len(positions) >= MAX_POSITIONS:
            continue   # 最大保有数に達したらスキップ

        if adx < ADX_MIN:
            continue   # 横ばい相場はスキップ

        golden_cross = (ef_prev <= es_prev) and (ef_now > es_now)
        dead_cross   = (ef_prev >= es_prev) and (ef_now < es_now)

        if golden_cross and close > sma200:
            sl = round(close - atr * SL_MULT, 3)
            tp = round(close + atr * TP_MULT, 3)
            if abs(close - sl) * 100 > MAX_SL_PIPS:
                continue
            positions.append({"direction": "BUY", "entry_price": close, "sl": sl,
                               "sl_orig": sl, "tp": tp, "entry_time": ts, "breakeven_done": False})

        elif dead_cross and close < sma200:
            sl = round(close + atr * SL_MULT, 3)
            tp = round(close - atr * TP_MULT, 3)
            if abs(close - sl) * 100 > MAX_SL_PIPS:
                continue
            positions.append({"direction": "SELL", "entry_price": close, "sl": sl,
                               "sl_orig": sl, "tp": tp, "entry_time": ts, "breakeven_done": False})

    return pd.DataFrame(trades)


# ─────────────────────────────────────────────────────────────
# ▌ H&Sバックテスト
# ─────────────────────────────────────────────────────────────
def _is_friday_close_bar(ts: pd.Timestamp) -> bool:
    """
    金曜 23:00 JST（= UTC 14:00）を含む4時間足かどうか判定。
    yfinance の4時間足は UTC で 12:00 始まりの足が 12:00〜16:00 をカバーするため、
    金曜 UTC 12:00 以降の足を強制決済対象とする。
    """
    # タイムゾーン付きに統一
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.weekday() == 4 and ts.hour >= 12   # 4 = Friday


def backtest_hs(df_4h: pd.DataFrame, pair_name: str,
                friday_close: bool = False) -> pd.DataFrame:
    """
    H&S戦略をバックテスト（run_backtest.pyと同ロジック統一版）

    friday_close=True: 金曜 23:00 JST（UTC 14:00）に未決済ポジションを強制決済
                       ← auto_trader.py の週末クローズ機能と同じ挙動
    friday_close=False: SL/TPのみで決済（デフォルト）
    """
    sma_s       = calc_sma200(df_4h)
    atr_s       = calc_atr(df_4h)
    trades      = []
    pos         = None
    last_rs_idx = -999   # 重複エントリー防止

    for i in range(SMA_PERIOD + 30, len(df_4h)):
        close  = float(df_4h["Close"].iloc[i])
        high   = float(df_4h["High"].iloc[i])
        low    = float(df_4h["Low"].iloc[i])
        ts     = df_4h.index[i]
        sma200 = float(sma_s.iloc[i]) if not pd.isna(sma_s.iloc[i]) else close

        # ── ポジション管理（ブレイクイーブンなし）────────────────
        if pos:
            direction   = pos["direction"]
            entry_price = pos["entry_price"]

            tp_hit = (direction == "BUY"  and high >= pos["tp"]) or \
                     (direction == "SELL" and low  <= pos["tp"])
            sl_hit = (direction == "BUY"  and low  <= pos["sl"]) or \
                     (direction == "SELL" and high >= pos["sl"])

            # 金曜強制決済チェック（SL/TP判定より先に行う）
            fri_hit = friday_close and _is_friday_close_bar(ts)

            if fri_hit and not tp_hit and not sl_hit:
                # 強制決済: 終値で処理
                exit_price = close
                pnl_pips   = _pips(exit_price - entry_price) if direction == "BUY" \
                             else _pips(entry_price - exit_price)
                trades.append({
                    "pair":        pair_name,
                    "strategy":    "H&S",
                    "direction":   direction,
                    "entry_time":  pos["entry_time"],
                    "exit_time":   ts,
                    "entry_price": entry_price,
                    "exit_price":  exit_price,
                    "sl":          pos["sl_orig"],
                    "tp":          pos["tp"],
                    "pnl_pips":    pnl_pips,
                    "pnl_jpy":     int(pnl_pips * LOT / 100),
                    "result":      "WIN" if pnl_pips > 0 else "LOSS",
                    "exit_reason": "FRIDAY_CLOSE",
                })
                pos = None
                continue

            if tp_hit or sl_hit:
                exit_price = pos["tp"] if tp_hit else pos["sl"]
                pnl_pips   = _pips(exit_price - entry_price) if direction == "BUY" \
                             else _pips(entry_price - exit_price)
                trades.append({
                    "pair":        pair_name,
                    "strategy":    "H&S",
                    "direction":   direction,
                    "entry_time":  pos["entry_time"],
                    "exit_time":   ts,
                    "entry_price": entry_price,
                    "exit_price":  exit_price,
                    "sl":          pos["sl_orig"],
                    "tp":          pos["tp"],
                    "pnl_pips":    pnl_pips,
                    "pnl_jpy":     int(pnl_pips * LOT / 100),
                    "result":      "WIN" if pnl_pips > 0 else "LOSS",
                    "exit_reason": "TP" if tp_hit else "SL",
                })
                pos = None
            continue

        # 金曜はエントリーしない（friday_close=True の場合）
        if friday_close and _is_friday_close_bar(ts):
            continue

        # ── エントリー判定 ────────────────────────────────
        hs = detect_hs_at(df_4h, i)
        if hs is None:
            continue

        # 重複エントリー防止
        global_rs = hs.get("rs_idx", -1)
        if global_rs <= last_rs_idx:
            continue

        pattern = hs["pattern"]
        if pattern == "HEAD_AND_SHOULDERS" and close < sma200:
            sl     = round(hs["right_shoulder_high"] + HS_BUFFER_PIPS, 3)
            sl_pip = abs(close - sl) * 100
            if sl_pip > MAX_SL_PIPS:
                continue
            # TP: ネックライン倍返し（run_backtest.pyと統一）
            depth = hs["head"] - hs["neckline"]
            tp    = round(hs["neckline"] - depth, 3)
            if tp >= close:
                tp = round(close - sl_pip / 100 * 2, 3)
            pos = {"direction": "SELL", "entry_price": close, "sl": sl, "sl_orig": sl,
                   "tp": tp, "entry_time": ts, "breakeven_done": False}
            last_rs_idx = global_rs

        elif pattern == "INV_HEAD_AND_SHOULDERS" and close > sma200:
            sl     = round(hs["right_shoulder_low"] - HS_BUFFER_PIPS, 3)
            sl_pip = abs(close - sl) * 100
            if sl_pip > MAX_SL_PIPS:
                continue
            depth = hs["neckline"] - hs["head"]
            tp    = round(hs["neckline"] + depth, 3)
            if tp <= close:
                tp = round(close + sl_pip / 100 * 2, 3)
            pos = {"direction": "BUY", "entry_price": close, "sl": sl, "sl_orig": sl,
                   "tp": tp, "entry_time": ts, "breakeven_done": False}
            last_rs_idx = global_rs

    return pd.DataFrame(trades)


# ─────────────────────────────────────────────────────────────
# ▌ 結果集計・表示
# ─────────────────────────────────────────────────────────────
def summarize(df: pd.DataFrame, label: str) -> dict:
    """取引ログをサマリーにまとめる"""
    if df.empty:
        print(f"  [{label}] トレードなし")
        return {}

    total     = len(df)
    wins      = (df["result"] == "WIN").sum()
    losses    = total - wins
    win_rate  = round(wins / total * 100, 1)
    total_pips = round(df["pnl_pips"].sum(), 1)
    total_jpy  = df["pnl_jpy"].sum()
    avg_win   = round(df.loc[df["result"] == "WIN",  "pnl_pips"].mean(), 1) if wins   > 0 else 0.0
    avg_loss  = round(df.loc[df["result"] == "LOSS", "pnl_pips"].mean(), 1) if losses > 0 else 0.0
    max_win   = round(df["pnl_pips"].max(), 1)
    max_loss  = round(df["pnl_pips"].min(), 1)

    # 最大ドローダウン（資産曲線から計算）
    balance_curve = INITIAL_BALANCE + df["pnl_jpy"].cumsum()
    peak          = balance_curve.cummax()
    drawdown      = (balance_curve - peak)
    max_dd        = int(drawdown.min())
    max_dd_pct    = round(drawdown.min() / peak[drawdown.idxmin()] * 100, 1) if not drawdown.empty else 0.0

    summary = {
        "label":      label,
        "trades":     total,
        "wins":       wins,
        "losses":     losses,
        "win_rate":   win_rate,
        "total_pips": total_pips,
        "total_jpy":  total_jpy,
        "avg_win":    avg_win,
        "avg_loss":   avg_loss,
        "max_win":    max_win,
        "max_loss":   max_loss,
        "max_dd_jpy": max_dd,
        "max_dd_pct": max_dd_pct,
        "final_balance": int(INITIAL_BALANCE + total_jpy),
    }

    print(f"\n  {'─'*45}")
    print(f"  【{label}】")
    print(f"  {'─'*45}")
    print(f"  総トレード数 : {total}回  ({wins}勝 {losses}敗)")
    print(f"  勝率         : {win_rate}%")
    print(f"  累計損益     : {total_pips:+.1f} pips  ({total_jpy:+,}円)")
    print(f"  平均利益     : +{avg_win:.1f} pips  /  平均損失: {avg_loss:.1f} pips")
    print(f"  最大利益     : +{max_win:.1f} pips  /  最大損失: {max_loss:.1f} pips")
    print(f"  最大DD       : {max_dd:,}円 ({max_dd_pct}%)")
    print(f"  最終残高     : {INITIAL_BALANCE:,}円 → {summary['final_balance']:,}円")

    return summary


def save_results(df: pd.DataFrame, summaries: list[dict]):
    """結果をCSVとサマリーテキストに保存"""
    RESULTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")

    # 取引ログCSV
    csv_path = RESULTS_DIR / f"trades_{ts}.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"\n  💾 取引ログ保存: {csv_path.name}")

    # サマリーテキスト
    summary_path = RESULTS_DIR / f"summary_{ts}.txt"
    lines = [f"FX Trade Luce バックテスト結果  {ts}\n", "="*50 + "\n"]
    for s in summaries:
        if not s:
            continue
        lines += [
            f"\n【{s['label']}】\n",
            f"  総トレード: {s['trades']}回  ({s['wins']}勝 {s['losses']}敗)\n",
            f"  勝率      : {s['win_rate']}%\n",
            f"  累計損益  : {s['total_pips']:+.1f} pips  ({s['total_jpy']:+,}円)\n",
            f"  最大DD    : {s['max_dd_jpy']:,}円 ({s['max_dd_pct']}%)\n",
            f"  最終残高  : {s['final_balance']:,}円\n",
        ]
    summary_path.write_text("".join(lines), encoding="utf-8")
    print(f"  💾 サマリー保存: {summary_path.name}")


# ─────────────────────────────────────────────────────────────
# ▌ メイン
# ─────────────────────────────────────────────────────────────
def compare_friday_close(df_4h: pd.DataFrame, pair_name: str):
    """金曜強制決済あり・なしを比較して差異を表示する"""
    df_no  = backtest_hs(df_4h, pair_name, friday_close=False)
    df_fri = backtest_hs(df_4h, pair_name, friday_close=True)

    def _stats(df):
        if df.empty:
            return {"trades": 0, "win_rate": 0, "total_pips": 0, "total_jpy": 0,
                    "max_dd_pct": 0, "avg_win": 0, "avg_loss": 0}
        total  = len(df)
        wins   = (df["result"] == "WIN").sum()
        losses = total - wins
        wr     = round(wins / total * 100, 1) if total > 0 else 0
        pips   = round(df["pnl_pips"].sum(), 1)
        jpy    = df["pnl_jpy"].sum()
        aw     = round(df.loc[df["result"] == "WIN",  "pnl_pips"].mean(), 1) if wins   > 0 else 0.0
        al     = round(df.loc[df["result"] == "LOSS", "pnl_pips"].mean(), 1) if losses > 0 else 0.0
        bal    = INITIAL_BALANCE + df["pnl_jpy"].cumsum()
        peak   = bal.cummax()
        dd     = (bal - peak)
        mdd    = round(dd.min() / peak[dd.idxmin()] * 100, 1) if not dd.empty else 0.0
        fri_n  = (df["exit_reason"] == "FRIDAY_CLOSE").sum() if "exit_reason" in df.columns else 0
        return {"trades": total, "wins": wins, "losses": losses,
                "win_rate": wr, "total_pips": pips, "total_jpy": jpy,
                "avg_win": aw, "avg_loss": al, "max_dd_pct": mdd, "fri_closes": fri_n}

    s0 = _stats(df_no)
    s1 = _stats(df_fri)

    # 金曜決済のうち何勝何敗か
    fri_trades = df_fri[df_fri["exit_reason"] == "FRIDAY_CLOSE"] if not df_fri.empty else pd.DataFrame()
    fri_win  = (fri_trades["result"] == "WIN").sum()  if not fri_trades.empty else 0
    fri_loss = (fri_trades["result"] == "LOSS").sum() if not fri_trades.empty else 0
    fri_pips = round(fri_trades["pnl_pips"].sum(), 1)  if not fri_trades.empty else 0.0

    w = 54
    print(f"\n  {'─'*w}")
    print(f"  【{pair_name}  金曜強制決済 比較】")
    print(f"  {'─'*w}")
    print(f"  {'項目':<20} {'なし（SL/TPのみ）':>15} {'あり（金曜23時）':>15}")
    print(f"  {'─'*w}")
    print(f"  {'トレード数':<20} {s0['trades']:>14}回 {s1['trades']:>14}回")
    print(f"  {'勝率':<20} {s0['win_rate']:>13.1f}% {s1['win_rate']:>13.1f}%")
    print(f"  {'累計損益(pips)':<20} {s0['total_pips']:>+14.1f} {s1['total_pips']:>+14.1f}")
    print(f"  {'累計損益(円)':<20} {s0['total_jpy']:>+14,} {s1['total_jpy']:>+14,}")
    print(f"  {'平均利益(pips)':<20} {s0['avg_win']:>+14.1f} {s1['avg_win']:>+14.1f}")
    print(f"  {'平均損失(pips)':<20} {s0['avg_loss']:>+14.1f} {s1['avg_loss']:>+14.1f}")
    print(f"  {'最大DD':<20} {s0['max_dd_pct']:>13.1f}% {s1['max_dd_pct']:>13.1f}%")
    print(f"  {'─'*w}")

    # 差分表示
    diff_pips = s1["total_pips"] - s0["total_pips"]
    diff_jpy  = s1["total_jpy"]  - s0["total_jpy"]
    sign_p = "+" if diff_pips >= 0 else ""
    sign_j = "+" if diff_jpy  >= 0 else ""
    verdict = "✅ 金曜クローズは有利" if diff_pips > 0 else "⚠️ 金曜クローズは不利" if diff_pips < 0 else "→ 差なし"
    print(f"  差分（あり − なし）: {sign_p}{diff_pips:.1f} pips  ({sign_j}{diff_jpy:,}円)  {verdict}")

    if s1["fri_closes"] > 0:
        print(f"  金曜決済の内訳      : {s1['fri_closes']}回（{fri_win}勝 {fri_loss}敗）  "
              f"合計 {fri_pips:+.1f} pips")

    return df_no, df_fri, s0, s1


def main():
    parser = argparse.ArgumentParser(description="FX Trade Luce バックテスト")
    parser.add_argument("--pair",     default="all",
                        help="USDJPY / GBPJPY / all（デフォルト: all）")
    parser.add_argument("--strategy", default="all",
                        help="ema / hs / all（デフォルト: all）")
    parser.add_argument("--friday-compare", action="store_true",
                        help="金曜強制決済あり・なしを比較モードで実行")
    args = parser.parse_args()

    run_ema = args.strategy in ("ema", "all")
    run_hs  = args.strategy in ("hs",  "all")

    # 対象ペア
    if args.pair.upper() == "ALL":
        target_pairs = PAIRS
    else:
        key = args.pair.upper() + "=X"
        target_pairs = {key: PAIRS[key]} if key in PAIRS else PAIRS

    print("=" * 60)
    print("  FX Trade Luce バックテスト開始")
    print(f"  対象ペア: {', '.join(target_pairs.values())}")
    print(f"  戦略   : {'EMAクロス ' if run_ema else ''}{'H&S' if run_hs else ''}")
    print(f"  初期資金: {INITIAL_BALANCE:,}円  ロット: {LOT:,}通貨")
    if args.friday_compare:
        print("  モード  : 金曜強制決済 比較モード")
    print("=" * 60)

    # ── 金曜比較モード ──────────────────────────────────────────
    if args.friday_compare and run_hs:
        all_no, all_fri = [], []
        for ticker, pair_name in target_pairs.items():
            print(f"\n▶ {pair_name}  データ取得中...")
            try:
                df_1h, df_4h = fetch_data(ticker)
            except Exception as e:
                print(f"  ❌ 取得失敗: {e}")
                continue
            if pair_name not in {"USD/JPY", "GBP/JPY", "EUR/JPY", "AUD/JPY"}:
                continue
            df_no, df_fri, _, _ = compare_friday_close(df_4h, pair_name)
            all_no.append(df_no)
            all_fri.append(df_fri)

        # 全ペア合計比較
        print("\n" + "=" * 60)
        print("  【全ペア合計 比較】")
        def _total_summary(dfs, label):
            combined = pd.concat([d for d in dfs if not d.empty], ignore_index=True) \
                       if any(not d.empty for d in dfs) else pd.DataFrame()
            if combined.empty:
                print(f"  [{label}] トレードなし")
                return
            summarize(combined, label)
        print("\n  ▼ 金曜クローズなし（SL/TPのみ）:")
        _total_summary(all_no,  "全ペア SL/TPのみ")
        print("\n  ▼ 金曜クローズあり（金曜23時強制決済）:")
        _total_summary(all_fri, "全ペア 金曜クローズ")
        print("\n✅ 比較完了！")
        return

    # ── 通常モード ──────────────────────────────────────────────
    all_trades  = []
    all_summaries = []

    for ticker, pair_name in target_pairs.items():
        print(f"\n▶ {pair_name}")
        try:
            df_1h, df_4h = fetch_data(ticker)
        except Exception as e:
            print(f"  ❌ データ取得失敗: {e}")
            continue

        EMA_PAIRS: set = set()  # 全ペア無効化（H&Sに統一）
        if run_ema and pair_name in EMA_PAIRS:
            print(f"\n  [EMAクロス 4h] バックテスト中...")
            df_ema = backtest_ema(df_4h, pair_name)
            s = summarize(df_ema, f"{pair_name} EMAクロス(4h)")
            all_trades.append(df_ema)
            all_summaries.append(s)

        HS_PAIRS = {"USD/JPY", "GBP/JPY", "EUR/JPY", "AUD/JPY"}
        if run_hs and pair_name in HS_PAIRS:
            print(f"\n  [H&S] バックテスト中...")
            df_hs = backtest_hs(df_4h, pair_name)
            s = summarize(df_hs, f"{pair_name} H&S")
            all_trades.append(df_hs)
            all_summaries.append(s)

    # 全トレード結合して保存
    if all_trades:
        combined = pd.concat([df for df in all_trades if not df.empty], ignore_index=True)
        if not combined.empty:
            print("\n" + "=" * 60)
            summarize(combined, "全戦略合計")
            save_results(combined, all_summaries)
    else:
        print("\n  トレードが1件もありませんでした。")

    print("\n✅ バックテスト完了！")


if __name__ == "__main__":
    main()
