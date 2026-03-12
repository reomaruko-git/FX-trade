"""
oanda_executor.py  ―  OANDA v3 REST API ラッパー
=================================================
OandaExecutor クラスが提供するメソッド:
  - place_order(instrument, units, stop_loss, take_profit)
  - close_position(instrument)
  - get_open_positions() → list[dict]
  - get_open_trade_id(instrument) → str | None
  - replace_stop_loss(trade_id, new_price, instrument) → dict  ← 建値移動用

環境変数（.env から読み込む）:
  OANDA_API_KEY       : アクセストークン
  OANDA_ACCOUNT_ID    : 口座番号（例: 001-009-XXXXXXX-001）
  OANDA_ENVIRONMENT   : "live" または "practice"（デフォルト: live）
"""

import os
import logging
from dotenv import load_dotenv

import pandas as pd
import oandapyV20
import oandapyV20.endpoints.orders      as orders
import oandapyV20.endpoints.positions   as pos_ep
import oandapyV20.endpoints.trades      as trades_ep
import oandapyV20.endpoints.instruments as instr_ep
import oandapyV20.endpoints.accounts   as accounts_ep
from oandapyV20.contrib.requests import (
    MarketOrderRequest,
    TakeProfitDetails,
    StopLossDetails,
)

load_dotenv()
logger = logging.getLogger(__name__)


def _price_fmt(instrument: str, price: float) -> str:
    """OANDA に渡す価格文字列。JPYペアは小数3桁、その他は小数5桁。"""
    if "JPY" in instrument:
        return f"{price:.3f}"
    return f"{price:.5f}"


