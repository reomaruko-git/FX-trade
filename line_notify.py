"""
line_notify.py — LINE Messaging API 通知モジュール
====================================================
・エントリー／決済／エラーをLINEグループへ送信
・.env の LINE_CHANNEL_TOKEN / LINE_GROUP_ID を自動読み込み

【グループIDの取得方法】
  1. LINE Developers Console でボットを作成し、Messaging APIを有効化
  2. Webhook URL に https://webhook.site/（ランダムURL）を設定
  3. ボットをグループに招待
  4. グループ内で誰かがメッセージを送る
  5. webhook.site に届いたJSONの "source" > "groupId" をコピー
     （例: C1234567890abcdef1234567890abcdef）
  6. .env に LINE_GROUP_ID=C1234... と記載

使い方:
    # 接続テスト
    python3 line_notify.py --test-line

    # フルテスト
    python3 line_notify.py

    # コードから呼ぶ場合
    from line_notify import notify_entry, notify_close, notify_error
"""

import os
import json
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

# ─────────────────────────────────────────────────────────────
# ▌ .env 読み込み（python-dotenv なしでも動く）
# ─────────────────────────────────────────────────────────────
def _load_env():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())

_load_env()

JST      = ZoneInfo("Asia/Tokyo")
LINE_API = "https://api.line.me/v2/bot/message/push"


# ─────────────────────────────────────────────────────────────
# ▌ 低レベル送信
# ─────────────────────────────────────────────────────────────
def send_line(message: str,
              token: str | None = None,
              group_id: str | None = None) -> bool:
    """
    LINE Messaging API でグループにテキストメッセージを送る。
    token    省略時: 環境変数 LINE_CHANNEL_TOKEN
    group_id 省略時: 環境変数 LINE_GROUP_ID  （例: C1234567890abcdef...）
    成功: True / 失敗: False
    """
    tok = token    or os.environ.get("LINE_CHANNEL_TOKEN", "")
    gid = group_id or os.environ.get("LINE_GROUP_ID",      "")

    if not tok:
        print("[LINE] ⚠️  LINE_CHANNEL_TOKEN が未設定です。.env を確認してください。")
        return False
    if not gid:
        print("[LINE] ⚠️  LINE_GROUP_ID が未設定です。.env を確認してください。")
        print("       取得方法: ボットをグループに招待 → webhook.site で groupId を確認")
        return False

    payload = {
        "to": gid,
        "messages": [{"type": "text", "text": message}]
    }
    headers = {
        "Authorization": f"Bearer {tok}",
        "Content-Type":  "application/json",
    }

    try:
        resp = requests.post(LINE_API, headers=headers,
                             data=json.dumps(payload), timeout=10)
        if resp.status_code == 200:
            print(f"[LINE] ✅ 送信成功: {message[:60]}{'…' if len(message)>60 else ''}")
            return True
        else:
            print(f"[LINE] ❌ エラー {resp.status_code}: {resp.text}")
            return False
    except requests.RequestException as e:
        print(f"[LINE] ❌ 通信エラー: {e}")
        return False


# ─────────────────────────────────────────────────────────────
# ▌ 高レベル通知テンプレート
# ─────────────────────────────────────────────────────────────
def _now_jst() -> str:
    return datetime.now(JST).strftime("%Y/%m/%d %H:%M JST")


# ペアごとの国旗絵文字
_PAIR_FLAGS = {
    "USD/JPY": "🇺🇸🇯🇵",
    "GBP/JPY": "🇬🇧🇯🇵",
    "EUR/JPY": "🇪🇺🇯🇵",
    "AUD/JPY": "🇦🇺🇯🇵",
    "EUR/USD": "🇪🇺🇺🇸",
}

def notify_entry(
    direction: str,
    price: float,
    stop_loss: float,
    take_profit: float,
    strategy: str,
    reason: str = "",
    lot: int = 1000,
    pair: str = "USD/JPY",
) -> bool:
    """エントリー通知"""
    sl_pips  = round((price - stop_loss)    * 100, 1) if direction == "BUY" \
               else round((stop_loss  - price) * 100, 1)
    tp_pips  = round((take_profit - price)  * 100, 1) if direction == "BUY" \
               else round((price - take_profit) * 100, 1)
    rr       = round(tp_pips / sl_pips, 1) if sl_pips > 0 else 0
    exp_jpy  = int(tp_pips * lot / 100)   # 期待利益（円）
    icon     = "📈" if direction == "BUY" else "📉"
    label    = "(買)ロング" if direction == "BUY" else "(売)ショート"
    flag     = _PAIR_FLAGS.get(pair, "")

    # 根拠をカンマ区切りで箇条書きに変換
    bullets = ""
    if reason:
        items = [r.strip() for r in reason.replace("、", ",").split(",") if r.strip()]
        bullets = "\n".join(f"・{item}" for item in items)

    msg = (
        f"{icon} {label}エントリー！！ 🚀\n"
        f"ペア   : {flag} {pair}\n"
        f"レート : {price:.3f}\n"
        f"────────────────────\n"
        f"💰 見込 : +{exp_jpy:,}円 (RR 1:{rr})\n"
        f"🛑 SL  : {stop_loss:.3f}  (-{sl_pips:.1f} pips)\n"
        f"🎯 TP  : {take_profit:.3f}  (+{tp_pips:.1f} pips)\n"
        f"────────────────────\n"
        f"🔍 戦略 : {strategy}\n"
        + (f"📊 根拠 :\n{bullets}\n" if bullets else "")
        + f"────────────────────\n"
        f"📅 {_now_jst()}"
    )
    return send_line(msg)


