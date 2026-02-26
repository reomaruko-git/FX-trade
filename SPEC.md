# FX Trade Luce — 現場仕様書

> 最終更新: 2026-02-25（初期資金・ログローテーション更新）

---

## ファイル構成

```
FX-trade/
├── auto_trader.py       # メイン司令塔（ループ・発注・ポジション管理）
├── oanda_executor.py    # OANDA v3 API ラッパー（発注・決済・SL変更・ポジション取得）
├── market_analyzer.py   # 相場分析エンジン（ダッシュボード用）
├── line_notify.py       # LINE通知モジュール（グループ対応）
├── trade_filter.py      # トレードフィルター（スプレッド・重要指標）
├── dashboard.py         # Streamlit ダッシュボード
├── news_events.json     # 重要指標スケジュール
├── setup_check.py       # 環境チェックスクリプト
├── check_account.py     # OANDAアカウントID確認スクリプト（初期設定用）
├── requirements.txt     # Python依存ライブラリ
├── COMMANDS.md          # よく使うコマンド集
├── .env                 # APIキー・設定（Git管理外）
└── .env.example         # .envのテンプレート
```

---

## .env 設定項目

```env
# OANDA
OANDA_API_KEY=xxxxxxxxxxxxxxxxxxxx
OANDA_ACCOUNT_ID=001-009-XXXXXXX-XXX   # check_account.py で確認
OANDA_ENVIRONMENT=live                  # live / practice

# LINE Messaging API（グループ送信）
LINE_CHANNEL_TOKEN=xxxxxxxxxxxx
LINE_GROUP_ID=Cxxxxxxxxxxxxxxxx         # Cで始まるグループID

# 運用設定
INITIAL_BALANCE=500000           # 初期資金（円）
DRY_RUN=false                    # false=OANDA本番発注 / true=シグナルログのみ
LOOP_SEC=300                     # チェック間隔（秒）
LOT=10000                        # 発注ロット（1万通貨）

# 動作パラメータ（省略時はデフォルト値）
MAX_RETRY=3                      # APIリトライ回数
SMA_PERIOD=200                   # トレンドフィルター用SMA期間
SPREAD_PIPS=0.4                  # スプレッド基準値
```

---

## 監視ペア

| ペア | yfinance ticker | 戦略 |
|------|----------------|------|
| USD/JPY | `USDJPY=X` | BB逆張り(1h) + H&S(4h) |
| GBP/JPY | `GBPJPY=X` | BB逆張り(1h) + H&S(4h) |

1ポジションのみ保有（どちらかのペアでエントリーしたら、もう片方はスキップ）。

---

## 戦略ロジック

### A. BB逆張り（1時間足）

| 項目 | 設定 |
|------|------|
| ライブラリ | pandas_ta |
| インジケーター | Bollinger Bands（期間14、σ=2.5）+ RSI（期間14） |
| BUYエントリー | 終値 ≤ BB下限 かつ RSI < 30 |
| SELLエントリー | 終値 ≥ BB上限 かつ RSI > 70 |
| SL | エントリー ± ATR × 3.0（変動値） |
| TP | エントリー ∓ ATR × 6.0（RR 1:2） |

### B. H&Sパターン（4時間足）

| 項目 | 設定 |
|------|------|
| 検知ライブラリ | scipy.signal.find_peaks |
| パターン | ヘッドアンドショルダー（天井）→ SELL |
|          | 逆ヘッドアンドショルダー（底）→ BUY |
| 検知パラメータ | distance=5, 許容誤差tol=1.5% |
| SL（SELL） | 右肩高値 + 5pips |
| SL（BUY） | 右肩安値 − 5pips |
| TP | リスク × RR比 2.0（RR 1:2） |
| TP参考 | ネックライン倍返し値も通知に表示 |

チェック順: BB逆張りを先に確認 → シグナルなし時のみH&Sをチェック。

---

## フィルター

### 200SMA トレンドフィルター（auto_trader.py 内蔵）

| 状態 | 制約 |
|------|------|
| 終値 < SMA200 | BUY禁止（シグナルが出ても無視） |
| 終値 > SMA200 | SELL禁止（シグナルが出ても無視） |

BB・H&Sどちらの戦略にも適用される。

### トレードフィルター（trade_filter.py）

- **スプレッド**: 1.5pips超のとき全エントリーを停止
- **重要指標**: `news_events.json` に記録されたイベントの前後30分はエントリー停止

---

## ポジション管理

| 機能 | 内容 |
|------|------|
| 建値移動 | 含み益が20pipsを超えたらSLを建値（+0.0001）に移動。OANDA APIでもSLを更新（`replace_stop_loss`） |
| 週末クローズ | 金曜23:00 JST に強制決済 |
| 同時保有上限 | 1ポジションのみ（全ペア合計） |
| trade_id管理 | 発注時にOANDAのtradeIDを取得・保存し、建値移動やポジション復元に使用 |

---

## LINE通知

エントリー・決済・エラーの3種類のみ。送信先はLINEグループ（`LINE_GROUP_ID`）。

