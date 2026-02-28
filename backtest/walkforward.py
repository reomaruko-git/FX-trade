"""
walkforward.py — ウォークフォワード検証
==========================================
過剰最適化チェック: データを前半(in-sample)と後半(out-of-sample)に分割し
同じパラメータで両期間の成績を比較する。

前半で良くて後半が悪い → 過剰最適化の疑い強
前半と後半が近い成績  → パラメータの汎化性が高い

使い方:
    python3 walkforward.py                  # 全ペア・前後半1年ずつ
    python3 walkforward.py --pair USDJPY    # ペア指定
    python3 walkforward.py --split 0.5      # 分割比率変更（デフォルト0.5=半分）
"""

from __future__ import annotations

import argparse
import warnings
from datetime import datetime, timezone, timedelta

import contextlib, io
import numpy as np
import pandas as pd
from scipy.signal import find_peaks

warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────
# ▌ パラメータ（backtest.py と完全一致）
# ─────────────────────────────────────────────────────────────
HS_BUFFER_PIPS = 0.05
MAX_SL_PIPS    = 80
SMA_PERIOD     = 200
HS_DISTANCE    = 5
HS_TOL         = 0.020
LOT            = 20000
INITIAL_BALANCE = 500_000

PAIRS = {
    "USDJPY=X": "USD/JPY",
    "EURJPY=X": "EUR/JPY",
    "AUDJPY=X": "AUD/JPY",
    "GBPJPY=X": "GBP/JPY",   # 比較のため含める
}


def _pips(diff: float) -> float:
    return round(diff * 100, 1)


def _quiet(func, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return func(*args, **kwargs)


# ─────────────────────────────────────────────────────────────
# ▌ データ取得
# ─────────────────────────────────────────────────────────────
def fetch_data(ticker: str) -> pd.DataFrame:
    import yfinance as yf
    end   = datetime.now(tz=timezone.utc)
    start = end - timedelta(days=728)
    raw = yf.download(ticker, start=start, end=end,
                      interval="1h", progress=False, auto_adjust=True)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df_4h = raw.resample("4h").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    ).dropna()
    return df_4h


# ─────────────────────────────────────────────────────────────
# ▌ H&S 検出（backtest.py と完全同一ロジック）
# ─────────────────────────────────────────────────────────────
def detect_hs_at(df: pd.DataFrame, i: int,
                 distance: int = HS_DISTANCE, tol: float = HS_TOL):
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
                    "right_shoulder_high": float(highs[rs_i]),
                    "right_shoulder_low": rs_low,
                    "head": float(head), "neckline": neckline,
                    "rs_idx": window_start + rs_i}
    return None


# ─────────────────────────────────────────────────────────────
# ▌ H&S バックテスト（期間指定付き）
# ─────────────────────────────────────────────────────────────
def backtest_hs(df_full: pd.DataFrame, pair_name: str,
                start_dt=None, end_dt=None) -> pd.DataFrame:
    """
    df_full: 全期間データ（SMA200計算のため分割前の全データを渡す）
    start_dt / end_dt: 評価対象期間（エントリー判定はこの期間のみ）
    """
    import pandas_ta as ta

    sma_s = df_full["Close"].rolling(SMA_PERIOD).mean()
    trades      = []
    pos         = None
    last_rs_idx = -999

    for i in range(SMA_PERIOD + 30, len(df_full)):
        ts = df_full.index[i]

        # 評価期間フィルター（エントリーのみ制限）
        in_window = True
        if start_dt is not None and ts < start_dt:
            in_window = False
        if end_dt is not None and ts >= end_dt:
            in_window = False

        close  = float(df_full["Close"].iloc[i])
        high   = float(df_full["High"].iloc[i])
        low    = float(df_full["Low"].iloc[i])
        sma200 = float(sma_s.iloc[i]) if not pd.isna(sma_s.iloc[i]) else close

        # ── ポジション管理（期間外でも継続） ────────────────
        if pos:
            direction   = pos["direction"]
            entry_price = pos["entry_price"]
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
                    "direction":   direction,
                    "entry_time":  pos["entry_time"],
                    "exit_time":   ts,
                    "entry_price": entry_price,
                    "exit_price":  exit_price,
                    "sl":          pos["sl"],
                    "tp":          pos["tp"],
                    "pnl_pips":    pnl_pips,
                    "pnl_jpy":     int(pnl_pips * LOT / 100),
                    "result":      "WIN" if pnl_pips > 0 else "LOSS",
                    "exit_reason": "TP" if tp_hit else "SL",
                })
                pos = None
            continue

        if not in_window:
            continue

        # ── エントリー判定 ───────────────────────────────────
        hs = detect_hs_at(df_full, i)
        if hs is None:
            continue
        global_rs = hs.get("rs_idx", -1)
        if global_rs <= last_rs_idx:
            continue

        pattern = hs["pattern"]
        if pattern == "HEAD_AND_SHOULDERS" and close < sma200:
            sl     = round(hs["right_shoulder_high"] + HS_BUFFER_PIPS, 3)
            sl_pip = abs(close - sl) * 100
            if sl_pip > MAX_SL_PIPS:
                continue
            if sl <= close:
                continue
            depth = hs["head"] - hs["neckline"]
            tp    = round(hs["neckline"] - depth, 3)
            if tp >= close:
                tp = round(close - sl_pip / 100 * 2, 3)
            pos = {"direction": "SELL", "entry_price": close,
                   "sl": sl, "tp": tp, "entry_time": ts}
            last_rs_idx = global_rs

        elif pattern == "INV_HEAD_AND_SHOULDERS" and close > sma200:
            sl     = round(hs["right_shoulder_low"] - HS_BUFFER_PIPS, 3)
            sl_pip = abs(close - sl) * 100
            if sl_pip > MAX_SL_PIPS:
                continue
            if sl >= close:
                continue
            depth = hs["neckline"] - hs["head"]
            tp    = round(hs["neckline"] + depth, 3)
            if tp <= close:
                tp = round(close + sl_pip / 100 * 2, 3)
            pos = {"direction": "BUY", "entry_price": close,
                   "sl": sl, "tp": tp, "entry_time": ts}
            last_rs_idx = global_rs

    return pd.DataFrame(trades)