class OandaExecutor:
    def __init__(self):
        token   = os.environ.get("OANDA_API_KEY", "")
        self.account_id = os.environ.get("OANDA_ACCOUNT_ID", "")
        environment     = os.environ.get("OANDA_ENVIRONMENT", "live")

        if not token:
            raise ValueError("OANDA_API_KEY が .env に設定されていません")
        if not self.account_id:
            raise ValueError("OANDA_ACCOUNT_ID が .env に設定されていません")

        self.client = oandapyV20.API(
            access_token=token,
            environment=environment,
        )
        logger.info(f"[OANDA] 接続完了: {environment} / {self.account_id}")

    # ──────────────────────────────────────────────────────────
    # 発注
    # ──────────────────────────────────────────────────────────
    def place_order(
        self,
        instrument: str,
        units: int,
        stop_loss: float,
        take_profit: float,
    ) -> dict:
        """
        成行注文を発注し SL/TP を設定する。

        Parameters
        ----------
        instrument  : 通貨ペア（例: "USD_JPY"）
        units       : 取引数量（BUY = 正値, SELL = 負値）
        stop_loss   : ストップロス価格
        take_profit : テイクプロフィット価格

        Returns
        -------
        OANDA API のレスポンス dict
        """
        sl_str = _price_fmt(instrument, stop_loss)
        tp_str = _price_fmt(instrument, take_profit)

        data = MarketOrderRequest(
            instrument=instrument,
            units=units,
            stopLossOnFill=StopLossDetails(price=sl_str).data,
            takeProfitOnFill=TakeProfitDetails(price=tp_str).data,
        )

        r = orders.OrderCreate(self.account_id, data=data.data)
        self.client.request(r)

        resp = r.response
        trade_id = (
            resp.get("orderFillTransaction", {}).get("tradeOpened", {}).get("tradeID")
            or resp.get("relatedTransactionIDs", ["?"])[0]
        )
        logger.info(
            f"[OANDA] 発注完了 instrument={instrument} units={units:+,} "
            f"SL={sl_str} TP={tp_str} tradeID={trade_id}"
        )
        return resp

    # ──────────────────────────────────────────────────────────
    # 決済
    # ──────────────────────────────────────────────────────────
    def close_position(self, instrument: str) -> dict:
        """
        指定した通貨ペアのポジションを全決済する（ロング・ショート両対応）。

        Parameters
        ----------
        instrument : 通貨ペア（例: "USD_JPY"）

        Returns
        -------
        OANDA API のレスポンス dict
        """
        # 現在のポジションを確認してロング/ショートを判定
        open_pos = self.get_open_positions()
        target   = next(
            (p for p in open_pos if p.get("instrument") == instrument), None
        )

        if target is None:
            logger.warning(f"[OANDA] close_position: {instrument} のポジションが見つかりません")
            return {}

        units = float(target.get("units", 0))

        if units > 0:
            close_data = {"longUnits": "ALL"}
        elif units < 0:
            close_data = {"shortUnits": "ALL"}
        else:
            logger.warning(f"[OANDA] close_position: {instrument} の units=0")
            return {}

        r = pos_ep.PositionClose(
            self.account_id,
            instrument=instrument,
            data=close_data,
        )
        self.client.request(r)

        resp = r.response
        logger.info(f"[OANDA] 決済完了 instrument={instrument} units={units:+,}")
        return resp

    # ──────────────────────────────────────────────────────────
    # ポジション一覧取得
    # ──────────────────────────────────────────────────────────
    def get_open_positions(self) -> list[dict]:
        """
        オープンポジション一覧を返す。

        Returns
        -------
        list of dict, 各要素:
          {
            "instrument" : "USD_JPY",
            "pair"       : "USD/JPY",
            "units"      : 10000.0,    # BUY=正, SELL=負
            "entry_price": 150.123,
          }
        """
        r = pos_ep.OpenPositions(self.account_id)
        self.client.request(r)

        result = []
        for p in r.response.get("positions", []):
            long_units  = float(p["long"]["units"])
            short_units = float(p["short"]["units"])

            if long_units != 0:
                units       = long_units
                avg_price   = float(p["long"].get("averagePrice", 0))
            elif short_units != 0:
                units       = short_units
                avg_price   = float(p["short"].get("averagePrice", 0))
            else:
                continue  # units=0 はスキップ

            instrument = p["instrument"]
            result.append({
                "instrument" : instrument,
                "pair"       : instrument.replace("_", "/"),
                "units"      : units,
                "entry_price": avg_price,
            })

        return result

    # ──────────────────────────────────────────────────────────
    # トレードID取得
    # ──────────────────────────────────────────────────────────
    def get_open_trade_id(self, instrument: str) -> str | None:
        """
        指定通貨ペアのオープントレードIDを返す。
        建値移動（replace_stop_loss）で使用するトレードIDの取得に利用。

        Parameters
        ----------
        instrument : 通貨ペア（例: "USD_JPY"）

        Returns
        -------
        str のトレードID、見つからない場合は None
        """
        r = trades_ep.OpenTrades(self.account_id)
        self.client.request(r)
        for trade in r.response.get("trades", []):
            if trade.get("instrument") == instrument:
                trade_id = str(trade.get("id", ""))
                logger.info(f"[OANDA] tradeID取得: {instrument} → {trade_id}")
                return trade_id
        return None

    # ──────────────────────────────────────────────────────────
    # ローソク足取得（リアルタイム価格データ）
    # ──────────────────────────────────────────────────────────
    def get_candles(
        self,
        instrument: str,
        granularity: str = "H1",
        count: int = 10,
        include_incomplete: bool = False,
    ) -> pd.DataFrame:
        """
        OANDA から OHLCV ローソク足を取得し DataFrame で返す。

        Parameters
        ----------
        instrument         : 通貨ペア（例: "USD_JPY"）
        granularity        : 時間軸（"H1", "H4", "D" 等）
        count              : 取得本数（最大 5000）
        include_incomplete : 最新の未確定足を含めるか
                             True  → 現在価格取得（H1 等）
                             False → 確定足のみ（H&S 検出・SMA 計算用）

        Returns
        -------
        pd.DataFrame  columns: Open, High, Low, Close, Volume
                      index  : DatetimeIndex（UTC）
        """
        r = instr_ep.InstrumentsCandles(
            instrument,
            params={"count": count, "granularity": granularity, "price": "M"},
        )
        self.client.request(r)

        rows, timestamps = [], []
        for c in r.response.get("candles", []):
            if not include_incomplete and not c.get("complete", True):
                continue   # 未確定足をスキップ
            mid = c.get("mid", {})
            rows.append({
                "Open":   float(mid.get("o", 0)),
                "High":   float(mid.get("h", 0)),
                "Low":    float(mid.get("l", 0)),
                "Close":  float(mid.get("c", 0)),
                "Volume": int(c.get("volume", 0)),
            })
            timestamps.append(pd.Timestamp(c["time"]))

        if not rows:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        df = pd.DataFrame(rows)
        df.index = pd.DatetimeIndex(timestamps, tz="UTC")
        df.index.name = "Datetime"
        return df

    # ──────────────────────────────────────────────────────────
    # 口座サマリー取得
    # ──────────────────────────────────────────────────────────
    def get_account_summary(self) -> dict:
        """
        OANDA 口座の残高・損益・スワップ等を取得して返す。

        Returns
        -------
        dict:
          balance       : 口座残高（確定済み）
          nav           : 純資産額（残高 + 含み損益）
          unrealized_pl : 未実現損益（含み損益）
          realized_pl   : 実現済み損益（累計）
          financing     : スワップ累計（マイナス = 支払い超過）
          open_trades   : オープントレード数
          currency      : 口座通貨（例: "JPY"）
        """
        r = accounts_ep.AccountSummary(self.account_id)
        self.client.request(r)
        a = r.response.get("account", {})

        return {
            "balance":       float(a.get("balance",       0)),
            "nav":           float(a.get("NAV",           0)),
            "unrealized_pl": float(a.get("unrealizedPL",  0)),
            "realized_pl":   float(a.get("pl",            0)),
            "financing":     float(a.get("financing",     0)),
            "open_trades":   int(a.get("openTradeCount",  0)),
            "currency":      a.get("currency", "JPY"),
        }

    # ──────────────────────────────────────────────────────────
    # ストップロス変更（建値移動）
    # ──────────────────────────────────────────────────────────
    def replace_stop_loss(
        self,
        trade_id: str,
        new_price: float,
        instrument: str = "",
    ) -> dict:
        """
        既存トレードのストップロス価格を変更する（建値移動用）。

        Parameters
        ----------
        trade_id   : 対象トレードのID（place_order のレスポンスから取得）
        new_price  : 新しいストップロス価格
        instrument : 通貨ペア（例: "USD_JPY"）。価格フォーマットの判定に使用。

        Returns
        -------
        OANDA API のレスポンス dict
        """
        price_str = _price_fmt(instrument, new_price) if instrument else f"{new_price:.3f}"
        data = {
            "stopLoss": {
                "price":       price_str,
                "timeInForce": "GTC",
            }
        }
        r = trades_ep.TradeCRCDO(self.account_id, tradeID=trade_id, data=data)
        self.client.request(r)
        resp = r.response
        logger.info(
            f"[OANDA] SL変更完了: tradeID={trade_id} 新SL={price_str}"
            f" instrument={instrument or '?'}"
        )
        return resp

    # ──────────────────────────────────────────────────────────
    # トレード詳細取得（外部決済検知用）
    # ──────────────────────────────────────────────────────────
    def get_trade_details(self, trade_id: str) -> dict:
        """
        指定 tradeID の詳細を取得。OPEN / CLOSED どちらでも取得可能。

        Returns
        -------
        dict:
            state        : "OPEN" | "CLOSED" | "CANCELLED"
            close_price  : float  （CLOSED の場合）
            close_reason : str    （"SL" | "TP" | "手動" | "不明"）
        """
        r = trades_ep.TradeDetails(self.account_id, tradeID=trade_id)
        self.client.request(r)
        trade = r.response.get("trade", {})
        state = trade.get("state", "OPEN")

        result: dict = {"state": state}
        if state == "CLOSED":
            result["close_price"] = float(trade.get("averageClosePrice", 0))
            # 決済理由を推定
            # ※ CLOSED後は stopLossOrderID/takeProfitOrderID が None になるため、
            #   ネストされた order オブジェクトの state で判定する
            sl_order = trade.get("stopLossOrder", {})
            tp_order = trade.get("takeProfitOrder", {})
            if tp_order.get("state") == "FILLED":
                result["close_reason"] = "TP"
            elif sl_order.get("state") == "FILLED":
                result["close_reason"] = "SL"
            else:
                result["close_reason"] = "手動"
        return result


