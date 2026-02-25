"""
CatHack AI Trader ダッシュボード ― SaaS モダンデザイン v3
=============================================================
起動方法:
  pip3 install streamlit plotly yfinance
  streamlit run dashboard.py
"""

import sys, os, json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st
import plotly.graph_objects as go
import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

_import_ok    = True
_import_error = ""
try:
    from market_analyzer import analyze_market, _Indicators
    from trade_filter import TradeFilter
except Exception as e:
    _import_ok    = False
    _import_error = str(e)

JST = ZoneInfo("Asia/Tokyo")

# ─────────────────────────────────────────────────────────────
# ページ設定
# ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="CatHack AI Trader",
    page_icon="🐱",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────
# ▌ CSS ── デザイナー指示書に基づく全面リファイン
# ─────────────────────────────────────────────────────────────
st.markdown("""
<style>
/* ── Google Fonts ─────────────────────────────────────────── */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

/* ── ベースリセット ────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; }
html, body, [class*="css"] {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    color: #1F2937;
}

/* ── メイン背景 ───────────────────────────────────────────── */
.stApp { background-color: #F8F9FA; }

/* ── ブロックコンテナの余白 ───────────────────────────────── */
.block-container {
    padding-top: 2rem !important;
    padding-bottom: 3rem !important;
    padding-left: 2rem !important;
    padding-right: 2rem !important;
    max-width: 1400px;
}

/* ── サイドバー ───────────────────────────────────────────── */
section[data-testid="stSidebar"] {
    background-color: #1E1E2F !important;
}
section[data-testid="stSidebar"] > div:first-child {
    padding: 1.5rem 1rem !important;
}
section[data-testid="stSidebar"] * { color: #CBD5E1 !important; }
section[data-testid="stSidebar"] .stButton button {
    background: rgba(79,70,229,0.15) !important;
    border: 1px solid rgba(79,70,229,0.35) !important;
    color: #A5B4FC !important;
    border-radius: 10px !important;
    font-weight: 600 !important;
    font-size: 0.8rem !important;
    letter-spacing: 0.02em !important;
    padding: 0.45rem 1rem !important;
}
section[data-testid="stSidebar"] .stButton button:hover {
    background: rgba(79,70,229,0.25) !important;
}
section[data-testid="stSidebar"] hr { border-color: #2E2E45 !important; }

/* ── カード（最重要） ─────────────────────────────────────── */
.card {
    background: #FFFFFF;
    border-radius: 16px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05), 0 1px 2px rgba(0,0,0,0.10);
    padding: 24px;
    margin-bottom: 20px;
    border: 1px solid #E5E7EB;
}

/* ── KPI カード ───────────────────────────────────────────── */
.kpi-card {
    background: #FFFFFF;
    border-radius: 16px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05), 0 1px 2px rgba(0,0,0,0.10);
    padding: 24px;
    border: 1px solid #E5E7EB;
    display: flex;
    flex-direction: column;
    gap: 16px;
}
.kpi-top-row {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
}
.kpi-icon-wrap {
    width: 44px; height: 44px;
    border-radius: 12px;
    display: flex; align-items: center; justify-content: center;
    font-size: 1.25rem;
    flex-shrink: 0;
}
.kpi-bottom-row {
    display: flex;
    flex-direction: column;
    gap: 4px;
}
.kpi-label {
    font-size: 0.75rem;
    font-weight: 500;
    color: #6B7280;
    letter-spacing: 0.04em;
    text-transform: uppercase;
}
.kpi-value-row {
    display: flex;
    align-items: baseline;
    gap: 5px;
}
.kpi-number {
    font-size: 2rem;
    font-weight: 800;
    color: #1F2937;
    line-height: 1;
    letter-spacing: -0.03em;
}
.kpi-unit {
    font-size: 0.85rem;
    font-weight: 500;
    color: #9CA3AF;
}
.kpi-delta {
    font-size: 0.78rem;
    font-weight: 600;
    margin-top: 2px;
}
.kpi-delta-up   { color: #10B981; }
.kpi-delta-down { color: #EF4444; }
.kpi-delta-flat { color: #6B7280; }

/* ── バッジ ───────────────────────────────────────────────── */
.badge {
    display: inline-flex; align-items: center;
    font-size: 0.7rem; font-weight: 700;
    padding: 3px 10px; border-radius: 20px;
    letter-spacing: 0.02em;
    white-space: nowrap;
}
.badge-emerald { background: #D1FAE5; color: #065F46; }
.badge-blue    { background: #EEF2FF; color: #3730A3; }
.badge-red     { background: #FEE2E2; color: #991B1B; }
.badge-amber   { background: #FEF3C7; color: #92400E; }
.badge-gray    { background: #F3F4F6; color: #4B5563; }
.badge-indigo  { background: #E0E7FF; color: #3730A3; }

/* ── ページタイトル ───────────────────────────────────────── */
.page-header {
    margin-bottom: 28px;
}
.page-title {
    font-size: 1.65rem;
    font-weight: 800;
    color: #1F2937;
    letter-spacing: -0.025em;
    margin-bottom: 4px;
    line-height: 1.2;
}
.page-subtitle {
    font-size: 0.85rem;
    color: #6B7280;
    font-weight: 400;
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
}
.rate-highlight {
    font-weight: 700;
    color: #1F2937;
}

/* ── ライブバッジ ─────────────────────────────────────────── */
.live-badge {
    display: inline-flex; align-items: center; gap: 6px;
    background: #ECFDF5; border: 1px solid #A7F3D0;
    border-radius: 20px; padding: 4px 12px;
    font-size: 0.73rem; font-weight: 700; color: #065F46;
    letter-spacing: 0.04em;
}
.live-dot {
    width: 6px; height: 6px; background: #10B981;
    border-radius: 50%; animation: blink 1.6s ease-in-out infinite;
    display: inline-block;
}
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.25} }

/* ── セクションラベル ─────────────────────────────────────── */
.section-label {
    font-size: 0.7rem;
    font-weight: 700;
    color: #9CA3AF;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    margin-bottom: 12px;
    margin-top: 4px;
}

/* ── カードヘッダー ───────────────────────────────────────── */
.card-header {
    display: flex; align-items: center;
    justify-content: space-between;
    margin-bottom: 20px;
    padding-bottom: 16px;
    border-bottom: 1px solid #F3F4F6;
}
.card-title { font-size: 0.9rem; font-weight: 700; color: #1F2937; }
.card-meta  { font-size: 0.75rem; color: #9CA3AF; font-weight: 400; }

/* ── フィルターバー ───────────────────────────────────────── */
.filter-bar {
    display: flex; align-items: center; gap: 10px;
    border-radius: 12px; padding: 12px 18px;
    margin-bottom: 20px; font-size: 0.83rem; font-weight: 500;
    border: 1px solid;
}
.filter-ok   { background: #ECFDF5; border-color: #A7F3D0; color: #065F46; }
.filter-ng   { background: #FEF2F2; border-color: #FCA5A5; color: #991B1B; }
.filter-warn { background: #FFFBEB; border-color: #FDE68A; color: #92400E; }

/* ── AI シグナルボックス（コンパクト版） ─────────────────── */
.signal-pill {
    display: inline-flex; align-items: center; gap: 6px;
    border-radius: 10px; padding: 6px 16px;
    font-size: 0.85rem; font-weight: 800;
    letter-spacing: 0.06em; border: 1.5px solid;
    width: 100%; justify-content: center;
    margin-bottom: 14px;
}
.signal-buy  { background: #D1FAE5; color: #065F46; border-color: #6EE7B7; }
.signal-sell { background: #FEE2E2; color: #991B1B; border-color: #FCA5A5; }
.signal-hold { background: #F3F4F6; color: #374151; border-color: #D1D5DB; }
.signal-wait { background: #FEF3C7; color: #92400E; border-color: #FDE68A; }

/* ── サイドバー内ステータス行 ────────────────────────────── */
.sb-row {
    display: flex; justify-content: space-between; align-items: center;
    padding: 8px 0; border-bottom: 1px solid rgba(255,255,255,0.06);
    font-size: 0.8rem;
}
.sb-row:last-child { border-bottom: none; }
.sb-key { color: #64748B !important; font-weight: 400; }
.sb-val { color: #E2E8F0 !important; font-weight: 600; text-align: right; }
.sb-section-title {
    font-size: 0.65rem; font-weight: 700; letter-spacing: 0.1em;
    text-transform: uppercase; color: #475569 !important;
    margin: 16px 0 8px;
}
.sb-logo {
    font-size: 1.1rem; font-weight: 800; color: #FFFFFF !important;
    letter-spacing: -0.01em;
}
.sb-logo-sub {
    font-size: 0.67rem; color: #4B5563 !important;
    letter-spacing: 0.08em; margin-top: 2px;
}

/* ── XAI ログ ─────────────────────────────────────────────── */
.xai-row { padding: 12px 0; border-bottom: 1px solid #F3F4F6; }
.xai-row:last-child { border-bottom: none; }
.xai-meta { display: flex; align-items: center; gap: 8px; margin-bottom: 5px; }
.xai-tag {
    font-size: 0.65rem; font-weight: 700; padding: 2px 8px;
    border-radius: 5px; letter-spacing: 0.04em;
}
.tag-entry  { background: #EEF2FF; color: #3730A3; }
.tag-close  { background: #FEE2E2; color: #991B1B; }
.tag-update { background: #F3F4F6; color: #4B5563; }
.tag-skip   { background: #FEF3C7; color: #92400E; }
.xai-time   { font-size: 0.72rem; color: #9CA3AF; }
.xai-pnl    { font-size: 0.78rem; font-weight: 700; }
.xai-pnl-pos { color: #10B981; }
.xai-pnl-neg { color: #EF4444; }
.xai-reason { font-size: 0.8rem; color: #6B7280; line-height: 1.55; }

/* ── 指標テーブル ─────────────────────────────────────────── */
.metrics-grid { display: flex; flex-direction: column; gap: 0; }
.metric-row {
    display: flex; justify-content: space-between; align-items: center;
    padding: 9px 0; border-bottom: 1px solid #F9FAFB;
    font-size: 0.83rem;
}
.metric-row:last-child { border-bottom: none; }
.metric-key { color: #6B7280; font-weight: 400; }
.metric-val { font-weight: 700; color: #1F2937; }

/* ── 重要指標行 ───────────────────────────────────────────── */
.event-row {
    display: flex; align-items: center; justify-content: space-between;
    padding: 10px 0; border-bottom: 1px solid #F3F4F6;
    font-size: 0.83rem;
}
.event-row:last-child { border-bottom: none; }
.event-name { font-weight: 600; color: #1F2937; }
.event-time { font-size: 0.72rem; color: #9CA3AF; margin-top: 2px; }
.event-countdown { font-size: 0.75rem; color: #6B7280; text-align: right; }

/* ── XAI 判定理由ボックス ─────────────────────────────────── */
.reason-box {
    background: #F8F9FA; border-radius: 10px; padding: 14px 16px;
    font-size: 0.79rem; color: #374151; line-height: 1.65;
    border-left: 3px solid #4F46E5; margin-top: 14px;
}

/* ── Streamlit 余分な装飾を消す ──────────────────────────── */
footer, #MainMenu { display: none !important; }
header[data-testid="stHeader"] { background: transparent !important; }
div[data-testid="stVerticalBlock"] > div { gap: 0 !important; }
.stPlotlyChart { border-radius: 12px; overflow: hidden; }

/* ══════════════════════════════════════════════════════════
   新ウィジェット共通スタイル
   ══════════════════════════════════════════════════════════ */

/* ── 市場心理メーター ─────────────────────────────────────── */
.sentiment-wrap {
    margin: 6px 0 4px;
}
.sentiment-bar-bg {
    position: relative;
    height: 12px;
    border-radius: 999px;
    background: linear-gradient(90deg,
        #EF4444 0%, #F97316 20%, #EAB308 40%,
        #EAB308 60%, #22C55E 80%, #10B981 100%);
    margin: 10px 0 6px;
}
.sentiment-marker {
    position: absolute;
    top: 50%;
    transform: translate(-50%, -50%);
    width: 20px; height: 20px;
    background: white;
    border-radius: 50%;
    border: 3px solid #1F2937;
    box-shadow: 0 2px 6px rgba(0,0,0,0.2);
    transition: left 0.6s ease;
}
.sentiment-labels {
    display: flex;
    justify-content: space-between;
    font-size: 0.72rem;
    color: #9CA3AF;
    margin-top: 4px;
}
.sentiment-score-row {
    display: flex;
    align-items: baseline;
    gap: 8px;
    margin-top: 10px;
}
.sentiment-score-num {
    font-size: 2.2rem;
    font-weight: 800;
    letter-spacing: -0.03em;
    line-height: 1;
}
.sentiment-score-label {
    font-size: 0.82rem;
    font-weight: 600;
}
.sentiment-sub {
    font-size: 0.75rem;
    color: #9CA3AF;
    margin-top: 4px;
}

/* ── 相場天気 ─────────────────────────────────────────────── */
.weather-icon {
    font-size: 3.2rem;
    line-height: 1;
    margin-bottom: 8px;
    display: block;
}
.weather-status {
    font-size: 1rem;
    font-weight: 800;
    color: #1F2937;
    margin-bottom: 4px;
}
.weather-desc {
    font-size: 0.78rem;
    color: #6B7280;
    line-height: 1.5;
}
.weather-chip {
    display: inline-block;
    border-radius: 8px;
    padding: 4px 12px;
    font-size: 0.72rem;
    font-weight: 700;
    margin-top: 10px;
}
.weather-trend    { background: #FEF9C3; color: #92400E; }
.weather-range    { background: #DBEAFE; color: #1E40AF; }
.weather-volatile { background: #FEE2E2; color: #991B1B; }
.weather-calm     { background: #D1FAE5; color: #065F46; }

/* ── AI思考タイムライン ───────────────────────────────────── */
.timeline-scroll {
    max-height: 340px;
    overflow-y: auto;
    padding-right: 4px;
    scrollbar-width: thin;
    scrollbar-color: #E5E7EB transparent;
}
.timeline-scroll::-webkit-scrollbar { width: 4px; }
.timeline-scroll::-webkit-scrollbar-thumb {
    background: #E5E7EB; border-radius: 4px;
}
.tl-item {
    display: flex;
    gap: 12px;
    padding: 10px 0;
    position: relative;
}
.tl-item:not(:last-child)::after {
    content: '';
    position: absolute;
    left: 17px; top: 44px;
    width: 2px; height: calc(100% - 24px);
    background: #F3F4F6;
}
.tl-icon-wrap {
    width: 34px; height: 34px;
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 0.95rem;
    flex-shrink: 0;
    position: relative;
    z-index: 1;
}
.tl-body { flex: 1; padding-top: 2px; }
.tl-time {
    font-size: 0.68rem;
    color: #9CA3AF;
    font-weight: 500;
    margin-bottom: 3px;
}
.tl-text {
    font-size: 0.83rem;
    font-weight: 600;
    color: #1F2937;
    line-height: 1.4;
}
.tl-sub {
    font-size: 0.76rem;
    color: #6B7280;
    margin-top: 3px;
    line-height: 1.45;
}
.tl-status-ok   { background: #D1FAE5; }
.tl-status-info { background: #EEF2FF; }
.tl-status-warn { background: #FEF3C7; }
.tl-status-act  { background: #FEE2E2; }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────
# ▌ カードラッパー
# ─────────────────────────────────────────────────────────────
def card_start(extra: str = ""):
    attr = f' style="{extra}"' if extra else ""
    st.markdown(f'<div class="card"{attr}>', unsafe_allow_html=True)

def card_end():
    st.markdown('</div>', unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────
# ▌ 新ウィジェット用ヘルパー
# ─────────────────────────────────────────────────────────────

def _confidence_pct(confidence: str, adx: float) -> float:
    """confidence 文字列 + ADX から 0–100 の数値スコアを計算"""
    base = {"High": 82.0, "Medium": 58.0, "Low": 34.0}.get(confidence, 50.0)
    # ADX が高いほど少し加点（最大 +10）
    bonus = min((adx - 20) / 3, 10.0) if adx > 20 else 0.0
    return round(min(base + bonus, 99.0), 1)


def render_gauge(conf_pct: float) -> None:
    """Plotly Indicator ゲージ — AI Confidence Level"""
    color = "#10B981" if conf_pct >= 80 else ("#F59E0B" if conf_pct >= 50 else "#9CA3AF")
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=conf_pct,
        number={"suffix": "%", "font": {"size": 32, "color": "#1F2937",
                                         "family": "Inter, sans-serif"}},
        title={"text": "AI 信頼度スコア",
               "font": {"size": 13, "color": "#6B7280", "family": "Inter"}},
        gauge={
            "axis": {"range": [0, 100], "tickwidth": 1, "tickcolor": "#E5E7EB",
                     "tickfont": {"size": 9, "color": "#9CA3AF"}},
            "bar":  {"color": color, "thickness": 0.28},
            "bgcolor": "white",
            "borderwidth": 0,
            "steps": [
                {"range": [0,  50], "color": "#F3F4F6"},
                {"range": [50, 80], "color": "#FEF3C7"},
                {"range": [80,100], "color": "#D1FAE5"},
            ],
            "threshold": {
                "line": {"color": color, "width": 3},
                "thickness": 0.8,
                "value": conf_pct,
            },
        },
    ))
    fig.update_layout(
        height=200, margin=dict(l=20, r=20, t=30, b=10),
        paper_bgcolor="white", font=dict(family="Inter"),
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def _sentiment_score(rsi: float, adx: float, action: str) -> tuple[float, str, str]:
    """
    RSI・ADX・アクションから 0–100 の Sentiment スコアを返す。
    (score, label, color)
    """
    # RSI ベース（50 中心）
    score = (rsi - 30) / 40 * 100   # RSI 30→0, RSI 70→100
    score = max(0.0, min(100.0, score))
    # BUY シグナルなら強気に寄せる
    if action == "BUY":
        score = min(score + 10, 100)
    elif action == "SELL":
        score = max(score - 10, 0)

    if score >= 70:
        label, color = "強気 🐂", "#10B981"
    elif score >= 55:
        label, color = "やや強気", "#34D399"
    elif score >= 45:
        label, color = "中立 ⚖️", "#9CA3AF"
    elif score >= 30:
        label, color = "やや弱気", "#F97316"
    else:
        label, color = "弱気 🐻", "#EF4444"

    return round(score, 1), label, color


def render_sentiment(score: float, label: str, color: str, rsi: float) -> None:
    """横長プログレスバー型 Fear & Greed メーター"""
    pct = score  # 0–100
    st.markdown(f"""
    <div class="sentiment-wrap">
      <div class="sentiment-bar-bg">
        <div class="sentiment-marker" style="left:{pct}%;"></div>
      </div>
      <div class="sentiment-labels">
        <span>弱気 🐻</span>
        <span>中立</span>
        <span>強気 🐂</span>
      </div>
      <div class="sentiment-score-row">
        <span class="sentiment-score-num" style="color:{color};">{score:.0f}</span>
        <div>
          <div class="sentiment-score-label" style="color:{color};">{label}</div>
          <div class="sentiment-sub">RSI {rsi:.1f} ベース</div>
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)


