"""
OANDAアカウントID確認スクリプト
実行方法: python3 check_account.py
"""
import os
from dotenv import load_dotenv
import requests

load_dotenv()

token = os.environ.get("OANDA_API_KEY", "")
if not token:
    print("❌ OANDA_API_KEY が .env に設定されていません")
    exit(1)

url = "https://api-fxtrade.oanda.com/v3/accounts"
headers = {"Authorization": f"Bearer {token}"}

print(f"接続中... {url}")
try:
    r = requests.get(url, headers=headers, timeout=15)
    print(f"ステータス: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print("\n✅ アカウント一覧:")
        for acc in data.get("accounts", []):
            print(f"  アカウントID: {acc['id']}")
    else:
        print(f"エラー: {r.text}")
except Exception as e:
    print(f"接続失敗: {e}")
