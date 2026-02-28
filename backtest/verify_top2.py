#!/usr/bin/env python3
"""
verify_top2.py — H&S肩エントリー & ボリバン逆張り+RSI 詳細検証
================================================================
2戦略に絞って月次内訳・連勝連敗・パラメータ感度を出力する。

使い方:
    python3 verify_top2.py           # 4時間足 2年（デフォルト）
    python3 verify_top2.py --tf 1h   # 1時間足
    python3 verify_top2.py --tf 1d   # 日足 5年
"""
from __future__ import annotations

import argparse
import contextlib
import io
import warnings
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pandas_ta as ta
from scipy.signal import find_peaks

warnings.filterwarnings("ignore")

TICKER          = "USDJPY=X"
LOT             = 10000
MAX_SL_PIPS     = 80
INITIAL_BALANCE = 500_000

ROOT = Path(__file__).parent


# ─────────────────────────────────────────────────────────────
# ▌ ユーティリティ
# ─────────────────────────────────────────────────────────────
def _q(func, *a, **kw):
    with contextlib.redirect_stdout(io.StringIO()):
        return func(*a, **kw)

def _pips(diff: float) -> float:
    return round(diff * 100, 1)

def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    s = _q(df.ta.atr, length=period, append=False)
    return s if s is not None else pd.Series([0.001] * len(df), index=df.index)

def _get(series, i, default=0.0):
    v = series.iloc[i] if i < len(series) else None
    return float(v) if v is not None and not pd.isna(v) else default


# ─────────────────────────────────────────────────────────────
# ▌ データ取得
# ─────────────────────────────────────────────────────────────
def fetch_data(tf: str) -> tuple[pd.DataFrame, str]:
    import yfinance as yf
    end = datetime.now(tz=timezone.utc)

    if tf == "1d_5y":
        start    = end - timedelta(days=365 * 5)
        interval = "1d"
        label    = "日足 (5年)"
    elif tf == "1h":
        start    = end - timedelta(days=728)
        interval = "1h"
        label    = "1時間足"
    elif tf == "4h":
        start    = end - timedelta(days=728)
        interval = "1h"
        label    = "4時間足"
    else:
        start    = end - timedelta(days=728)
        interval = "1d"
        label    = "日足"

    print(f"📥 データ取得中: {TICKER}  {label}  ({start.date()} ～ {end.date()})")
    raw = yf.download(TICKER, start=start, end=end,
                      interval=interval, progress=False, auto_adjust=True)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Open", "High", "Low", "Close", "Volume"]].dropna()

    if tf == "4h":
        df = df.resample("4h").agg(
            {"Open": "first", "High": "max", "Low": "min",
             "Close": "last", "Volume": "sum"}
        ).dropna()

    print(f"✅ 取得完了: {len(df):,} 本のデータ")
    return df, label


# ─────────────────────────────────────────────────────────────
# ▌ ポジション管理ヘルパー
# ─────────────────────────────────────────────────────────────
def _manage_pos(pos, high, low, ts):
    if pos is None:
        return None, None
    direction   = pos["dir"]
    entry_price = pos["entry"]
    sl          = pos["sl"]
    tp          = pos["tp"]

    if direction == "BUY":
        if high >= tp:
            return _pips(tp - entry_price), None
        if low  <= sl:
            return _pips(sl - entry_price), None
    else:
        if low  <= tp:
            return _pips(entry_price - tp), None
        if high >= sl:
            return _pips(entry_price - sl), None
    return None, pos