# ──────────────────────────────────────────────────────────────
# 動作確認用（python3 oanda_executor.py で実行）
# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        ex = OandaExecutor()

        # ── 口座サマリー ──────────────────────────────────────
        summary = ex.get_account_summary()
        cur = summary["currency"]
        print(f"\n{'='*50}")
        print("  OANDA 口座サマリー")
        print(f"{'='*50}")
        print(f"  口座残高（確定済み） : {summary['balance']:>12,.0f} {cur}")
        print(f"  純資産額（NAV）      : {summary['nav']:>12,.0f} {cur}  ← 含み損益込み")
        print(f"  含み損益             : {summary['unrealized_pl']:>+12,.0f} {cur}")
        print(f"  実現済み損益（累計） : {summary['realized_pl']:>+12,.0f} {cur}")
        print(f"  スワップ累計         : {summary['financing']:>+12,.0f} {cur}")
        print(f"  オープントレード数   : {summary['open_trades']} 件")
        print(f"{'='*50}")

        # ── オープンポジション ───────────────────────────────
        positions = ex.get_open_positions()
        if positions:
            print(f"\n  オープンポジション ({len(positions)}件):")
            for p in positions:
                direction = "BUY" if p["units"] > 0 else "SELL"
                print(f"  {p['pair']}  {direction}  {abs(p['units']):,.0f}通貨"
                      f"  @ {p['entry_price']:.3f}")
        else:
            print("\n  現在オープンポジションはありません。")
        print()

    except Exception as e:
        print(f"\n❌ エラー: {e}")
