"""
OANDAの全決済済みトレードを取得してpips合計を確認する
"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / '.env')
from oandapyV20 import API
import oandapyV20.endpoints.trades as trades_ep

env = os.environ.get('OANDA_ENVIRONMENT', 'live')
client = API(access_token=os.environ['OANDA_API_KEY'], environment=env)
account_id = os.environ['OANDA_ACCOUNT_ID']

r = trades_ep.TradesList(account_id, params={'state': 'CLOSED', 'count': 50})
client.request(r)
tlist = r.response.get('trades', [])
print(f"決済済みトレード数: {len(tlist)}")
print(f"{'ID':>4}  {'通貨ペア':<12}  {'方向':<4}  {'エントリー':>8}  {'決済':>8}  {'pips':>8}  {'時刻'}")
print("-" * 80)
total = 0.0
for t in tlist:
    pid = t['id']
    inst = t['instrument'].replace('_', '/')
    units = int(t['initialUnits'])
    entry = float(t['price'])
    close = float(t.get('averageClosePrice', 0))
    direction = 'BUY' if units > 0 else 'SELL'
    pips = (close - entry) * 100 if direction == 'BUY' else (entry - close) * 100
    total += pips
    close_time = t.get('closeTime', '')[:16].replace('T', ' ')
    print(f"  {pid:>4}  {inst:<12}  {direction:<4}  {entry:>8.3f}  {close:>8.3f}  {pips:>+8.1f}  {close_time}")
print("-" * 80)
print(f"  OANDA合計pips: {total:+.1f}")
print()

# trade_stats.jsonと比較
import json
stats_file = Path(__file__).parent / 'trade_stats.json'
if stats_file.exists():
    stats = json.loads(stats_file.read_text())
    print(f"  Luce統計 total_pips: {stats['total_pips']:+.1f}")
    print(f"  差分: {total - stats['total_pips']:+.1f} pips（OANDAとの乖離）")
