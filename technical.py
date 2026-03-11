"""
technical.py — 共通テクニカル計算モジュール
=============================================
auto_trader.py と backtest/backtest.py で共有する
H&S パターン検知コアロジックと共通ユーティリティ。
"""

from __future__ import annotations

import contextlib
import io

import pandas as pd
from scipy.signal import find_peaks


# ─────────────────────────────────────────────────────────────
# ▌ 共通ユーティリティ
# ─────────────────────────────────────────────────────────────

def _quiet(func, *args, **kwargs):
    """pandas_ta の verbose 出力を抑制して関数を実行する"""
    with contextlib.redirect_stdout(io.StringIO()):
        return func(*args, **kwargs)


# ─────────────────────────────────────────────────────────────
# ▌ H&S パターン検知コアロジック
# ─────────────────────────────────────────────────────────────

def detect_hs_window(
    window: pd.DataFrame,
    distance: int = 5,
    tol: float = 0.020,
) -> dict | None:
    """
    スライス済みウィンドウ上で H&S パターンを検知する。

    auto_trader.py と backtest/backtest.py が共有するコアアルゴリズム。
    呼び出し側でウィンドウを切り出してから渡す:
        - ライブ監視: detect_hs_window(df.tail(100))
        - バックテスト: detect_hs_window(df.iloc[max(0, i-100):i+1])

    Parameters
    ----------
    window   : スライス済み OHLCV DataFrame（最大100本程度）
    distance : find_peaks の distance パラメータ（ピーク間最小距離）
    tol      : 左右の肩の対称性許容誤差

    Returns
    -------
    dict or None:
        {
          "pattern":             "HEAD_AND_SHOULDERS" | "INV_HEAD_AND_SHOULDERS",
          "right_shoulder_high": float,
          "right_shoulder_low":  float,  # INV_H&S のみ
          "head":                float,
          "neckline":            float,
        }
    """
    if len(window) < distance * 3:
        return None

    highs = window["High"].values
    lows  = window["Low"].values
    n     = len(highs)

    # ── 天井 H&S（3ピーク：左肩 < 頭 > 右肩、左肩 ≈ 右肩） ──────────
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
            neckline = round((neck1 + neck2) / 2, 3)
            buf      = max(1, distance // 2)
            rs_high  = float(highs[max(0, rs_i - buf): rs_i + buf + 1].max())

            return {
                "pattern":             "HEAD_AND_SHOULDERS",
                "right_shoulder_high": rs_high,
                "head":                float(head),
                "neckline":            neckline,
                "_rs_i":               rs_i,   # バックテスト用インデックス（内部用）
            }

    # ── 逆 H&S（3トラフ：左肩 > 頭 < 右肩、左肩 ≈ 右肩） ────────────
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

            return {
                "pattern":             "INV_HEAD_AND_SHOULDERS",
                "right_shoulder_high": float(highs[rs_i]),
                "right_shoulder_low":  rs_low,
                "head":                float(head),
                "neckline":            neckline,
                "_rs_i":               rs_i,   # バックテスト用インデックス（内部用）
            }

    return None