def _weather(adx: float, atr: float, regime: str) -> tuple[str, str, str, str, str]:
    """ADX/ATR/regime から天気情報を返す (icon, status, desc, chip_label, chip_cls)"""
    if atr * 100 > 80:   # ATR > 80pips → 嵐
        return ("⛈️", "高ボラティリティ",
                f"ATR {atr*100:.0f}pips — 大きな値動きに注意。\nスプレッドの拡大リスクあり。",
                "荒れ相場", "weather-volatile")
    if adx >= 30:
        return ("☀️", "強トレンド",
                f"ADX {adx:.1f} — {regime}相場。\nトレンド系戦略が有効なコンディション。",
                "トレンド", "weather-trend")
    if adx >= 20:
        return ("🌤️", "緩やかなトレンド",
                f"ADX {adx:.1f} — やや方向感あり。\n様子見しながらエントリーを検討。",
                "穏やか", "weather-calm")
    return ("☁️", "レンジ相場",
            f"ADX {adx:.1f} — レンジ相場。\nボリバン逆張り系戦略が有効なコンディション。",
            "レンジ", "weather-range")


def _build_thinking_log(
    signal_data: dict,
    adx: float, rsi: float, atr: float,
    regime: str, action: str, confidence: str,
    filter_ok: bool, filter_msg: str,
    now_jst: "datetime",
) -> list[dict]:
    """AI の思考ステップを時系列リストとして生成"""
    t = now_jst
    steps = []

    def _ts(delta_sec: int) -> str:
        return (t - timedelta(seconds=delta_sec)).strftime("%H:%M:%S")

    steps.append({"icon":"🔍","bg":"tl-status-info","time":_ts(55),
                  "text":"データ取得・前処理",
                  "sub":f"yfinance から USD/JPY 1h/4h 足を受信。欠損値チェック完了。"})

    steps.append({"icon":"📡","bg":"tl-status-info","time":_ts(45),
                  "text":"ADX 相場環境スキャン",
                  "sub":f"ADX = {adx:.1f} → "
                        f"{'強いトレンド ✅' if adx>=30 else ('中間 🔶' if adx>=20 else 'レンジ相場 ☁️')}"})

    steps.append({"icon":"📊","bg":"tl-status-info","time":_ts(35),
                  "text":"テクニカル指標計算",
                  "sub":f"RSI={rsi:.1f}  ATR={atr*100:.1f}pips  BB={signal_data.get('bb_position','-').replace('_',' ')}"})

    reg_emoji = {"TREND":"📈","RANGE":"↔️","WAIT":"⏸️"}.get(regime,"")
    steps.append({"icon":reg_emoji or "🧠","bg":"tl-status-info","time":_ts(28),
                  "text":f"レジーム判定: {regime}",
                  "sub":f"採用戦略 → {signal_data.get('strategy_used','-')}"})

    if not filter_ok:
        steps.append({"icon":"🚫","bg":"tl-status-warn","time":_ts(18),
                      "text":"TradeFilter 発動",
                      "sub":filter_msg})
    else:
        steps.append({"icon":"✅","bg":"tl-status-ok","time":_ts(18),
                      "text":"フィルターチェック: 通過",
                      "sub":f"スプレッド・指標フィルターともに問題なし。"})

    action_bg = {"BUY":"tl-status-ok","SELL":"tl-status-act",
                 "HOLD":"tl-status-info","WAIT":"tl-status-warn"}.get(action,"tl-status-info")
    action_icon = {"BUY":"▲","SELL":"▼","HOLD":"⏸","WAIT":"⏳"}.get(action,"🤖")
    steps.append({"icon":action_icon,"bg":action_bg,"time":_ts(5),
                  "text":f"シグナル確定: {action}",
                  "sub":f"信頼度 {confidence} — {signal_data.get('reason','')[:100]}…"
                         if len(signal_data.get('reason',''))>100
                         else f"信頼度 {confidence} — {signal_data.get('reason','-')}"})

    return steps