def notify_close(
    direction: str,
    entry_price: float,
    close_price: float,
    pnl_pips: float,
    reason: str = "",
    lot: int = 1000,
    pair: str = "USD/JPY",
    daily_pips: float = 0.0,
    daily_jpy: int = 0,
    total_pips: float = 0.0,
    balance: int = 0,
) -> bool:
    """決済通知"""
    win        = pnl_pips >= 0
    pnl_jpy    = int(pnl_pips * lot / 100)
    flag       = _PAIR_FLAGS.get(pair, "")
    dir_label  = "(買)" if direction == "BUY" else "(売)"
    pips_emoji = "✨" if win else "💧"
    header     = "💰 決済完了！ おめでとうございます 🎊" if win else "❌ 決済完了 😢"

    # 運用実績セクション（balance が設定されている場合のみ表示）
    stats_section = ""
    if balance > 0:
        stats_section = (
            f"────────────────────\n"
            f"📈 【運用実績】\n"
            f"📅 本日合計 : {daily_pips:+.1f} pips  (計 {daily_jpy:+,}円)\n"
            f"🏆 全期間   : {total_pips:+.1f} pips\n"
            f"💰 口座残高 : {balance:,}円\n"
        )

    msg = (
        f"{header}\n"
        f"────────────────────\n"
        f"{flag} {pair}  {dir_label}\n"
        f"--------------------\n"
        f"🏁 決済 : {close_price:.3f}\n"
        f"🛫 始値 : {entry_price:.3f}\n"
        f"--------------------\n"
        f"📊 損益 : {pnl_pips:+.1f} pips {pips_emoji}\n"
        f"💰 収支 : {pnl_jpy:+,}円\n"
        f"🎯 理由 : {reason}\n"
        + stats_section
        + f"────────────────────\n"
        f"📅 {_now_jst()}"
    )
    return send_line(msg)


def notify_skip(reason: str, filter_type: str = "フィルター") -> bool:
    """スキップ通知（重要指標・スプレッド拡大等）"""
    msg = (
        f"⏸️ トレードスキップ [{filter_type}]\n"
        f"理由 : {reason[:120]}\n"
        f"────────────\n"
        f"{_now_jst()}"
    )
    return send_line(msg)


def notify_signal(action: str, confidence: str,
                  strategy: str, reason: str = "") -> bool:
    """シグナル検知通知"""
    icon_map = {"BUY":"📈","SELL":"📉","HOLD":"⏸","WAIT":"⏳"}
    msg = (
        f"{icon_map.get(action,'🤖')} シグナル検知\n"
        f"アクション: {action}\n"
        f"信頼度    : {confidence}\n"
        f"戦略      : {strategy}\n"
        + (f"補足      : {reason[:80]}\n" if reason else "")
        + f"────────────\n"
        f"{_now_jst()}"
    )
    return send_line(msg)


def notify_error(error_msg: str) -> bool:
    """エラー通知"""
    msg = (
        f"🚨 エラー発生\n"
        f"{error_msg[:200]}\n"
        f"────────────\n"
        f"{_now_jst()}"
    )
    return send_line(msg)


def send_heartbeat(stats: dict) -> bool:
    """ハートビート通知（毎日7:00・21:00 JST に自動送信）"""
    daily_pips = stats.get("daily_pips", 0.0)
    daily_jpy  = stats.get("daily_jpy",  0)
    total_pips = stats.get("total_pips", 0.0)
    balance    = stats.get("balance",    0)

    now_h    = datetime.now(JST).hour
    greeting = "☀️ おはようございます" if now_h < 12 else "🌙 こんばんは"

    msg = (
        f"💓 {greeting}  稼働中\n"
        f"────────────────────\n"
        f"📅 本日 : {daily_pips:+.1f} pips  ({daily_jpy:+,}円)\n"
        f"🏆 累計 : {total_pips:+.1f} pips\n"
        f"💰 残高 : {balance:,}円\n"
        f"────────────────────\n"
        f"{_now_jst()}"
    )
    return send_line(msg)