### エントリー通知（notify_entry）

```
📈 (買)ロングエントリー！！ 🚀
ペア   : 🇺🇸🇯🇵 USD/JPY
レート : 150.250
────────────────────
💰 見込 : +20,000円 (RR 1:2)
🛑 SL  : 149.850  (-40.0 pips)
🎯 TP  : 151.050  (+80.0 pips)
────────────────────
🔍 戦略 : BB逆張り 1h (σ=2.5)
📊 根拠 :
・RSI=27.3（売られ過ぎ）, BB下限タッチ, SMA200より上
・🛑 SL根拠: ATR×3.0 (ATR=40.0pips)
・🎯 TP根拠: ATR×6.0 (RR 1:2)
────────────────────
📅 2026/02/25 10:00 JST
```

### 決済通知（notify_close）

```
💰 決済完了！ おめでとうございます 🎊
────────────────────
🇺🇸🇯🇵 USD/JPY  (買)
--------------------
🏁 決済 : 151.050
🛫 始値 : 150.250
--------------------
📊 損益 : +80.0 pips ✨
💰 収支 : +8,000円
🎯 理由 : TP到達 (利確)
────────────────────
📈 【運用実績】
📅 本日合計 : +80.0 pips  (計 +8,000円)
🏆 全期間   : +80.0 pips
💰 口座残高 : 1,008,000円
────────────────────
📅 2026/02/25 14:30 JST
```

### エラー通知（notify_error）

```
🚨 エラー発生
（エラー内容）
────────────
📅 2026/02/25 xx:xx JST
```

---

## 戦績トラッキング（trade_stats.json）

| フィールド | 内容 |
|-----------|------|
| `daily_pips` | 本日の損益（pips）※日付変更でリセット |
| `daily_jpy` | 本日の損益（円） |
| `weekly_pips` | 今週の損益（pips）※月曜でリセット |
| `weekly_jpy` | 今週の損益（円） |
| `weekly_trades` | 今週のトレード回数 |
| `weekly_wins` | 今週の勝ちトレード数 |
| `total_pips` | 累計損益（pips） |
| `balance` | 口座残高（初期値: INITIAL_BALANCE） |

---

## 起動方法

```bash
# 本番モード（.env に DRY_RUN=false 設定済み）
python3 auto_trader.py

# バックグラウンド起動（ターミナルを閉じても動き続ける）
nohup python3 auto_trader.py > /dev/null 2>&1 &

# 停止
kill $(pgrep -f auto_trader.py)

# ログをリアルタイムで確認
tail -f auto_trader.log

# ドライランモード（発注なし・確認用）
DRY_RUN=true python3 auto_trader.py

# LINEグループへの接続確認
python3 line_notify.py --test-line

# OANDAアカウントID確認（初期設定時）
python3 check_account.py
```

---

## OANDA発注フロー（oanda_executor.py）

```
auto_trader.py
  └─ place_order()
       └─ OandaExecutor.place_order(instrument, units, stop_loss, take_profit)
            └─ OANDA v3 API: POST /v3/accounts/{id}/orders
                 └─ MarketOrder + SL/TP 同時設定 → tradeID を取得・保存

  └─ manage_position()  ← 建値移動トリガー時
       └─ OandaExecutor.replace_stop_loss(trade_id, new_price, instrument)
            └─ OANDA v3 API: PUT /v3/accounts/{id}/trades/{tradeID}/orders

  └─ _close_position()
       └─ OandaExecutor.close_position(instrument)
            └─ OANDA v3 API: PUT /v3/accounts/{id}/positions/{instrument}/close
```

---

## 依存ライブラリ

| ライブラリ | 用途 |
|-----------|------|
| `oandapyV20` | OANDA v3 REST API クライアント |
| `pandas` / `numpy` | データ処理 |
| `pandas_ta` | テクニカル指標（BB・RSI・ATR・ADX） |
| `scipy` | H&Sパターン検知（find_peaks） |
| `yfinance` | 価格データ取得 |
| `python-dotenv` | .env 読み込み |
| `line-bot-sdk` | LINE Messaging API |
| `streamlit` | ダッシュボード |

---

## 堅牢性

| 機能 | 内容 |
|------|------|
| リトライ | API失敗時に最大MAX_RETRY回（デフォルト3回）、60秒待機して再試行 |
| 例外処理 | メインループ全体をtry-exceptで囲み、想定外エラーでも停止しない |
| ポジション照合 | 起動時にOANDA APIとposition.jsonを照合、ズレがあればAPI側を正として修正（本番モードのみ） |
| データ不足 | SMA200のデータが不足する場合は全期間平均で補完 |
| verbose抑制 | pandas_taのstdout出力を_quiet()で抑制し、ログを見やすく保つ |
| ログローテーション | `RotatingFileHandler` により1MBで自動切り替え、最大5世代（合計~6MB）保持。ファイルは `auto_trader.log` / `auto_trader.log.1` … `auto_trader.log.5` |
