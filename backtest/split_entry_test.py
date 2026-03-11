"""
split_entry_test.py — 分割エントリー検証
==========================================
H&Sパターン検出後の以下2戦略をバックテストで比較する。

  【A】通常エントリー  : ブレイク確認後、即座に全量エントリー（現行）
  【B】分割エントリー  : 半量を即エントリー + 残り半量をネックラインへの
                         リテスト待ち指値（リテストなしの場合は指値キャンセル）

検証項目:
  1. リテスト発生率（ブレイク後に何%のケースでネックラインまで戻るか）
  2. 通常 vs 分割 の成績比較（pips, 勝率, 最大DD, pips/trade）
  3. 「リテストあり」「リテストなし」ケースそれぞれの分析
  4. USD/JPY と AUD/JPY で差があるか

使い方:
    python3 backtest/split_entry_test.py              # 両ペア
    python3 backtest/split_entry_test.py --pair USDJPY
    python3 backtest/split_entry_test.py --pair AUDJPY
    python3 backtest/split_entry_test.py --max-retest 10  # リテスト待ち上限バー数変更
"""

from __future__ import annotations

import argparse
import warnings
from datetime import datetime, timezone, timedelta
from pathlib import Path

import contextlib, io
import numpy as np
import pandas as pd


def find_peaks(x, distance=1):
    """scipy.signal.find_peaks の代替（numpy のみ）"""
    x = np.asarray(x, dtype=float)
    n = len(x)
    peaks = []
    for i in range(1, n - 1):
        if x[i] > x[i - 1] and x[i] > x[i + 1]:
            peaks.append(i)
    if not peaks:
        return np.array([], dtype=int), {}
    if distance <= 1:
        return np.array(peaks, dtype=int), {}
    peaks_arr = np.array(peaks, dtype=int)
    keep = np.ones(len(peaks_arr), dtype=bool)
    for i in range(len(peaks_arr)):
        if not keep[i]:
            continue
        for j in range(i + 1, len(peaks_arr)):
            if not keep[j]:
                continue
            if peaks_arr[j] - peaks_arr[i] < distance:
                if x[peaks_arr[i]] >= x[peaks_arr[j]]:
                    keep[j] = False
                else:
                    keep[i] = False
                    break
            else:
                break
    return peaks_arr[keep], {}

warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────────
# ▌ パラメータ（backtest.py と完全一致）
# ─────────────────────────────────────────────────────────────────
HS_BUFFER_PIPS  = 0.05
MAX_SL_PIPS     = 80
SMA_PERIOD      = 200
HS_DISTANCE     = 5
HS_TOL          = 0.020
LOT_FULL        = 20000   # 通常エントリーのロット
LOT_HALF        = 10000   # 分割エントリー片方のロット
INITIAL_BALANCE = 500_000
MAX_RETEST_BARS = 20      # リテスト待ちの上限バー数（デフォルト）

PAIRS = {
    "USDJPY=X": "USD/JPY",
    "AUDJPY=X": "AUD/JPY",
}


def _pips(diff: float) -> float:
    return round(diff * 100, 1)