# ─────────────────────────────────────────────────────────────
# ▌ 戦略 1: ボリバン逆張り + RSI（パラメータ可変）
# ─────────────────────────────────────────────────────────────
def strat_bb_rsi(df: pd.DataFrame,
                 bb_len: int    = 20,
                 bb_std: float  = 2.0,
                 rsi_buy: int   = 55,
                 rsi_sell: int  = 45,
                 sl_mult: float = 2.0,
                 tp_mult: float = 2.0) -> list[dict]:
    """
    BB クロスバック逆張り + RSI確認
    価格が BBL/BBU の外→中に戻った瞬間にエントリー（本来の平均回帰型）
    """
    bb  = _q(df.ta.bbands, length=bb_len, std=bb_std, append=False)
    rsi = _q(df.ta.rsi,    length=14, append=False)
    atr = _atr(df)

    bbl_col = bbu_col = None
    if bb is not None:
        for col in bb.columns:
            if col.startswith("BBL"):
                bbl_col = col
            if col.startswith("BBU"):
                bbu_col = col

    trades, pos = [], None
    for i in range(30, len(df)):
        close      = float(df["Close"].iloc[i])
        close_prev = float(df["Close"].iloc[i - 1])
        high       = float(df["High"].iloc[i])
        low        = float(df["Low"].iloc[i])
        ts         = df.index[i]
        atr_v      = _get(atr, i, 0.001)

        if pos:
            pnl, pos = _manage_pos(pos, high, low, ts)
            if pnl is not None:
                trades.append({"pnl": pnl, "ts": ts})
            continue

        if bb is None or rsi is None or bbl_col is None or bbu_col is None:
            continue

        bbl      = _get(bb[bbl_col], i)
        bbl_prev = _get(bb[bbl_col], i - 1)
        bbu      = _get(bb[bbu_col], i)
        bbu_prev = _get(bb[bbu_col], i - 1)
        rv       = _get(rsi, i)
        bbm      = _get(bb[bbl_col.replace("BBL", "BBM")], i) if bbl_col else 0

        if bbl == 0 or bbu == 0 or rv == 0:
            continue

        # BUY: 前足がBBL外（下）→ 今足がBBL内に戻る + RSI < rsi_buy
        crossback_up = (close_prev < bbl_prev) and (close >= bbl)
        if crossback_up and rv < rsi_buy:
            sl = round(close - atr_v * sl_mult, 3)
            tp = round(bbm, 3) if bbm > close else round(close + atr_v * tp_mult, 3)
            if abs(close - sl) * 100 <= MAX_SL_PIPS:
                pos = {"dir": "BUY", "entry": close, "sl": sl, "tp": tp}

        # SELL: 前足がBBU外（上）→ 今足がBBU内に戻る + RSI > rsi_sell
        elif (close_prev > bbu_prev) and (close <= bbu) and rv > rsi_sell:
            sl = round(close + atr_v * sl_mult, 3)
            tp = round(bbm, 3) if bbm < close else round(close - atr_v * tp_mult, 3)
            if abs(close - sl) * 100 <= MAX_SL_PIPS:
                pos = {"dir": "SELL", "entry": close, "sl": sl, "tp": tp}
    return trades


# ─────────────────────────────────────────────────────────────
# ▌ 戦略 2: H&S 肩エントリー（パラメータ可変）
# ─────────────────────────────────────────────────────────────
def _detect_hs(df_window: pd.DataFrame, distance: int = 5, tol: float = 0.015):
    highs = df_window["High"].values
    lows  = df_window["Low"].values
    n     = len(highs)

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
            return {"type": "HS",  "rs_high": rs_high, "head": float(head),
                    "neckline": neckline, "rs_idx": rs_i}

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
            return {"type": "IHS", "rs_low": rs_low, "head": float(head),
                    "neckline": neckline, "rs_idx": rs_i}
    return None