def render_thinking_timeline(steps: list[dict]) -> None:
    """吹き出し風タイムラインを描画"""
    items_html = ""
    for s in steps:
        items_html += f"""
        <div class="tl-item">
          <div class="tl-icon-wrap {s['bg']}">{s['icon']}</div>
          <div class="tl-body">
            <div class="tl-time">{s['time']}</div>
            <div class="tl-text">{s['text']}</div>
            <div class="tl-sub">{s['sub']}</div>
          </div>
        </div>"""
    st.markdown(f'<div class="timeline-scroll">{items_html}</div>',
                unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────
# ▌ KPI カード描画関数（指示書準拠）
# ─────────────────────────────────────────────────────────────
def render_kpi_card(
    icon: str,
    icon_bg: str,        # 例: "#EEF2FF"
    icon_color: str,     # 例: "#4F46E5"
    label: str,
    number: str,         # 数値部分（大きく表示）
    unit: str,           # 単位（小さくグレー）
    delta: str,          # 変化量テキスト
    delta_class: str,    # "kpi-delta-up" / "kpi-delta-down" / "kpi-delta-flat"
    badge: str,
    badge_class: str,
):
    st.markdown(f"""
    <div class="kpi-card">
      <div class="kpi-top-row">
        <div class="kpi-icon-wrap" style="background:{icon_bg}; color:{icon_color};">
          {icon}
        </div>
        <span class="badge {badge_class}">{badge}</span>
      </div>
      <div class="kpi-bottom-row">
        <div class="kpi-label">{label}</div>
        <div class="kpi-value-row">
          <span class="kpi-number">{number}</span>
          <span class="kpi-unit">{unit}</span>
        </div>
        <div class="kpi-delta {delta_class}">{delta}</div>
      </div>
    </div>
    """, unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────
# ▌ データ取得
# ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=300, show_spinner=False)
def fetch_price_data():
    import yfinance as yf
    end   = datetime.now(tz=timezone.utc)
    start = end - timedelta(days=90)
    raw   = yf.download("USDJPY=X", start=start, end=end,
                        interval="1h", progress=False, auto_adjust=True)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df_1h = raw[["Open","High","Low","Close","Volume"]].dropna()
    df_4h = raw.resample("4h").agg({"Open":"first","High":"max",
                                     "Low":"min","Close":"last","Volume":"sum"}).dropna()
    return df_1h, df_4h

def load_xai_log():
    p = ROOT / "xai_log.json"
    if not p.exists(): return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []

def load_filter():
    if not _import_ok: return None
    try:
        tf = TradeFilter(max_spread_pips=1.5, news_buffer_min=30)
        jp = ROOT / "news_events.json"
        if jp.exists(): tf.load_news_from_json(str(jp))
        return tf
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# ▌ 初期化・計算
# ─────────────────────────────────────────────────────────────
_fetch_error = ""
with st.spinner("📡 USD/JPY データ取得中..."):
    try:
        df_1h, df_4h = fetch_price_data()
        data_ok = len(df_1h) > 50
        if not data_ok:
            _fetch_error = f"データが少なすぎます（{len(df_1h)} 本）。yfinance の返却が空の可能性があります。"
    except Exception as e:
        data_ok = False
        _fetch_error = str(e)
        df_1h = df_4h = pd.DataFrame()

# ── yfinance が取れない場合はモックデータで代替表示 ─────────
if not data_ok:
    _n     = 300
    _now   = datetime.now(tz=timezone.utc)
    _times = [_now - timedelta(hours=_n - i) for i in range(_n)]
    _rng   = np.random.default_rng(42)
    _drift = np.linspace(147.5, 150.8, _n)
    _close = _drift + np.cumsum(_rng.standard_normal(_n) * 0.15)
    _open  = np.roll(_close, 1); _open[0] = _close[0]
    _high  = np.maximum(_open, _close) + np.abs(_rng.standard_normal(_n) * 0.12)
    _low   = np.minimum(_open, _close) - np.abs(_rng.standard_normal(_n) * 0.12)
    _idx   = pd.DatetimeIndex(_times, tz=timezone.utc)
    df_1h  = pd.DataFrame({"Open":_open,"High":_high,"Low":_low,
                            "Close":_close,"Volume":10000}, index=_idx)
    df_4h  = df_1h.resample("4h").agg({"Open":"first","High":"max",
                                        "Low":"min","Close":"last","Volume":"sum"}).dropna()
    data_ok = True   # モックデータで続行

now_utc = datetime.now(tz=timezone.utc)
now_jst = now_utc.astimezone(JST)

# AI シグナル
signal_data: dict = {}
if _import_ok and data_ok:
    try:
        signal_data = analyze_market(df_4h, adx_trend=30, adx_range=20)
    except Exception as e:
        signal_data = {"action":"WAIT","regime":"WAIT","confidence":"Low",
                       "strategy_used":"Error","adx":0.0,"atr":0.0,
                       "atr_pips":0.0,"rsi":0.0,"bb_position":"-","reason":str(e)}

action     = signal_data.get("action",      "WAIT")
regime     = signal_data.get("regime",      "-")
confidence = signal_data.get("confidence",  "-")
strategy   = signal_data.get("strategy_used","-")
adx_val    = float(signal_data.get("adx",   0.0))
atr_pips   = float(signal_data.get("atr_pips",0.0))
rsi_val    = float(signal_data.get("rsi",   0.0))
bb_pos     = signal_data.get("bb_position", "-")
ai_reason  = signal_data.get("reason",      "データ取得中...")

# TradeFilter
tf             = load_filter()
spread_now     = 0.4
filter_ok      = True
filter_msg     = "フィルター: 全OK — トレード可能"
upcoming_ev: list = []
if tf:
    filter_ok, filter_msg = tf.is_tradeable(now_jst, spread_now)
    upcoming_ev = tf.upcoming_events(now_jst, hours=48)

# レート・変動
price      = float(df_1h["Close"].iloc[-1]) if data_ok else 0.0
open_price = float(df_1h["Close"].resample("1D").first().iloc[-1]) if data_ok else price
d_chg      = price - open_price if data_ok else 0.0
d_pct      = (d_chg / open_price * 100) if open_price > 0 else 0.0

xai_log = load_xai_log()


# ─────────────────────────────────────────────────────────────
# ▌ サイドバー
# ─────────────────────────────────────────────────────────────
with st.sidebar:
    # ロゴ
    st.markdown("""
    <div style="padding:4px 0 20px;">
      <div class="sb-logo">🐱 CatHack</div>
      <div class="sb-logo-sub">AI TRADER  ·  v3.0</div>
    </div>
    """, unsafe_allow_html=True)

    # ナビ
    for ico, lbl, act in [("📊","ダッシュボード",True),("📈","バックテスト",False),
                           ("⚙️","エンジン設定",False),("🗒️","XAI ログ",False),("💹","注文履歴",False)]:
        cls = "nav-item active" if act else "nav-item"
        bg  = "rgba(79,70,229,0.18)" if act else "transparent"
        col = "#A5B4FC" if act else "#64748B"
        st.markdown(f"""
        <div style="display:flex;align-items:center;gap:9px;padding:8px 10px;
                    border-radius:9px;margin-bottom:2px;background:{bg};
                    font-size:0.84rem;font-weight:{'600' if act else '400'};
                    color:{col} !important;">
          {ico}&nbsp;{lbl}
        </div>""", unsafe_allow_html=True)

    # シグナルピル（コンパクト）
    st.markdown("<hr>", unsafe_allow_html=True)
    sig_cls_map = {"BUY":"signal-buy","SELL":"signal-sell",
                   "HOLD":"signal-hold","WAIT":"signal-wait"}
    sig_icon    = {"BUY":"▲","SELL":"▼","HOLD":"⏸","WAIT":"⏳"}
    st.markdown(f"""
    <div class="signal-pill {sig_cls_map.get(action,'signal-hold')}">
      {sig_icon.get(action,"")} &nbsp; {action}
      <span style="font-weight:500;font-size:0.78rem;margin-left:4px;">
        ({confidence})
      </span>
    </div>
    """, unsafe_allow_html=True)

    # ステータスグリッド
    regime_icon = {"TREND":"📈","RANGE":"↔️","WAIT":"⏸️"}.get(regime,"")
    sb_items = [
        ("相場",    f"{regime_icon} {regime}"),
        ("ADX",     f"{adx_val:.1f}"),
        ("RSI",     f"{rsi_val:.1f}"),
        ("ATR",     f"{atr_pips:.1f} pips"),
        ("BB 位置", bb_pos.replace("_"," ")),
        ("戦略",    strategy),
    ]
    rows_html = "".join(f"""
    <div class="sb-row">
      <span class="sb-key">{k}</span>
      <span class="sb-val">{v}</span>
    </div>""" for k, v in sb_items)
    st.markdown(f'<div style="margin-bottom:4px;">{rows_html}</div>', unsafe_allow_html=True)

    # 次回指標
    if upcoming_ev:
        ev  = upcoming_ev[0]
        ejst = ev.dt.astimezone(JST)
        dh   = (ev.dt - now_utc).total_seconds() / 3600
        st.markdown(f"""
        <div class="sb-section-title">NEXT EVENT</div>
        <div style="background:rgba(255,255,255,0.04);border-radius:9px;
                    padding:10px 12px;border:1px solid rgba(255,255,255,0.07);">
          <div style="font-size:0.8rem;font-weight:600;color:#E2E8F0 !important;">
            {ev.name}
          </div>
          <div style="font-size:0.71rem;color:#475569 !important;margin-top:3px;">
            {ejst.strftime('%m/%d %H:%M JST')} &nbsp;·&nbsp; あと {dh:.1f}h
          </div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("<hr>", unsafe_allow_html=True)
    st.markdown(f"""
    <div style="font-size:0.7rem;color:#374151 !important;text-align:center;line-height:1.6;">
      最終更新: {now_jst.strftime('%H:%M:%S JST')}<br>
      <span style="color:#2D2D45 !important;">5分ごとに自動更新</span>
    </div>
    """, unsafe_allow_html=True)
    st.markdown("<div style='height:10px;'></div>", unsafe_allow_html=True)
    if st.button("🔄  今すぐ更新", use_container_width=True):
        st.cache_data.clear()
        st.rerun()


# ─────────────────────────────────────────────────────────────
# ▌ メインエリア
# ─────────────────────────────────────────────────────────────

# ── エラー・警告バナー ────────────────────────────────────
if not _import_ok:
    st.error(f"⚠️ モジュール読み込みエラー: {_import_error}\n\n"
             "`pip3 install -r requirements.txt` を実行してください。", icon="🚨")

if _fetch_error:
    st.warning(
        f"⚠️ yfinance でのデータ取得に失敗しました（理由: `{_fetch_error}`）。\n\n"
        "**モックデータで代替表示しています。** "
        "インターネット接続を確認後、サイドバーの「🔄 今すぐ更新」を押してください。",
        icon="📡",
    )

# ── ページヘッダー ────────────────────────────────────────
chg_sign   = "+" if d_chg >= 0 else ""
chg_color  = "#10B981" if d_chg >= 0 else "#EF4444"
rate_str   = f"{price:.2f}" if data_ok else "---"
chg_str    = f"{chg_sign}{d_chg:.2f} ({chg_sign}{d_pct:.2f}%)" if data_ok else ""

st.markdown(f"""
<div class="page-header">
  <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;">
    <div>
      <div class="page-title">FX 自動売買ダッシュボード</div>
      <div class="page-subtitle">
        <span>USD / JPY</span>
        <span style="color:#E5E7EB;">·</span>
        <span class="rate-highlight">{rate_str} 円</span>
        <span style="color:{chg_color};font-weight:600;">{chg_str}</span>
        <span style="color:#E5E7EB;">·</span>
        <span>{now_jst.strftime("%Y年 %m月 %d日  %H:%M JST")}</span>
      </div>
    </div>
    <div class="live-badge"><span class="live-dot"></span>ライブ配信中</div>
  </div>
</div>
""", unsafe_allow_html=True)

# ── フィルターバー ────────────────────────────────────────
if not filter_ok:
    fb_cls, fb_icon = "filter-bar filter-ng",   "🚫"
elif upcoming_ev and (upcoming_ev[0].dt - now_utc).total_seconds() < 3600:
    fb_cls, fb_icon = "filter-bar filter-warn", "⚠️"
else:
    fb_cls, fb_icon = "filter-bar filter-ok",   "✅"
st.markdown(f'<div class="{fb_cls}">{fb_icon}&nbsp; {filter_msg}</div>',
            unsafe_allow_html=True)


# ── KPI カード 4枚（render_kpi_card 使用） ────────────────
st.markdown('<div class="section-label">リアルタイム状況</div>', unsafe_allow_html=True)
k1, k2, k3, k4 = st.columns(4, gap="medium")

with k1:
    render_kpi_card(
        icon="💱", icon_bg="#EEF2FF", icon_color="#4F46E5",
        label="USD / JPY 現在レート",
        number=f"{price:.2f}", unit="円",
        delta=f"前日比 {chg_sign}{d_chg:.2f} ({chg_sign}{d_pct:.2f}%)" if data_ok else "-",
        delta_class="kpi-delta-up" if d_chg >= 0 else "kpi-delta-down",
        badge="今日" if data_ok else "---",
        badge_class="badge-blue",
    )

with k2:
    adx_lbl  = "トレンド相場" if adx_val >= 30 else ("レンジ相場" if adx_val < 20 else "中間")
    adx_bcls = "badge-emerald" if adx_val >= 30 else ("badge-amber" if adx_val < 20 else "badge-gray")
    render_kpi_card(
        icon="📡", icon_bg="#ECFDF5", icon_color="#059669",
        label="ADX（相場強度）",
        number=f"{adx_val:.1f}", unit="",
        delta=f"RSI {rsi_val:.1f}  ·  ATR {atr_pips:.1f} pips",
        delta_class="kpi-delta-flat",
        badge=adx_lbl, badge_class=adx_bcls,
    )

with k3:
    rsi_lbl  = "売られ過ぎ" if rsi_val < 30 else ("買われ過ぎ" if rsi_val > 70 else "中立")
    rsi_bcls = "badge-emerald" if rsi_val < 30 else ("badge-red" if rsi_val > 70 else "badge-gray")
    render_kpi_card(
        icon="🔄", icon_bg="#FEF3C7", icon_color="#D97706",
        label="RSI（14）",
        number=f"{rsi_val:.1f}", unit="",
        delta=f"BB: {bb_pos.replace('_',' ')}",
        delta_class="kpi-delta-flat",
        badge=rsi_lbl, badge_class=rsi_bcls,
    )

with k4:
    ai_badge_map = {"BUY":("▲ BUY","badge-emerald"),"SELL":("▼ SELL","badge-red"),
                    "HOLD":("HOLD","badge-gray"),"WAIT":("WAIT","badge-amber")}
    ai_lbl, ai_bcls = ai_badge_map.get(action, ("---","badge-gray"))
    render_kpi_card(
        icon="🤖", icon_bg="#EEF2FF", icon_color="#4F46E5",
        label="AI シグナル",
        number=action, unit="",
        delta=f"信頼度: {confidence}  ·  {strategy}",
        delta_class="kpi-delta-flat",
        badge=ai_lbl, badge_class=ai_bcls,
    )

st.markdown("<div style='height:4px;'></div>", unsafe_allow_html=True)


# ── チャート 2カラム ──────────────────────────────────────
st.markdown('<div class="section-label">価格分析チャート</div>', unsafe_allow_html=True)
ch1, ch2 = st.columns([3, 2], gap="medium")

with ch1:
    card_start()
    st.markdown("""
    <div class="card-header">
      <span class="card-title">📈 USD/JPY ローソク足チャート（直近 200 本）</span>
      <span class="card-meta">1h 足 · yfinance</span>
    </div>
    """, unsafe_allow_html=True)

    if data_ok:
        fig = go.Figure()
        disp = df_1h.iloc[-200:]
        if len(disp) >= 20:
            rm = disp["Close"].rolling(20).mean()
            rs = disp["Close"].rolling(20).std()
            for band, c, n in [
                (rm+2*rs, "rgba(79,70,229,0.45)", "BB +2σ"),
                (rm,       "rgba(165,180,252,0.7)","BB Mid"),
                (rm-2*rs,  "rgba(79,70,229,0.45)","BB -2σ"),
            ]:
                fig.add_trace(go.Scatter(
                    x=disp.index, y=band, mode="lines", name=n,
                    line=dict(color=c, width=1, dash="dot"), opacity=0.9,
                ))
        fig.add_trace(go.Candlestick(
            x=disp.index,
            open=disp["Open"], high=disp["High"],
            low=disp["Low"],   close=disp["Close"],
            name="USD/JPY",
            increasing_line_color="#10B981",
            decreasing_line_color="#EF4444",
            increasing_fillcolor="rgba(16,185,129,0.80)",
            decreasing_fillcolor="rgba(239,68,68,0.80)",
        ))
        fig.update_layout(
            height=300,
            margin=dict(l=0, r=0, t=4, b=0),
            paper_bgcolor="white", plot_bgcolor="white",
            showlegend=True,
            legend=dict(orientation="h", y=1.1, x=0,
                        font=dict(size=10, color="#6B7280", family="Inter")),
            xaxis=dict(showgrid=False, rangeslider=dict(visible=False),
                       tickfont=dict(size=9, color="#9CA3AF")),
            yaxis=dict(showgrid=True, gridcolor="#F3F4F6", gridwidth=1,
                       tickfont=dict(size=9, color="#9CA3AF"),
                       title=dict(text="JPY", font=dict(size=9, color="#9CA3AF"))),
            hovermode="x unified",
            font=dict(family="Inter, sans-serif", color="#6B7280"),
        )
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    card_end()

with ch2:
    card_start()
    st.markdown("""
    <div class="card-header">
      <span class="card-title">🤖 AI 判定詳細（XAI）</span>
      <span class="card-meta">最新シグナル</span>
    </div>
    """, unsafe_allow_html=True)

    sig_cls_map2 = {"BUY":"signal-buy","SELL":"signal-sell",
                    "HOLD":"signal-hold","WAIT":"signal-wait"}
    st.markdown(f"""
    <div class="signal-pill {sig_cls_map2.get(action,'signal-hold')}">
      {sig_icon.get(action,"")} &nbsp; {action}
      <span style="font-size:0.8rem;font-weight:500;margin-left:4px;">
        ({confidence})
      </span>
    </div>
    """, unsafe_allow_html=True)

    metrics = [
        ("相場レジーム", f"{regime_icon} {regime}",  "#4F46E5"),
        ("ADX",         f"{adx_val:.1f}",             "#0EA5E9"),
        ("RSI",         f"{rsi_val:.1f}",             "#8B5CF6"),
        ("ATR",         f"{atr_pips:.1f} pips",       "#F59E0B"),
        ("BB 位置",     bb_pos.replace("_"," "),      "#10B981"),
        ("採用戦略",    strategy,                      "#6B7280"),
    ]
    rows = "".join(f"""
    <div class="metric-row">
      <span class="metric-key">{k}</span>
      <span class="metric-val" style="color:{c};">{v}</span>
    </div>""" for k, v, c in metrics)
    st.markdown(f'<div class="metrics-grid">{rows}</div>', unsafe_allow_html=True)

    st.markdown(f'<div class="reason-box">{ai_reason[:280]}{"…" if len(ai_reason)>280 else ""}</div>',
                unsafe_allow_html=True)
    card_end()


# ═══════════════════════════════════════════════════════════
# ── AI WIDGETS ROW ── ゲージ / センチメント / 天気 ─────────
# ═══════════════════════════════════════════════════════════
st.markdown('<div class="section-label">AI インサイト</div>', unsafe_allow_html=True)

conf_pct  = _confidence_pct(confidence, adx_val)
sent_score, sent_label, sent_color = _sentiment_score(rsi_val, adx_val, action)
w_icon, w_status, w_desc, w_chip, w_chip_cls = _weather(adx_val,
    float(signal_data.get("atr", 0.0)), regime)

w1, w2, w3 = st.columns(3, gap="medium")

# ── 1. 勝率予測ゲージ ──────────────────────────────────────
with w1:
    card_start()
    st.markdown("""
    <div class="card-header">
      <span class="card-title">🎯 勝率予測</span>
      <span class="card-meta">AI 信頼度スコア</span>
    </div>
    """, unsafe_allow_html=True)
    render_gauge(conf_pct)
    # スコア帯の凡例
    st.markdown("""
    <div style="display:flex;gap:10px;justify-content:center;
                flex-wrap:wrap;margin-top:4px;font-size:0.72rem;">
      <span style="display:flex;align-items:center;gap:4px;">
        <span style="width:8px;height:8px;border-radius:2px;
                     background:#D1FAE5;display:inline-block;"></span>
        <span style="color:#6B7280;">80–100%: 高</span>
      </span>
      <span style="display:flex;align-items:center;gap:4px;">
        <span style="width:8px;height:8px;border-radius:2px;
                     background:#FEF3C7;display:inline-block;"></span>
        <span style="color:#6B7280;">50–80%: 中</span>
      </span>
      <span style="display:flex;align-items:center;gap:4px;">
        <span style="width:8px;height:8px;border-radius:2px;
                     background:#F3F4F6;display:inline-block;"></span>
        <span style="color:#6B7280;">0–50%: 低</span>
      </span>
    </div>
    """, unsafe_allow_html=True)
    card_end()

# ── 2. 市場心理メーター ─────────────────────────────────────
with w2:
    card_start()
    st.markdown("""
    <div class="card-header">
      <span class="card-title">📊 市場心理メーター</span>
      <span class="card-meta">恐怖・強欲インデックス</span>
    </div>
    """, unsafe_allow_html=True)
    render_sentiment(sent_score, sent_label, sent_color, rsi_val)

    # 補足指標バー
    indicators_sentiment = [
        ("ADX",      adx_val,  100, "#4F46E5"),
        ("RSI",      rsi_val,  100, "#8B5CF6"),
        ("センチメント", sent_score, 100, sent_color),
    ]
    st.markdown("<div style='margin-top:14px;'>", unsafe_allow_html=True)
    for lbl, val, mx, col in indicators_sentiment:
        pct_w = val / mx * 100
        st.markdown(f"""
        <div style="margin-bottom:8px;">
          <div style="display:flex;justify-content:space-between;
                      font-size:0.72rem;color:#9CA3AF;margin-bottom:3px;">
            <span>{lbl}</span><span style="font-weight:600;color:#374151;">{val:.1f}</span>
          </div>
          <div style="background:#F3F4F6;border-radius:999px;height:5px;">
            <div style="background:{col};border-radius:999px;height:5px;
                        width:{pct_w:.1f}%;transition:width 0.5s ease;"></div>
          </div>
        </div>
        """, unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)
    card_end()

# ── 3. 相場天気予報 ─────────────────────────────────────────
with w3:
    card_start()
    st.markdown("""
    <div class="card-header">
      <span class="card-title">🌤️ 相場天気予報</span>
      <span class="card-meta">現在のコンディション</span>
    </div>
    """, unsafe_allow_html=True)
    w_desc_html = w_desc.replace("\n", "<br>")
    _conditions = [
        ("☀️", "トレンド", "ADX≥30: 強トレンド相場"),
        ("🌤️", "穏やか",   "ADX 20–29: 緩やかなトレンド"),
        ("☁️",  "レンジ",   "ADX&lt;20: レンジ相場"),
        ("⛈️", "荒れ相場", "ATR&gt;80p: 高ボラティリティ"),
    ]
    _cond_rows = ""
    for _ico, _chip, _txt in _conditions:
        _active_style = "font-weight:700;color:#1F2937" if w_chip == _chip else "color:#9CA3AF"
        _cond_rows += (
            f'<div style="display:flex;align-items:center;gap:8px;padding:4px 0;'
            f'font-size:0.78rem;{_active_style};">'
            f'<span style="font-size:1rem;">{_ico}</span>{_txt}</div>'
        )

    st.markdown(f"""
    <div style="text-align:center;padding:8px 0 4px;">
      <span class="weather-icon">{w_icon}</span>
      <div class="weather-status">{w_status}</div>
      <div class="weather-desc">{w_desc_html}</div>
      <span class="weather-chip {w_chip_cls}">{w_chip}</span>
    </div>
    <div style="margin-top:18px;padding-top:14px;border-top:1px solid #F3F4F6;">
      <div style="font-size:0.7rem;color:#9CA3AF;font-weight:700;
                  letter-spacing:0.08em;text-transform:uppercase;margin-bottom:8px;">
        コンディション早見表
      </div>
      {_cond_rows}
    </div>
    """, unsafe_allow_html=True)
    card_end()


# ── 4. AI 思考タイムライン ──────────────────────────────────
st.markdown('<div class="section-label">AI 思考プロセス</div>', unsafe_allow_html=True)
card_start()
st.markdown(f"""
<div class="card-header">
  <span class="card-title">🧠 AI 思考ログ — リアルタイム</span>
  <span class="card-meta">{now_jst.strftime('%H:%M:%S JST')} 更新</span>
</div>
""", unsafe_allow_html=True)

thinking_steps = _build_thinking_log(
    signal_data, adx_val, rsi_val,
    float(signal_data.get("atr", 0.0)),
    regime, action, confidence,
    filter_ok, filter_msg, now_jst,
)
render_thinking_timeline(thinking_steps)
card_end()


# ── 重要指標スケジュール ──────────────────────────────────
st.markdown('<div class="section-label">直近の重要指標</div>', unsafe_allow_html=True)
card_start()
st.markdown("""
<div class="card-header">
  <span class="card-title">📅 重要指標スケジュール（直近 48h）</span>
  <span class="card-meta">news_events.json</span>
</div>
""", unsafe_allow_html=True)

if upcoming_ev:
    ev_html = ""
    for ev in upcoming_ev[:8]:
        ejst    = ev.dt.astimezone(JST)
        dm      = (ev.dt - now_utc).total_seconds() / 60
        cd      = f"あと {dm/60:.1f}h" if dm > 60 else f"あと {int(dm)}分"
        imp_map = {"HIGH":("badge badge-red","HIGH"),
                   "MEDIUM":("badge badge-amber","MED")}
        imp_cls, imp_lbl = imp_map.get(ev.impact, ("badge badge-gray", ev.impact))
        ev_html += f"""
        <div class="event-row">
          <div>
            <div class="event-name">{ev.name}</div>
            <div class="event-time">{ejst.strftime('%m/%d %H:%M JST')}</div>
          </div>
          <div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px;">
            <span class="{imp_cls}">{imp_lbl}</span>
            <div class="event-countdown">{cd}</div>
          </div>
        </div>"""
    st.markdown(ev_html, unsafe_allow_html=True)
else:
    st.markdown('<p style="color:#9CA3AF;font-size:0.85rem;margin:0;">直近 48 時間に重要指標はありません</p>',
                unsafe_allow_html=True)
card_end()


# ── XAI ログ ─────────────────────────────────────────────
st.markdown('<div class="section-label">AI 判定ログ</div>', unsafe_allow_html=True)
card_start()
st.markdown(f"""
<div class="card-header">
  <span class="card-title">🗒️ エンジン判定ログ</span>
  <span class="card-meta">直近 {min(len(xai_log),10)} 件 · xai_log.json</span>
</div>
""", unsafe_allow_html=True)

if xai_log:
    log_html = ""
    tag_cls  = {"ENTRY":"tag-entry","CLOSE":"tag-close",
                "UPDATE":"tag-update","SKIP":"tag-skip"}
    for item in reversed(xai_log[-10:]):
        t      = item.get("timestamp","")[:16].replace("T"," ")
        typ    = item.get("type","UPDATE")
        tc     = tag_cls.get(typ,"tag-update")
        reason = item.get("reason","")[:180]
        extra  = ""
        if typ == "ENTRY":
            extra = (f'<span style="font-size:0.78rem;color:#6B7280;margin-left:4px;">'
                     f'@ {item.get("entry_price",0):.3f}'
                     f' | SL {item.get("stop_loss",0):.3f}'
                     f' | TP {item.get("take_profit",0):.3f}'
                     f' [{item.get("regime","")}]</span>')
        elif typ in ("UPDATE","CLOSE"):
            pnl   = item.get("pnl_pips", 0)
            pc    = "xai-pnl-pos" if pnl >= 0 else "xai-pnl-neg"
            extra = f'<span class="xai-pnl {pc}" style="margin-left:4px;">{pnl:+.1f} pips</span>'

        log_html += f"""
        <div class="xai-row">
          <div class="xai-meta">
            <span class="xai-tag {tc}">{typ}</span>
            <span class="xai-time">{t}</span>
            {extra}
          </div>
          <div class="xai-reason">{reason}{"…" if len(item.get("reason",""))>180 else ""}</div>
        </div>"""
    st.markdown(log_html, unsafe_allow_html=True)
    st.markdown(f'<p style="margin:10px 0 0;font-size:0.72rem;color:#9CA3AF;">合計 {len(xai_log)} 件</p>',
                unsafe_allow_html=True)
else:
    st.markdown("""
    <p style="color:#9CA3AF;font-size:0.85rem;margin:0;">
      ログがまだありません。<br>
      <code style="background:#F3F4F6;padding:2px 6px;border-radius:4px;">python3 trading_engine.py</code>
      を実行すると <code>xai_log.json</code> が生成されここに表示されます。
    </p>""", unsafe_allow_html=True)
card_end()

# ── フッター ──────────────────────────────────────────────
st.markdown("""
<p style="text-align:center;color:#D1D5DB;font-size:0.72rem;margin-top:28px;">
  CatHack AI Trader &nbsp;·&nbsp; データ: yfinance (USD/JPY) &nbsp;·&nbsp;
  シグナル: market_analyzer.py &nbsp;·&nbsp; ※実際の取引判断はご自身の責任で行ってください
</p>
""", unsafe_allow_html=True)