# ─────────────────────────────────────────────────────────────
# ▌ 成績サマリー
# ─────────────────────────────────────────────────────────────
def summarize(df: pd.DataFrame, label: str) -> dict:
    if df.empty:
        print(f"    トレードなし")
        return {"label": label, "trades": 0, "win_rate": 0,
                "total_pips": 0, "total_jpy": 0, "max_dd_pct": 0}
    total     = len(df)
    wins      = (df["result"] == "WIN").sum()
    losses    = total - wins
    win_rate  = round(wins / total * 100, 1)
    total_pips = round(df["pnl_pips"].sum(), 1)
    total_jpy  = df["pnl_jpy"].sum()
    avg_win   = round(df.loc[df["result"] == "WIN", "pnl_pips"].mean(), 1) if wins   > 0 else 0.0
    avg_loss  = round(df.loc[df["result"] == "LOSS","pnl_pips"].mean(), 1) if losses > 0 else 0.0

    balance_curve = INITIAL_BALANCE + df["pnl_jpy"].cumsum()
    peak     = balance_curve.cummax()
    dd       = (balance_curve - peak)
    max_dd_pct = round(dd.min() / peak[dd.idxmin()] * 100, 1) if not dd.empty else 0.0

    print(f"    トレード: {total:3d}回（{wins}勝 {losses}敗）  "
          f"勝率: {win_rate:5.1f}%  "
          f"損益: {total_pips:+8.1f} pips  ({total_jpy:+,}円)  "
          f"最大DD: {max_dd_pct:.1f}%")

    return {"label": label, "trades": total, "wins": wins, "losses": losses,
            "win_rate": win_rate, "total_pips": total_pips, "total_jpy": total_jpy,
            "avg_win": avg_win, "avg_loss": avg_loss, "max_dd_pct": max_dd_pct}


# ─────────────────────────────────────────────────────────────
# ▌ 過剰最適化スコア判定
# ─────────────────────────────────────────────────────────────
def overfit_score(in_sample: dict, out_sample: dict) -> str:
    """
    in-sampleとout-of-sampleの成績乖離を評価
    """
    if in_sample["trades"] == 0 or out_sample["trades"] == 0:
        return "⚪ データ不足"

    # pips per trade で比較
    in_ppt  = in_sample["total_pips"]  / in_sample["trades"]  if in_sample["trades"]  > 0 else 0
    out_ppt = out_sample["total_pips"] / out_sample["trades"] if out_sample["trades"] > 0 else 0

    if in_ppt <= 0:
        ratio = 0
    else:
        ratio = out_ppt / in_ppt   # 1.0 = 同じ成績, 0 = 全く再現せず

    if out_ppt < 0 and in_ppt > 0:
        verdict = "🔴 過剰最適化の疑い強（OOS がマイナス）"
    elif ratio < 0.3:
        verdict = "🔴 過剰最適化の疑い強（OOS がIS比 30%未満）"
    elif ratio < 0.6:
        verdict = "🟡 やや劣化あり（OOS がIS比 30〜60%）"
    elif ratio < 0.9:
        verdict = "🟢 許容範囲内（OOS がIS比 60〜90%）"
    else:
        verdict = "🟢 良好（OOS がIS比 90%以上）"

    print(f"      IS: {in_ppt:+.1f} pips/trade  →  OOS: {out_ppt:+.1f} pips/trade  "
          f"（再現率 {ratio*100:.0f}%）  {verdict}")
    return verdict


