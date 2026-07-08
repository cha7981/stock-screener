import yfinance as yf
import pandas as pd
import time
import requests
import os

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

def send_telegram_message(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("❌ 에러: 환경변수 세팅을 확인하세요.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try: requests.post(url, json=payload)
    except Exception as e: print(f"텔레그램 전송 에러: {e}")

def get_sp500_tickers():
    try: return pd.read_html('https://en.wikipedia.org/wiki/List_of_S%26P_500_companies')[0]['Symbol'].str.replace('.', '-', regex=False).tolist()
    except: return []

def get_nasdaq100_tickers():
    try: return pd.read_html('https://en.wikipedia.org/wiki/Nasdaq-100', attrs={'id': 'constituents'})[0]['Ticker'].str.replace('.', '-', regex=False).tolist()
    except: return []

def get_russell2000_tickers():
    try:
        df = pd.read_csv("https://raw.githubusercontent.com/SergioIommi/Quant-Trading-Dashboards/master/RUT.csv")
        tickers = df['Symbol'].dropna().tolist() if 'Symbol' in df.columns else df.iloc[:, 0].dropna().tolist()
        return [str(t).strip().replace('.', '-') for t in tickers]
    except: return []

def check_trend_and_rs(df, spy_close_series):
    if len(df) < 200: return False
    df['MA50'] = df['Close'].rolling(window=50).mean()
    df['MA150'] = df['Close'].rolling(window=150).mean()
    df['MA200'] = df['Close'].rolling(window=200).mean()
    df['Vol_MA50'] = df['Volume'].rolling(window=50).mean()
    
    current_price = df['Close'].iloc[-1]
    ma50, ma150, ma200 = df['MA50'].iloc[-1], df['MA150'].iloc[-1], df['MA200'].iloc[-1]
    ma200_20days_ago = df['MA200'].iloc[-20]
    low_52week, high_52week = df['Close'].min(), df['Close'].max()
    
    cond_1 = current_price > ma150 and current_price > ma200
    cond_2 = ma150 > ma200
    cond_3 = ma200 > ma200_20days_ago
    cond_4 = ma50 > ma150 and ma50 > ma200
    cond_5 = current_price > ma50
    cond_6 = current_price >= (low_52week * 1.30)
    cond_7 = current_price >= (high_52week * 0.75)
    cond_vol_base = df['Vol_MA50'].iloc[-1] > 150000
    
    combined = pd.DataFrame({'Stock': df['Close'], 'SPY': spy_close_series}).dropna()
    if len(combined) < 50: return False
    combined['RS_Line'] = combined['Stock'] / combined['SPY']
    combined['RS_MA50'] = combined['RS_Line'].rolling(window=50).mean()
    cond_rs = combined['RS_Line'].iloc[-1] > combined['RS_MA50'].iloc[-1]
    
    return cond_1 and cond_2 and cond_3 and cond_4 and cond_5 and cond_6 and cond_7 and cond_rs and cond_vol_base

def check_vcp_and_get_prices(df):
    try:
        recent_df = df.tail(30).copy()
        seg1, seg2, seg3 = recent_df.iloc[0:10], recent_df.iloc[10:20], recent_df.iloc[20:30]
        range1 = (seg1['High'].max() - seg1['Low'].min()) / seg1['Low'].min()
        range2 = (seg2['High'].max() - seg2['Low'].min()) / seg2['Low'].min()
        range3 = (seg3['High'].max() - seg3['Low'].min()) / seg3['Low'].min()
        
        if range1 > range2 and range2 > range3 and range3 < 0.08:
            if recent_df['Volume'].tail(5).mean() < recent_df['Volume'].mean():
                entry_price = round(seg3['High'].max(), 2)
                stop_loss = round(seg3['Low'].min(), 2)
                risk_pct = round(((entry_price - stop_loss) / entry_price) * 100, 1)
                return True, entry_price, stop_loss, risk_pct
    except: pass
    return False, 0, 0, 0

if __name__ == "__main__":
    spy_close = yf.Ticker("^GSPC").history(period="1y")['Close']
    tickers = list(set(get_sp500_tickers() + get_nasdaq100_tickers() + get_russell2000_tickers()))
    
    stage1_count = 0       
    stage3_vcp_details = [] 
    
    for i, ticker_symbol in enumerate(tickers):
        try:
            df = yf.Ticker(ticker_symbol).history(period="1y")
            if check_trend_and_rs(df, spy_close):
                stage1_count += 1
                is_vcp, entry, stop, risk = check_vcp_and_get_prices(df)
                if is_vcp:
                    stage3_vcp_details.append({'ticker': ticker_symbol, 'entry': entry, 'stop': stop, 'risk': risk})
        except: pass
        time.sleep(0.2)
        
    today_str = pd.Timestamp.now().strftime('%Y-%m-%d')
    t3_text = "\n".join([f"• *{item['ticker']}* ➔ 진입가: {item['entry']}$ | 손절가: {item['stop']}$ (-{item['risk']}%)" for item in stage3_vcp_details]) if stage3_vcp_details else "조건을 만족하는 매수 임박 종목이 없습니다."
    
    msg = f"🔔 *[{today_str}] 미너비니 스크리닝 결과*\n------------------------------------\n📊 *대상:* S&P500+나스닥100+러셀2000 (총 {len(tickers)}개)\n📈 *1차 추세+거래량 필터 통과:* 총 {stage1_count}개\n\n🔥 *최종 VCP 통과*\n{t3_text}\n------------------------------------"
    send_telegram_message(msg)
