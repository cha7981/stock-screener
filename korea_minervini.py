import os
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import warnings
import requests

warnings.filterwarnings('ignore')

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

def calculate_atr(df, period=14):
    high_low = df['High'] - df['Low']
    high_close = np.abs(df['High'] - df['Close'].shift())
    low_close = np.abs(df['Low'] - df['Close'].shift())
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = true_range.rolling(period).mean()
    return atr.iloc[-1] if not atr.empty else 0

def calculate_vcp(df, lookback=120):
    if len(df) < lookback:
        return False, 0, 0.0
    df = df.copy()
    df['range_pct'] = (df['High'] - df['Low']) / df['Close'] * 100
    ranges_ma = df['range_pct'].rolling(10).mean()
    tight_periods = sum(ranges_ma < ranges_ma.rolling(15).mean() * 0.85)
    vcp_score = min(tight_periods / 8, 1.0) * 100
    return (tight_periods >= 2), tight_periods, round(vcp_score, 1)

def send_telegram_message(message, title=""):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("텔레그램 설정 오류")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        full_message = f"{title}\n\n{message}" if title else message
        requests.post(url, json={"chat_id": CHAT_ID, "text": full_message[:4000]}, timeout=15)
        print("✅ Telegram 전송 완료")
    except Exception as e:
        print(f"전송 실패: {e}")

def run_korea_minervini():
    print(f"🇰🇷 한국 미너비니 SEPA 스크리너 시작 - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    
    import FinanceDataReader as fdr
    
    stocks = fdr.StockListing('KRX')
    stocks = stocks[stocks['MarketCap'] > 300000000000].copy()  # 3000억 이상
    
    results = []
    
    for _, row in stocks.iterrows():
        try:
            ticker = row['Code']
            name = row['Name']
            
            df = fdr.DataReader(ticker, datetime.now() - timedelta(days=650))
            if len(df) < 250:
                continue
                
            latest = df.iloc[-1]
            price = float(latest['Close'])
            
            # 이동평균
            ma50 = df['Close'].rolling(50).mean().iloc[-1]
            ma150 = df['Close'].rolling(150).mean().iloc[-1]
            ma200 = df['Close'].rolling(200).mean().iloc[-1]
            
            # Trend Template
            tt_score = 0
            if price > ma200 and price > ma150: tt_score += 1
            if ma150 > ma200: tt_score += 1
            if ma200 > df['Close'].rolling(200).mean().iloc[-20]: tt_score += 1
            if price > ma50: tt_score += 1
            
            high52 = df['Close'].rolling(252).max().iloc[-1]
            low52 = df['Close'].rolling(252).min().iloc[-1]
            if price > low52 * 1.3: tt_score += 1
            if price >= high52 * 0.75: tt_score += 1
            
            vcp_pass, vcp_con, _ = calculate_vcp(df)
            atr = calculate_atr(df)
            
            if tt_score >= 6 and vcp_pass:
                entry = round(price * 1.02)
                stop_loss = round(price - 2.0 * atr)
                stop_loss = max(stop_loss, int(price * 0.85))
                
                results.append({
                    '종목명': name,
                    '티커': ticker,
                    '현재가': int(price),
                    '진입가': entry,
                    '손절가': stop_loss,
                    'TT': tt_score,
                    'VCP': vcp_con,
                    'ATR': int(atr),
                    '고점대비': round(price / high52 * 100, 1)
                })
        except:
            continue
    
    df_result = pd.DataFrame(results).head(12)
    
    if df_result.empty:
        message = "🟡 오늘은 미너비니 조건을 만족하는 한국 종목이 없습니다."
    else:
        message = f"🔔 미너비니 SEPA Daily Report (한국시장) - {datetime.now().strftime('%Y-%m-%d')}\n\n"
        for _, r in df_result.iterrows():
            message += f"📌 {r['종목명']} ({r['티커']})\n"
            message += f"현재가: {r['현재가']:,}원\n"
            message += f"진입: {r['진입가']:,}원 (+2%)\n"
            message += f"손절 (ATR2x): {r['손절가']:,}원\n"
            message += f"TT:{r['TT']} | VCP:{r['VCP']} | ATR:{r['ATR']} | 고점대비:{r['고점대비']}%\n\n"
    
    send_telegram_message(message, "🇰🇷 한국 미너비니 리포트")
    print("🇰🇷 한국 스크리너 완료")

if __name__ == "__main__":
    run_korea_minervini()
