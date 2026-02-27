"""
verify_live.py — 本番環境の動作確認スクリプト
=============================================
auto_trader.py の本番稼働前チェック。実際の発注は一切しない。

確認内容:
  1. OANDA 接続・ローソク足取得
  2. H&S 検出ロジック（バックテストと同一パラメータか）
  3. SL/TP 計算の正当性チェック
  4. LINE 通知のテスト送信
  5. バックテストとのパラメータ一致確認

使い方:
    python3 verify_live.py          # 全チェック
    python3 verify_live.py --no-line  # LINE送信をスキップ
"""

from __future__ import annotations
import os, sys, argparse
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# ── .env 読み込み ────────────────────────────────────────────
def _load_env():
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())

_load_env()
JST = ZoneInfo("Asia/Tokyo")

PASS = "✅"
FAIL = "❌"
WARN = "⚠️ "

results: list[tuple[str, bool, str]] = []   # (label, ok, detail)

def check(label: str, ok: bool, detail: str = ""):
    results.append((label, ok, detail))
    mark = PASS if ok else FAIL
    print(f"  {mark} {label}" + (f"  ({detail})" if detail else ""))

def section(title: str):
    print(f"\n{'─'*50}")
    print(f"  {title}")
    print(f"{'─'*50}")


# ════════════════════════════════════════════════════════════
# 1. パラメータ一致確認（コード読み取り）
# ════════════════════════════════════════════════════════════
section("1. バックテストとのパラメータ一致確認")

with open(ROOT / "auto_trader.py") as f:   at = f.read()
with open(ROOT / "backtest.py")    as f:   bt = f.read()

param_checks = [
    ("distance=5",           "HS_DISTANCE    = 5"   in at, "auto_trader"),
    ("tol=0.020",            "HS_TOL         = 0.020" in at, "auto_trader"),
    ("MAX_SL_PIPS=80",       "MAX_SL_PIPS    = 80"  in at, "auto_trader"),
    ("buffer=0.05",          "HS_BUFFER_PIPS = 0.05" in at, "auto_trader"),
    ("直近100本窓",           "df.tail(100)"         in at, "auto_trader"),
    ("全組み合わせ探索",       "range(len(peak_idx) - 3, -1, -1)" in at, "auto_trader"),
    ("ネックライン倍返しTP",   "depth     = head_val - neckline"  in at, "auto_trader"),
    ("SL方向チェック",        "sl <= close"          in at, "auto_trader"),
    ("ブレイクイーブン廃止",   "BREAKEVEN_PIPS"  not in at, "auto_trader"),
    ("EMAクロス廃止",         "check_ema_signal(" not in at.split("def check_ema_signal")[1], "メインループ"),
    ("OANDA価格取得",         'granularity="H4"'     in at, "auto_trader"),
    ("yfinanceフォールバック", "yfinance にフォールバック" in at, "auto_trader"),
]
for label, ok, where in param_checks:
    check(label, ok, where)


# ════════════════════════════════════════════════════════════
# 2. OANDA 接続・ローソク足取得
# ════════════════════════════════════════════════════════════
section("2. OANDA 接続・ローソク足取得")

oanda = None
try:
    from oanda_executor import OandaExecutor
    oanda = OandaExecutor()
    check("OANDA 接続", True, os.environ.get("OANDA_ENVIRONMENT", "live"))
except Exception as e:
    check("OANDA 接続", False, str(e))
    print(f"\n  {WARN} OANDA に接続できないため以降の確認をスキップします")

if oanda:
    import pandas as pd
    PAIRS = {"USD_JPY": "USD/JPY", "EUR_JPY": "EUR/JPY", "AUD_JPY": "AUD/JPY"}

    for instrument, pair_name in PAIRS.items():
        try:
            df_1h = oanda.get_candles(instrument, granularity="H1", count=10, include_incomplete=True)
            df_4h = oanda.get_candles(instrument, granularity="H4", count=350, include_incomplete=False)

            ok_1h = not df_1h.empty and len(df_1h) >= 2
            ok_4h = not df_4h.empty and len(df_4h) >= 200
            current = float(df_1h["Close"].iloc[-1]) if ok_1h else 0.0

            check(f"{pair_name} H1取得",  ok_1h, f"{len(df_1h)}本 / 現在値 {current:.3f}")
            check(f"{pair_name} H4取得",  ok_4h, f"{len(df_4h)}本 (SMA200={'OK' if ok_4h else '不足'})")
        except Exception as e:
            check(f"{pair_name} 取得", False, str(e))


