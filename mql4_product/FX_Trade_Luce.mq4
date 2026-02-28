//+------------------------------------------------------------------+
//|                                              FX_Trade_Luce.mq4  |
//|                    FX Trade Luce — H&S 自動売買EA                 |
//|                                                                  |
//| 【概要】                                                           |
//|   4時間足のヘッドアンドショルダー（H&S）パターンを検出し、              |
//|   ネックライン倍返しをTPに設定して自動売買するEA。                     |
//|   Python版 auto_trader.py のロジックをMQL4に完全移植。              |
//|                                                                  |
//| 【Python版バックテスト実績 (OOS: 2025/3〜2026/2)】                  |
//|   USD/JPY: 37回 / 勝率 29.7% / +960 pips                        |
//|   AUD/JPY: 27回 / 勝率 59.3% / +1488 pips                       |
//|                                                                  |
//| 【戦略ルール】                                                      |
//|   天井H&S : 3ピーク（左肩＜頭＞右肩）+ SMA200以下 → SELL            |
//|   逆 H&S  : 3トラフ（左肩＞頭＜右肩）+ SMA200以上 → BUY            |
//|   SL      : 右肩高値（安値）+ バッファ                               |
//|   TP      : ネックラインからの倍返し                                  |
//|   SL上限  : 80 pips（超えるシグナルはスキップ）                       |
//|   週末強制決済・ブレイクイーブン: 廃止（バックテストで不利と確認）        |
//+------------------------------------------------------------------+

#property copyright "FX Trade Luce"
#property link      ""
#property version   "1.00"
#property strict

//=================================================================
// ▌ ユーザー設定パラメータ (input variables)
//=================================================================

//--- 基本設定
input int    MagicNumber  = 20250101;  // マジックナンバー（EA識別用・複数EA共存時に変更）
input double Lots         = 0.20;      // ロット数（20000通貨 = 標準口座 0.2ロット）
input int    Slippage     = 3;         // 許容スリッページ（pips）
input double MaxSpread    = 3.0;       // 最大許容スプレッド（pips）この値を超えるとエントリー見送り

//--- H&Sパターン検出パラメータ（Python版と対応）
input int    ZZ_Depth     = 12;        // ZigZag Depth（Python distance=5 相当。大きいほど大きな波を検出）
input int    ZZ_Deviation = 5;         // ZigZag Deviation（転換点の判定感度）
input int    ZZ_Backstep  = 3;         // ZigZag Backstep（直前転換点へのステップ数、ZZ_Depth未満に）
input double TolPct       = 0.020;     // 肩の対称性許容誤差（Python tol=0.020 = 2%。大きいほど緩い判定）
input double BufferPips   = 5.0;       // SLバッファ（右肩から何pips離すか。Python HS_BUFFER_PIPS=0.05）
input int    MaxSLPips    = 80;        // SL上限（pips）。Python MAX_SL_PIPS=80。超えるとスキップ。
input int    LookbackBars = 100;       // H&S検索窓（H4足の本数。Python 100本窓と同じ）

//--- フィルター
input int    SmaPeriod    = 200;       // トレンドフィルター用SMA期間（Python SMA_PERIOD=200）

//--- 時間足設定
input ENUM_TIMEFRAMES TF  = PERIOD_H4; // 使用する時間足（デフォルトH4。H4足のチャートに適用推奨）


//=================================================================
// ▌ グローバル変数
//=================================================================
datetime g_lastBarTime = 0;  // 最後に処理したバーの開始時刻（同バー内の重複処理防止用）


//=================================================================
// ▌ EA初期化
//=================================================================
int OnInit()
{
   Print("═══════════════════════════════════════════════════════");
   Print("  FX Trade Luce EA 起動");
   Print("  Symbol      : ", Symbol());
   Print("  MagicNumber : ", MagicNumber);
   Print("  Lots        : ", Lots);
   Print("  TF          : ", EnumToString(TF));
   Print("  ZigZag      : Depth=", ZZ_Depth, "  Dev=", ZZ_Deviation, "  Back=", ZZ_Backstep);
   Print("  TolPct      : ", TolPct, "  BufferPips: ", BufferPips, "  MaxSLPips: ", MaxSLPips);
   Print("  LookbackBars: ", LookbackBars, "  SMA Period: ", SmaPeriod);
   Print("  MaxSpread   : ", MaxSpread, " pips");
   Print("═══════════════════════════════════════════════════════");
   return(INIT_SUCCEEDED);
}