def _quiet(func, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return func(*args, **kwargs)


# ─────────────────────────────────────────────────────────────────
# ▌ データ取得
# ─────────────────────────────────────────────────────────────────
def fetch_data(ticker: str) -> pd.DataFrame:
    import yfinance as yf
    end   = datetime.now(tz=timezone.utc)
    start = end - timedelta(days=728)
    raw = yf.download(ticker, start=start, end=end,
                      interval="1h", progress=False, auto_adjust=True)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df_4h = raw.resample("4h").agg(
        {"Open": "first", "High": "max", "Low": "min",
         "Close": "last", "Volume": "sum"}
    ).dropna()
    return df_4h


# ─────────────────────────────────────────────────────────────────
# ▌ H&S 検出（backtest.py と完全同一ロジック）
# ─────────────────────────────────────────────────────────────────
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
            if rs_i < n - distance * 4: continue
            if head <= max(ls, rs): continue
            if abs(ls - rs) / (head + 1e-9) > tol: continue
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
            if rs_i < n - distance * 4: continue
            if head >= min(ls, rs): continue
            if abs(ls - rs) / (abs(head) + 1e-9) > tol: continue
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


# ─────────────────────────────────────────────────────────────────
# ▌ 通常エントリー バックテスト（現行・比較ベース）
# ─────────────────────────────────────────────────────────────────
def backtest_normal(df: pd.DataFrame, pair_name: str) -> pd.DataFrame:
    """現行と同じブレイク即エントリー（全量）"""
    sma_s       = df["Close"].rolling(SMA_PERIOD).mean()
    trades      = []
    pos         = None
    last_rs_idx = -999

    for i in range(SMA_PERIOD + 30, len(df)):
        close  = float(df["Close"].iloc[i])
        high   = float(df["High"].iloc[i])
        low    = float(df["Low"].iloc[i])
        ts     = df.index[i]
        sma200 = float(sma_s.iloc[i]) if not pd.isna(sma_s.iloc[i]) else close

        if pos:
            direction = pos["direction"]
            tp_hit = (direction == "BUY"  and high >= pos["tp"]) or \
                     (direction == "SELL" and low  <= pos["tp"])
            sl_hit = (direction == "BUY"  and low  <= pos["sl"]) or \
                     (direction == "SELL" and high >= pos["sl"])
            if tp_hit or sl_hit:
                exit_price = pos["tp"] if tp_hit else pos["sl"]
                pnl = _pips(exit_price - pos["entry"]) if direction == "BUY" \
                      else _pips(pos["entry"] - exit_price)
                trades.append({
                    "pair": pair_name, "strategy": "通常",
                    "direction": direction,
                    "entry_time": pos["entry_time"], "exit_time": ts,
                    "entry": pos["entry"], "exit": exit_price,
                    "sl": pos["sl"], "tp": pos["tp"],
                    "pnl_pips": pnl,
                    "pnl_jpy": int(pnl * LOT_FULL / 100),
                    "result": "WIN" if pnl > 0 else "LOSS",
                    "exit_reason": "TP" if tp_hit else "SL",
                    "lot": LOT_FULL,
                })
                pos = None
            continue

        hs = detect_hs_at(df, i)
        if hs is None: continue
        global_rs = hs.get("rs_idx", -1)
        if global_rs <= last_rs_idx: continue

        pattern = hs["pattern"]
        if pattern == "HEAD_AND_SHOULDERS" and close < sma200:
            sl = round(hs["right_shoulder_high"] + HS_BUFFER_PIPS, 3)
            sl_pip = abs(close - sl) * 100
            if sl_pip > MAX_SL_PIPS or sl <= close: continue
            depth = hs["head"] - hs["neckline"]
            tp    = round(hs["neckline"] - depth, 3)
            if tp >= close: tp = round(close - sl_pip / 100 * 2, 3)
            pos = {"direction": "SELL", "entry": close, "sl": sl, "tp": tp,
                   "entry_time": ts}
            last_rs_idx = global_rs

        elif pattern == "INV_HEAD_AND_SHOULDERS" and close > sma200:
            sl = round(hs["right_shoulder_low"] - HS_BUFFER_PIPS, 3)
            sl_pip = abs(close - sl) * 100
            if sl_pip > MAX_SL_PIPS or sl >= close: continue
            depth = hs["neckline"] - hs["head"]
            tp    = round(hs["neckline"] + depth, 3)
            if tp <= close: tp = round(close + sl_pip / 100 * 2, 3)
            pos = {"direction": "BUY", "entry": close, "sl": sl, "tp": tp,
                   "entry_time": ts}
            last_rs_idx = global_rs

    return pd.DataFrame(trades)


# ─────────────────────────────────────────────────────────────────
# ▌ 分割エントリー バックテスト（新戦略）
# ─────────────────────────────────────────────────────────────────
def backtest_split(df: pd.DataFrame, pair_name: str,
                   max_retest_bars: int = MAX_RETEST_BARS) -> tuple[pd.DataFrame, dict]:
    """
    分割エントリー: 半量即エントリー + 半量リテスト待ち

    返り値:
      trades_df: 全トレードのDataFrame（PartA / PartB / Combo）
      stats:     リテスト発生率などの統計情報
    """
    sma_s       = df["Close"].rolling(SMA_PERIOD).mean()
    trades      = []
    last_rs_idx = -999

    # リテスト統計
    total_signals = 0
    retest_occurred = 0
    retest_pips_gain = []   # リテスト時の入値改善（pips）

    i = SMA_PERIOD + 30
    while i < len(df):
        close  = float(df["Close"].iloc[i])
        sma200 = float(sma_s.iloc[i]) if not pd.isna(sma_s.iloc[i]) else close

        hs = detect_hs_at(df, i)
        if hs is None:
            i += 1
            continue

        global_rs = hs.get("rs_idx", -1)
        if global_rs <= last_rs_idx:
            i += 1
            continue

        pattern  = hs["pattern"]
        neckline = hs["neckline"]

        # ── シグナル判定 ────────────────────────────────────────
        if pattern == "HEAD_AND_SHOULDERS" and close < sma200:
            sl = round(hs["right_shoulder_high"] + HS_BUFFER_PIPS, 3)
            sl_pip = abs(close - sl) * 100
            if sl_pip > MAX_SL_PIPS or sl <= close:
                i += 1; continue
            depth = hs["head"] - neckline
            tp    = round(neckline - depth, 3)
            if tp >= close: tp = round(close - sl_pip / 100 * 2, 3)
            direction = "SELL"

        elif pattern == "INV_HEAD_AND_SHOULDERS" and close > sma200:
            sl = round(hs["right_shoulder_low"] - HS_BUFFER_PIPS, 3)
            sl_pip = abs(close - sl) * 100
            if sl_pip > MAX_SL_PIPS or sl >= close:
                i += 1; continue
            depth = neckline - hs["head"]
            tp    = round(neckline + depth, 3)
            if tp <= close: tp = round(close + sl_pip / 100 * 2, 3)
            direction = "BUY"

        else:
            i += 1; continue

        total_signals += 1
        last_rs_idx = global_rs
        entry_time  = df.index[i]

        # ════════════════════════════════════════════════════════
        # Part A: 即時エントリー（半量 LOT_HALF）
        # ════════════════════════════════════════════════════════
        partA_entry = close
        partB_entry = None
        partB_fill_time = None
        retest_found = False

        # Part A の決済追跡 + Part B の指値監視
        partA_closed = False
        partA_pnl    = None
        partA_exit   = None
        partA_reason = None
        partA_exit_time = None

        # Part B のリテスト監視（i+1 から最大 max_retest_bars 本）
        scan_end = min(i + max_retest_bars + 1, len(df))

        for j in range(i + 1, scan_end):
            high_j  = float(df["High"].iloc[j])
            low_j   = float(df["Low"].iloc[j])
            close_j = float(df["Close"].iloc[j])
            ts_j    = df.index[j]

            # Part A まだ生きている場合 → SL/TP チェック
            if not partA_closed:
                tp_hit = (direction == "BUY"  and high_j  >= tp) or \
                         (direction == "SELL" and low_j   <= tp)
                sl_hit = (direction == "BUY"  and low_j   <= sl) or \
                         (direction == "SELL" and high_j  >= sl)
                if tp_hit or sl_hit:
                    partA_exit   = tp if tp_hit else sl
                    partA_pnl    = _pips(partA_exit - partA_entry) if direction == "BUY" \
                                   else _pips(partA_entry - partA_exit)
                    partA_reason = "TP" if tp_hit else "SL"
                    partA_closed = True
                    partA_exit_time = ts_j

                    # Part A が決済済み → Part B の指値もキャンセル（まだ未フィルなら）
                    if not retest_found:
                        break  # ループを抜けて統計に反映

            # Part B リテスト監視（まだ未フィルの場合のみ）
            if not retest_found:
                if direction == "BUY":
                    # 逆H&S: 上ブレイク後に安値がネックラインまで戻る
                    retest_touched = (low_j <= neckline)
                elif direction == "SELL":
                    # 天井H&S: 下ブレイク後に高値がネックラインまで戻る
                    retest_touched = (high_j >= neckline)
                else:
                    retest_touched = False

                if retest_touched:
                    retest_found    = True
                    retest_occurred += 1
                    partB_entry     = neckline  # 指値約定（ネックラインで）
                    partB_fill_time = ts_j

                    # 入値改善を記録
                    if direction == "BUY":
                        improvement = _pips(partA_entry - partB_entry)   # 安く買えた分
                    else:
                        improvement = _pips(partB_entry - partA_entry)   # 高く売れた分
                    retest_pips_gain.append(improvement)

            # Part B フィル済み → Combo で SL/TP 監視
            if retest_found and partB_entry is not None and not partA_closed:
                tp_hit = (direction == "BUY"  and high_j  >= tp) or \
                         (direction == "SELL" and low_j   <= tp)
                sl_hit = (direction == "BUY"  and low_j   <= sl) or \
                         (direction == "SELL" and high_j  >= sl)
                if tp_hit or sl_hit:
                    partA_exit   = tp if tp_hit else sl
                    partA_pnl    = _pips(partA_exit - partA_entry) if direction == "BUY" \
                                   else _pips(partA_entry - partA_exit)
                    partA_reason = "TP" if tp_hit else "SL"
                    partA_closed = True
                    partA_exit_time = ts_j
                    break

        # ── scan_end に達した（SL/TP未到達）→ 最終バーの終値で強制決済 ──
        if not partA_closed:
            last_j = min(scan_end - 1, len(df) - 1)
            partA_exit   = float(df["Close"].iloc[last_j])
            partA_pnl    = _pips(partA_exit - partA_entry) if direction == "BUY" \
                           else _pips(partA_entry - partA_exit)
            partA_reason = "TIMEOUT"
            partA_closed = True
            partA_exit_time = df.index[last_j]
            # 検索バー数が足りない場合（まだポジション生存中）→ 後続バーで継続
            # ただし今回は簡略化のため TIMEOUT 扱い

        # ════════════════════════════════════════════════════════
        # トレード記録
        # ════════════════════════════════════════════════════════

        # Part A（必ず記録）
        if partA_pnl is not None:
            trades.append({
                "pair": pair_name, "strategy": "分割_PartA",
                "direction": direction,
                "entry_time": entry_time, "exit_time": partA_exit_time,
                "entry": partA_entry, "exit": partA_exit,
                "sl": sl, "tp": tp,
                "pnl_pips": partA_pnl,
                "pnl_jpy": int(partA_pnl * LOT_HALF / 100),
                "result": "WIN" if partA_pnl > 0 else "LOSS",
                "exit_reason": partA_reason,
                "lot": LOT_HALF,
                "retest": retest_found,
            })

        # Part B（リテスト成立時のみ記録）
        if retest_found and partB_entry is not None and partA_exit is not None:
            partB_pnl = _pips(partA_exit - partB_entry) if direction == "BUY" \
                        else _pips(partB_entry - partA_exit)
            trades.append({
                "pair": pair_name, "strategy": "分割_PartB",
                "direction": direction,
                "entry_time": partB_fill_time, "exit_time": partA_exit_time,
                "entry": partB_entry, "exit": partA_exit,
                "sl": sl, "tp": tp,
                "pnl_pips": partB_pnl,
                "pnl_jpy": int(partB_pnl * LOT_HALF / 100),
                "result": "WIN" if partB_pnl > 0 else "LOSS",
                "exit_reason": partA_reason,
                "lot": LOT_HALF,
                "retest": True,
            })

        # 次のシグナル探索（scan_end から再開）
        i = scan_end

    stats = {
        "total_signals":    total_signals,
        "retest_count":     retest_occurred,
        "retest_rate":      round(retest_occurred / total_signals * 100, 1) if total_signals > 0 else 0,
        "avg_improvement":  round(float(np.mean(retest_pips_gain)), 1) if retest_pips_gain else 0,
    }
    return pd.DataFrame(trades), stats


# ─────────────────────────────────────────────────────────────────
# ▌ 成績サマリー
# ─────────────────────────────────────────────────────────────────
def summarize(df: pd.DataFrame, label: str, lot: int = LOT_FULL) -> dict:
    if df.empty:
        print(f"    [{label}] トレードなし")
        return {}
    total  = len(df)
    wins   = (df["result"] == "WIN").sum()
    losses = total - wins
    wr     = round(wins / total * 100, 1)
    pips   = round(df["pnl_pips"].sum(), 1)
    jpy    = df["pnl_jpy"].sum()
    aw     = round(df.loc[df["result"] == "WIN",  "pnl_pips"].mean(), 1) if wins   > 0 else 0.0
    al     = round(df.loc[df["result"] == "LOSS", "pnl_pips"].mean(), 1) if losses > 0 else 0.0
    ppt    = round(pips / total, 1) if total > 0 else 0
    bal    = INITIAL_BALANCE + df["pnl_jpy"].cumsum()
    peak   = bal.cummax()
    dd     = (bal - peak)
    mdd    = round(dd.min() / peak[dd.idxmin()] * 100, 1) if not dd.empty else 0.0
    print(f"  {label:<22} {total:3d}回({wins}勝{losses}敗) 勝率{wr:5.1f}%  "
          f"{pips:+8.1f}pips ({jpy:+,}円)  "
          f"avg+{aw:.1f}/-{abs(al):.1f}  {ppt:+.1f}pips/trade  DD{mdd:.1f}%")
    return {"label": label, "trades": total, "wins": wins, "losses": losses,
            "win_rate": wr, "total_pips": pips, "total_jpy": jpy,
            "avg_win": aw, "avg_loss": al, "pips_per_trade": ppt, "max_dd_pct": mdd}


# ─────────────────────────────────────────────────────────────────
# ▌ メイン
# ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="分割エントリー検証")
    parser.add_argument("--pair", default="all", help="USDJPY / AUDJPY / all")
    parser.add_argument("--max-retest", type=int, default=MAX_RETEST_BARS,
                        help=f"リテスト待ち上限バー数（デフォルト {MAX_RETEST_BARS}本）")
    args = parser.parse_args()

    if args.pair.upper() == "ALL":
        target = PAIRS
    else:
        key = args.pair.upper() + "=X"
        target = {key: PAIRS[key]} if key in PAIRS else PAIRS

    print("=" * 72)
    print("  分割エントリー検証（ブレイク即 vs 半量即+半量リテスト待ち）")
    print(f"  リテスト待ち上限: {args.max_retest}本（{args.max_retest * 4}時間）")
    print("=" * 72)

    all_normal, all_splitA, all_splitB = [], [], []

    for ticker, pair_name in target.items():
        print(f"\n▶ {pair_name}  データ取得中...")
        try:
            df = fetch_data(ticker)
        except Exception as e:
            print(f"  ❌ 取得失敗: {e}")
            continue
        print(f"  4H足: {len(df)}本  ({df.index[0].date()} 〜 {df.index[-1].date()})")

        # 通常エントリー
        df_normal = backtest_normal(df, pair_name)

        # 分割エントリー
        df_split, stats = backtest_split(df, pair_name, args.max_retest)
        df_splitA = df_split[df_split["strategy"] == "分割_PartA"]
        df_splitB = df_split[df_split["strategy"] == "分割_PartB"]

        w = 72
        print(f"\n  {'─'*w}")
        print(f"  【{pair_name} — 成績比較】")
        print(f"  {'─'*w}")
        print(f"  {'戦略':<22} {'回数':>4}       {'勝率':>6}  {'累計pips':>10}  "
              f"{'平均利/損':>12}  {'p/t':>7}  {'最大DD':>6}")
        print(f"  {'─'*w}")

        s_n  = summarize(df_normal, "通常（全量即エントリー）", LOT_FULL)

        # 分割戦略は合算 pnl_jpy で評価
        if not df_split.empty:
            df_combined = df_split.copy()
            s_a  = summarize(df_splitA, "分割 PartA（半量即）", LOT_HALF)
            s_b  = summarize(df_splitB, "分割 PartB（リテスト）", LOT_HALF) if not df_splitB.empty else {}

            # 合算評価（PartA + PartBをセットで）
            total_jpy_split = df_split["pnl_jpy"].sum()
            total_pips_split = round(df_split["pnl_pips"].sum(), 1)  # 参考値（lot異なるため目安）
            print(f"\n  ▼ 分割戦略 合計 (PartA+B): {total_jpy_split:+,}円")

        # ── リテスト統計 ──
        print(f"\n  {'─'*w}")
        print(f"  【{pair_name} — リテスト発生統計】")
        print(f"  {'─'*w}")
        print(f"  シグナル総数     : {stats['total_signals']}回")
        print(f"  リテスト発生     : {stats['retest_count']}回")
        print(f"  リテスト発生率   : {stats['retest_rate']:.1f}%")
        if stats['avg_improvement'] != 0:
            print(f"  リテスト時の入値改善: 平均 {stats['avg_improvement']:+.1f} pips（通常より有利な分）")

        # ── 直接比較 ──
        if s_n and not df_split.empty:
            print(f"\n  {'─'*w}")
            print(f"  【{pair_name} — 同ロット換算比較（LOT_FULL基準）】")
            print(f"  {'─'*w}")
            # 分割戦略の合計円を LOT_FULL 換算に補正して通常と比較
            # PartA(LOT_HALF) + PartB(LOT_HALF) は最大 LOT_FULL 相当
            # リテストなしの場合は LOT_HALF のみ → 平均ロット = LOT_HALF * (1 + retest_rate/100)
            avg_lot = LOT_HALF * (1 + stats["retest_rate"] / 100)
            scale   = LOT_FULL / avg_lot if avg_lot > 0 else 1
            adj_jpy = int(total_jpy_split * scale)
            adj_diff = adj_jpy - s_n.get("total_jpy", 0)
            sign = "+" if adj_diff >= 0 else ""
            verdict = "✅ 分割有利" if adj_diff > 0 else "⚠️ 通常有利"
            print(f"  通常エントリー   : {s_n.get('total_jpy', 0):+,}円")
            print(f"  分割エントリー   : {adj_jpy:+,}円（ロット補正後）")
            print(f"  差分             : {sign}{adj_diff:,}円  {verdict}")
            print(f"  ※ リテスト発生率 {stats['retest_rate']}% → 平均実効ロット {avg_lot:,.0f}通貨")

        all_normal.append(df_normal)
        if not df_split.empty:
            all_splitA.append(df_splitA)
            if not df_splitB.empty:
                all_splitB.append(df_splitB)

    # ══════════════════════════════════════════════════════════════
    # 全ペア合計
    # ══════════════════════════════════════════════════════════════
    if len(target) > 1:
        print("\n" + "=" * 72)
        print("  【全ペア合計】")
        print("=" * 72)

        if all_normal:
            df_all_n = pd.concat(all_normal, ignore_index=True)
            summarize(df_all_n, "通常（全量即）", LOT_FULL)

        if all_splitA:
            df_all_A = pd.concat(all_splitA, ignore_index=True)
            summarize(df_all_A, "分割 PartA（半量即）", LOT_HALF)
        if all_splitB:
            df_all_B = pd.concat(all_splitB, ignore_index=True)
            summarize(df_all_B, "分割 PartB（リテスト）", LOT_HALF)
        if all_splitA or all_splitB:
            combined_split = pd.concat(all_splitA + all_splitB, ignore_index=True)
            total_split_jpy = combined_split["pnl_jpy"].sum()
            print(f"\n  分割 合計 (PartA+B): {total_split_jpy:+,}円")

    print("\n" + "=" * 72)
    print("\n📌 見方:")
    print("  リテスト発生率が高い  → 分割エントリーの恩恵が大きい")
    print("  リテスト発生率が低い  → 通常エントリーが有利（機会損失コストが高い）")
    print("  目安: 発生率60%超 かつ リテスト入値改善10pips超 → 分割エントリー検討価値あり")
    print()


if __name__ == "__main__":
    main()