def strat_hs_shoulder(df: pd.DataFrame,
                      distance: int   = 5,
                      tol: float      = 0.015,
                      window: int     = 100) -> list[dict]:
    sma200      = df["Close"].rolling(200).mean()
    atr         = _atr(df)
    trades      = []
    pos         = None
    last_rs_idx = -999

    for i in range(60, len(df)):
        close = float(df["Close"].iloc[i])
        high  = float(df["High"].iloc[i])
        low   = float(df["Low"].iloc[i])
        ts    = df.index[i]
        sma   = _get(sma200, i, close)
        atr_v = _get(atr,    i, 0.001)

        if pos:
            pnl, pos = _manage_pos(pos, high, low, ts)
            if pnl is not None:
                trades.append({"pnl": pnl, "ts": ts})
            continue

        window_start = max(0, i - window)
        w_df         = df.iloc[window_start: i + 1]
        hs           = _detect_hs(w_df, distance=distance, tol=tol)
        if hs is None:
            continue

        global_rs = window_start + hs["rs_idx"]
        if global_rs <= last_rs_idx:
            continue

        if hs["type"] == "HS" and close < sma:
            sl     = round(hs["rs_high"] + 0.05, 3)
            sl_pip = abs(close - sl) * 100
            if sl_pip > MAX_SL_PIPS:
                continue
            depth = hs["head"] - hs["neckline"]
            tp    = round(hs["neckline"] - depth, 3)
            if tp >= close:
                tp = round(close - sl_pip / 100 * 2, 3)
            pos          = {"dir": "SELL", "entry": close, "sl": sl, "tp": tp}
            last_rs_idx  = global_rs

        elif hs["type"] == "IHS" and close > sma:
            sl     = round(hs["rs_low"] - 0.05, 3)
            sl_pip = abs(close - sl) * 100
            if sl_pip > MAX_SL_PIPS:
                continue
            depth = hs["neckline"] - hs["head"]
            tp    = round(hs["neckline"] + depth, 3)
            if tp <= close:
                tp = round(close + sl_pip / 100 * 2, 3)
            pos         = {"dir": "BUY", "entry": close, "sl": sl, "tp": tp}
            last_rs_idx = global_rs

    return trades


