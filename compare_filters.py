"""
ADX閾値・RSIフィルターの効果比較バックテスト
"""
import sys, warnings
warnings.filterwarnings('ignore')
sys.path.insert(0, '/Users/naoko/naoko書類/FX-trade')
sys.path.insert(0, '/Users/naoko/naoko書類/FX-trade/backtest')

import pandas as pd
import numpy as np
import yfinance as yf
from backtest import detect_hs_at, calc_sma200, calc_adx
from backtest import HS_BUFFER_PIPS, MAX_SL_PIPS, SMA_PERIOD, LOT, _pips

PAIRS = {'USDJPY=X': 'USD/JPY', 'AUDJPY=X': 'AUD/JPY'}

def calc_rsi(df, period=14):
    delta = df['Close'].diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period-1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period-1, min_periods=period).mean()
    rs  = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi

def backtest_hs_filtered(df_4h, pair_name, adx_min=20, rsi_filter=False, rsi_min=50):
    sma_s = calc_sma200(df_4h)
    adx_s = calc_adx(df_4h)
    rsi_s = calc_rsi(df_4h) if rsi_filter else None
    trades = []
    pos = None
    last_rs_idx = -999

    for i in range(SMA_PERIOD + 30, len(df_4h)):
        close  = float(df_4h['Close'].iloc[i])
        high   = float(df_4h['High'].iloc[i])
        low    = float(df_4h['Low'].iloc[i])
        ts     = df_4h.index[i]
        sma200 = float(sma_s.iloc[i]) if not pd.isna(sma_s.iloc[i]) else close
        adx    = float(adx_s.iloc[i]) if not pd.isna(adx_s.iloc[i]) else 0.0
        rsi    = float(rsi_s.iloc[i]) if rsi_filter and not pd.isna(rsi_s.iloc[i]) else 50.0

        if pos:
            direction = pos['direction']
            ep = pos['entry_price']
            tp_hit = (direction == 'BUY'  and high >= pos['tp']) or (direction == 'SELL' and low  <= pos['tp'])
            sl_hit = (direction == 'BUY'  and low  <= pos['sl']) or (direction == 'SELL' and high >= pos['sl'])
            if tp_hit or sl_hit:
                exit_price = pos['tp'] if tp_hit else pos['sl']
                pnl = _pips(exit_price - ep) if direction == 'BUY' else _pips(ep - exit_price)
                trades.append({'pair': pair_name, 'direction': direction,
                    'entry_time': pos['entry_time'], 'exit_time': ts,
                    'entry_price': ep, 'exit_price': exit_price,
                    'pnl_pips': pnl, 'pnl_jpy': int(pnl * LOT / 100),
                    'result': 'WIN' if pnl > 0 else 'LOSS',
                    'exit_reason': 'TP' if tp_hit else 'SL'})
                pos = None
            continue

        # ADXフィルター
        if adx < adx_min:
            continue

        hs = detect_hs_at(df_4h, i)
        if hs is None:
            continue
        global_rs = hs.get('rs_idx', -1)
        if global_rs <= last_rs_idx:
            continue

        pattern  = hs['pattern']
        neckline = hs['neckline']

        if pattern == 'HEAD_AND_SHOULDERS' and close < sma200:
            # RSIフィルター: SELL → RSI < (100 - rsi_min)
            if rsi_filter and rsi > (100 - rsi_min):
                continue
            sl = round(hs['right_shoulder_high'] + HS_BUFFER_PIPS, 3)
            sl_pip = abs(close - sl) * 100
            if sl_pip > MAX_SL_PIPS:
                continue
            depth = hs['head'] - neckline
            tp = round(neckline - depth, 3)
            if tp >= close:
                tp = round(close - sl_pip / 100 * 2, 3)
            pos = {'direction': 'SELL', 'entry_price': close, 'sl': sl, 'sl_orig': sl,
                   'tp': tp, 'entry_time': ts, 'breakeven_done': False}
            last_rs_idx = global_rs

        elif pattern == 'INV_HEAD_AND_SHOULDERS' and close > sma200:
            # RSIフィルター: BUY → RSI > rsi_min
            if rsi_filter and rsi < rsi_min:
                continue
            sl = round(hs['right_shoulder_low'] - HS_BUFFER_PIPS, 3)
            sl_pip = abs(close - sl) * 100
            if sl_pip > MAX_SL_PIPS:
                continue
            depth = neckline - hs['head']
            tp = round(neckline + depth, 3)
            if tp <= close:
                tp = round(close + sl_pip / 100 * 2, 3)
            pos = {'direction': 'BUY', 'entry_price': close, 'sl': sl, 'sl_orig': sl,
                   'tp': tp, 'entry_time': ts, 'breakeven_done': False}
            last_rs_idx = global_rs

    return pd.DataFrame(trades)

def stats(df, label):
    if df.empty:
        print(f'  {label:35s}: トレードなし')
        return
    wins  = (df['pnl_pips'] > 0).sum()
    total = len(df)
    wr    = wins / total * 100
    total_pips = df['pnl_pips'].sum()
    total_jpy  = df['pnl_jpy'].sum()
    avg_w = df.loc[df['pnl_pips']>0,'pnl_pips'].mean() if wins > 0 else 0
    avg_l = df.loc[df['pnl_pips']<0,'pnl_pips'].mean() if (total-wins) > 0 else 0
    rr    = abs(avg_w / avg_l) if avg_l != 0 else 0
    print(f'  {label:35s}: {total:3d}件  勝率{wr:5.1f}%  {total_pips:+8.1f}pips({total_jpy:+9,}円)  勝:{avg_w:+6.1f} 負:{avg_l:+6.1f}  RR:{rr:.2f}')

# データ取得
print('データ取得中...')
dfs = {}
for ticker, pair in PAIRS.items():
    df = yf.download(ticker, period='700d', interval='4h', auto_adjust=True, progress=False)
    if df.empty:
        print(f'{pair}: データなし')
        continue
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    dfs[pair] = df.dropna()

print(f'\n取得期間: {list(dfs.values())[0].index[0].date()} 〜 {list(dfs.values())[0].index[-1].date()}\n')

# 比較パターン
patterns = [
    ('現行 (ADX≥20、RSIなし)',        dict(adx_min=20,  rsi_filter=False)),
    ('ADX≥25',                         dict(adx_min=25,  rsi_filter=False)),
    ('ADX≥30',                         dict(adx_min=30,  rsi_filter=False)),
    ('ADX≥20 + RSI>50',               dict(adx_min=20,  rsi_filter=True,  rsi_min=50)),
    ('ADX≥20 + RSI>55',               dict(adx_min=20,  rsi_filter=True,  rsi_min=55)),
    ('ADX≥25 + RSI>50',               dict(adx_min=25,  rsi_filter=True,  rsi_min=50)),
    ('ADX≥25 + RSI>55',               dict(adx_min=25,  rsi_filter=True,  rsi_min=55)),
]

print('=' * 95)
print(f'  {"条件":35s}  {"件数":>4}  {"勝率":>6}  {"合計pips/円":>22}  {"平均勝ち/負け":>16}  {"RR":>5}')
print('=' * 95)

for label, kwargs in patterns:
    all_trades = []
    for pair, df in dfs.items():
        t = backtest_hs_filtered(df, pair, **kwargs)
        all_trades.append(t)
    combined = pd.concat(all_trades) if all_trades else pd.DataFrame()
    stats(combined, label)

print('=' * 95)
