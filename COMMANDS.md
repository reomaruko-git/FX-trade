# FX Trade Luce — よく使うコマンド

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

## 各種テスト

```bash
# LINE グループへの接続テスト
python3 line_notify.py --test-line

# OANDA 接続・アカウントID確認
python3 check_account.py

# OANDA ポジション確認
python3 oanda_executor.py

# 環境チェック
python3 setup_check.py
```

## ダッシュボード

```bash
streamlit run dashboard.py
```

## DRY RUN（テスト発注なし）で起動したい場合

```bash
DRY_RUN=true python3 auto_trader.py
```

## .env の設定確認

```bash
grep -v "^#" .env | grep -v "^$"
```