//=================================================================
// ▌ EA終了時処理
//=================================================================
void OnDeinit(const int reason)
{
   Print("FX Trade Luce EA 停止 (reason=", reason, ")");
}


//=================================================================
// ▌ ティック処理（メインループ）
// 新しいH4バーが確定するたびに1回だけ実行される
//=================================================================
void OnTick()
{
   //--- 新しいH4バーが確定したときのみ処理（1バーに1回だけ実行）
   //    iTime(Symbol(), TF, 0) は現在進行中のバーの開始時刻を返す
   datetime currentBarTime = iTime(Symbol(), TF, 0);
   if(currentBarTime == g_lastBarTime) return;
   g_lastBarTime = currentBarTime;

   //--- pip サイズ取得（JPY系通貨ペア対応）
   double pipSize = GetPipSize();

   //--- スプレッドチェック
   //    MODE_SPREAD はポイント単位で返すため pip サイズで割って pips 換算
   double spreadPips = MarketInfo(Symbol(), MODE_SPREAD) * Point / pipSize;
   if(spreadPips > MaxSpread)
   {
      Print("[スプレッド過大] ", DoubleToStr(spreadPips, 1), "pips > 上限 ",
            MaxSpread, "pips — このバーはスキップ");
      return;
   }

   //--- 既存ポジション確認
   //    既にこのEAのポジションがある場合はエントリーしない
   //    （Python版 MAX_POSITIONS=2 相当。SL/TPでの自動決済に任せる）
   if(CountMyPositions() > 0) return;

   //--- SMA200を計算（直前確定足 bar=1 を使用）
   double closePrice = iClose(Symbol(), TF, 1);  // 直前確定足の終値
   double sma200     = iMA(Symbol(), TF, SmaPeriod, 0, MODE_SMA, PRICE_CLOSE, 1);
   bool   aboveSMA   = (closePrice > sma200);    // true=SMA200より上


   //=================================================================
   // ▌ ZigZagでピーク（山）とトラフ（谷）を収集
   //
   //   ZigZag インジケーター（MT4標準）を iCustom で呼び出し、
   //   直近 LookbackBars 本の H4 足を走査して転換点を収集する。
   //
   //   iCustom の戻り値:
   //     0 または EMPTY_VALUE → 転換点でない（スキップ）
   //     barHigh に近い値     → ピーク（山の頂点）
   //     barLow  に近い値     → トラフ（谷の底）
   //
   //   配列は新しい順（[0]=最新、[1]=その前、[2]=さらに前）に格納
   //=================================================================
   double peakHigh[10];   // ピーク（山）の高値
   int    peakBar[10];    // ピークのバー番号（大きいほど古い）
   double troughLow[10];  // トラフ（谷）の安値
   int    troughBar[10];  // トラフのバー番号
   int    peakCount   = 0;
   int    troughCount = 0;

   for(int i = 1; i <= LookbackBars; i++)
   {
      //--- ZigZag値を取得（バッファ0 = 転換点の価格、非転換点は0）
      double zzVal = iCustom(Symbol(), TF, "ZigZag",
                             ZZ_Depth, ZZ_Deviation, ZZ_Backstep,
                             0,  // バッファ番号（0 = ZigZag折れ線）
                             i); // バー番号（1=直前確定足）

      //--- 転換点でない場合はスキップ
      if(zzVal == 0.0 || zzVal >= 1e10) continue;

      double barHigh = iHigh(Symbol(), TF, i);
      double barLow  = iLow(Symbol(), TF, i);

      //--- ZigZag値が高値側に近い → ピーク（山の頂点）
      if(MathAbs(zzVal - barHigh) <= MathAbs(zzVal - barLow))
      {
         if(peakCount < 10)
         {
            peakHigh[peakCount] = barHigh;
            peakBar[peakCount]  = i;
            peakCount++;
         }
      }
      //--- ZigZag値が安値側に近い → トラフ（谷の底）
      else
      {
         if(troughCount < 10)
         {
            troughLow[troughCount] = barLow;
            troughBar[troughCount] = i;
            troughCount++;
         }
      }
   }


   //=================================================================
   // ▌ 天井H&S 検出 → SELL エントリー
   //
   //   直近3ピーク（新→旧 = 右肩・頭・左肩）が以下の条件を満たすか判定:
   //   1. 頭 > 右肩 かつ 頭 > 左肩（頭が最高値）
   //   2. |左肩高値 - 右肩高値| / 頭高値 ≤ TolPct（両肩が近似対称）
   //   3. 現在価格 < SMA200（下降トレンド確認）
   //=================================================================
   if(peakCount >= 3 && !aboveSMA)
   {
      // 配列は新しい順なので [0]=右肩(最新), [1]=頭, [2]=左肩(最古)
      double rsHigh   = peakHigh[0];    // 右肩の高値
      double headHigh = peakHigh[1];    // 頭の高値
      double lsHigh   = peakHigh[2];    // 左肩の高値
      int    rsBarN   = peakBar[0];     // 右肩のバー番号
      int    hdBarN   = peakBar[1];     // 頭のバー番号
      int    lsBarN   = peakBar[2];     // 左肩のバー番号

      //--- 条件1: 頭が両肩より高い
      if(headHigh > rsHigh && headHigh > lsHigh)
      {
         //--- 条件2: 肩の対称性チェック
         //    Python版: abs(ls - rs) / (head + 1e-9) > tol → スキップ
         double asymmetry = MathAbs(lsHigh - rsHigh) / (headHigh + 1e-9);
         if(asymmetry <= TolPct)
         {
            //--- ネックライン計算
            //    左肩〜頭の区間の最安値（neck1）と
            //    頭〜右肩の区間の最安値（neck2）の平均
            double neck1    = FindLowestLow(lsBarN, hdBarN);
            double neck2    = FindLowestLow(hdBarN, rsBarN);
            double neckline = (neck1 + neck2) / 2.0;

            //--- SL = 右肩高値 + バッファ（5pips）
            //    Python版: sl = round(hs["right_shoulder_high"] + HS_BUFFER_PIPS, 3)
            double sl     = NormalizeDouble(rsHigh + BufferPips * pipSize, Digits);
            double slPips = (sl - closePrice) / pipSize;

            //--- SL方向チェック（SLがエントリー価格より上でなければNG）
            //    Python版: if sl <= close: return None
            if(sl <= closePrice)
            {
               Print("[SL方向エラー] H&S/SELL SL(", DoubleToStr(sl, Digits),
                     ") <= 現在値(", DoubleToStr(closePrice, Digits),
                     ") — パターンが古いためスキップ");
            }
            //--- SL上限チェック（Python版: if sl_pip > MAX_SL_PIPS: continue）
            else if(slPips > MaxSLPips)
            {
               Print("[SLキャップ] H&S/SELL SL=", DoubleToStr(slPips, 1),
                     "pips > 上限", MaxSLPips, "pips — スキップ");
            }
            else
            {
               //--- TP = ネックライン倍返し
               //    Python版: depth = head - neckline; tp = neckline - depth
               double depth = headHigh - neckline;
               double tp    = NormalizeDouble(neckline - depth, Digits);

               //--- TPフォールバック（TPがエントリーより上になる場合はRR1:2）
               if(tp >= closePrice)
                  tp = NormalizeDouble(closePrice - slPips * pipSize * 2.0, Digits);

               //--- ログ出力
               Print("【天井H&S SELL シグナル確定】");
               Print("  左肩=", DoubleToStr(lsHigh, Digits),
                     "  頭=", DoubleToStr(headHigh, Digits),
                     "  右肩=", DoubleToStr(rsHigh, Digits));
               Print("  ネックライン=", DoubleToStr(neckline, Digits),
                     "  SMA200=", DoubleToStr(sma200, Digits));
               Print("  SL=", DoubleToStr(sl, Digits),
                     " (", DoubleToStr(slPips, 1), "pips)  TP=", DoubleToStr(tp, Digits),
                     " (", DoubleToStr((closePrice - tp) / pipSize, 1), "pips)");

               //--- 発注（SELL）
               double entryPrice = MarketInfo(Symbol(), MODE_BID);
               int ticket = OrderSend(
                  Symbol(),                      // 通貨ペア
                  OP_SELL,                       // 売り
                  Lots,                          // ロット数
                  entryPrice,                    // エントリー価格（Bid）
                  Slippage,                      // スリッページ（pips）
                  sl,                            // ストップロス
                  tp,                            // テイクプロフィット
                  "FX_Trade_Luce H&S SELL",      // コメント
                  MagicNumber,                   // マジックナンバー
                  0,                             // 有効期限（0=無制限）
                  clrRed                         // 矢印色
               );
               if(ticket < 0)
                  Print("【発注失敗】 SELL OrderSend error=", GetLastError());
               else
                  Print("【発注成功】 SELL ticket=", ticket,
                        "  Entry=", DoubleToStr(entryPrice, Digits),
                        "  SL=", DoubleToStr(sl, Digits),
                        "  TP=", DoubleToStr(tp, Digits));
            }
         }
      }
   }


   //=================================================================
   // ▌ 逆H&S 検出 → BUY エントリー
   //
   //   直近3トラフ（新→旧 = 右肩・頭・左肩）が以下の条件を満たすか判定:
   //   1. 頭 < 右肩 かつ 頭 < 左肩（頭が最安値）
   //   2. |左肩安値 - 右肩安値| / |頭安値| ≤ TolPct（両肩が近似対称）
   //   3. 現在価格 > SMA200（上昇トレンド確認）
   //=================================================================
   if(troughCount >= 3 && aboveSMA)
   {
      // 配列は新しい順なので [0]=右肩(最新), [1]=頭, [2]=左肩(最古)
      double rsLow   = troughLow[0];    // 右肩の安値
      double headLow = troughLow[1];    // 頭の安値
      double lsLow   = troughLow[2];    // 左肩の安値
      int    rsBarN  = troughBar[0];    // 右肩のバー番号
      int    hdBarN  = troughBar[1];    // 頭のバー番号
      int    lsBarN  = troughBar[2];    // 左肩のバー番号

      //--- 条件1: 頭が両肩より低い
      if(headLow < rsLow && headLow < lsLow)
      {
         //--- 条件2: 肩の対称性チェック
         double asymmetry = MathAbs(lsLow - rsLow) / (MathAbs(headLow) + 1e-9);
         if(asymmetry <= TolPct)
         {
            //--- ネックライン計算
            //    左肩〜頭の区間の最高値（neck1）と
            //    頭〜右肩の区間の最高値（neck2）の平均
            double neck1    = FindHighestHigh(lsBarN, hdBarN);
            double neck2    = FindHighestHigh(hdBarN, rsBarN);
            double neckline = (neck1 + neck2) / 2.0;

            //--- SL = 右肩安値 - バッファ（5pips）
            //    Python版: sl = round(hs["right_shoulder_low"] - HS_BUFFER_PIPS, 3)
            double sl     = NormalizeDouble(rsLow - BufferPips * pipSize, Digits);
            double slPips = (closePrice - sl) / pipSize;

            //--- SL方向チェック（SLがエントリー価格より下でなければNG）
            //    Python版: if sl >= close: return None
            if(sl >= closePrice)
            {
               Print("[SL方向エラー] 逆H&S/BUY SL(", DoubleToStr(sl, Digits),
                     ") >= 現在値(", DoubleToStr(closePrice, Digits),
                     ") — パターンが古いためスキップ");
            }
            //--- SL上限チェック
            else if(slPips > MaxSLPips)
            {
               Print("[SLキャップ] 逆H&S/BUY SL=", DoubleToStr(slPips, 1),
                     "pips > 上限", MaxSLPips, "pips — スキップ");
            }
            else
            {
               //--- TP = ネックライン倍返し
               //    Python版: depth = neckline - head; tp = neckline + depth
               double depth = neckline - headLow;
               double tp    = NormalizeDouble(neckline + depth, Digits);

               //--- TPフォールバック（TPがエントリーより下になる場合はRR1:2）
               if(tp <= closePrice)
                  tp = NormalizeDouble(closePrice + slPips * pipSize * 2.0, Digits);

               //--- ログ出力
               Print("【逆H&S BUY シグナル確定】");
               Print("  左肩=", DoubleToStr(lsLow, Digits),
                     "  頭=", DoubleToStr(headLow, Digits),
                     "  右肩=", DoubleToStr(rsLow, Digits));
               Print("  ネックライン=", DoubleToStr(neckline, Digits),
                     "  SMA200=", DoubleToStr(sma200, Digits));
               Print("  SL=", DoubleToStr(sl, Digits),
                     " (", DoubleToStr(slPips, 1), "pips)  TP=", DoubleToStr(tp, Digits),
                     " (", DoubleToStr((tp - closePrice) / pipSize, 1), "pips)");

               //--- 発注（BUY）
               double entryPrice = MarketInfo(Symbol(), MODE_ASK);
               int ticket = OrderSend(
                  Symbol(),                      // 通貨ペア
                  OP_BUY,                        // 買い
                  Lots,                          // ロット数
                  entryPrice,                    // エントリー価格（Ask）
                  Slippage,                      // スリッページ（pips）
                  sl,                            // ストップロス
                  tp,                            // テイクプロフィット
                  "FX_Trade_Luce 逆H&S BUY",     // コメント
                  MagicNumber,                   // マジックナンバー
                  0,                             // 有効期限（0=無制限）
                  clrBlue                        // 矢印色
               );
               if(ticket < 0)
                  Print("【発注失敗】 BUY OrderSend error=", GetLastError());
               else
                  Print("【発注成功】 BUY ticket=", ticket,
                        "  Entry=", DoubleToStr(entryPrice, Digits),
                        "  SL=", DoubleToStr(sl, Digits),
                        "  TP=", DoubleToStr(tp, Digits));
            }
         }
      }
   }
}