# ─────────────────────────────────────────────────────────────
# ▌ メイン
# ─────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="ウォークフォワード検証")
    parser.add_argument("--pair",  default="all",
                        help="USDJPY / EURJPY / AUDJPY / GBPJPY / all")
    parser.add_argument("--split", type=float, default=0.5,
                        help="前後半の分割比率（デフォルト 0.5 = 半分）")
    args = parser.parse_args()

    if args.pair.upper() == "ALL":
        target_pairs = PAIRS
    else:
        key = args.pair.upper() + "=X"
        target_pairs = {key: PAIRS[key]} if key in PAIRS else PAIRS

    print("=" * 70)
    print("  ウォークフォワード検証（過剰最適化チェック）")
    print(f"  分割比率: 前半（in-sample）{args.split*100:.0f}% ／ "
          f"後半（out-of-sample）{(1-args.split)*100:.0f}%")
    print(f"  パラメータ: distance={HS_DISTANCE}, tol={HS_TOL}, "
          f"MAX_SL_PIPS={MAX_SL_PIPS}")
    print("=" * 70)

    all_in, all_out = [], []

    for ticker, pair_name in target_pairs.items():
        print(f"\n▶ {pair_name}  データ取得中...")
        try:
            df_4h = fetch_data(ticker)
        except Exception as e:
            print(f"  ❌ 取得失敗: {e}")
            continue

        # 期間分割
        split_idx = int(len(df_4h) * args.split)
        split_dt  = df_4h.index[split_idx]

        start_dt  = df_4h.index[0]
        end_dt    = df_4h.index[-1]

        print(f"  全期間: {start_dt.date()} 〜 {end_dt.date()}  ({len(df_4h)}本)")
        print(f"  分割点: {split_dt.date()}")
        print(f"  前半（IS） : {start_dt.date()} 〜 {split_dt.date()}")
        print(f"  後半（OOS）: {split_dt.date()} 〜 {end_dt.date()}")

        # in-sample バックテスト
        print(f"\n  【前半 in-sample（パラメータ最適化期間）】")
        df_in = backtest_hs(df_4h, pair_name,
                            start_dt=start_dt, end_dt=split_dt)
        s_in = summarize(df_in, f"{pair_name} IS")

        # out-of-sample バックテスト（未知の期間）
        print(f"\n  【後半 out-of-sample（未知の期間・汎化性チェック）】")
        df_out = backtest_hs(df_4h, pair_name,
                             start_dt=split_dt, end_dt=None)
        s_out = summarize(df_out, f"{pair_name} OOS")

        # 乖離評価
        print(f"\n  ▼ 過剰最適化判定:")
        overfit_score(s_in, s_out)

        all_in.append(df_in)
        all_out.append(df_out)

    # ─── 全ペア合計 ───────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  【全ペア合計】")

    df_all_in  = pd.concat([d for d in all_in  if not d.empty], ignore_index=True) \
                 if any(not d.empty for d in all_in)  else pd.DataFrame()
    df_all_out = pd.concat([d for d in all_out if not d.empty], ignore_index=True) \
                 if any(not d.empty for d in all_out) else pd.DataFrame()

    print(f"\n  前半（in-sample）:")
    s_total_in  = summarize(df_all_in,  "全ペア IS")
    print(f"\n  後半（out-of-sample）:")
    s_total_out = summarize(df_all_out, "全ペア OOS")
    print(f"\n  ▼ 総合判定:")
    overfit_score(s_total_in, s_total_out)

    print("\n" + "=" * 70)
    print("\n📌 見方のガイド:")
    print("  🟢 OOS ≥ IS の60%  → パラメータは汎化性あり、本番でも期待できる")
    print("  🟡 OOS = IS の30〜60% → やや劣化。ロット控えめで様子見を推奨")
    print("  🔴 OOS < IS の30%  → 過剰最適化の疑い強。パラメータ見直しを推奨")
    print()


if __name__ == "__main__":
    main()
