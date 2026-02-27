# FX Trade Luce — 現場仕様書

> 最終更新: 2026-02-27（ウォークフォワード検証でEUR/JPY過剰最適化確認 → 2ペア体制に変更）

---

## ファイル構成

```
FX-trade/
├── auto_trader.py       # メイン司令塔（ループ・発注・ポジション管理）
├── backtest.py          # 4ペア長期バックテスト（2年分・4時間足）
├── run_backtest.py      # 単一ペア・複数戦略の比較検証スクリプト
├── verify_top2.py       # H&S / BB+RSI 詳細検証・パラメータ感度テスト
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
LOT=20000                        # 発注ロット（2万通貨）

# 動作パラメータ（省略時はデフォルト値）
MAX_RETRY=3                      # APIリトライ回数
SMA_PERIOD=200                   # トレンドフィルター用SMA期間
SPREAD_PIPS=0.4                  # スプレッド基準値
```

---

## 監視ペア

| ペア | yfinance ticker | 戦略 | OOS結果（後半1年・未知期間） |
|------|----------------|------|---------------------------|
| USD/JPY | `USDJPY=X` | H&S（4時間足） | 37回, 29.7%勝率, **+960 pips** 🟢 |
| AUD/JPY | `AUDJPY=X` | H&S（4時間足） | 27回, 59.3%勝率, **+1488 pips** 🟢 |

> **2ペア合計（OOS）**: 64回, 43.8%勝率, **+2,449 pips / 年**
>
> ウォークフォワード検証（2024前半で最適化 → 2025後半で検証）で汎化性を確認済み。
>
> **除外ペア:**
> - GBP/JPY: OOS 7.7%勝率, -1,000pips（成績不良）
> - EUR/JPY: OOS 22.0%勝率, -368pips（**過剰最適化と判定** — IS +1007pips が再現せず）

最大2ポジションまで同時保有可（ペアをまたいで保有可能）。

---

## 戦略ロジック

### H&S パターン（4時間足）— 全ペア共通

| 項目 | 設定 |
|------|------|
| 検知ライブラリ | `scipy.signal.find_peaks` |
| パターン① | ヘッドアンドショルダー（天井）→ **SELL** |
| パターン② | 逆ヘッドアンドショルダー（底）→ **BUY** |
| 検知窓 | 直近100本（backtest.py と統一） |
| 探索方式 | 全組み合わせ（新しい順に探索） |
| distance | `5`（ピーク間最小距離） |
| tol | `0.020`（肩の対称性許容誤差 2%、感度テストで最適化） |
| SL（SELL） | 右肩高値 + 5pips |
| SL（BUY） | 右肩安値 − 5pips |
| SL上限 | 80pips（超えるシグナルはスキップ） |
| **TP** | **ネックライン倍返し**（`neckline - (head - neckline)`） |
| TPフォールバック | TPがエントリー価格と逆方向の場合は SL距離 × 2（RR 1:2） |

#### TP計算（ネックライン倍返し）

```
天井H&S(SELL):  tp = neckline - (head - neckline)
逆H&S(BUY):    tp = neckline + (neckline - head)
```

ネックラインからヘッドまでの距離をネックラインの反対側に延長した値をTPとする。

---

## フィルター

### 200SMA トレンドフィルター（auto_trader.py 内蔵）

| 状態 | 制約 |
|------|------|
| 終値 < SMA200 | BUY禁止（逆H&Sシグナルを無視） |
| 終値 > SMA200 | SELL禁止（天井H&Sシグナルを無視） |

H&S 戦略に適用。

### トレードフィルター（trade_filter.py）

- **スプレッド**: 1.5pips超のとき全エントリーを停止
- **重要指標**: `news_events.json` に記録されたイベントの前後30分はエントリー停止

---

## ポジション管理

| 機能 | 内容 |
|------|------|
| 同時保有上限 | 最大2ポジション（全ペア合計） |
| trade_id管理 | 発注時にOANDAのtradeIDを取得・保存し、ポジション復元に使用 |
| ブレイクイーブン | **廃止**（バックテストで「含み益を0pipsで強制終了→負け計上」の悪影響が判明） |
| 週末クローズ | **廃止**（バックテスト検証で「含み益ポジションを途中で切る→利益を大幅に削る」と判明。金曜クローズあり: +2,262 pips vs なし: +5,004 pips） |

> **ポジションはSL/TPのみで管理。** 途中で決済ルールを追加しない設計がバックテスト結果と一致する。

---

## LINE通知

エントリー・決済・エラーの3種類のみ。送信先はLINEグループ（`LINE_GROUP_ID`）。

### エントリー通知（notify_entry）