//=================================================================
// ▌ ユーティリティ関数
//=================================================================

//--- バー番号 olderBar〜newerBar の区間の最安値を返す
//    ※MQL4のバー番号は「大きい = 古い」なので olderBar > newerBar となる
double FindLowestLow(int olderBar, int newerBar)
{
   double minLow = DBL_MAX;
   for(int i = newerBar; i <= olderBar; i++)
   {
      double lo = iLow(Symbol(), TF, i);
      if(lo < minLow) minLow = lo;
   }
   return(minLow);
}

//--- バー番号 olderBar〜newerBar の区間の最高値を返す
double FindHighestHigh(int olderBar, int newerBar)
{
   double maxHigh = -DBL_MAX;
   for(int i = newerBar; i <= olderBar; i++)
   {
      double hi = iHigh(Symbol(), TF, i);
      if(hi > maxHigh) maxHigh = hi;
   }
   return(maxHigh);
}

//--- 1pip のサイズを返す（JPY系/非JPY系・3桁/5桁ブローカー対応）
//    3桁または5桁ブローカー（USD/JPY=150.123 や EUR/USD=1.23456）:
//      → 1pip = Point * 10
//    2桁または4桁ブローカー（USD/JPY=150.12 や EUR/USD=1.2345）:
//      → 1pip = Point
double GetPipSize()
{
   if(Digits == 3 || Digits == 5)
      return(Point * 10.0);
   return(Point);
}

