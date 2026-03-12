# Luce 開発ガイドライン

このファイルはClaudeへの開発ルールです。必ず読んでから作業を開始してください。

---

## 1. 作業前に必ず確認する（PM思考）

コードを書く前に、以下を naoko に確認・提示すること：

- **何を変えるか**（変更対象のファイル・関数）
- **なぜ変えるか**（目的・解決したい問題）
- **どこに影響するか**（呼び出し元、依存関係）
- **リスクはないか**（本番稼働中の場合は特に注意）

> ❌ 「とりあえずコードを書いて後で説明する」はNG
> ✅ 「こういう理由でこう変えます。影響はここだけです」を先に言う

---

## 2. セキュリティルール（絶対厳守）

### APIキー・認証情報
- シークレット情報（APIキー、トークン、パスワード）を**コードに直接書かない**
- 必ず `os.environ.get("KEY_NAME")` で環境変数から取得する
- `.env` ファイルが `.gitignore` に含まれているか確認する

### コードレビュー時のチェック
新しいファイルを作る・変更するたびに以下を確認：
```bash
# ハードコードされたシークレットがないか
grep -rn "sk-\|token\s*=\s*['\"]" --include="*.py" .

# .envがgitignoreに入っているか
cat .gitignore | grep .env
```

### 個人情報
- LINEグループID、OANDA口座IDなどは `.env` のみに記載
- ログに個人情報・認証情報が出力されないか確認する

---

## 3. 変更後の確認手順（毎回実行）

コードを変更したら、必ずこの順番で確認する：

### Step 1: 静的解析（Ruff）
```bash
cd ~/naoko書類/FX-trade
python3 -m ruff check auto_trader.py oanda_executor.py technical.py line_notify.py --no-cache
```
**注意:** Ruffが「未使用import」と指摘しても、`pandas_ta` のような**副作用import**（importするだけで機能が有効になるもの）は削除しないこと。`# noqa: F401` コメントを付けて残す。

### Step 2: 構文チェック
```bash
python3 -c "import auto_trader" 2>&1
python3 -c "import oanda_executor; import technical; import line_notify; print('OK')"
```

### Step 3: バックテスト（コアロジックの変動確認）
```bash
python3 backtest/backtest.py 2>&1 | tail -15
```
リファクタリング前後で累計pipsが変わっていないことを確認する。

### Step 4: ログ確認（本番稼働中の場合）
```bash
tail -20 ~/naoko書類/FX-trade/auto_trader.log
```
ERRORが出ていないことを確認する。

---

## 4. リファクタリングのルール

- **一度に大きく変えない**。1ファイル・1関数ずつ変えてバックテストで確認する
- デッドコードを削除する前に「本当に呼ばれていないか」を確認する
  ```bash
  grep -rn "関数名" --include="*.py" .
  ```
- 外部ライブラリのAPIレスポンス構造は**実際に出力して確認**してから実装する。ドキュメントと実際が異なることがある

---

## 5. 外部API（OANDA / LINE）を触る時の注意

- OANDAのレスポンスフィールドはCLOSED/OPEN状態で変わる。`state` が変わると消えるフィールドがある
- 決済理由の判定は `stopLossOrderID` ではなく `stopLossOrder.state == "FILLED"` で行う
- `environment` は `.env` の `OANDA_ENVIRONMENT` から取得する（`'practice'` ハードコードはNG）

---

## 6. ログ・統計の整合性

- 決済をLuceが検知できなかった場合（sync clearなど）、`trade_stats.json` の `total_pips` がずれる
- 疑わしい場合は `check_trades.py` でOANDAの実績と照合する：
  ```bash
  python3 check_trades.py
  ```

---

## プロジェクト構成メモ

| ファイル | 役割 |
|---------|------|
| `auto_trader.py` | メインループ（本番稼働） |
| `oanda_executor.py` | OANDA API ラッパー |
| `technical.py` | H&S検知コアロジック（auto_trader / backtest 共通） |
| `line_notify.py` | LINE通知 |
| `backtest/backtest.py` | バックテスト本体 |
| `trade_stats.json` | 累計成績（OANDA実績と定期的に照合） |
| `.env` | APIキー等（gitignore済み・コミット禁止） |
