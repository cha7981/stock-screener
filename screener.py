import os
import sys
import time
import io
import csv
import re
import random
import pandas as pd
import requests
import yfinance as yf

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# ============================================================
# V9 핵심 설정 (K-미너비니 & 유동성 필터 강화)
# ============================================================
# 기본값을 STRICT로 변경하여 잡주 필터링 강화
STRICT_MODE = True

# Trend Template
MIN_RS_RATING_BALANCED = 80
MIN_RS_RATING_STRICT = 85
MIN_PRICE = 10.0                # 동전주/잡주 필터링 강화를 위해 5.0 -> 10.0 상향
MIN_AVG_VOLUME = 200000         # 최소 거래량 요건 상향 (20만주)
MIN_DOLLAR_VOLUME = 3000000     # 최소 거래대금 요건 상향 (300만 달러)
HIGH_52W_NEAR_BALANCED = 0.80   
HIGH_52W_NEAR_STRICT = 0.85     

# Fundamentals
MIN_EPS_GROWTH = 0.20
MIN_REV_GROWTH = 0.15
MAX_FUNDAMENTAL_CALLS = 40
FUNDAMENTAL_SLEEP = 3
FUNDAMENTAL_RETRY_SLEEP = 45

# VCP Balanced / Strict thresholds
VCP_LAST_CONTRACTION_BALANCED = 0.08    
VCP_LAST_CONTRACTION_STRICT = 0.05      
VCP_CONTRACTION_RATIO_BALANCED = 0.85   
VCP_CONTRACTION_RATIO_STRICT = 0.75
VOLUME_DRYUP_BALANCED = 0.85            
VOLUME_DRYUP_STRICT = 0.70
ATR_DRYUP_BALANCED = 0.90               
ATR_DRYUP_STRICT = 0.70
MAX_RISK_BALANCED = 8.0
MAX_RISK_STRICT = 6.0
PIVOT_NEAR_PCT_BALANCED = 5.0
PIVOT_NEAR_PCT_STRICT = 3.0
MIN_VCP_SCORE_BALANCED = 5              
MIN_VCP_SCORE_STRICT = 6                

PRICE_PERIOD = "18mo"
CHUNK_SIZE = 50
SLEEP_BETWEEN_CHUNKS = 8
MARKET_FILTER_ENABLED = False

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/csv,application/csv,text/plain,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

def cfg():
    if STRICT_MODE:
        return {
            "rs": MIN_RS_RATING_STRICT,
            "high52": HIGH_52W_NEAR_STRICT,
            "last_contraction": VCP_LAST_CONTRACTION_STRICT,
            "contraction_ratio": VCP_CONTRACTION_RATIO_STRICT,
            "volume_dryup": VOLUME_DRYUP_STRICT,
            "atr_dryup": ATR_DRYUP_STRICT,
            "max_risk": MAX_RISK_STRICT,
            "pivot_near": PIVOT_NEAR_PCT_STRICT,
            "min_vcp_score": MIN_VCP_SCORE_STRICT,
            "mode": "STRICT",
        }
    return {
        "rs": MIN_RS_RATING_BALANCED,
        "high52": HIGH_52W_NEAR_BALANCED,
        "last_contraction": VCP_LAST_CONTRACTION_BALANCED,
        "contraction_ratio": VCP_CONTRACTION_RATIO_BALANCED,
        "volume_dryup": VOLUME_DRYUP_BALANCED,
        "atr_dryup": ATR_DRYUP_BALANCED,
        "max_risk": MAX_RISK_BALANCED,
        "pivot_near": PIVOT_NEAR_PCT_BALANCED,
        "min_vcp_score": MIN_VCP_SCORE_BALANCED,
        "mode": "BALANCED",
    }


