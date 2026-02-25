"""
トレードフィルター
==================

エントリーをブロックする条件を一元管理するクラス。

フィルター一覧:
  1. スプレッドフィルター
       現在のスプレッドが閾値を超えていたらエントリー禁止。
       （スプレッド拡大 = ボラ急騰・流動性枯渇のサイン）

  2. 重要経済指標フィルター
       指定した発表時刻の前後N分はエントリー禁止。
       news_events.json から自動読み込み可能。

使い方:
  from trade_filter import TradeFilter

  f = TradeFilter(max_spread_pips=1.5, news_buffer_min=30)
  f.load_news_from_json("news_events.json")

  ok, reason = f.is_tradeable(now, spread_pips=0.4)
  if not ok:
      print(f"エントリー見送り: {reason}")
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


# =============================================
# ニュースイベント
# =============================================
@dataclass
class NewsEvent:
    """経済指標イベント 1件"""
    dt:     datetime        # 発表時刻（タイムゾーン付き推奨）
    name:   str             # イベント名
    impact: str = "HIGH"    # HIGH / MEDIUM / LOW

    def is_blackout(self, now: datetime, buffer_min: int) -> bool:
        """now が発表前後 buffer_min 分以内なら True"""
        now_aware = _ensure_aware(now)
        dt_aware  = _ensure_aware(self.dt)
        delta = abs((now_aware - dt_aware).total_seconds()) / 60
        return delta <= buffer_min


# =============================================
# メインフィルタークラス
# =============================================
class TradeFilter:
    """
    スプレッド + 重要指標 のダブルフィルター。

    パラメータ:
        max_spread_pips : これを超えるスプレッドではエントリー禁止
        news_buffer_min : 指標発表前後この分数はエントリー禁止
        block_impacts   : フィルター対象のインパクトレベル（デフォルト HIGH のみ）
    """

    def __init__(
        self,
        max_spread_pips: float       = 1.5,
        news_buffer_min: int         = 30,
        block_impacts:   list[str]   = None,
    ):
        self.max_spread_pips = max_spread_pips
        self.news_buffer_min = news_buffer_min
        self.block_impacts   = block_impacts or ["HIGH"]
        self._events: list[NewsEvent] = []

    # ------------------------------------------------------------------
    # イベント登録
    # ------------------------------------------------------------------
    def add_event(self, dt: datetime, name: str, impact: str = "HIGH") -> None:
        """イベントを手動で追加する"""
        self._events.append(NewsEvent(dt=dt, name=name, impact=impact))

    def load_news_from_json(self, path: str) -> int:
        """
        JSON ファイルからイベントを一括読み込みする。

        フォーマット:
          [
            {"datetime": "2026-03-07T22:30:00+09:00", "name": "米雇用統計(NFP)", "impact": "HIGH"},
            ...
          ]

        戻り値: 読み込んだ件数
        """
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return 0

        count = 0
        for item in data:
            try:
                dt_str = item["datetime"]
                # タイムゾーン指定がない場合は JST とみなす
                if "+" not in dt_str and "Z" not in dt_str:
                    dt = datetime.fromisoformat(dt_str).replace(tzinfo=JST)
                else:
                    dt = datetime.fromisoformat(dt_str)
                self._events.append(NewsEvent(
                    dt     = dt,
                    name   = item.get("name", "不明"),
                    impact = item.get("impact", "HIGH").upper(),
                ))
                count += 1
            except (KeyError, ValueError):
                continue
        return count

    def get_events(self) -> list[NewsEvent]:
        return list(self._events)

    def upcoming_events(self, now: datetime, hours: int = 24) -> list[NewsEvent]:
        """now から hours 時間以内に発表されるイベントを返す"""
        now_a = _ensure_aware(now)
        limit = now_a + timedelta(hours=hours)
        return [e for e in self._events
                if _ensure_aware(e.dt) >= now_a
                and _ensure_aware(e.dt) <= limit]

    # ------------------------------------------------------------------
    # フィルター判定
    # ------------------------------------------------------------------
    def check_spread(self, spread_pips: float) -> tuple[bool, str]:
        """
        スプレッドチェック。
        戻り値: (通過=True, 理由テキスト)
        """
        if spread_pips > self.max_spread_pips:
            reason = (
                f"スプレッド拡大フィルター発動。"
                f"現在スプレッド={spread_pips:.2f}pips > 閾値={self.max_spread_pips}pips。"
                f"流動性低下またはボラ急騰の可能性。エントリー見送り。"
            )
            return False, reason
        return True, f"スプレッド={spread_pips:.2f}pips（閾値以内）"

    def check_news(self, now: datetime) -> tuple[bool, str]:
        """
        重要指標フィルター。
        対象インパクトのイベントが前後 news_buffer_min 分以内なら False。
        戻り値: (通過=True, 理由テキスト)
        """
        now_aware = _ensure_aware(now)
        for event in self._events:
            if event.impact not in self.block_impacts:
                continue
            if event.is_blackout(now_aware, self.news_buffer_min):
                dt_aware = _ensure_aware(event.dt)
                diff_min = (dt_aware - now_aware).total_seconds() / 60
                timing   = (
                    f"発表{abs(diff_min):.0f}分前" if diff_min > 0
                    else f"発表{abs(diff_min):.0f}分後"
                )
                reason = (
                    f"重要指標フィルター発動。"
                    f"「{event.name}」({event.impact}) が {timing}。"
                    f"前後{self.news_buffer_min}分はエントリー禁止。"
                )
                return False, reason
        return True, "重要指標なし（直近±30分）"

    def is_tradeable(
        self,
        now:          datetime,
        spread_pips:  Optional[float] = None,
    ) -> tuple[bool, str]:
        """
        全フィルターを実行して総合判定を返す。

        引数:
            now         : 現在時刻
            spread_pips : 現在のスプレッド（pips）。None なら スプレッドチェックをスキップ。

        戻り値:
            (True=エントリー可, 理由テキスト)
        """
        # スプレッドチェック
        if spread_pips is not None:
            ok, reason = self.check_spread(spread_pips)
            if not ok:
                return False, reason

        # 重要指標チェック
        ok, reason = self.check_news(now)
        if not ok:
            return False, reason

        return True, "全フィルター通過。エントリー可能。"

    def summary(self) -> str:
        """設定サマリーを文字列で返す"""
        lines = [
            f"TradeFilter 設定:",
            f"  スプレッド上限  : {self.max_spread_pips} pips",
            f"  指標ブロック幅  : 前後 {self.news_buffer_min} 分",
            f"  対象インパクト  : {', '.join(self.block_impacts)}",
            f"  登録イベント数  : {len(self._events)} 件",
        ]
        return "\n".join(lines)


# =============================================
# ユーティリティ
# =============================================
def _ensure_aware(dt: datetime) -> datetime:
    """naive な datetime を JST として扱う"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=JST)
    return dt
