#!/usr/bin/env python3
"""
run_backtest.py — FX Trade Luce 5戦略比較バックテスト
=====================================================
5つの戦略を同一データで比較し、最適戦略を特定する。

使い方:
    python3 run_backtest.py           # 日足 2年
    python3 run_backtest.py --tf 1h   # 1時間足 2年
    python3 run_backtest.py --tf 4h   # 4時間足 2年
    python3 run_backtest.py --tf 1d   # 日足 5年
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
        label    = ""

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
# ▌ ポジション管理ヘルパー（全戦略共通）
# ─────────────────────────────────────────────────────────────
def _manage_pos(pos, high, low, ts):
    """TP/SL 判定。決済したら (pnl, "closed") を返す。継続なら (None, pos) を返す。"""
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
# ▌ 戦略 1: ボリバン逆張り + RSI
# ─────────────────────────────────────────────────────────────
def strat_bb_rsi(df: pd.DataFrame) -> list[dict]:
    """
    BB クロスバック逆張り + RSI確認
    ──────────────────────────────────────────────
    【旧】価格がBBの外かつRSI極値 → BBが無意味（RSI単体と同じ）
    【新】価格がBBの外→中に戻った瞬間（クロスバック）+ RSI < 50 / > 50 で確認
          BBバンドを本来の使い方（平均回帰）で活かす
    """
    bb  = _q(df.ta.bbands, length=20, std=2.0, append=False)
    rsi = _q(df.ta.rsi,    length=14, append=False)
    atr = _atr(df)

    # ── BB列名を動的に検出（pandas_ta バージョン差吸収） ──
    bbl_col = bbu_col = None
    if bb is not None:
        for col in bb.columns:
            if col.startswith("BBL"):
                bbl_col = col
            if col.startswith("BBU"):
                bbu_col = col

    trades, pos = [], None
    for i in range(30, len(df)):
        close  = float(df["Close"].iloc[i])
        close_prev = float(df["Close"].iloc[i - 1])
        high   = float(df["High"].iloc[i])
        low    = float(df["Low"].iloc[i])
        ts     = df.index[i]
        atr_v  = _get(atr, i, 0.001)

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

        # ── BUY: 前足が BBL の外（下）→ 今足が BBL の内側に戻る + RSI < 55（まだ安値圏）
        crossback_up = (close_prev < bbl_prev) and (close >= bbl)
        if crossback_up and rv < 55:
            sl = round(close - atr_v * 2, 3)
            tp = round(bbm, 3) if bbm > close else round(close + atr_v * 2, 3)
            if abs(close - sl) * 100 <= MAX_SL_PIPS:
                pos = {"dir": "BUY", "entry": close, "sl": sl, "tp": tp}

        # ── SELL: 前足が BBU の外（上）→ 今足が BBU の内側に戻る + RSI > 45（まだ高値圏）
        elif (close_prev > bbu_prev) and (close <= bbu) and rv > 45:
            sl = round(close + atr_v * 2, 3)
            tp = round(bbm, 3) if bbm < close else round(close - atr_v * 2, 3)
            if abs(close - sl) * 100 <= MAX_SL_PIPS:
                pos = {"dir": "SELL", "entry": close, "sl": sl, "tp": tp}
    return trades


# ─────────────────────────────────────────────────────────────
# ▌ 戦略 2: ゴールデンクロス（EMA9 / EMA21 + SMA200フィルター）
# ─────────────────────────────────────────────────────────────
def strat_golden_cross(df: pd.DataFrame) -> list[dict]:
    ema9   = _q(df.ta.ema, length=9,   append=False)
    ema21  = _q(df.ta.ema, length=21,  append=False)
    sma200 = df["Close"].rolling(200).mean()
    atr    = _atr(df)

    trades, pos = [], None
    for i in range(205, len(df)):
        close = float(df["Close"].iloc[i])
        high  = float(df["High"].iloc[i])
        low   = float(df["Low"].iloc[i])
        ts    = df.index[i]
        e9    = _get(ema9,   i)
        e21   = _get(ema21,  i)
        e9p   = _get(ema9,   i - 1)
        e21p  = _get(ema21,  i - 1)
        s200  = _get(sma200, i, close)
        atr_v = _get(atr,    i, 0.001)

        if pos:
            pnl, pos = _manage_pos(pos, high, low, ts)
            if pnl is not None:
                trades.append({"pnl": pnl, "ts": ts})
            continue

        if e9 == 0 or e21 == 0:
            continue

        # ゴールデンクロス: EMA9がEMA21を上抜け かつ SMA200より上
        if e9p <= e21p and e9 > e21 and close > s200:
            sl = round(close - atr_v * 2, 3)
            tp = round(close + atr_v * 4, 3)
            if abs(close - sl) * 100 <= MAX_SL_PIPS:
                pos = {"dir": "BUY", "entry": close, "sl": sl, "tp": tp}
        # デッドクロス: EMA9がEMA21を下抜け かつ SMA200より下
        elif e9p >= e21p and e9 < e21 and close < s200:
            sl = round(close + atr_v * 2, 3)
            tp = round(close - atr_v * 4, 3)
            if abs(close - sl) * 100 <= MAX_SL_PIPS:
                pos = {"dir": "SELL", "entry": close, "sl": sl, "tp": tp}
    return trades


# ─────────────────────────────────────────────────────────────
# ▌ 戦略 3: ゴトー日戦略（5・10・15・20・25日、月末）
# ─────────────────────────────────────────────────────────────
def _is_goto_bi(ts: pd.Timestamp) -> bool:
    d = ts.day
    if d in (5, 10, 15, 20, 25):
        return True
    # 月末最終営業日
    next_day = ts + pd.tseries.offsets.BDay(1)
    return next_day.month != ts.month

def strat_goto_bi(df: pd.DataFrame) -> list[dict]:
    atr        = _atr(df)
    trades     = []
    last_date  = None   # ── バグ③修正: 同じ日付に複数バーで発火しないよう記録

    for i in range(15, len(df) - 1):
        ts   = df.index[i]
        date = pd.Timestamp(ts).date()

        # 同じ日付は1回だけ（4h足など複数バーがある日の重複防止）
        if date == last_date:
            continue
        if not _is_goto_bi(pd.Timestamp(ts)):
            continue

        entry = float(df["Open"].iloc[i])
        close = float(df["Close"].iloc[i])
        low   = float(df["Low"].iloc[i])
        atr_v = _get(atr, i, 0.001)
        sl    = entry - atr_v * 1.5

        pnl = _pips(sl - entry) if low <= sl else _pips(close - entry)
        trades.append({"pnl": pnl, "ts": ts})
        last_date = date   # この日はもうエントリー済み

    return trades


# ─────────────────────────────────────────────────────────────
# ▌ 戦略 4: H&S 肩エントリー
# ─────────────────────────────────────────────────────────────
def _detect_hs(df_window: pd.DataFrame, distance: int = 3, tol: float = 0.02):
    highs = df_window["High"].values
    lows  = df_window["Low"].values
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
            return {"type": "HS",  "rs_high": rs_high, "head": float(head),
                    "neckline": neckline, "rs_idx": rs_i}

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
            return {"type": "IHS", "rs_low": rs_low,   "head": float(head),
                    "neckline": neckline, "rs_idx": rs_i}
    return None


def strat_hs_shoulder(df: pd.DataFrame) -> list[dict]:
    """
    H&S 肩エントリー（最適化済み: distance=5, tol=0.020）
    TP : ネックライン倍返し（測定値ムーブ）
    SL : 右肩高値/安値 ± 0.05円バッファ、上限80pips
    """
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

        # 直近100本の窓でH&Sを検知（distance=5・tol=0.020 最適化済み）
        window_start = max(0, i - 100)
        window       = df.iloc[window_start: i + 1]
        hs           = _detect_hs(window, distance=5, tol=0.020)
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
# ▌ 戦略 5: MACD シグナルライン交差
# ─────────────────────────────────────────────────────────────
def strat_macd_divergence(df: pd.DataFrame) -> list[dict]:
    """MACDラインがシグナルラインを上抜け→BUY、下抜け→SELL"""
    macd_df = _q(df.ta.macd, fast=12, slow=26, signal=9, append=False)
    atr     = _atr(df)

    if macd_df is None:
        return []

    # 列名を動的に検出
    macd_col = sig_col = None
    for col in macd_df.columns:
        if col.startswith("MACD_") and not col.startswith("MACDs") and not col.startswith("MACDh"):
            macd_col = col
        if col.startswith("MACDs"):
            sig_col = col

    if macd_col is None or sig_col is None:
        return []

    trades, pos = [], None

    for i in range(30, len(df)):
        close = float(df["Close"].iloc[i])
        high  = float(df["High"].iloc[i])
        low   = float(df["Low"].iloc[i])
        ts    = df.index[i]
        atr_v = _get(atr, i, 0.001)

        if pos:
            pnl, pos = _manage_pos(pos, high, low, ts)
            if pnl is not None:
                trades.append({"pnl": pnl, "ts": ts})
            continue

        m_now  = _get(macd_df[macd_col], i)
        m_prev = _get(macd_df[macd_col], i - 1)
        s_now  = _get(macd_df[sig_col],  i)
        s_prev = _get(macd_df[sig_col],  i - 1)

        if m_now == 0 or s_now == 0:
            continue

        # MACDがシグナルを上抜け → BUY
        if m_prev <= s_prev and m_now > s_now:
            sl = round(close - atr_v * 2, 3)
            tp = round(close + atr_v * 4, 3)
            if abs(close - sl) * 100 <= MAX_SL_PIPS:
                pos = {"dir": "BUY", "entry": close, "sl": sl, "tp": tp}
        # MACDがシグナルを下抜け → SELL
        elif m_prev >= s_prev and m_now < s_now:
            sl = round(close + atr_v * 2, 3)
            tp = round(close - atr_v * 4, 3)
            if abs(close - sl) * 100 <= MAX_SL_PIPS:
                pos = {"dir": "SELL", "entry": close, "sl": sl, "tp": tp}
    return trades


# ─────────────────────────────────────────────────────────────
# ▌ サマリー計算
# ─────────────────────────────────────────────────────────────
def _summarize(trades: list[dict], label: str) -> dict:
    if not trades:
        return {"label": label, "trades": 0, "win_rate": 0.0,
                "total_pips": 0.0, "pf": 0.0, "max_dd": 0.0, "sharpe": 0.0}

    pnls     = pd.Series([t["pnl"] for t in trades])
    total    = len(pnls)
    wins     = (pnls > 0).sum()
    win_rate = round(wins / total * 100, 1)
    total_pips = round(pnls.sum(), 1)

    gross_p = pnls[pnls > 0].sum()
    gross_l = abs(pnls[pnls < 0].sum())
    pf      = round(gross_p / gross_l, 2) if gross_l > 0 else float("inf")

    # 最大ドローダウン
    cum   = pnls.cumsum()
    peak  = cum.cummax()
    max_dd = round((cum - peak).min(), 1)

    # シャープ比（取引ベース）
    if total > 1 and pnls.std() > 0:
        sharpe = round((pnls.mean() / pnls.std()) * (total ** 0.5), 2)
    else:
        sharpe = 0.0

    return {"label": label, "trades": total, "win_rate": win_rate,
            "total_pips": total_pips, "pf": pf, "max_dd": max_dd, "sharpe": sharpe}


# ─────────────────────────────────────────────────────────────
# ▌ 結果表示
# ─────────────────────────────────────────────────────────────
def print_results(summaries: list[dict]):
    ranked = sorted(summaries, key=lambda x: x["sharpe"], reverse=True)
    medals = ["🥇", "🥈", "🥉"]
    w      = 70

    print("\n" + "=" * w)
    print(f"{'戦略名':<28} {'取引数':>6}  {'勝率%':>6}  {'総pips':>9}  "
          f"{'PF':>6}  {'最大DD':>8}  {'シャープ':>8}")
    print("=" * w)

    for rank, s in enumerate(ranked):
        if s["trades"] == 0:
            continue
        medal  = medals[rank] if rank < 3 else "  "
        pf_str = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "  inf"
        print(f"{medal} {s['label']:<26} {s['trades']:>6}  {s['win_rate']:>5.1f}%  "
              f"{s['total_pips']:>9.1f}  {pf_str:>6}  {s['max_dd']:>8.1f}  "
              f"{s['sharpe']:>8.2f}")
    print("=" * w)


# ─────────────────────────────────────────────────────────────
# ▌ グラフ保存
# ─────────────────────────────────────────────────────────────
def save_chart(all_results: dict[str, list[dict]], tf: str):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(12, 6))
        for label, trades in all_results.items():
            if not trades:
                continue
            pnls = pd.Series([t["pnl"] for t in trades])
            ax.plot(range(len(pnls)), pnls.cumsum().values, label=label, linewidth=1.5)

        ax.axhline(0, color="black", linewidth=0.8, linestyle="--")
        ax.set_title(f"FX Trade Luce — 戦略別累積損益 ({tf})")
        ax.set_xlabel("取引回数")
        ax.set_ylabel("累積 pips")
        ax.legend(loc="upper left")
        ax.grid(True, alpha=0.3)

        suffix = {"1h": "_1h", "4h": "_4h", "1d_5y": "_1d"}.get(tf, "")
        fname  = Path(__file__).parent / f"backtest_result{suffix}.png"
        plt.tight_layout()
        plt.savefig(fname, dpi=120)
        plt.close()
        print(f"\n📊 グラフ保存完了: {fname.name}")
    except Exception as e:
        print(f"  (グラフ保存スキップ: {e})")


# ─────────────────────────────────────────────────────────────
# ▌ メイン
# ─────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="FX Trade Luce 5戦略比較")
    parser.add_argument("--tf", default="", help="1h / 4h / 1d")
    args = parser.parse_args()

    tf_arg      = args.tf if args.tf in ("1h", "4h", "1d") else ""
    tf_internal = "1d_5y" if tf_arg == "1d" else tf_arg
    df, tf_label = fetch_data(tf_internal)

    header = f"  USD/JPY バックテスト — 5戦略 競合比較"
    if tf_label:
        header += f" [{tf_label}]"
    print(f"\n{'='*55}\n{header}\n{'='*55}")

    strategy_funcs = [
        ("ボリバン逆張り+RSI",   strat_bb_rsi),
        ("ゴールデンクロス",     strat_golden_cross),
        ("ゴトー日戦略",         strat_goto_bi),
        ("H&S肩エントリー",      strat_hs_shoulder),
        ("MACDシグナル交差",      strat_macd_divergence),
    ]

    all_results = {}
    summaries   = []

    for name, func in strategy_funcs:
        print(f"⚙️  {name} を検証中...")
        trades = func(df)
        all_results[name] = trades
        summaries.append(_summarize(trades, name))

    print_results(summaries)
    save_chart(all_results, tf_internal)


if __name__ == "__main__":
    main()