def send_weekly_report(
    weekly_pips:  float,
    weekly_jpy:   int,
    trade_count:  int,
    win_count:    int,
    balance:      int,
) -> bool:
    """週次レポート通知（金曜クローズ後に自動送信）"""
    win_rate     = round(win_count / trade_count * 100, 1) if trade_count > 0 else 0.0
    lose_count   = trade_count - win_count
    result_emoji = "🎊" if weekly_pips >= 0 else "😢"

    msg = (
        f"📊 週次レポート {result_emoji}\n"
        f"────────────────────\n"
        f"📈 週間損益 : {weekly_pips:+.1f} pips\n"
        f"💰 週間収支 : {weekly_jpy:+,}円\n"
        f"────────────────────\n"
        f"🔢 トレード : {trade_count}回\n"
        f"🏆 勝率     : {win_rate}%  ({win_count}勝{lose_count}敗)\n"
        f"💰 口座残高 : {balance:,}円\n"
        f"────────────────────\n"
        f"{_now_jst()}"
    )
    return send_line(msg)


# ─────────────────────────────────────────────────────────────
# ▌ 単体テスト
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="LINE Messaging API 通知テスト")
    parser.add_argument(
        "--test-line", action="store_true",
        help="接続テストのみ実行（簡易確認用）"
    )
    args = parser.parse_args()

    tok = os.environ.get("LINE_CHANNEL_TOKEN", "")
    gid = os.environ.get("LINE_GROUP_ID", "")

    if not tok:
        print("\n⚠️  LINE_CHANNEL_TOKEN が未設定です。")
        print("   .env に追加してください: LINE_CHANNEL_TOKEN=（チャンネルアクセストークン）")
        exit(1)
    if not gid:
        print("\n⚠️  LINE_GROUP_ID が未設定です。")
        print("   .env に追加してください: LINE_GROUP_ID=Cxxxxxxx...")
        print()
        print("   【グループIDの取得手順】")
        print("   1. LINE Developers Console > Messaging API > Webhook URL を設定")
        print("      （例: https://webhook.site/xxxx-xxxx）")
        print("   2. ボットをグループに招待")
        print("   3. グループ内で誰かがメッセージを送る")
        print("   4. webhook.site のJSONから source.groupId をコピー")
        print("      （例: C1234567890abcdef1234567890abcdef）")
        exit(1)

    print(f"\nトークン  : ...{tok[-8:]} （末尾8文字）")
    print(f"グループID: {gid[:6]}...{gid[-4:]}\n")

    # ─── --test-line: 簡易接続テストのみ ───────────────────────
    if args.test_line:
        print("接続テスト送信中...")
        ok = send_line(
            f"🌙 FX Trade Luce\n"
            f"━━━━━━━━━━━━\n"
            f"✅ LINE 接続テスト成功！\n"
            f"システムが正常に動作しています。\n"
            f"────────────\n"
            f"{_now_jst()}"
        )
        print("✅ 完了！LINEを確認してください。" if ok else "❌ 送信失敗")
        exit(0 if ok else 1)

    # ─── フルテスト（引数なしで実行） ──────────────────────────
    print("=" * 50)
    print("  LINE Messaging API フルテスト")
    print("=" * 50)

    # ① 接続テスト
    print("① 接続テスト送信中...")
    send_line(
        f"🌙 FX Trade Luce\n"
        f"━━━━━━━━━━━━\n"
        f"✅ LINE 接続テスト成功！\n"
        f"システムが正常に動作しています。\n"
        f"────────────\n"
        f"{_now_jst()}"
    )

    # ② エントリー通知サンプル
    print("\n② エントリー通知テスト...")
    notify_entry(
        direction="BUY",
        price=150.250,
        stop_loss=149.750,
        take_profit=151.250,
        strategy="BB逆張り (1h)",
        reason="RSI=28.3 (売られすぎ), BB下限タッチ, ADX=18.5 (レンジ継続)",
        lot=1000,
        pair="USD/JPY",
    )

    # ③ 決済通知サンプル
    print("\n③ 決済通知テスト...")
    notify_close(
        direction="BUY",
        entry_price=150.250,
        close_price=151.050,
        pnl_pips=80.0,
        reason="TP到達 (利確)",
        lot=1000,
        pair="USD/JPY",
        daily_pips=125.0,
        daily_jpy=12500,
        total_pips=1580.5,
        balance=1250480,
    )

    # ④ ハートビートサンプル
    print("\n④ ハートビートテスト...")
    send_heartbeat({
        "daily_pips": 45.5,
        "daily_jpy":  4550,
        "total_pips": 1580.5,
        "balance":    1250480,
    })

    # ⑤ 週次レポートサンプル
    print("\n⑤ 週次レポートテスト...")
    send_weekly_report(
        weekly_pips  = 125.0,
        weekly_jpy   = 12500,
        trade_count  = 8,
        win_count    = 5,
        balance      = 1262980,
    )

    print("\n✅ テスト完了！LINEを確認してください。")
