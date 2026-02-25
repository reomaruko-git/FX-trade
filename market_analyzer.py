"""
プロ仕様 FX 判定エンジン
========================

機能:
  1. 相場環境認識 (Meta-Strategy)
       ADX(14) で「強いトレンド / レンジ / 様子見」を自動判別し、
       採用する戦略ロジックを動的に切り替える。

  2. 適応型トレーリングストップ (Dynamic Trailing Stop)
       ATR(14) ベースで、ボラティリティに連動してストップラインを更新する。

  3. 説明可能AI (XAI) 出力
       判定根拠・採用指標・信頼度を辞書で返す（Streamlit ダッシュボード連携用）。

主要関数:
  analyze_market(df)                      → シグナル辞書
  manage_position(current_price, pos)     → ポジション更新辞書

依存ライブラリ: pandas, numpy（ta-lib があれば自動使用、なければ内部実装）
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Literal

# ta-lib はオプション（なければ内部実装を使用）
try:
    import talib
    _TALIB_AVAILABLE = True
except ImportError:
    _TALIB_AVAILABLE = False


# =============================================
# 型定義
# =============================================
Regime    = Literal["TREND", "RANGE", "WAIT"]
Action    = Literal["BUY", "SELL", "HOLD", "WAIT"]
Confidence = Literal["High", "Medium", "Low"]

@dataclass
class SignalResult:
    """analyze_market() の戻り値"""
    action:         Action
    reason:         str
    confidence:     Confidence
    regime:         Regime
    strategy_used:  str            # "H&S" / "Bollinger" / "None"
    adx:            float
    adx_trend:      str            # "+DI > -DI (上昇)" etc.
    atr:            float
    atr_pips:       float          # ATR を pips 換算
    rsi:            float
    bb_position:    str            # "ABOVE_UPPER" / "BELOW_LOWER" / "INSIDE"
    entry_sl:       float          # 推奨ストップロス価格
    entry_tp:       float          # 推奨利確価格（ATR × 3.0）
    sma200:         float          # 200SMA値
    above_sma200:   bool           # 価格がSMA200より上か
    timestamp:      str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PositionData:
    """manage_position() に渡す現在ポジション情報"""
    direction:     Literal["BUY", "SELL"]
    entry_price:   float
    stop_loss:     float
    take_profit:   float
    atr_at_entry:  float
    highest_price: float = 0.0    # BUY 時の最高値（トレーリング用）
    lowest_price:  float = 999999 # SELL 時の最安値（トレーリング用）
    units:         int   = 10_000

@dataclass
class PositionUpdate:
    """manage_position() の戻り値"""
    should_close:  bool
    close_reason:  str
    new_stop_loss: float
    trailing_moved: bool          # ストップが動いたか
    pnl_pips:      float          # 現在の含み損益 (pips)
    reason:        str


# =============================================
# 内部インジケーター計算（ta-lib 非依存）
# =============================================
class _Indicators:

    @staticmethod
    def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Average True Range"""
        if _TALIB_AVAILABLE:
            return pd.Series(
                talib.ATR(df["High"].values, df["Low"].values, df["Close"].values, timeperiod=period),
                index=df.index
            )
        high, low, prev_close = df["High"], df["Low"], df["Close"].shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)
        return tr.ewm(span=period, adjust=False).mean()

    @staticmethod
    def adx(df: pd.DataFrame, period: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
        """ADX, +DI, -DI を返す"""
        if _TALIB_AVAILABLE:
            adx_vals = talib.ADX(df["High"].values, df["Low"].values, df["Close"].values, timeperiod=period)
            plus_di  = talib.PLUS_DI(df["High"].values, df["Low"].values, df["Close"].values, timeperiod=period)
            minus_di = talib.MINUS_DI(df["High"].values, df["Low"].values, df["Close"].values, timeperiod=period)
            return (pd.Series(adx_vals, index=df.index),
                    pd.Series(plus_di,  index=df.index),
                    pd.Series(minus_di, index=df.index))

        # pandas 実装
        high, low = df["High"], df["Low"]
        prev_high = high.shift(1)
        prev_low  = low.shift(1)

        plus_dm  = (high - prev_high).clip(lower=0)
        minus_dm = (prev_low - low).clip(lower=0)
        # どちらか大きい方だけを採用
        mask = plus_dm >= minus_dm
        plus_dm  = plus_dm.where(mask, 0)
        minus_dm = minus_dm.where(~mask, 0)

        atr_vals  = _Indicators.atr(df, period)
        plus_di   = 100 * plus_dm.ewm(span=period, adjust=False).mean()  / atr_vals
        minus_di  = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr_vals

        dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
        adx_vals = dx.ewm(span=period, adjust=False).mean()
        return adx_vals, plus_di, minus_di

    @staticmethod
    def rsi(series: pd.Series, period: int = 14) -> pd.Series:
        """RSI"""
        if _TALIB_AVAILABLE:
            return pd.Series(talib.RSI(series.values, timeperiod=period), index=series.index)
        delta    = series.diff()
        gain     = delta.clip(lower=0)
        loss     = (-delta).clip(lower=0)
        avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
        avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return 100 - 100 / (1 + rs)

    @staticmethod
    def bollinger_bands(series: pd.Series, period: int = 20, std: float = 2.0):
        """(upper, mid, lower) を返す"""
        mid   = series.rolling(period).mean()
        sigma = series.rolling(period).std()
        return mid + std * sigma, mid, mid - std * sigma

    @staticmethod
    def sma(series: pd.Series, period: int = 200) -> float:
        """単純移動平均（最新値のみ）"""
        val = series.rolling(period).mean().iloc[-1]
        return float(val) if not np.isnan(val) else float(series.mean())


# =============================================
# 相場環境認識クラス
# =============================================
class MarketRegimeDetector:
    """ADX(14) で相場状態を判別し、採用すべき戦略を選ぶ"""

    def __init__(self, adx_trend: float = 30.0, adx_range: float = 20.0,
                 period: int = 14):
        self.adx_trend = adx_trend   # ADX > this → トレンド
        self.adx_range = adx_range   # ADX < this → レンジ
        self.period    = period

    def detect(self, df: pd.DataFrame) -> dict:
        adx, plus_di, minus_di = _Indicators.adx(df, self.period)

        latest_adx      = float(adx.iloc[-1])
        latest_plus_di  = float(plus_di.iloc[-1])
        latest_minus_di = float(minus_di.iloc[-1])
        adx_slope       = float(adx.iloc[-1] - adx.iloc[-4])  # 3本前との差

        # 相場判定
        if latest_adx > self.adx_trend:
            regime   = "TREND"
            strategy = "H&S"
            regime_reason = (
                f"ADX={latest_adx:.1f}（>{self.adx_trend}）で強いトレンド相場。"
                f"{'上昇' if latest_plus_di > latest_minus_di else '下降'}トレンド "
                f"(+DI={latest_plus_di:.1f}, -DI={latest_minus_di:.1f})。"
                f"H&Sロジック（4時間足）を優先採用。"
            )
            confidence_base = "High" if latest_adx > 40 else "Medium"

        elif latest_adx < self.adx_range:
            regime   = "RANGE"
            strategy = "Bollinger"
            regime_reason = (
                f"ADX={latest_adx:.1f}（<{self.adx_range}）でレンジ相場。"
                f"ボリバン逆張りロジック（1時間足）を優先採用。"
            )
            confidence_base = "Medium"

        else:
            regime   = "WAIT"
            strategy = "None"
            regime_reason = (
                f"ADX={latest_adx:.1f}（{self.adx_range}〜{self.adx_trend}）で移行期。"
                f"ADXは{'上昇中（トレンド形成の可能性）' if adx_slope > 0 else '低下中（レンジ移行の可能性）'}。"
                f"シグナル待ち。"
            )
            confidence_base = "Low"

        return {
            "regime":          regime,
            "strategy":        strategy,
            "adx":             latest_adx,
            "plus_di":         latest_plus_di,
            "minus_di":        latest_minus_di,
            "adx_slope":       adx_slope,
            "adx_trend":       f"+DI>{'-DI（上昇トレンド）' if latest_plus_di > latest_minus_di else '-DI（下降トレンド）'}",
            "regime_reason":   regime_reason,
            "confidence_base": confidence_base,
        }


# =============================================
# シグナル生成クラス
# =============================================
class SignalGenerator:
    """レジームに応じてエントリーシグナルを生成する"""

    def __init__(self, bb_period: int = 14, bb_std: float = 2.5,
                 rsi_period: int = 14):
        self.bb_period = bb_period
        self.bb_std    = bb_std
        self.rsi_period = rsi_period

    def generate(self, df: pd.DataFrame, regime_info: dict,
                 atr_val: float, sma200: float = 0.0) -> dict:
        close = df["Close"]
        upper, mid, lower = _Indicators.bollinger_bands(close, self.bb_period, self.bb_std)
        rsi = _Indicators.rsi(close, self.rsi_period)

        latest_close = float(close.iloc[-1])
        latest_rsi   = float(rsi.iloc[-1])
        latest_upper = float(upper.iloc[-1])
        latest_lower = float(lower.iloc[-1])
        latest_mid   = float(mid.iloc[-1])

        # ── 200SMA トレンドフィルター ──────────────────────
        above_sma200     = latest_close > sma200 if sma200 > 0 else True
        sma_filter_buy   = above_sma200           # SMA上 → BUY 許可
        sma_filter_sell  = not above_sma200       # SMA下 → SELL 許可
        sma_filter_note  = (
            f"200SMA={sma200:.3f} 価格{'↑上' if above_sma200 else '↓下'} "
            f"→ {'BUY許可/SELL禁止' if above_sma200 else 'SELL許可/BUY禁止'}"
        ) if sma200 > 0 else ""

        # BB位置の判定
        if latest_close > latest_upper:
            bb_pos = "ABOVE_UPPER"
        elif latest_close < latest_lower:
            bb_pos = "BELOW_LOWER"
        else:
            bb_pos = "INSIDE"

        regime   = regime_info["regime"]
        strategy = regime_info["strategy"]
        action: Action   = "WAIT"
        signal_reason    = ""
        confidence: Confidence = regime_info["confidence_base"]

        # --- レンジ相場: ボリバン逆張り ---
        if regime == "RANGE":
            if bb_pos == "BELOW_LOWER" and latest_rsi < 35:
                if sma_filter_buy:
                    action = "BUY"
                    signal_reason = (
                        f"終値({latest_close:.3f})が下限BB({latest_lower:.3f})を下回り、"
                        f"RSI={latest_rsi:.1f}で売られすぎ。逆張り買いシグナル。"
                        + (f" {sma_filter_note}" if sma_filter_note else "")
                    )
                    confidence = "High" if latest_rsi < 30 else "Medium"
                else:
                    action = "HOLD"
                    signal_reason = (
                        f"BB下限タッチ・RSI={latest_rsi:.1f}だが"
                        f"200SMA({sma200:.3f})より下のためBUY禁止。"
                    )
                    confidence = "Low"
            elif bb_pos == "ABOVE_UPPER" and latest_rsi > 65:
                if sma_filter_sell:
                    action = "SELL"
                    signal_reason = (
                        f"終値({latest_close:.3f})が上限BB({latest_upper:.3f})を上回り、"
                        f"RSI={latest_rsi:.1f}で買われすぎ。逆張り売りシグナル。"
                        + (f" {sma_filter_note}" if sma_filter_note else "")
                    )
                    confidence = "High" if latest_rsi > 70 else "Medium"
                else:
                    action = "HOLD"
                    signal_reason = (
                        f"BB上限タッチ・RSI={latest_rsi:.1f}だが"
                        f"200SMA({sma200:.3f})より上のためSELL禁止。"
                    )
                    confidence = "Low"
            else:
                action = "HOLD"
                signal_reason = (
                    f"レンジ相場だが BB内部({bb_pos})・RSI={latest_rsi:.1f}でエントリー条件未成立。"
                )
                confidence = "Low"

        # --- トレンド相場: H&S シグナル（シンプル版サマリー） ---
        elif regime == "TREND":
            plus_di  = regime_info["plus_di"]
            minus_di = regime_info["minus_di"]
            if plus_di > minus_di:
                # 上昇トレンド: プルバック買い（MIDバンド付近）
                if latest_close <= latest_mid * 1.001:
                    if sma_filter_buy:
                        action = "BUY"
                        signal_reason = (
                            f"上昇トレンド確認（+DI={plus_di:.1f} > -DI={minus_di:.1f}）。"
                            f"終値({latest_close:.3f})がMA({latest_mid:.3f})付近までプルバック。"
                            f"H&Sロジックと併用してエントリー好機。"
                            + (f" {sma_filter_note}" if sma_filter_note else "")
                        )
                    else:
                        action = "HOLD"
                        signal_reason = (
                            f"上昇トレンド・プルバックだが200SMA({sma200:.3f})より下のためBUY禁止。"
                        )
                        confidence = "Low"
                else:
                    action = "HOLD"
                    signal_reason = (
                        f"上昇トレンドだが終値({latest_close:.3f})がMA({latest_mid:.3f})より上。"
                        f"H&Sの右肩形成を待機中。"
                    )
                    confidence = "Low"
            else:
                # 下降トレンド: プルバック売り
                if latest_close >= latest_mid * 0.999:
                    if sma_filter_sell:
                        action = "SELL"
                        signal_reason = (
                            f"下降トレンド確認（-DI={minus_di:.1f} > +DI={plus_di:.1f}）。"
                            f"終値({latest_close:.3f})がMA({latest_mid:.3f})付近までプルバック。"
                            f"H&Sロジックと併用してエントリー好機。"
                            + (f" {sma_filter_note}" if sma_filter_note else "")
                        )
                    else:
                        action = "HOLD"
                        signal_reason = (
                            f"下降トレンド・プルバックだが200SMA({sma200:.3f})より上のためSELL禁止。"
                        )
                        confidence = "Low"
                else:
                    action = "HOLD"
                    signal_reason = (
                        f"下降トレンドだが終値({latest_close:.3f})がMA({latest_mid:.3f})より下。"
                        f"逆H&Sの右肩形成を待機中。"
                    )
                    confidence = "Low"

        # --- 様子見 ---
        else:
            action = "WAIT"
            signal_reason = "ADXが移行期のためシグナルなし。"
            confidence = "Low"

        # ストップロス・利確の計算
        atr_buffer = atr_val * 2.0
        atr_tp     = atr_val * 3.0
        if action == "BUY":
            entry_sl = round(latest_close - atr_buffer, 3)
            entry_tp = round(latest_close + atr_tp, 3)
        elif action == "SELL":
            entry_sl = round(latest_close + atr_buffer, 3)
            entry_tp = round(latest_close - atr_tp, 3)
        else:
            entry_sl = entry_tp = 0.0

        return {
            "action":       action,
            "signal_reason": signal_reason,
            "confidence":   confidence,
            "bb_position":  bb_pos,
            "rsi":          latest_rsi,
            "entry_sl":     entry_sl,
            "entry_tp":     entry_tp,
            "close":        latest_close,
        }


# =============================================
# メイン判定関数
# =============================================
def analyze_market(df: pd.DataFrame,
                   adx_trend: float = 30.0,
                   adx_range: float = 20.0,
                   bb_period: int = 14,
                   bb_std: float = 2.5,
                   sma_period: int = 200) -> dict:
    """
    相場環境を分析し、エントリーシグナルと根拠を返す。

    引数:
        df        : OHLCV DataFrame（インデックス: DatetimeIndex）
        adx_trend : ADX がこの値を超えたらトレンド判定
        adx_range : ADX がこの値を下回ったらレンジ判定
        bb_period : ボリバンの期間
        bb_std    : ボリバンの標準偏差倍率

    戻り値:
        SignalResult.to_dict() 形式の辞書
        {
          "action":       "BUY" | "SELL" | "HOLD" | "WAIT",
          "reason":       "なぜそう判断したかの根拠テキスト",
          "confidence":   "High" | "Medium" | "Low",
          "regime":       "TREND" | "RANGE" | "WAIT",
          "strategy_used": "H&S" | "Bollinger" | "None",
          "adx": float, "adx_trend": str,
          "atr": float, "atr_pips": float,
          "rsi": float, "bb_position": str,
          "entry_sl": float, "entry_tp": float,
          "timestamp": str (ISO 8601),
        }
    """
    if len(df) < 30:
        return {
            "action": "WAIT", "reason": "データ不足（30本以上必要）",
            "confidence": "Low", "regime": "WAIT",
            "strategy_used": "None",
        }

    # --- 各指標を計算 ---
    atr_series = _Indicators.atr(df, 14)
    atr_val    = float(atr_series.iloc[-1])
    atr_pips   = round(atr_val * 100, 2)  # JPY pair

    # --- 200SMA 計算 ---
    sma200 = _Indicators.sma(df["Close"], sma_period)

    # --- 相場環境判別 ---
    detector    = MarketRegimeDetector(adx_trend, adx_range)
    regime_info = detector.detect(df)

    # --- シグナル生成（SMAフィルター付き） ---
    generator  = SignalGenerator(bb_period, bb_std)
    signal     = generator.generate(df, regime_info, atr_val, sma200=sma200)

    # --- XAI 根拠文を組み立て ---
    full_reason = (
        f"【相場環境】{regime_info['regime_reason']} "
        f"【シグナル】{signal['signal_reason']} "
        f"【リスク管理】ATR={atr_pips:.1f}pips → SL={signal['entry_sl']}, TP={signal['entry_tp']}"
    )

    result = SignalResult(
        action        = signal["action"],
        reason        = full_reason,
        confidence    = signal["confidence"],
        regime        = regime_info["regime"],
        strategy_used = regime_info["strategy"],
        adx           = round(regime_info["adx"], 2),
        adx_trend     = regime_info["adx_trend"],
        atr           = round(atr_val, 4),
        atr_pips      = atr_pips,
        rsi           = round(signal["rsi"], 1),
        bb_position   = signal["bb_position"],
        entry_sl      = signal["entry_sl"],
        entry_tp      = signal["entry_tp"],
        sma200        = round(sma200, 3),
        above_sma200  = float(df["Close"].iloc[-1]) > sma200,
        timestamp     = datetime.now().isoformat(timespec="seconds"),
    )
    return result.to_dict()


# =============================================
# ポジション管理関数（適応型トレーリングストップ）
# =============================================
def manage_position(current_price: float,
                    position: PositionData,
                    atr_multiplier: float = 2.0) -> dict:
    """
    既存ポジションを評価し、ストップ更新・決済判断を行う。

    引数:
        current_price  : 現在の価格
        position       : PositionData（エントリー情報）
        atr_multiplier : ATR × この倍率でストップを計算

    戻り値:
        PositionUpdate.to_dict() 形式の辞書
        {
          "should_close":  bool,
          "close_reason":  str,
          "new_stop_loss": float,
          "trailing_moved": bool,
          "pnl_pips":      float,
          "reason":        str,
        }
    """
    atr_buffer     = position.atr_at_entry * atr_multiplier
    trailing_moved = False
    should_close   = False
    close_reason   = ""

    if position.direction == "BUY":
        # 含み損益 (pips)
        pnl_pips = (current_price - position.entry_price) * 100

        # 最高値の更新
        new_highest  = max(position.highest_price, current_price)
        new_stop     = round(new_highest - atr_buffer, 3)

        # ストップが上昇したか
        if new_stop > position.stop_loss:
            trailing_moved = True

        # 決済判断
        if current_price <= new_stop:
            should_close = True
            close_reason = (
                f"トレーリングストップ到達。現在値={current_price:.3f} ≤ SL={new_stop:.3f}。"
                f"最高値={new_highest:.3f} - ATR×{atr_multiplier}={atr_buffer:.3f}。"
            )
        elif current_price >= position.take_profit:
            should_close = True
            close_reason = (
                f"利確ライン到達。現在値={current_price:.3f} ≥ TP={position.take_profit:.3f}。"
            )

        reason = (
            f"{'🔺 ストップを' + str(new_stop) + 'に引き上げ。' if trailing_moved else 'ストップ変更なし。'}"
            f"含み損益: {pnl_pips:+.1f}pips。"
            f"最高値={new_highest:.3f}、現在SL={new_stop:.3f}、TP={position.take_profit:.3f}。"
        )

    else:  # SELL
        pnl_pips = (position.entry_price - current_price) * 100

        new_lowest = min(position.lowest_price, current_price)
        new_stop   = round(new_lowest + atr_buffer, 3)

        if new_stop < position.stop_loss:
            trailing_moved = True

        if current_price >= new_stop:
            should_close = True
            close_reason = (
                f"トレーリングストップ到達。現在値={current_price:.3f} ≥ SL={new_stop:.3f}。"
                f"最安値={new_lowest:.3f} + ATR×{atr_multiplier}={atr_buffer:.3f}。"
            )
        elif current_price <= position.take_profit:
            should_close = True
            close_reason = (
                f"利確ライン到達。現在値={current_price:.3f} ≤ TP={position.take_profit:.3f}。"
            )

        reason = (
            f"{'🔻 ストップを' + str(new_stop) + 'に引き下げ。' if trailing_moved else 'ストップ変更なし。'}"
            f"含み損益: {pnl_pips:+.1f}pips。"
            f"最安値={new_lowest:.3f}、現在SL={new_stop:.3f}、TP={position.take_profit:.3f}。"
        )

    update = PositionUpdate(
        should_close   = should_close,
        close_reason   = close_reason,
        new_stop_loss  = new_stop,
        trailing_moved = trailing_moved,
        pnl_pips       = round(pnl_pips, 1),
        reason         = reason if not should_close else close_reason,
    )
    return asdict(update)


# =============================================
# 動作確認用デモ
# =============================================
if __name__ == "__main__":
    import yfinance as yf
    from datetime import timedelta

    print("=" * 60)
    print("  market_analyzer.py  動作確認デモ")
    print(f"  ta-lib: {'利用可能 ✅' if _TALIB_AVAILABLE else '未インストール（内部実装を使用）'}")
    print("=" * 60)

    # データ取得
    end   = datetime.now()
    start = end - timedelta(days=60)
    df = yf.download("USDJPY=X", start=start, end=end, interval="1h",
                     progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    print(f"\n📥 USD/JPY 1時間足 {len(df)} 本取得完了\n")

    # --- 1. analyze_market のデモ ---
    signal = analyze_market(df)

    print("【 analyze_market() の結果 】")
    print(f"  action       : {signal['action']}")
    print(f"  confidence   : {signal['confidence']}")
    print(f"  regime       : {signal['regime']}  (ADX={signal['adx']:.1f})")
    print(f"  strategy_used: {signal['strategy_used']}")
    print(f"  ATR          : {signal['atr_pips']:.1f} pips")
    print(f"  RSI          : {signal['rsi']:.1f}")
    print(f"  BB position  : {signal['bb_position']}")
    print(f"  entry SL     : {signal['entry_sl']}")
    print(f"  entry TP     : {signal['entry_tp']}")
    print(f"\n  reason:\n    {signal['reason']}\n")

    # --- 2. manage_position のデモ ---
    close_price = float(df["Close"].iloc[-1])
    atr_val     = float(_Indicators.atr(df).iloc[-1])

    pos = PositionData(
        direction     = "BUY",
        entry_price   = close_price - 0.30,
        stop_loss     = close_price - 0.30 - atr_val * 2,
        take_profit   = close_price - 0.30 + atr_val * 3,
        atr_at_entry  = atr_val,
        highest_price = close_price,
        units         = 10_000,
    )

    update = manage_position(close_price, pos)
    print("【 manage_position() の結果 】")
    print(f"  should_close   : {update['should_close']}")
    print(f"  trailing_moved : {update['trailing_moved']}")
    print(f"  new_stop_loss  : {update['new_stop_loss']}")
    print(f"  pnl_pips       : {update['pnl_pips']:+.1f}")
    print(f"\n  reason:\n    {update['reason']}")
