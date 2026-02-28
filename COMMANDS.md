# FX Trade Luce — よく使うコマンド

## フォルダ構成

```
FX-trade/
├── auto_trader.py        # 本番メイン
├── oanda_executor.py     # OANDA APIラッパー
├── line_notify.py        # LINE通知
├── trade_filter.py       # スプレッド・重要指標フィルター
├── trade_stats.json      # 運用実績（pips・損益）
├── positions.json        # 保有ポジション管理
├── news_events.json      # 重要指標スケジュール
├── verify_live.py        # 本番動作確認スクリプト
├── dashboard.py          # Streamlitダッシュボード
├── market_analyzer.py    # 相場分析エンジン（dashboard用）
├── SPEC.md               # 現場仕様書
├── COMMANDS.md           # このファイル
│
├── backtest/             # バックテスト・検証ツール
│   ├── backtest.py           # 4ペア・2年分バックテスト
│   ├── run_backtest.py       # 単一ペア詳細検証
│   ├── walkforward.py        # ウォークフォワード検証（過剰最適化チェック）
│   ├── verify_top2.py        # H&S詳細検証・パラメータ感度テスト
│   ├── optimize.py           # パラメータ最適化
│   └── backtest_results/     # バックテスト結果（CSV・PNG・サマリー）
│
└── mql4_product/         # MT4 EA（商品レベル）
    └── FX_Trade_Luce.mq4
```

---

## 起動・停止

```bash
# 通常起動（ターミナルを閉じると止まる）
python3 auto_trader.py

# バックグラウンド起動（ターミナルを閉じても動き続ける）
nohup python3 auto_trader.py > /dev/null 2>&1 &

# 停止
kill $(pgrep -f auto_trader.py)

# 動いているか確認
pgrep -a auto_trader.py
```

---

## ログ確認

```bash
# リアルタイムでログを追いかける
tail -f auto_trader.log

# 直近50行だけ見る
tail -50 auto_trader.log

# エラーだけ抽出
grep ERROR auto_trader.log

# 今日のログだけ表示
grep "$(date +%Y-%m-%d)" auto_trader.log
```

---

## 各種テスト・確認

```bash
# LINE グループへの接続テスト
python3 line_notify.py --test-line

# OANDA 接続・アカウントID確認
python3 check_account.py

# OANDA ポジション確認
python3 oanda_executor.py

# 環境チェック
python3 setup_check.py

# 本番動作確認（パラメータ一致・OANDA接続・H&S検出・LINE送信）
python3 verify_live.py

# LINEテストをスキップして確認
python3 verify_live.py --no-line
```

---

## バックテスト

```bash
# 全ペア・2年分バックテスト
python3 backtest/backtest.py

# 特定ペアのみ
python3 backtest/backtest.py --pair USDJPY

# 金曜強制決済あり・なし 比較検証
python3 backtest/backtest.py --friday-compare

# ウォークフォワード検証（過剰最適化チェック）
python3 backtest/walkforward.py

# 単一ペア詳細検証
python3 backtest/run_backtest.py

# H&S詳細検証・パラメータ感度テスト
python3 backtest/verify_top2.py

# パラメータ最適化
python3 backtest/optimize.py
```

---

## ダッシュボード

```bash
streamlit run dashboard.py
```

---

## DRY RUN（テスト発注なし）

```bash
DRY_RUN=true python3 auto_trader.py
```

---

## .env の設定確認

```bash
grep -v "^#" .env | grep -v "^$"
```
