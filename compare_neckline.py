import sys, warnings
warnings.filterwarnings('ignore')
sys.path.insert(0, '/Users/naoko/naoko書類/FX-trade')
sys.path.insert(0, '/Users/naoko/naoko書類/FX-trade/backtest')

import pandas as pd
import numpy as np
import yfinance as yf
from backtest import backtest_hs, detect_hs_at, calc_sma200
from backtest import HS_BUFFER_PIPS, MAX_SL_PIPS, SMA_PERIOD, LOT, _pips

PAIRS = {'USDJPY=X': 'USD/JPY', 'AUDJPY=X': 'AUD/JPY'}

def backtest_hs_neckline(df_4h, pair_name):
    sma_s = calc_sma200(df_4h)
    trades = []
    pos = None
    last_rs_idx = -999

    for i in range(SMA_PERIOD + 30, len(df_4h)):
        close  = float(df_4h['Close'].iloc[i])
        high   = float(df_4h['High'].iloc[i])
        low    = float(df_4h['Low'].iloc[i])
        ts     = df_4h.index[i]
        sma200 = float(sma_s.iloc[i]) if not pd.isna(sma_s.iloc[i]) else close

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

        hs = detect_hs_at(df_4h, i)
        if hs is None:
            continue
        global_rs = hs.get('rs_idx', -1)
        if global_rs <= last_rs_idx:
            continue

        pattern  = hs['pattern']
        neckline = hs['neckline']

        if pattern == 'HEAD_AND_SHOULDERS' and close < sma200:
            if close >= neckline:   # ネックライン突破確認なしはスキップ
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
            if close <= neckline:   # ネックライン突破確認なしはスキップ
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
        print(f'  {label}: トレードなし')
        return
    wins  = (df['pnl_pips'] > 0).sum()
    total = len(df)
    wr    = wins / total * 100
    total_pips = df['pnl_pips'].sum()
    total_jpy  = df['pnl_jpy'].sum()
    avg_w = df.loc[df['pnl_pips']>0,'pnl_pips'].mean() if wins > 0 else 0
    avg_l = df.loc[df['pnl_pips']<0,'pnl_pips'].mean() if (total-wins) > 0 else 0
    print(f'  {label}: {total}件  勝率{wr:.1f}%  合計{total_pips:+.1f}pips({total_jpy:+,}円)  平均勝:{avg_w:+.1f}  平均負:{avg_l:+.1f}')

print('データ取得中...')
b_all, a_all = [], []

for ticker, pair in PAIRS.items():
    df = yf.download(ticker, period="700d", interval='4h', auto_adjust=True, progress=False)
    if df.empty:
        print(f'{pair}: データなし')
        continue
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df.dropna()

    b = backtest_hs(df, pair)
    a = backtest_hs_neckline(df, pair)
    b_all.append(b)
    a_all.append(a)
    print(f'\n{pair}:')
    stats(b, '現行（確認なし）')
    stats(a, '修正後（確認あり）')

print('\n=== 全ペア合計 ===')
if b_all:
    stats(pd.concat(b_all), '現行（確認なし）')
if a_all:
    stats(pd.concat(a_all), '修正後（確認あり）')