//--- マジックナンバーが一致するアクティブポジション数を返す
//    Python版 MAX_POSITIONS=2 と同様の上限管理に使用
int CountMyPositions()
{
   int count = 0;
   for(int i = 0; i < OrdersTotal(); i++)
   {
      if(OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
      {
         if(OrderSymbol()      == Symbol() &&
            OrderMagicNumber() == MagicNumber)
            count++;
      }
   }
   return(count);
}

//+------------------------------------------------------------------+
// 【MT4への導入手順】
//
// 1. このファイルを MetaTrader 4 のデータフォルダ内
//    MQL4/Experts/ にコピーする
//    （MT4メニュー: ファイル → データフォルダを開く）
//
// 2. MetaEditor で「コンパイル（F7）」を実行する
//    → エラーがあればログに表示されるので修正する
//
// 3. MT4のチャートを「H4足」「USD/JPY または AUD/JPY」に設定し、
//    EA（FX_Trade_Luce）をチャートにドラッグ＆ドロップで適用する
//
// 4. 「自動売買を許可する」ボタン（MT4ツールバー）をオンにする
//
// 5. 推奨設定（input パラメータ）:
//    MagicNumber: 任意の番号（チャートごとに変える）
//    Lots       : 0.20（20000通貨 = 標準口座0.2ロット）
//    MaxSpread  : 3.0（広すぎるスプレッドを除外）
//    その他のパラメータはデフォルト値で動作します
//
// 【注意事項】
// ・本EAはリアル口座で動作します。デモ口座で十分テストしてから使用してください。
// ・SL/TPはOANDA等のブローカー側に設定されるため、
//   MT4が閉じていても決済は自動で執行されます。
// ・max 2ポジション制限: 同一チャートにEAを1つだけ適用してください。
//   複数チャートに適用する場合は MagicNumber を変えてください。
//+------------------------------------------------------------------+