# ════════════════════════════════════════════════════════════
# 3. H&S 検出ロジック（ライブデータで確認）
# ════════════════════════════════════════════════════════════
section("3. H&S 検出ロジック動作確認")

if oanda:
    from scipy.signal import find_peaks
    import numpy as np

    HS_DISTANCE = 5
    HS_TOL      = 0.020

    def detect_hs(window):
        highs = window["High"].values
        lows  = window["Low"].values
        n     = len(highs)
        peak_idx, _ = find_peaks(highs, distance=HS_DISTANCE)
        if len(peak_idx) >= 3:
            for k in range(len(peak_idx) - 3, -1, -1):
                ls_i, hd_i, rs_i = int(peak_idx[k]), int(peak_idx[k+1]), int(peak_idx[k+2])
                ls, head, rs = highs[ls_i], highs[hd_i], highs[rs_i]
                if rs_i < n - HS_DISTANCE * 4: continue
                if head <= max(ls, rs):         continue
                if abs(ls - rs) / (head + 1e-9) > HS_TOL: continue
                neck1    = float(lows[ls_i:hd_i].min()) if hd_i > ls_i else float(lows[ls_i])
                neck2    = float(lows[hd_i:rs_i].min()) if rs_i > hd_i else float(lows[hd_i])
                neckline = (neck1 + neck2) / 2
                buf      = max(1, HS_DISTANCE // 2)
                rs_high  = float(highs[max(0, rs_i - buf): rs_i + buf + 1].max())
                return {"pattern": "HEAD_AND_SHOULDERS", "head": float(head),
                        "neckline": neckline, "rs_high": rs_high}
        trough_idx, _ = find_peaks(-lows, distance=HS_DISTANCE)
        if len(trough_idx) >= 3:
            for k in range(len(trough_idx) - 3, -1, -1):
                ls_i, hd_i, rs_i = int(trough_idx[k]), int(trough_idx[k+1]), int(trough_idx[k+2])
                ls, head, rs = lows[ls_i], lows[hd_i], lows[rs_i]
                if rs_i < n - HS_DISTANCE * 4: continue
                if head >= min(ls, rs):         continue
                if abs(ls - rs) / (abs(head) + 1e-9) > HS_TOL: continue
                neck1    = float(highs[ls_i:hd_i].max()) if hd_i > ls_i else float(highs[ls_i])
                neck2    = float(highs[hd_i:rs_i].max()) if rs_i > hd_i else float(highs[hd_i])
                neckline = (neck1 + neck2) / 2
                buf      = max(1, HS_DISTANCE // 2)
                rs_low   = float(lows[max(0, rs_i - buf): rs_i + buf + 1].min())
                return {"pattern": "INV_HEAD_AND_SHOULDERS", "head": float(head),
                        "neckline": neckline, "rs_low": rs_low}
        return None

    MAX_SL_PIPS    = 80
    HS_BUFFER_PIPS = 0.05

    for instrument, pair_name in PAIRS.items():
        try:
            df_4h  = oanda.get_candles(instrument, granularity="H4", count=350, include_incomplete=False)
            df_1h  = oanda.get_candles(instrument, granularity="H1", count=10,  include_incomplete=True)
            if df_4h.empty or df_1h.empty:
                check(f"{pair_name} H&S検出", False, "データなし")
                continue

            close  = float(df_1h["Close"].iloc[-1])
            window = df_4h.tail(100)
            hs     = detect_hs(window)

            if hs is None:
                check(f"{pair_name} H&S検出", True, "パターンなし（正常）")
                continue

            pattern = hs["pattern"]
            label_p = "天井H&S→SELL" if pattern == "HEAD_AND_SHOULDERS" else "逆H&S→BUY"

            # SL計算
            if pattern == "HEAD_AND_SHOULDERS":
                sl      = round(hs["rs_high"] + HS_BUFFER_PIPS, 3)
                sl_ok   = sl > close     # SELL: SLはエントリーより上
                depth   = hs["head"] - hs["neckline"]
                tp      = round(hs["neckline"] - depth, 3)
                tp_ok   = tp < close     # SELL: TPはエントリーより下
            else:
                sl      = round(hs["rs_low"] - HS_BUFFER_PIPS, 3)
                sl_ok   = sl < close     # BUY: SLはエントリーより下
                depth   = hs["neckline"] - hs["head"]
                tp      = round(hs["neckline"] + depth, 3)
                tp_ok   = tp > close     # BUY: TPはエントリーより上

            sl_pips = abs(close - sl) * 100
            sl_cap  = sl_pips <= MAX_SL_PIPS

            all_ok  = sl_ok and tp_ok and sl_cap
            detail  = (f"{label_p} / 現在値:{close:.3f} SL:{sl:.3f}({sl_pips:.1f}p) "
                       f"TP:{tp:.3f} / SL方向:{'OK' if sl_ok else 'NG'} "
                       f"TP方向:{'OK' if tp_ok else 'NG'} "
                       f"SL上限:{'OK' if sl_cap else 'NG'}")
            check(f"{pair_name} H&S検出+SL/TP計算", all_ok, detail)

        except Exception as e:
            check(f"{pair_name} H&S検出", False, str(e))

else:
    print(f"  {WARN} OANDA 未接続のためスキップ")


# ════════════════════════════════════════════════════════════
# 4. LINE 通知テスト
# ════════════════════════════════════════════════════════════
section("4. LINE 通知テスト")

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--no-line", action="store_true")
args, _ = parser.parse_known_args()

if args.no_line:
    print(f"  {WARN} --no-line 指定のためスキップ")
else:
    try:
        from line_notify import send_line
        now_str = datetime.now(JST).strftime("%Y/%m/%d %H:%M JST")
        ok = send_line(
            f"🔍 verify_live.py 動作確認\n"
            f"━━━━━━━━━━━━\n"
            f"✅ auto_trader.py の確認テストです\n"
            f"実際の発注は行っていません。\n"
            f"────────────\n"
            f"{now_str}"
        )
        check("LINE 送信", ok, "LINEを確認してください")
    except Exception as e:
        check("LINE 送信", False, str(e))


# ════════════════════════════════════════════════════════════
# 5. .env 設定確認
# ════════════════════════════════════════════════════════════
section("5. .env 設定確認")

env_checks = [
    ("OANDA_API_KEY",       bool(os.environ.get("OANDA_API_KEY"))),
    ("OANDA_ACCOUNT_ID",    bool(os.environ.get("OANDA_ACCOUNT_ID"))),
    ("OANDA_ENVIRONMENT",   os.environ.get("OANDA_ENVIRONMENT") in ("live", "practice")),
    ("LINE_CHANNEL_TOKEN",  bool(os.environ.get("LINE_CHANNEL_TOKEN"))),
    ("LINE_GROUP_ID",       bool(os.environ.get("LINE_GROUP_ID"))),
    ("DRY_RUN=false",       os.environ.get("DRY_RUN", "true").lower() == "false"),
    ("LOT=20000",           os.environ.get("LOT", "0") == "20000"),
    ("INITIAL_BALANCE設定", bool(os.environ.get("INITIAL_BALANCE"))),
]
env = os.environ.get("OANDA_ENVIRONMENT", "未設定")
lot = os.environ.get("LOT", "未設定")
dry = os.environ.get("DRY_RUN", "true")
check("OANDA_API_KEY",      bool(os.environ.get("OANDA_API_KEY")),      "設定済み" if os.environ.get("OANDA_API_KEY") else "未設定")
check("OANDA_ACCOUNT_ID",   bool(os.environ.get("OANDA_ACCOUNT_ID")),   "設定済み" if os.environ.get("OANDA_ACCOUNT_ID") else "未設定")
check("OANDA_ENVIRONMENT",  env in ("live","practice"),                  env)
check("LINE_CHANNEL_TOKEN", bool(os.environ.get("LINE_CHANNEL_TOKEN")), "設定済み" if os.environ.get("LINE_CHANNEL_TOKEN") else "未設定")
check("LINE_GROUP_ID",      bool(os.environ.get("LINE_GROUP_ID")),      "設定済み" if os.environ.get("LINE_GROUP_ID") else "未設定")
check("DRY_RUN=false（本番モード）", dry.lower() == "false",            f"DRY_RUN={dry}")
check("LOT=20000",          lot == "20000",                              f"LOT={lot}")


# ════════════════════════════════════════════════════════════
# 最終サマリー
# ════════════════════════════════════════════════════════════
total  = len(results)
passed = sum(1 for _, ok, _ in results if ok)
failed = total - passed

print(f"\n{'═'*50}")
print(f"  最終結果: {passed}/{total} 項目 OK")
if failed == 0:
    print(f"  {PASS} すべてのチェックに合格しました！本番運用 OK です。")
else:
    print(f"  {FAIL} {failed} 項目に問題があります。上記の ❌ を確認してください。")
print(f"{'═'*50}\n")
