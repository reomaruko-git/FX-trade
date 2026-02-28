"""
optimize.py — FX Trade Luce 自動パラメータ最適化
==================================================
一度実行するだけで 50通り以上のパラメータを自動検証し、
最良の設定を auto_trader.py に自動で書き込みます。

使い方:
    python3 optimize.py

所要時間: 約 3〜5分
"""
from __future__ import annotations

import json
import warnings
from datetime import datetime, timezone, timedelta
from itertools import product
from pathlib import Path

import contextlib, io
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

ROOT        = Path(__file__).parent
RESULTS_DIR = ROOT / "backtest_results"
LOT            = 10000
INITIAL_BALANCE = 500000
MAX_SL_PIPS    = 50
SMA_PERIOD     = 200


# ─────────────────────────────────────────────────────────────
# データ取得
# ─────────────────────────────────────────────────────────────
def fetch_data(ticker: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    import yfinance as yf
    end   = datetime.now(tz=timezone.utc)
    start = end - timedelta(days=728)
    print(f"  {ticker} データ取得中 ({start.date()} 〜 {end.date()})...")
    raw = yf.download(ticker, start=start, end=end,
                      interval="1h", progress=False, auto_adjust=True)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df_1h = raw[["Open","High","Low","Close","Volume"]].dropna()
    df_4h = raw.resample("4h").agg(
        {"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}
    ).dropna()
    print(f"  → 1h: {len(df_1h)}本  4h: {len(df_4h)}本")
    return df_1h, df_4h


# ─────────────────────────────────────────────────────────────
# テクニカル指標（純粋 pandas/numpy — pandas_ta 不要）
# ─────────────────────────────────────────────────────────────
def ema(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(span=period, adjust=False).mean()


def atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl = df["High"] - df["Low"]
    hc = (df["High"] - df["Close"].shift(1)).abs()
    lc = (df["Low"]  - df["Close"].shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def sma(s: pd.Series, period: int) -> pd.Series:
    return s.rolling(period).mean()


def find_peaks(arr: np.ndarray, distance: int = 5) -> np.ndarray:
    peaks = []
    for i in range(distance, len(arr) - distance):
        if arr[i] == arr[i-distance:i+distance+1].max():
            if arr[i] > arr[i-1] and arr[i] > arr[i+1]:
                if not peaks or i - peaks[-1] >= distance:
                    peaks.append(i)
    return np.array(peaks, dtype=int)


def find_troughs(arr: np.ndarray, distance: int = 5) -> np.ndarray:
    return find_peaks(-arr, distance)


# ─────────────────────────────────────────────────────────────
# H&S パターン検知
# ─────────────────────────────────────────────────────────────
def detect_hs_at(df: pd.DataFrame, i: int, distance: int = 5, tol: float = 0.015):
    window = df.iloc[max(0, i - distance * 8): i + 1]
    if len(window) < distance * 6:
        return None
    highs = window["High"].values
    lows  = window["Low"].values

    pk = find_peaks(highs, distance)
    if len(pk) >= 3:
        li, hi, ri = int(pk[-3]), int(pk[-2]), int(pk[-1])
        if ri < len(highs) - distance * 3:
            return None
        ls, hd, rs = highs[li], highs[hi], highs[ri]
        if abs(ls - rs) / (hd + 1e-9) < tol and hd > max(ls, rs) * (1 + tol * 0.5):
            n1 = lows[li:hi].min() if hi > li else lows[li]
            n2 = lows[hi:ri].min() if ri > hi else lows[hi]
            buf = max(1, distance // 2)
            rs_low = lows[max(0, ri-buf): ri+buf+1].min()
            return {"pattern": "H&S",
                    "rs_high": float(rs), "rs_low": float(rs_low),
                    "neck": round((n1+n2)/2, 3)}

    tr = find_troughs(lows, distance)
    if len(tr) >= 3:
        li, hi, ri = int(tr[-3]), int(tr[-2]), int(tr[-1])
        if ri < len(lows) - distance * 3:
            return None
        ls, hd, rs = lows[li], lows[hi], lows[ri]
        if abs(ls - rs) / (abs(hd) + 1e-9) < tol and hd < min(ls, rs) * (1 - tol * 0.5):
            n1 = highs[li:hi].max() if hi > li else highs[li]
            n2 = highs[hi:ri].max() if ri > hi else highs[hi]
            buf = max(1, distance // 2)
            rs_high = highs[max(0, ri-buf): ri+buf+1].max()
            return {"pattern": "INV_H&S",
                    "rs_high": float(rs_high), "rs_low": float(rs),
                    "neck": round((n1+n2)/2, 3)}
    return None


# ─────────────────────────────────────────────────────────────
# バックテストコア
# ─────────────────────────────────────────────────────────────
def _pips(d): return round(d * 100, 1)


def backtest_ema(df: pd.DataFrame, pair: str,
                 ef: int, es: int, slm: float, tpm: float, bev: int) -> pd.DataFrame:
    ef_s  = ema(df["Close"], ef)
    es_s  = ema(df["Close"], es)
    atr_s = atr_series(df)
    sma_s = sma(df["Close"], SMA_PERIOD)
    trades, pos = [], None
    start = SMA_PERIOD + es + 5
    for i in range(start, len(df)):
        c  = float(df["Close"].iloc[i])
        h  = float(df["High"].iloc[i])
        lo = float(df["Low"].iloc[i])
        ts = df.index[i]
        at = float(atr_s.iloc[i]) if not pd.isna(atr_s.iloc[i]) else 0.001
        sm = float(sma_s.iloc[i]) if not pd.isna(sma_s.iloc[i]) else c
        fn = float(ef_s.iloc[i])   if not pd.isna(ef_s.iloc[i])  else c
        sn = float(es_s.iloc[i])   if not pd.isna(es_s.iloc[i])  else c
        fp = float(ef_s.iloc[i-1]) if not pd.isna(ef_s.iloc[i-1]) else c
        sp = float(es_s.iloc[i-1]) if not pd.isna(es_s.iloc[i-1]) else c

        if pos:
            d = pos["d"]
            pnl_now = _pips(c - pos["e"]) if d == "B" else _pips(pos["e"] - c)
            if not pos["be"] and pnl_now >= bev:
                pos["sl"] = pos["e"]
                pos["be"] = True
            tp_hit = (d=="B" and h >= pos["tp"]) or (d=="S" and lo <= pos["tp"])
            sl_hit = (d=="B" and lo <= pos["sl"]) or (d=="S" and h  >= pos["sl"])
            if tp_hit or sl_hit:
                ex  = pos["tp"] if tp_hit else pos["sl"]
                pnl = _pips(ex - pos["e"]) if d == "B" else _pips(pos["e"] - ex)
                trades.append({"pair": pair, "strategy": f"EMA{ef}/{es}",
                                "direction": d, "entry_time": pos["ts"], "exit_time": ts,
                                "pnl_pips": pnl, "pnl_jpy": int(pnl * LOT / 100),
                                "result": "W" if pnl > 0 else "L",
                                "exit_reason": "TP" if tp_hit else "SL"})
                pos = None
            continue

        gc = (fp <= sp) and (fn > sn)
        dc = (fp >= sp) and (fn < sn)
        if gc and c > sm:
            sl = round(c - at * slm, 3)
            tp = round(c + at * tpm, 3)
            if abs(c - sl) * 100 <= MAX_SL_PIPS:
                pos = {"d":"B","e":c,"sl":sl,"tp":tp,"be":False,"ts":ts}
        elif dc and c < sm:
            sl = round(c + at * slm, 3)
            tp = round(c - at * tpm, 3)
            if abs(c - sl) * 100 <= MAX_SL_PIPS:
                pos = {"d":"S","e":c,"sl":sl,"tp":tp,"be":False,"ts":ts}
    return pd.DataFrame(trades)


def backtest_hs(df: pd.DataFrame, pair: str, rr: float, bev: int) -> pd.DataFrame:
    sma_s = sma(df["Close"], SMA_PERIOD)
    trades, pos = [], None
    for i in range(SMA_PERIOD + 30, len(df)):
        c  = float(df["Close"].iloc[i])
        h  = float(df["High"].iloc[i])
        lo = float(df["Low"].iloc[i])
        ts = df.index[i]
        sm = float(sma_s.iloc[i]) if not pd.isna(sma_s.iloc[i]) else c

        if pos:
            d = pos["d"]
            pnl_now = _pips(c - pos["e"]) if d == "B" else _pips(pos["e"] - c)
            if not pos["be"] and pnl_now >= bev:
                pos["sl"] = pos["e"]
                pos["be"] = True
            tp_hit = (d=="B" and h >= pos["tp"]) or (d=="S" and lo <= pos["tp"])
            sl_hit = (d=="B" and lo <= pos["sl"]) or (d=="S" and h  >= pos["sl"])
            if tp_hit or sl_hit:
                ex  = pos["tp"] if tp_hit else pos["sl"]
                pnl = _pips(ex - pos["e"]) if d == "B" else _pips(pos["e"] - ex)
                trades.append({"pair": pair, "strategy": "H&S",
                                "direction": d, "entry_time": pos["ts"], "exit_time": ts,
                                "pnl_pips": pnl, "pnl_jpy": int(pnl * LOT / 100),
                                "result": "W" if pnl > 0 else "L",
                                "exit_reason": "TP" if tp_hit else "SL"})
                pos = None
            continue

        hs = detect_hs_at(df, i)
        if not hs:
            continue
        if hs["pattern"] == "H&S" and c < sm:
            sl = round(hs["rs_high"] + 0.05, 3)
            if abs(sl - c) * 100 > MAX_SL_PIPS:
                continue
            tp = round(c - (sl - c) * rr, 3)
            pos = {"d":"S","e":c,"sl":sl,"tp":tp,"be":False,"ts":ts}
        elif hs["pattern"] == "INV_H&S" and c > sm:
            sl = round(hs["rs_low"] - 0.05, 3)
            if abs(c - sl) * 100 > MAX_SL_PIPS:
                continue
            tp = round(c + (c - sl) * rr, 3)
            pos = {"d":"B","e":c,"sl":sl,"tp":tp,"be":False,"ts":ts}
    return pd.DataFrame(trades)


# ─────────────────────────────────────────────────────────────
# スコア計算
# ─────────────────────────────────────────────────────────────
def calc_score(df: pd.DataFrame) -> dict:
    if df.empty or len(df) < 3:
        return {"trades":0,"win_rate":0,"total_pips":0,"total_jpy":0,
                "final_balance":INITIAL_BALANCE,"max_dd_pct":0,"score":-9999}
    total = len(df)
    wins  = (df["result"] == "W").sum()
    tp    = round(df["pnl_pips"].sum(), 1)
    tj    = int(df["pnl_jpy"].sum())
    bal   = INITIAL_BALANCE + df["pnl_jpy"].cumsum()
    pk    = bal.cummax()
    dd    = (bal - pk)
    mdd   = round(dd.min() / pk.iloc[dd.idxmin()] * 100, 1) if not dd.empty else 0.0
    wr    = wins / total
    # スコア = pips合計 × 勝率 × sqrt(取引数) / (|DD%| + 1)
    sc    = tp * wr * np.sqrt(total) / (abs(mdd) + 1)
    return {"trades":total,"win_rate":round(wr*100,1),"total_pips":tp,
            "total_jpy":tj,"final_balance":int(INITIAL_BALANCE+tj),
            "max_dd_pct":mdd,"score":round(sc,2)}


# ─────────────────────────────────────────────────────────────
# グリッドサーチ
# ─────────────────────────────────────────────────────────────
EMA_FAST_LIST  = [5, 9, 12, 21]
EMA_SLOW_LIST  = [21, 34, 55]
SL_MULT_LIST   = [1.0, 1.5, 2.0]
TP_MULT_LIST   = [2.0, 3.0, 4.0]
BREAKEVEN_LIST = [10, 15, 20]
HS_RR_LIST     = [1.5, 2.0, 2.5]

PAIRS = {"USDJPY=X": "USD/JPY", "GBPJPY=X": "GBP/JPY"}


def main():
    print("=" * 60)
    print("  FX Trade Luce 自動最適化バックテスト")
    print("  約 3〜5分かかります。お待ちください...")
    print("=" * 60)

    # ── データ取得 ───────────────────────────────────────────
    datasets = {}
    for ticker, pair_name in PAIRS.items():
        try:
            df_1h, df_4h = fetch_data(ticker)
            datasets[pair_name] = (df_1h, df_4h)
        except Exception as e:
            print(f"  ❌ {pair_name}: {e}")

    if not datasets:
        print("データが取得できませんでした。")
        return

    RESULTS_DIR.mkdir(exist_ok=True)
    all_best = {}

    for pair_name, (df_1h, df_4h) in datasets.items():
        print(f"\n{'═'*60}")
        print(f"  [{pair_name}] パラメータ網羅探索")

        # EMA グリッドサーチ
        ema_combos = [
            (ef, es, slm, tpm, bev)
            for ef, es, slm, tpm, bev
            in product(EMA_FAST_LIST, EMA_SLOW_LIST,
                       SL_MULT_LIST, TP_MULT_LIST, BREAKEVEN_LIST)
            if ef < es and tpm > slm
        ]
        print(f"  EMAクロス: {len(ema_combos)}通りを検証中...")

        ema_rows = []
        for n, (ef, es, slm, tpm, bev) in enumerate(ema_combos, 1):
            df_t = backtest_ema(df_1h, pair_name, ef, es, slm, tpm, bev)
            s = calc_score(df_t)
            s.update({"ema_fast":ef,"ema_slow":es,"sl_mult":slm,
                       "tp_mult":tpm,"breakeven":bev})
            ema_rows.append(s)
            if n % 30 == 0:
                print(f"    {n}/{len(ema_combos)} 完了...")

        ema_rows.sort(key=lambda x: x["score"], reverse=True)
        best_ema = ema_rows[0]

        # H&S（USD/JPYのみ）
        best_hs = None
        if pair_name == "USD/JPY":
            hs_combos = list(product(HS_RR_LIST, BREAKEVEN_LIST))
            print(f"  H&S: {len(hs_combos)}通りを検証中...")
            hs_rows = []
            for rr, bev in hs_combos:
                df_t = backtest_hs(df_4h, pair_name, rr, bev)
                s = calc_score(df_t)
                s.update({"hs_rr": rr, "breakeven": bev})
                hs_rows.append(s)
            hs_rows.sort(key=lambda x: x["score"], reverse=True)
            best_hs = hs_rows[0]

        all_best[pair_name] = {"ema": best_ema, "hs": best_hs, "top5": ema_rows[:5]}

        # 表示
        print(f"\n  ┌─ {pair_name} EMA最良 {'─'*35}")
        be = best_ema
        print(f"  │ EMA{be['ema_fast']}/{be['ema_slow']}  SL×{be['sl_mult']}  TP×{be['tp_mult']}  建値{be['breakeven']}pips")
        print(f"  │ {be['trades']}回  勝率{be['win_rate']}%  {be['total_pips']:+.1f}pips"
              f"  ({be['total_jpy']:+,}円)  DD:{be['max_dd_pct']}%  最終:{be['final_balance']:,}円")
        if best_hs:
            bh = best_hs
            print(f"  ├─ {pair_name} H&S最良")
            print(f"  │ RR×{bh['hs_rr']}  建値{bh['breakeven']}pips")
            print(f"  │ {bh['trades']}回  勝率{bh['win_rate']}%  {bh['total_pips']:+.1f}pips"
                  f"  ({bh['total_jpy']:+,}円)  DD:{bh['max_dd_pct']}%  最終:{bh['final_balance']:,}円")
        print(f"  └─ EMAトップ5:")
        for i, r in enumerate(ema_rows[:5], 1):
            mark = "★" if i == 1 else f" {i}"
            print(f"     {mark}. EMA{r['ema_fast']}/{r['ema_slow']} SL×{r['sl_mult']} TP×{r['tp_mult']}"
                  f" | {r['trades']}回 {r['win_rate']}% {r['total_pips']:+.1f}pips"
                  f" 最終{r['final_balance']:,}円")

    # ── 自動反映 ────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  ✅ 最適パラメータを auto_trader.py に自動反映します")
    print("=" * 60)
    _apply_best_params(all_best)

    # ── JSON保存 ─────────────────────────────────────────────
    ts_str = datetime.now().strftime("%Y%m%d_%H%M")
    out = RESULTS_DIR / f"optimize_{ts_str}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(all_best, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n  💾 詳細結果: backtest_results/optimize_{ts_str}.json")
    print("\n✅ 最適化完了！ auto_trader.py が更新されました。")


def _apply_best_params(all_best: dict):
    """最良のパラメータを auto_trader.py に書き込む"""
    # USD/JPY を優先。なければ GBP/JPY を使用
    best = all_best.get("USD/JPY", {}).get("ema") or \
           all_best.get("GBP/JPY", {}).get("ema")
    if not best:
        print("  ⚠️  反映対象なし")
        return

    ef  = best["ema_fast"]
    es  = best["ema_slow"]
    slm = best["sl_mult"]
    tpm = best["tp_mult"]
    bev = best["breakeven"]

    auto_path = ROOT / "auto_trader.py"
    text = auto_path.read_text(encoding="utf-8")

    import re
    text = re.sub(r"EMA_FAST\s*=\s*\d+",   f"EMA_FAST   = {ef}",  text)
    text = re.sub(r"EMA_SLOW\s*=\s*\d+",   f"EMA_SLOW   = {es}",  text)
    text = re.sub(r"SL_MULT\s*=\s*[\d.]+", f"SL_MULT        = {slm}", text)
    text = re.sub(r"TP_MULT\s*=\s*[\d.]+", f"TP_MULT        = {tpm}", text)
    text = re.sub(r"BREAKEVEN_PIPS\s*=\s*\d+", f"BREAKEVEN_PIPS = {bev}", text)
    auto_path.write_text(text, encoding="utf-8")

    print(f"  EMA_FAST={ef}  EMA_SLOW={es}  SL_MULT={slm}  TP_MULT={tpm}  BREAKEVEN={bev}pips")
    print(f"  → USD/JPY EMA: {best['trades']}回  勝率{best['win_rate']}%  "
          f"{best['total_pips']:+.1f}pips  最終残高{best['final_balance']:,}円")


if __name__ == "__main__":
    main()