# ─────────────────────────────────────────────────────────────
# ▌ 詳細分析
# ─────────────────────────────────────────────────────────────
def analyze_trades(trades: list[dict], label: str):
    if not trades:
        print(f"\n{label}: トレードなし")
        return

    pnls = pd.Series([t["pnl"] for t in trades])
    tss  = [t["ts"] for t in trades]

    total      = len(pnls)
    wins       = (pnls > 0).sum()
    losses     = (pnls < 0).sum()
    win_rate   = wins / total * 100
    total_pips = pnls.sum()
    gross_p    = pnls[pnls > 0].sum()
    gross_l    = abs(pnls[pnls < 0].sum())
    pf         = gross_p / gross_l if gross_l > 0 else float("inf")
    avg_win    = pnls[pnls > 0].mean() if wins > 0 else 0
    avg_loss   = pnls[pnls < 0].mean() if losses > 0 else 0
    rr         = abs(avg_win / avg_loss) if avg_loss != 0 else 0

    cum   = pnls.cumsum()
    peak  = cum.cummax()
    max_dd = (cum - peak).min()

    w = 60
    print(f"\n{'='*w}")
    print(f"  📊 {label} — 詳細検証レポート")
    print(f"{'='*w}")
    print(f"  取引数     : {total}  （勝{wins} / 負{losses}）")
    print(f"  勝率       : {win_rate:.1f}%")
    print(f"  総損益     : {total_pips:+.1f} pips")
    print(f"  プロフィットファクター: {pf:.2f}")
    print(f"  平均利益   : +{avg_win:.1f} pips")
    print(f"  平均損失   : {avg_loss:.1f} pips")
    print(f"  実質RR比   : 1 : {rr:.2f}")
    print(f"  最大ドローダウン: {max_dd:.1f} pips")

    # ── 月次内訳 ──────────────────────────────────────────────
    df_t = pd.DataFrame({"pnl": pnls.values, "ts": tss})
    df_t["ts"] = pd.to_datetime(df_t["ts"], utc=True)
    df_t["ym"] = df_t["ts"].dt.to_period("M")

    monthly = df_t.groupby("ym")["pnl"].agg(
        trades="count", wins=lambda x: (x > 0).sum(), total=np.sum
    )

    print(f"\n  {'月次内訳':─<48}")
    print(f"  {'年月':<10}  {'取引':>4}  {'勝率%':>6}  {'損益 pips':>10}")
    print(f"  {'─'*42}")
    for ym, row in monthly.iterrows():
        wr = row["wins"] / row["trades"] * 100 if row["trades"] > 0 else 0
        bar = "▓" * int(abs(row["total"]) // 20) if row["total"] != 0 else ""
        sign = "+" if row["total"] >= 0 else ""
        print(f"  {str(ym):<10}  {int(row['trades']):>4}  {wr:>5.0f}%  {sign}{row['total']:>8.1f}  {bar}")

    # ── 年次サマリー ───────────────────────────────────────────
    df_t["year"] = df_t["ts"].dt.year
    yearly = df_t.groupby("year")["pnl"].agg(
        trades="count", wins=lambda x: (x > 0).sum(), total=np.sum
    )
    print(f"\n  {'年次サマリー':─<48}")
    for yr, row in yearly.iterrows():
        wr = row["wins"] / row["trades"] * 100 if row["trades"] > 0 else 0
        sign = "+" if row["total"] >= 0 else ""
        print(f"  {yr}年  取引{int(row['trades'])}回  勝率{wr:.0f}%  {sign}{row['total']:.1f} pips")

    # ── 連勝・連敗 ─────────────────────────────────────────────
    max_streak_w = max_streak_l = cur_w = cur_l = 0
    for p in pnls:
        if p > 0:
            cur_w += 1
            cur_l  = 0
            max_streak_w = max(max_streak_w, cur_w)
        else:
            cur_l += 1
            cur_w  = 0
            max_streak_l = max(max_streak_l, cur_l)

    print(f"\n  最大連勝: {max_streak_w}連勝  ／  最大連敗: {max_streak_l}連敗")
    print(f"{'='*w}")


# ─────────────────────────────────────────────────────────────
# ▌ パラメータ感度テスト
# ─────────────────────────────────────────────────────────────
def sensitivity_hs(df: pd.DataFrame):
    """H&S: distance と tol のグリッドサーチ"""
    print("\n\n🔬 H&S パラメータ感度テスト")
    print(f"{'distance':>10}  {'tol':>6}  {'取引数':>6}  {'勝率%':>6}  {'総pips':>9}  {'PF':>6}")
    print("─" * 52)
    best_pips = -9999
    best_params = {}
    for dist in [3, 5, 7, 10]:
        for tol in [0.01, 0.015, 0.02, 0.03]:
            t = strat_hs_shoulder(df, distance=dist, tol=tol)
            if not t:
                print(f"{dist:>10}  {tol:>6.3f}  {'0':>6}  {'—':>6}  {'—':>9}  {'—':>6}")
                continue
            p = pd.Series([x["pnl"] for x in t])
            n  = len(p)
            wr = (p > 0).sum() / n * 100
            tp = p.sum()
            gl = abs(p[p < 0].sum())
            pf = p[p > 0].sum() / gl if gl > 0 else float("inf")
            pf_s = f"{pf:.2f}" if pf != float("inf") else " inf"
            mark = " ◀ 最良" if tp > best_pips else ""
            if tp > best_pips:
                best_pips   = tp
                best_params = {"distance": dist, "tol": tol}
            print(f"{dist:>10}  {tol:>6.3f}  {n:>6}  {wr:>5.1f}%  {tp:>+9.1f}  {pf_s:>6}{mark}")
    print(f"\n  → 最良パラメータ: distance={best_params.get('distance')}  tol={best_params.get('tol')}")


def sensitivity_bb(df: pd.DataFrame):
    """BB+RSI クロスバック: bb_std と rsi_buy のグリッドサーチ"""
    print("\n\n🔬 ボリバン+RSI（クロスバック版）パラメータ感度テスト")
    print(f"{'bb_std':>7}  {'rsi_buy':>8}  {'取引数':>6}  {'勝率%':>6}  {'総pips':>9}  {'PF':>6}")
    print("─" * 55)
    best_pips = -9999
    best_params = {}
    for std in [1.5, 2.0, 2.5]:
        for rsi_buy in [45, 50, 55, 60]:
            t = strat_bb_rsi(df, bb_std=std, rsi_buy=rsi_buy, rsi_sell=100 - rsi_buy)
            if not t:
                print(f"{std:>7.1f}  {rsi_buy:>8}  {'0':>6}  {'—':>6}  {'—':>9}  {'—':>6}")
                continue
            p = pd.Series([x["pnl"] for x in t])
            n  = len(p)
            wr = (p > 0).sum() / n * 100
            tp = p.sum()
            gl = abs(p[p < 0].sum())
            pf = p[p > 0].sum() / gl if gl > 0 else float("inf")
            pf_s = f"{pf:.2f}" if pf != float("inf") else " inf"
            mark = " ◀ 最良" if tp > best_pips else ""
            if tp > best_pips:
                best_pips   = tp
                best_params = {"bb_std": std, "rsi_buy": rsi_buy}
            print(f"{std:>7.1f}  {rsi_buy:>8}  {n:>6}  {wr:>5.1f}%  {tp:>+9.1f}  {pf_s:>6}{mark}")
    print(f"\n  → 最良パラメータ: bb_std={best_params.get('bb_std')}  rsi_buy={best_params.get('rsi_buy')}")


# ─────────────────────────────────────────────────────────────
# ▌ グラフ保存（詳細版）
# ─────────────────────────────────────────────────────────────
def save_chart(results: dict[str, list[dict]], tf: str):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(2, 1, figsize=(14, 10), sharex=False)
        colors = {"H&S肩エントリー": "#e74c3c", "ボリバン逆張り+RSI": "#3498db"}

        for ax, (label, trades) in zip(axes, results.items()):
            if not trades:
                continue
            pnls = pd.Series([t["pnl"] for t in trades])
            cum  = pnls.cumsum()
            ax.plot(range(len(cum)), cum.values, color=colors.get(label, "gray"),
                    linewidth=2, label=label)
            ax.fill_between(range(len(cum)), cum.values, 0,
                            where=(cum.values >= 0), alpha=0.15, color="green")
            ax.fill_between(range(len(cum)), cum.values, 0,
                            where=(cum.values < 0), alpha=0.15, color="red")
            ax.axhline(0, color="black", linewidth=0.8, linestyle="--")
            ax.set_title(f"{label}  (総損益: {pnls.sum():+.1f} pips, 勝率: {(pnls>0).sum()/len(pnls)*100:.1f}%)",
                         fontsize=11)
            ax.set_ylabel("累積 pips")
            ax.legend()
            ax.grid(True, alpha=0.3)

        axes[-1].set_xlabel("取引回数")
        suffix = {"1h": "_1h", "4h": "_4h", "1d_5y": "_1d"}.get(tf, "")
        fname  = ROOT / f"verify_top2{suffix}.png"
        plt.suptitle(f"FX Trade Luce — H&S & ボリバン詳細検証 ({tf})", fontsize=13)
        plt.tight_layout()
        plt.savefig(fname, dpi=130)
        plt.close()
        print(f"\n📊 グラフ保存完了: {fname.name}")
    except Exception as e:
        print(f"  (グラフ保存スキップ: {e})")


# ─────────────────────────────────────────────────────────────
# ▌ メイン
# ─────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="H&S & ボリバン詳細検証")
    parser.add_argument("--tf",   default="4h", help="1h / 4h / 1d")
    parser.add_argument("--sens", action="store_true",
                        help="パラメータ感度テストも実行する（時間がかかる）")
    args = parser.parse_args()

    tf_arg      = args.tf if args.tf in ("1h", "4h", "1d") else "4h"
    tf_internal = "1d_5y" if tf_arg == "1d" else tf_arg
    df, tf_label = fetch_data(tf_internal)

    print(f"\n{'='*60}")
    print(f"  USD/JPY  2戦略 詳細検証 [{tf_label}]")
    print(f"{'='*60}")

    # ── メイン実行 ─────────────────────────────────────────────
    hs_trades  = strat_hs_shoulder(df, distance=5, tol=0.020)
    bb_trades  = strat_bb_rsi(df)

    analyze_trades(hs_trades,  "H&S肩エントリー")
    analyze_trades(bb_trades,  "ボリバン逆張り+RSI")

    # ── グラフ ──────────────────────────────────────────────────
    save_chart({"H&S肩エントリー": hs_trades, "ボリバン逆張り+RSI": bb_trades}, tf_internal)

    # ── パラメータ感度（--sens オプション時のみ） ──────────────
    if args.sens:
        sensitivity_hs(df)
        sensitivity_bb(df)

    print("\n✅ 検証完了！\n")


if __name__ == "__main__":
    main()