# ============================================================
# Common utilities & Universe (기존 동일)
# ============================================================
def send_telegram_message(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("❌ 텔레그램 환경변수가 없습니다.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        res = requests.post(url, json={"chat_id": CHAT_ID, "text": message}, timeout=15)
    except Exception as e:
        pass

def clean_ticker(ticker):
    if ticker is None: return ""
    return str(ticker).strip().replace("\ufeff", "").replace('"', "").replace("/", "-").replace(".", "-").upper()

def is_valid_us_ticker(ticker):
    ticker = clean_ticker(ticker)
    if not ticker or " " in ticker or len(ticker) > 8: return False
    if ticker in {"CASH", "USD", "-", "N/A", "VALUE", "TICKER", "SYMBOL", "NO", "CONSTITUENTS"}: return False
    if ticker.endswith(("-TO", "-OL", "-DE", "-L", "-PA", "-AS", "-SW", "-VI", "-F")): return False
    if re.search(r"\d{3,}", ticker): return False
    return bool(re.match(r"^[A-Z][A-Z0-9]*(?:-[A-Z])?$", ticker))

def is_probably_common_stock(name):
    if not name: return True
    n = str(name).lower()
    bad = [" etf", "exchange traded fund", "etn", "warrant", "right", "unit", "preferred", "depositary", "bond", "fund"]
    return not any(x in n for x in bad)

def get_text(url, timeout=30):
    res = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout)
    res.raise_for_status()
    return res.text

def decode_response_text_safely(response):
    for enc in ["utf-8-sig", "utf-8", "latin1"]:
        try:
            return response.content.decode(enc, errors="ignore").replace("\x00", "")
        except: pass
    return response.text.replace("\x00", "")

def get_sp500_tickers():
    try:
        html = get_text("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
        df = pd.read_html(io.StringIO(html))[0]
        return sorted(set(clean_ticker(x) for x in df["Symbol"].dropna().tolist()))
    except: return []

def get_nasdaq100_tickers():
    try:
        html = get_text("https://en.wikipedia.org/wiki/Nasdaq-100")
        dfs = pd.read_html(io.StringIO(html), attrs={"id": "constituents"})
        return sorted(set(clean_ticker(x) for x in dfs[0]["Ticker"].dropna().tolist())) if dfs else []
    except: return []

def get_russell2000_tickers():
    url = "https://raw.githubusercontent.com/quanthero/US_Indices_Constituents/main/Russell2000.csv"
    try:
        res = requests.get(url, headers=REQUEST_HEADERS, timeout=35)
        df = pd.read_csv(io.StringIO(decode_response_text_safely(res)))
        col = df.columns[0]
        return sorted(set(clean_ticker(x) for x in df[col].dropna().astype(str).tolist() if is_valid_us_ticker(clean_ticker(x)))), "GitHub Fallback"
    except: return [], "Failed"

def get_official_listed_universe():
    official = {}
    try:
        res = requests.get("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", headers=REQUEST_HEADERS)
        df = pd.read_csv(io.StringIO(res.text), sep="|")
        for _, row in df.iterrows():
            sym = clean_ticker(row.get("Symbol", ""))
            if sym and not sym.startswith("FILE"):
                official[sym] = {"symbol": sym, "name": str(row.get("Security Name", "")), "etf": str(row.get("ETF", "N")), "test_issue": str(row.get("Test Issue", "N"))}
        res2 = requests.get("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", headers=REQUEST_HEADERS)
        df2 = pd.read_csv(io.StringIO(res2.text), sep="|")
        for _, row in df2.iterrows():
            sym = clean_ticker(row.get("ACT Symbol", ""))
            if sym and not sym.startswith("FILE"):
                official[sym] = {"symbol": sym, "name": str(row.get("Security Name", "")), "etf": str(row.get("ETF", "N")), "test_issue": str(row.get("Test Issue", "N"))}
    except: pass
    return official

def validate_candidate_universe(tickers, official_map):
    included = []
    for raw in tickers:
        t = clean_ticker(raw)
        if t in official_map:
            meta = official_map[t]
            if meta.get("test_issue") != "Y" and meta.get("etf") != "Y" and is_probably_common_stock(meta.get("name")):
                included.append(t)
    return sorted(set(included)), [], []

# ============================================================
# Price and filters (핵심 수정 구간)
# ============================================================
def get_ticker_dataframe(raw_data, ticker):
    try:
        if isinstance(raw_data.columns, pd.MultiIndex):
            return raw_data[ticker].copy() if ticker in raw_data.columns.get_level_values(0) else None
        if "Close" in raw_data.columns: return raw_data.copy()
    except: return None
    return None

def prepare_price_dataframe(df):
    if df is None or df.empty or len(df) < 260: return None
    df = df.dropna(subset=["Close"]).copy()
    
    # 1. 이동평균선 추가 (K-미너비니 눌림목용 단기 이평선 포함)
    df["MA10"] = df["Close"].rolling(10).mean()
    df["MA20"] = df["Close"].rolling(20).mean()
    df["MA50"] = df["Close"].rolling(50).mean()
    df["MA150"] = df["Close"].rolling(150).mean()
    df["MA200"] = df["Close"].rolling(200).mean()
    
    # 2. 거래량 착시 완벽 차단 (중앙값 및 단기 평균 도입)
    df["Vol_MA50"] = df["Volume"].rolling(50).mean()
    df["Vol_Median50"] = df["Volume"].rolling(50).median() # ◀ 중앙값 (폭등 착시 제거)
    df["Vol_MA10"] = df["Volume"].rolling(10).mean()       # ◀ 최근 10일 거래량 (현재 유동성 확인)
    
    # 3. 정확한 거래대금 산출
    df["Daily_Dollar_Volume"] = df["Close"] * df["Volume"]
    df["DollarVol_MA50"] = df["Daily_Dollar_Volume"].rolling(50).mean()
    
    tr1 = df["High"] - df["Low"]
    tr2 = (df["High"] - df["Close"].shift(1)).abs()
    tr3 = (df["Low"] - df["Close"].shift(1)).abs()
    df["TR"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["ATR10"] = df["TR"].rolling(10).mean()
    df["ATR30"] = df["TR"].rolling(30).mean()
    return df

def safe_return(close, days):
    if len(close) <= days: return None
    start, end = close.iloc[-days], close.iloc[-1]
    return end / start - 1 if start and start > 0 else None

def calculate_rs_scores(price_data):
    rows = []
    for ticker, df in price_data.items():
        close = df["Close"].dropna()
        r3, r6, r12 = safe_return(close, 63), safe_return(close, 126), safe_return(close, 252)
        if r3 is None or r6 is None or r12 is None: continue
        wr = r3 * 0.4 + r6 * 0.3 + r12 * 0.3
        rows.append({"ticker": ticker, "r3": r3, "r6": r6, "r12": r12, "weighted_return": wr})
    if not rows: return {}
    rs_df = pd.DataFrame(rows)
    rs_df["rs_rating"] = (rs_df["weighted_return"].rank(pct=True) * 99).round(0).astype(int)
    return {r["ticker"]: r for _, r in rs_df.iterrows()}

def passes_trend_template(ticker, df, rs_info):
    c = cfg()
    try:
        cp = df["Close"].iloc[-1]
        ma50, ma150, ma200 = df["MA50"].iloc[-1], df["MA150"].iloc[-1], df["MA200"].iloc[-1]
        ma200_22 = df["MA200"].iloc[-22]
        low52, high52 = df["Close"].tail(252).min(), df["Close"].tail(252).max()
        
        avg_vol = df["Vol_MA50"].iloc[-1]
        med_vol = df["Vol_Median50"].iloc[-1]
        recent_vol = df["Vol_MA10"].iloc[-1]
        avg_dvol = df["DollarVol_MA50"].iloc[-1]
        
        rs = rs_info.get("rs_rating", 0)
        vals = [cp, ma50, ma150, ma200, low52, high52, avg_vol, med_vol, avg_dvol]
        if any(pd.isna(x) for x in vals): return False
        
        return all([
            cp >= MIN_PRICE,
            cp > ma150 and cp > ma200,
            ma150 > ma200,
            ma200 > ma200_22, # 200일선 최소 1개월 상승 추세
            cp > ma50,
            cp >= low52 * 1.30,
            cp >= high52 * c["high52"],
            rs >= c["rs"],
            avg_vol >= MIN_AVG_VOLUME,
            med_vol >= MIN_AVG_VOLUME * 0.4,   # ◀ 유령주 차단: 중앙값도 최소수준 유지해야 함
            recent_vol >= MIN_AVG_VOLUME * 0.3, # ◀ 현재 호가창이 죽어있지 않아야 함
            avg_dvol >= MIN_DOLLAR_VOLUME,
        ])
    except: return False

def check_vcp_pattern(ticker, df):
    c = cfg()
    try:
        recent = df.tail(75).copy()
        if len(recent) < 75: return False, {}
        
        seg1, seg2, seg3 = recent.iloc[0:30], recent.iloc[30:55], recent.iloc[55:75]
        def cr(seg):
            low, high = seg["Low"].min(), seg["High"].max()
            return (high - low) / low if low > 0 else 0
        r1, r2, r3 = cr(seg1), cr(seg2), cr(seg3)

        vol_early = recent["Volume"].iloc[0:35].mean()
        vol_recent = recent["Volume"].iloc[-10:].mean()
        vol_ma50 = df["Vol_MA50"].iloc[-1]
        
        current, current_vol = df["Close"].iloc[-1], df["Volume"].iloc[-1]
        ma10, ma20 = df["MA10"].iloc[-1], df["MA20"].iloc[-1]
        
        pivot = recent["High"].tail(20).max()
        stop = max(recent["Low"].tail(20).min(), df["MA50"].iloc[-1] * 0.97)
        risk = (pivot - stop) / pivot * 100
        dist = (pivot - current) / pivot * 100 # 양수면 피봇 아래(돌파 전), 음수면 돌파 상태

        # --------------------------------------------------------
        # K-미너비니 셋업 타입 분류 (Breakout / Pullback / Near Pivot)
        # --------------------------------------------------------
        breakout = current > pivot and current_vol >= vol_ma50 * 1.5
        
        # Pullback (선진입/눌림목): 피봇 아래 1~8% 구간 + 10/20일선 근접 지지 + 거래량 바닥
        near_ma = (abs(current - ma20)/ma20 < 0.03) or (abs(current - ma10)/ma10 < 0.03)
        volume_dried = current_vol < vol_ma50 * 0.8
        is_pullback = (0.5 <= dist <= 8.0) and near_ma and volume_dried
        
        if breakout:
            setup_type = "🚀돌파(Breakout)"
        elif is_pullback:
            setup_type = "🎯눌림목/선진입(Pullback)"
        elif 0 <= dist <= c["pivot_near"]:
            setup_type = "🔥돌파임박(Near Pivot)"
        else:
            setup_type = "👀관찰(Watch)"

        tight_closes = recent["Close"].tail(10).std() / recent["Close"].tail(10).mean()
        
        score_items = {
            "range_contracts": r1 > r2 > r3,
            "last_contraction_ok": r3 <= c["last_contraction"],
            "volume_dryup": vol_recent <= vol_early * c["volume_dryup"],
            "setup_ready": (setup_type != "👀관찰(Watch)"),
            "risk_ok": 0 < risk <= c["max_risk"],
            "tight_closes": tight_closes <= 0.025,
        }
        
        vcp_score = sum(1 for v in score_items.values() if v)

        # K-미너비니의 핵심: 형태가 완벽하지 않더라도 '눌림목/돌파임박' 상태면 통과시킴
        if not (score_items["setup_ready"] and score_items["risk_ok"]):
            return False, {}
        if vcp_score < c["min_vcp_score"]:
            return False, {}

        details = {
            "setup_type": setup_type,
            "vcp_score": vcp_score,
            "current_price": round(current, 2),
            "entry": round(pivot, 2),
            "stop": round(stop, 2),
            "risk": round(risk, 1),
            "dist_pct": round(dist, 1),
            "vcp_r1": round(r1 * 100, 1), "vcp_r2": round(r2 * 100, 1), "vcp_r3": round(r3 * 100, 1),
        }
        return True, details
    except: return False, {}

def get_fundamental_info(ticker):
    try:
        info = yf.Ticker(ticker).info
        eps, rev = info.get("earningsGrowth"), info.get("revenueGrowth")
        if eps is not None and rev is not None and eps >= MIN_EPS_GROWTH and rev >= MIN_REV_GROWTH:
            return "PASS", eps, rev
        return "FAIL", eps, rev
    except: return "UNKNOWN", None, None

def main():
    today = pd.Timestamp.now(tz="Asia/Seoul").strftime("%Y-%m-%d")
    c = cfg()

    print("📦 1. 시장 종목 명단 수집 시작...")
    raw_tickers = sorted(set(get_sp500_tickers() + get_nasdaq100_tickers() + get_russell2000_tickers()[0]))
    official_map = get_official_listed_universe()
    tickers, _, _ = validate_candidate_universe(raw_tickers, official_map)

    print("📥 2. 주가 데이터 다운로드 시작...")
    raw_data = pd.DataFrame()
    for i in range(0, len(tickers), CHUNK_SIZE):
        chunk = tickers[i:i + CHUNK_SIZE]
        try:
            cd = yf.download(chunk, period=PRICE_PERIOD, interval="1d", group_by="ticker", progress=False, threads=False, timeout=40, auto_adjust=True)
            if cd is not None and not cd.empty:
                raw_data = cd if raw_data.empty else pd.concat([raw_data, cd], axis=1)
        except: pass
        time.sleep(SLEEP_BETWEEN_CHUNKS)

    print("🧮 3. 필터링 및 RS 계산...")
    price_data = {}
    for t in tickers:
        df = prepare_price_dataframe(get_ticker_dataframe(raw_data, t))
        if df is not None: price_data[t] = df
    rs_map = calculate_rs_scores(price_data)

    print("📉 4. 트렌드 템플릿 및 VCP(눌림목/돌파) 검사...")
    vcp_candidates = []
    for t, df in price_data.items():
        rs = rs_map.get(t, {"rs_rating": 0})
        if passes_trend_template(t, df, rs):
            ok_v, v = check_vcp_pattern(t, df)
            if ok_v:
                vcp_candidates.append((t, rs, v))
    
    # 셋업 타입별 정렬 (돌파 -> 눌림목 -> 돌파임박)
    def setup_priority(setup):
        if "🚀" in setup: return 1
        if "🎯" in setup: return 2
        if "🔥" in setup: return 3
        return 4
    
    vcp_candidates = sorted(vcp_candidates, key=lambda x: (setup_priority(x[2]["setup_type"]), -x[2]["vcp_score"], -x[1].get("rs_rating", 0)))

    print("🧬 5. 실적 검사 및 텔레그램 전송 준비")
    final_results = []
    for t, rs, v in vcp_candidates[:MAX_FUNDAMENTAL_CALLS]:
        status, eps, rev = get_fundamental_info(t)
        v["fundamental_status"] = status
        v["rs_rating"] = rs.get("rs_rating", 0)
        final_results.append(v | {"ticker": t})
        time.sleep(FUNDAMENTAL_SLEEP)

    # Telegram 메시지 포맷팅 (셋업별 시각적 분리)
    msg = f"🔔 [{today}] K-미너비니 정밀 스크리닝 (v9)\n------------------------------------\n"
    
    for setup_kind in ["🚀돌파(Breakout)", "🎯눌림목/선진입(Pullback)", "🔥돌파임박(Near Pivot)"]:
        items = [x for x in final_results if x["setup_type"] == setup_kind]
        if items:
            msg += f"\n{setup_kind} 종목 ({len(items)}개)\n"
            for i in items:
                f_mark = "✅실적OK" if i["fundamental_status"] == "PASS" else "⚠️실적미달"
                msg += f"• {i['ticker']} | 현재 {i['current_price']}$ | 피봇 {i['entry']}$ (이격 {i['dist_pct']}%)\n  - 리스크: {i['risk']}% | RS: {i['rs_rating']} | {f_mark}\n"

    if len(final_results) == 0:
        msg += "\n금일 조건을 만족하는 주도주 후보가 없습니다. (현금 보유 권장)\n"
        
    msg += "\n------------------------------------\n※ 유령주(거래량 착시) 차단 & 눌림목 포착 기능 적용 완료."
    send_telegram_message(msg)
    print("🎯 스크리닝 완료")

if __name__ == "__main__":
    main()