```
📈 (売)ショートエントリー！！ 🚀
ペア   : 🇺🇸🇯🇵 USD/JPY
レート : 150.250
────────────────────
🛑 SL  : 150.700  (+45.0 pips)
🎯 TP  : 148.150  (-210.5 pips)
────────────────────
🔍 戦略 : H&S 4h
📊 根拠 :
・天井H&Sパターン検知, ADX=28.3, SMA200=151.200より下
・🛑 SL根拠: 右肩高値(150.650)+5pips
・🎯 TP根拠: ネックライン倍返し(149.200 - 1.050 = 148.150)
────────────────────
📅 2026/02/26 10:00 JST
```

### 決済通知（notify_close）

```
💰 決済完了！ おめでとうございます 🎊
────────────────────
🇺🇸🇯🇵 USD/JPY  (売)
--------------------
🏁 決済 : 148.150
🛫 始値 : 150.250
--------------------
📊 損益 : +210.5 pips ✨
💰 収支 : +42,100円
🎯 理由 : TP到達 (利確)
────────────────────
📈 【運用実績】
📅 本日合計 : +210.5 pips  (計 +42,100円)
🏆 全期間   : +210.5 pips
💰 口座残高 : 542,100円
────────────────────
📅 2026/02/26 14:30 JST
```

### エラー通知（notify_error）

```
🚨 エラー発生
（エラー内容）
────────────
📅 2026/02/26 xx:xx JST
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

# ドライランモード（発注なし・動作確認用）
DRY_RUN=true python3 auto_trader.py

# LINEグループへの接続確認
python3 line_notify.py --test-line

# OANDAアカウントID確認（初期設定時）
python3 check_account.py

# バックテスト（4ペア・2年分）
python3 backtest.py

# パラメータ感度テスト
python3 verify_top2.py --sens
```

---

## OANDA発注フロー（oanda_executor.py）

```
auto_trader.py
  └─ place_order()
       └─ OandaExecutor.place_order(instrument, units, stop_loss, take_profit)
            └─ OANDA v3 API: POST /v3/accounts/{id}/orders
                 └─ MarketOrder + SL/TP 同時設定 → tradeID を取得・保存

  └─ _close_position()
       └─ OandaExecutor.close_position(instrument)
            └─ OANDA v3 API: PUT /v3/accounts/{id}/positions/{instrument}/close
```

> 建値移動（`replace_stop_loss`）は廃止のため、SL更新APIは使用しない。

---

## 依存ライブラリ

| ライブラリ | 用途 |
|-----------|------|
| `oandapyV20` | OANDA v3 REST API クライアント |
| `pandas` / `numpy` | データ処理 |
| `pandas_ta` | テクニカル指標（ATR・ADX・SMA） |
| `scipy` | H&Sパターン検知（`find_peaks`） |
| `yfinance` | 価格データ取得（1h足 → 4h足にリサンプル） |
| `python-dotenv` | .env 読み込み |
| `line-bot-sdk` | LINE Messaging API |
| `streamlit` | ダッシュボード |

---

## 堅牢性

| 機能 | 内容 |
|------|------|
| リトライ | API失敗時に最大MAX_RETRY回（デフォルト3回）、60秒待機して再試行 |
| 例外処理 | メインループ全体をtry-exceptで囲み、想定外エラーでも停止しない |
| ポジション照合 | 起動時にOANDA APIとpositions.jsonを照合、ズレがあればAPI側を正として修正（本番モードのみ） |
| verbose抑制 | pandas_taのstdout出力を`_quiet()`で抑制し、ログを見やすく保つ |
| ログローテーション | `RotatingFileHandler` により1MBで自動切り替え、最大5世代（合計~6MB）保持 |

---

## ウォークフォワード検証結果（過剰最適化チェック済み）

> **前半（2024/3〜2025/2）= in-sample / 後半（2025/3〜2026/2）= out-of-sample**

| ペア | IS（前半1年） | OOS（後半1年） | 判定 |
|------|-------------|--------------|------|
| USD/JPY H&S | 29回, 24.1%, +532pips | **37回, 29.7%, +960pips** | 🟢 OOS が IS を上回る |
| AUD/JPY H&S | 29回, 41.4%, +1140pips | **27回, 59.3%, +1488pips** | 🟢 OOS が IS を上回る |
| EUR/JPY H&S | 23回, 30.4%, +1007pips | **41回, 22.0%, −368pips** | 🔴 過剰最適化 → **除外** |
| GBP/JPY H&S | 25回, 20.0%, +821pips | **26回, 7.7%, −1000pips** | 🔴 過剰最適化 → **除外** |

> **採用2ペアのOOS合計**: 64回, 43.8%勝率, **+2,449 pips / 年**（20000lot換算: +489,800円/年）
>
> パラメータ: distance=5, tol=0.020, MAX_SL_PIPS=80, TP=ネックライン倍返し
