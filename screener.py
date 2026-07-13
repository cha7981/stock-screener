import os
import sys
import time
import io
import csv
import re
import random
import traceback
import pandas as pd
import requests
import yfinance as yf
from collections import defaultdict

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# ============================================================
# FINAL V11.3 핵심 설정 (유형별 Top5 출력 적용)
# ============================================================
STRICT_MODE = False

# Trend Template
MIN_RS_RATING_BALANCED = 80
MIN_RS_RATING_STRICT = 90
MIN_PRICE = 10.0
MIN_AVG_VOLUME = 200000
MIN_DOLLAR_VOLUME = 3000000
HIGH_52W_NEAR_BALANCED = 0.75
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
MAX_RISK_BALANCED = 10.0
MAX_RISK_STRICT = 6.0
PIVOT_NEAR_PCT_BALANCED = 6.0
PIVOT_NEAR_PCT_STRICT = 4.0
RANGE_TOLERANCE = 0.98
MIN_VCP_SCORE_BALANCED = 5
MIN_VCP_SCORE_STRICT = 7

# Download stability
PRICE_PERIOD = "18mo"
CHUNK_SIZE = 50
SLEEP_BETWEEN_CHUNKS = 8
MARKET_FILTER_ENABLED = True
TOP_TELEGRAM_COUNT = int(os.environ.get("TOP_TELEGRAM_COUNT", "5")) # 각 유형별 최대 5개로 활용
ACCOUNT_SIZE = float(os.environ.get("ACCOUNT_SIZE", "100000"))
ACCOUNT_RISK_PCT = float(os.environ.get("ACCOUNT_RISK_PCT", "0.005"))
MAX_POSITION_PCT = float(os.environ.get("MAX_POSITION_PCT", "0.15"))
ENTRY_BUFFER_PCT = 0.001
ENTRY_ZONE_PCT = 0.02
MAX_CHASE_PCT = 0.05
MAX_STOP_LOSS_PCT = 0.07
MIN_STOP_LOSS_PCT = 0.02
BREAKOUT_VOLUME_RATIO = 1.5

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/csv,application/csv,text/plain,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

def cfg():
    if STRICT_MODE:
        return {
            "rs": MIN_RS_RATING_STRICT, "high52": HIGH_52W_NEAR_STRICT,
            "last_contraction": VCP_LAST_CONTRACTION_STRICT, "contraction_ratio": VCP_CONTRACTION_RATIO_STRICT,
            "volume_dryup": VOLUME_DRYUP_STRICT, "atr_dryup": ATR_DRYUP_STRICT,
            "max_risk": MAX_RISK_STRICT, "pivot_near": PIVOT_NEAR_STRICT,
            "min_vcp_score": MIN_VCP_SCORE_STRICT, "mode": "STRICT",
        }
    return {
        "rs": MIN_RS_RATING_BALANCED, "high52": HIGH_52W_NEAR_BALANCED,
        "last_contraction": VCP_LAST_CONTRACTION_BALANCED, "contraction_ratio": VCP_CONTRACTION_RATIO_BALANCED,
        "volume_dryup": VOLUME_DRYUP_BALANCED, "atr_dryup": ATR_DRYUP_BALANCED,
        "max_risk": MAX_RISK_BALANCED, "pivot_near": PIVOT_NEAR_PCT_BALANCED,
        "min_vcp_score": MIN_VCP_SCORE_BALANCED, "mode": "BALANCED",
    }

# ============================================================
# Common utilities
# ============================================================
def send_telegram_message(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("❌ 텔레그램 환경변수 TELEGRAM_TOKEN 또는 CHAT_ID가 없습니다.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        res = requests.post(url, json={"chat_id": CHAT_ID, "text": message[:4096]}, timeout=15)
        if res.status_code != 200:
            print(f"⚠️ 텔레그램 전송 실패: {res.text}")
    except Exception as e:
        print(f"⚠️ 텔레그램 전송 에러: {e}")

def send_telegram_file(file_path, caption=""):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
        with open(file_path, "rb") as f:
            res = requests.post(
                url, data={"chat_id": CHAT_ID, "caption": caption[:1024]},
                files={"document": (os.path.basename(file_path), f, "text/plain")}, timeout=60,
            )
        if res.status_code != 200:
            return False
        return True
    except Exception as e:
        return False

def clean_ticker(ticker):
    if ticker is None: return ""
    return str(ticker).strip().replace("\ufeff", "").replace('"', "").replace("/", "-").replace(".", "-").upper()

def is_valid_us_ticker(ticker):
    ticker = clean_ticker(ticker)
    if not ticker: return False
    if ticker in {"CASH", "USD", "-", "N/A", "VALUE", "TICKER", "SYMBOL", "NO", "CONSTITUENTS"}: return False
    if " " in ticker or len(ticker) > 8: return False
    if ticker.endswith(("-TO", "-OL", "-DE", "-L", "-PA", "-AS", "-SW", "-VI", "-F")): return False
    if ticker.startswith("RTY") or ticker.startswith("RTYM"): return False
    if re.search(r"\d{3,}", ticker): return False
    return bool(re.match(r"^[A-Z][A-Z0-9]*(?:-[A-Z])?$", ticker))

def is_probably_common_stock(name):
    if not name: return True
    n = str(name).lower()
    bad = [" etf", "exchange traded fund", "etn", "exchange traded note", "warrant", "right", "unit", "preferred", "preference", "depositary share", "depositary shares", "note due", "notes due", "bond", "debenture", "income fund", "closed end fund", "preferred stock", "preferred shares"]
    return not any(x in n for x in bad)

def get_text(url, timeout=30):
    res = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout)
    res.raise_for_status()
    return res.text

def decode_response_text_safely(response):
    content = response.content
    if content.startswith(b"\xff\xfe") or content.startswith(b"\xfe\xff") or content.count(b"\x00") > max(10, len(content) // 20):
        for enc in ["utf-16", "utf-16-le", "utf-16-be"]:
            try:
                text = content.decode(enc, errors="ignore")
                if "Ticker" in text or "Symbol" in text or "Name" in text: return text.replace("\x00", "")
            except Exception: pass
    for enc in ["utf-8-sig", "utf-8", "latin1"]:
        try:
            text = content.decode(enc, errors="ignore").replace("\x00", "")
            if text.strip(): return text
        except Exception: pass
    return response.text.replace("\x00", "")

# ============================================================
# Universe
# ============================================================
def get_sp500_tickers():
    try:
        html = get_text("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
        df = pd.read_html(io.StringIO(html))[0]
        tickers = sorted(set(clean_ticker(x) for x in df["Symbol"].dropna().tolist()))
        return tickers
    except Exception as e:
        return []

def get_nasdaq100_tickers():
    try:
        html = get_text("https://en.wikipedia.org/wiki/Nasdaq-100")
        for df in pd.read_html(io.StringIO(html)):
            df = normalize_columns(df)
            for col in df.columns:
                if str(col).strip().lower() not in {"ticker", "symbol"}: continue
                tickers = sorted(set(clean_ticker(x) for x in df[col].dropna().tolist() if is_valid_us_ticker(clean_ticker(x))))
                if 90 <= len(tickers) <= 110: return tickers
        return []
    except Exception as e:
        return []

def normalize_columns(df):
    df = df.copy()
    df.columns = [str(c).strip().replace("\ufeff", "") for c in df.columns]
    return df

def find_ticker_column(df):
    for c in df.columns:
        lc = str(c).strip().lower().replace(" ", "_")
        if lc in ["ticker", "symbol", "holding_ticker", "constituents"] or "ticker" in lc or "symbol" in lc: return c
    return None

def parse_iwm_text_to_tickers(text):
    text = text.replace("\x00", "").replace("\ufeff", "")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    header_rows = []
    for i, line in enumerate(lines[:100]):
        norm = line.replace('"', '').lower()
        if "ticker" in norm and ("name" in norm or "sector" in norm or "asset class" in norm or "weight" in norm): header_rows.append(i)
    for start in header_rows + list(range(0, min(12, len(lines)))):
        for sep in [",", "\t", ";"]:
            try:
                df = pd.read_csv(io.StringIO("\n".join(lines[start:])), sep=sep, engine="python", on_bad_lines="skip")
                if df.empty or len(df.columns) < 2: continue
                df = normalize_columns(df)
                col = find_ticker_column(df)
                if col is None: continue
                tickers = []
                for x in df[col].dropna().astype(str).tolist():
                    t = clean_ticker(x)
                    if t.startswith("THE") or "BLACKROCK" in t: break
                    if is_valid_us_ticker(t): tickers.append(t)
                tickers = sorted(set(tickers))
                if len(tickers) >= 1000: return tickers
            except Exception: pass
    return []

def get_iwm_official_holdings():
    urls = [
        "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund",
        "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM_holdings",
        "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM",
        "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv",
    ]
    headers = dict(REQUEST_HEADERS)
    headers.update({"Accept": "text/csv,application/csv,text/plain,application/octet-stream,*/*", "Referer": "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf"})
    for url in urls:
        try:
            res = requests.get(url, headers=headers, timeout=45)
            res.raise_for_status()
            tickers = parse_iwm_text_to_tickers(decode_response_text_safely(res))
            if len(tickers) >= 1500: return tickers, f"iShares IWM official CSV ({len(tickers)} tickers)"
        except Exception as e: pass
    return [], "iShares official failed"

def parse_generic_csv_tickers(text):
    try:
        df = pd.read_csv(io.StringIO(text.replace("\x00", "").replace("\ufeff", "")))
        df = normalize_columns(df)
        col = find_ticker_column(df)
        if col is None and len(df.columns) > 0: col = df.columns[0]
        tickers = sorted(set(clean_ticker(x) for x in df[col].dropna().astype(str).tolist() if is_valid_us_ticker(clean_ticker(x))))
        return tickers
    except Exception: return []

def get_russell2000_tickers():
    tickers, source = get_iwm_official_holdings()
    if len(tickers) >= 1500: return tickers, source
    fallbacks = [
        ("GitHub quanthero fallback", "https://raw.githubusercontent.com/quanthero/US_Indices_Constituents/main/Russell2000.csv"),
        ("GitHub ikoniaris fallback", "https://raw.githubusercontent.com/ikoniaris/Russell2000/master/russell_2000_components.csv"),
    ]
    for name, url in fallbacks:
        try:
            res = requests.get(url, headers=REQUEST_HEADERS, timeout=35)
            res.raise_for_status()
            tickers = parse_generic_csv_tickers(decode_response_text_safely(res))
            if len(tickers) >= 1400: return tickers, f"{name} + official listing validation"
        except Exception as e: pass
    return [], "Russell2000 source failed"

def parse_pipe_text(text):
    lines = [ln.strip() for ln in text.replace("\ufeff", "").splitlines() if ln.strip()]
    useful = [ln for ln in lines if not ln.startswith("File Creation Time")]
    return pd.read_csv(io.StringIO("\n".join(useful)), sep="|") if useful else pd.DataFrame()

def get_official_listed_universe():
    official = {}
    try:
        res = requests.get("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", headers=REQUEST_HEADERS, timeout=30)
        res.raise_for_status()
        df = parse_pipe_text(res.text)
        for _, row in df.iterrows():
            sym = clean_ticker(row.get("Symbol", ""))
            if not sym or sym.startswith("FILE"): continue
            official[sym] = {"symbol": sym, "name": str(row.get("Security Name", "")), "exchange": "NASDAQ", "etf": str(row.get("ETF", "N")), "test_issue": str(row.get("Test Issue", "N")), "financial_status": str(row.get("Financial Status", "N")), "source": "nasdaqlisted.txt"}
    except Exception: pass
    try:
        res = requests.get("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", headers=REQUEST_HEADERS, timeout=30)
        res.raise_for_status()
        df = parse_pipe_text(res.text)
        for _, row in df.iterrows():
            sym = clean_ticker(row.get("ACT Symbol", ""))
            if not sym or sym.startswith("FILE"): continue
            official[sym] = {"symbol": sym, "name": str(row.get("Security Name", "")), "exchange": str(row.get("Exchange", "")), "etf": str(row.get("ETF", "N")), "test_issue": str(row.get("Test Issue", "N")), "financial_status": "N", "source": "otherlisted.txt"}
    except Exception: pass
    return official

def validate_candidate_universe(tickers, official_map):
    included, excluded_not_listed, excluded_non_common = [], [], []
    seen = set()
    for raw in tickers:
        t = clean_ticker(raw)
        if not t or t in seen: continue
        seen.add(t)
        if not is_valid_us_ticker(t):
            excluded_non_common.append({"ticker": t, "reason": "BAD_TICKER_FORMAT", "name": "", "exchange": "", "source": "input"})
            continue
        meta = official_map.get(t)
        if meta is None:
            excluded_not_listed.append({"ticker": t, "reason": "NOT_IN_CURRENT_OFFICIAL_LIST", "name": "", "exchange": "", "source": ""})
            continue
        if str(meta.get("test_issue", "N")).upper() == "Y":
            excluded_non_common.append({"ticker": t, "reason": "TEST_ISSUE", **meta})
            continue
        if str(meta.get("etf", "N")).upper() == "Y":
            excluded_non_common.append({"ticker": t, "reason": "ETF", **meta})
            continue
        if not is_probably_common_stock(meta.get("name", "")):
            excluded_non_common.append({"ticker": t, "reason": "NON_COMMON_SECURITY_NAME", **meta})
            continue
        included.append(t)
    return sorted(set(included)), excluded_not_listed, excluded_non_common

# ============================================================
# Price and filters
# ============================================================
def get_ticker_dataframe(raw_data, ticker):
    try:
        if raw_data is None or raw_data.empty: return None
        if not isinstance(raw_data.columns, pd.MultiIndex): return raw_data.copy() if "Close" in raw_data.columns else None
        level0 = raw_data.columns.get_level_values(0)
        level1 = raw_data.columns.get_level_values(1)
        if ticker in level0: return raw_data[ticker].copy()
        if ticker in level1: return raw_data.xs(ticker, axis=1, level=1).copy()
    except Exception: pass
    return None

def prepare_price_dataframe(df):
    if df is None or df.empty: return None
    for col in ["High", "Low", "Close", "Volume"]:
        if col not in df.columns: return None
    df = df.dropna(subset=["Close"]).copy()
    if len(df) < 260: return None
    if getattr(df.index, "tz", None) is not None: df.index = df.index.tz_localize(None)

    df["MA10"] = df["Close"].rolling(10).mean()
    df["MA20"] = df["Close"].rolling(20).mean()
    df["MA50"] = df["Close"].rolling(50).mean()
    df["MA150"] = df["Close"].rolling(150).mean()
    df["MA200"] = df["Close"].rolling(200).mean()

    df["Vol_MA50"] = df["Volume"].rolling(50).mean()
    df["Vol_Median50"] = df["Volume"].rolling(50).median()
    df["Vol_MA10"] = df["Volume"].rolling(10).mean()

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
    if start is None or start <= 0: return None
    return end / start - 1

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
    return {r["ticker"]: {"rs_rating": int(r["rs_rating"]), "r3": float(r["r3"]), "r6": float(r["r6"]), "r12": float(r["r12"])} for _, r in rs_df.iterrows()}

def passes_market_filter():
    if not MARKET_FILTER_ENABLED: return "UNKNOWN", "시장 필터 비활성화"
    try:
        spy = yf.download("SPY", period=PRICE_PERIOD, progress=False, auto_adjust=True, multi_level_index=True, timeout=40)
        close = spy["Close"].dropna()
        if isinstance(close, pd.DataFrame): close = close.iloc[:, 0]
        if len(close) < 210: return "UNKNOWN", "SPY 데이터 부족 → 신규 진입 보류"
        price = float(close.iloc[-1])
        ma50 = float(close.rolling(50).mean().iloc[-1])
        ma200 = float(close.rolling(200).mean().iloc[-1])
        state = "GREEN" if price > ma50 and price > ma200 else "CAUTION"
        status = f"SPY {price:.2f} (MA50 {ma50:.2f}, MA200 {ma200:.2f}) " + ("→ 상승추세 ✅" if state == "GREEN" else "→ 방어모드 ⚠️")
        return state, status
    except Exception as e:
        return "UNKNOWN", f"시장 필터 조회 실패({e}) → 신규 진입 보류"

def passes_trend_template(ticker, df, rs_info):
    c = cfg()
    try:
        cp = df["Close"].iloc[-1]
        ma50, ma150, ma200 = df["MA50"].iloc[-1], df["MA150"].iloc[-1], df["MA200"].iloc[-1]
        ma200_22, ma200_44 = df["MA200"].iloc[-22], df["MA200"].iloc[-44]
        low52, high52 = df["Close"].tail(252).min(), df["Close"].tail(252).max()
        avg_vol = df["Vol_MA50"].iloc[-1]
        med_vol = df["Vol_Median50"].iloc[-1]
        recent_vol = df["Vol_MA10"].iloc[-1]
        avg_dvol = df["DollarVol_MA50"].iloc[-1]
        rs = rs_info.get("rs_rating", 0)
        vals = [cp, ma50, ma150, ma200, ma200_22, ma200_44, low52, high52, avg_vol, med_vol, recent_vol, avg_dvol]
        if any(pd.isna(x) for x in vals): return False
        return all([
            cp >= MIN_PRICE, cp > ma150 and cp > ma200, ma150 > ma200,
            ma200 > ma200_22 and ma200 > ma200_44, ma50 > ma150 and ma50 > ma200,
            cp > ma50, cp >= low52 * 1.30, cp >= high52 * c["high52"], rs >= c["rs"],
            avg_vol >= MIN_AVG_VOLUME, med_vol >= MIN_AVG_VOLUME * 0.4,
            recent_vol >= MIN_AVG_VOLUME * 0.3, avg_dvol >= MIN_DOLLAR_VOLUME,
        ])
    except Exception: return False

def check_vcp_pattern(ticker, df):
    c = cfg()
    try:
        recent = df.tail(75).copy()
        if len(recent) < 75: return False, {}
        def cr(seg):
            low, high = float(seg["Low"].min()), float(seg["High"].max())
            return None if low <= 0 else (high - low) / low
        r1, r2, r3 = cr(recent.iloc[:30]), cr(recent.iloc[30:55]), cr(recent.iloc[55:])
        if None in (r1, r2, r3): return False, {}

        current = float(df["Close"].iloc[-1])
        current_vol = float(df["Volume"].iloc[-1])
        vol_ma50 = float(df["Vol_MA50"].iloc[-1])
        atr10, atr30 = float(df["ATR10"].iloc[-1]), float(df["ATR30"].iloc[-1])
        ma10, ma20, ma50 = (float(df[x].iloc[-1]) for x in ("MA10", "MA20", "MA50"))
        vol_early = float(recent["Volume"].iloc[:35].median())
        vol_recent = float(recent["Volume"].iloc[-10:].median())
        pivot = float(df["High"].iloc[-21:-1].max())
        entry = pivot * (1 + ENTRY_BUFFER_PCT)
        entry_high = pivot * (1 + ENTRY_ZONE_PCT)
        max_chase = pivot * (1 + MAX_CHASE_PCT)
        last_low = float(df["Low"].iloc[-11:-1].min())
        stop = max(last_low - atr10 * 0.25, entry * (1 - MAX_STOP_LOSS_PCT))
        if stop >= entry: return False, {}
        risk_per_share = entry - stop
        risk = risk_per_share / entry * 100
        target1, target2 = entry + risk_per_share * 2, entry + risk_per_share * 3
        dist = (pivot - current) / pivot * 100
        vol_ratio = current_vol / vol_ma50 if vol_ma50 > 0 else 0

        tight10 = float(recent["Close"].tail(10).std() / recent["Close"].tail(10).mean())
        last5 = df.iloc[-6:-1]
        tight5 = float((last5["High"].max() - last5["Low"].min()) / last5["Close"].mean())
        low20 = float(df["Low"].iloc[-21:-11].min())
        low10 = float(df["Low"].iloc[-11:-1].min())
        higher_low = low10 >= low20 * 0.98

        vcp_items = {
            "range_contracts": r1 >= r2 * RANGE_TOLERANCE and r2 >= r3 * RANGE_TOLERANCE,
            "meaningful_contracts": r2 <= r1 * c["contraction_ratio"] and r3 <= r2 * c["contraction_ratio"],
            "last_contraction": r3 <= c["last_contraction"],
            "volume_dryup": vol_recent <= vol_early * c["volume_dryup"],
            "atr_dryup": atr30 > 0 and atr10 <= atr30 * c["atr_dryup"],
            "tight10": tight10 <= 0.03, "tight5": tight5 <= 0.05, "higher_low": higher_low,
        }
        pullback_items = {
            "near_ma10": abs(current / ma10 - 1) <= 0.03, "near_ma20": abs(current / ma20 - 1) <= 0.03,
            "above_ma50": current > ma50, "volume_dryup": vol_recent <= vol_early * 0.85,
            "higher_low": higher_low, "pivot_distance": 0.5 <= dist <= 10.0,
            "risk_ok": MIN_STOP_LOSS_PCT * 100 <= risk <= c["max_risk"], "tight5": tight5 <= 0.06,
        }
        vcp_score = sum(vcp_items.values())
        pullback_score = sum(pullback_items.values())
        breakout = entry <= current <= max_chase and vol_ratio >= BREAKOUT_VOLUME_RATIO

        if breakout: setup_type = "🚀돌파(Breakout)"
        elif pullback_score >= 7 and 0.5 <= dist <= 8.0: setup_type = "🎯눌림목(Pullback)"
        elif 0 <= dist <= c["pivot_near"]: setup_type = "🔥돌파임박(NearPivot)"
        elif pullback_score >= 5 and 0 <= dist <= 12.0: setup_type = "👀Near 눌림목(NearPullback)"
        else: setup_type = "제외"

        if current < entry: action, action_reason = "대기", f"${entry:.2f} 이상 돌파와 거래량 증가를 확인하세요."
        elif current <= entry_high and vol_ratio >= BREAKOUT_VOLUME_RATIO: action, action_reason = "진입 가능", "권장 진입 구간과 거래량 조건을 충족했습니다."
        elif current <= max_chase and vol_ratio >= BREAKOUT_VOLUME_RATIO: action, action_reason = "소액만 검토", "피벗에서 다소 상승했습니다. 수량 축소가 필요합니다."
        elif current > max_chase: action, action_reason = "추격 금지", "피벗에서 5% 이상 상승하여 손익비가 불리합니다."
        else: action, action_reason = "돌파 확인 대기", "가격은 피벗 위지만 거래량 조건이 부족합니다."

        shares_by_risk = int(ACCOUNT_SIZE * ACCOUNT_RISK_PCT / risk_per_share)
        shares_by_value = int(ACCOUNT_SIZE * MAX_POSITION_PCT / entry)
        shares = max(0, min(shares_by_risk, shares_by_value))
        details = {
            "setup_type": setup_type, "action": action, "action_reason": action_reason,
            "vcp_score": vcp_score, "pullback_score": pullback_score,
            "vcp_checks": "; ".join(k for k,v in vcp_items.items() if v),
            "pullback_checks": "; ".join(k for k,v in pullback_items.items() if v),
            "current_price": round(current,2), "pivot": round(pivot,2),
            "entry": round(entry,2), "entry_zone_high": round(entry_high,2),
            "max_chase": round(max_chase,2), "stop": round(stop,2), "risk": round(risk,1),
            "risk_per_share": round(risk_per_share,2), "target1": round(target1,2), "target2": round(target2,2),
            "position_shares": shares, "position_value": round(shares*entry,2),
            "expected_loss": round(shares*risk_per_share,2), "distance_from_pivot": round(dist,1),
            "vcp_r1_pct": round(r1*100,1), "vcp_r2_pct": round(r2*100,1), "vcp_r3_pct": round(r3*100,1),
            "vol_decline_pct": round((1-vol_recent/vol_early)*100,1) if vol_early>0 else None,
            "atr10_atr30_ratio": round(atr10/atr30,2) if atr30>0 else None,
            "tight_close_pct": round(tight10*100,2), "breakout_volume_ratio": round(vol_ratio,2),
            "entry_explanation": f"${entry:.2f}~${entry_high:.2f}, 거래량 50일 평균 {BREAKOUT_VOLUME_RATIO:.1f}배 이상. ${max_chase:.2f} 초과 추격 금지.",
            "stop_explanation": f"마지막 수축 저점과 ATR을 반영한 ${stop:.2f}를 종가 기준 이탈 시 손절.",
            "target_explanation": f"${target1:.2f}(2R)에서 30~50% 분할매도, ${target2:.2f}(3R) 또는 10일선 종가 이탈 시 잔량 관리.",
        }
        risk_ok = MIN_STOP_LOSS_PCT*100 <= risk <= c["max_risk"]
        passed = setup_type != "제외" and risk_ok and (vcp_score >= c["min_vcp_score"] or pullback_score >= 5)
        return passed, details
    except Exception: return False, {}

# ============================================================
# Fundamentals
# ============================================================
def build_fundamental_reason(status, eps, rev, raw_reason):
    eps_txt = "없음" if eps is None else f"{eps * 100:.1f}%"
    rev_txt = "없음" if rev is None else f"{rev * 100:.1f}%"
    if status == "PASS": return f"통과: EPS {eps_txt} ≥ {MIN_EPS_GROWTH * 100:.0f}%, 매출 {rev_txt} ≥ {MIN_REV_GROWTH * 100:.0f}%"
    if status == "FAIL": return f"미통과: EPS {eps_txt}, 매출 {rev_txt} (기준 미달)"
    return f"미확인: {raw_reason}; EPS {eps_txt}, 매출 {rev_txt}"

def get_fundamental_info(ticker):
    for _ in range(2):
        try:
            info = yf.Ticker(ticker).info
            eps, rev = info.get("earningsGrowth"), info.get("revenueGrowth")
            sector, industry = info.get("sector", ""), info.get("industry", "")
            if eps is None or rev is None: return "UNKNOWN", {"eps_growth": eps, "rev_growth": rev, "sector": sector, "industry": industry, "reason": build_fundamental_reason("UNKNOWN", eps, rev, "yfinance 실적 데이터 없음")}
            if eps >= MIN_EPS_GROWTH and rev >= MIN_REV_GROWTH: return "PASS", {"eps_growth": eps, "rev_growth": rev, "sector": sector, "industry": industry, "reason": build_fundamental_reason("PASS", eps, rev, "")}
            return "FAIL", {"eps_growth": eps, "rev_growth": rev, "sector": sector, "industry": industry, "reason": build_fundamental_reason("FAIL", eps, rev, "성장률 기준 미달")}
        except Exception as e:
            if "Too Many Requests" in str(e) or "Rate limited" in str(e):
                time.sleep(FUNDAMENTAL_RETRY_SLEEP)
                continue
            return "UNKNOWN", {"eps_growth": None, "rev_growth": None, "sector": "", "industry": "", "reason": build_fundamental_reason("UNKNOWN", None, None, f"실적 조회 오류: {e}")}
    return "UNKNOWN", {"eps_growth": None, "rev_growth": None, "sector": "", "industry": "", "reason": build_fundamental_reason("UNKNOWN", None, None, "rate limit으로 실적 미확인")}

# ============================================================
# Main
# ============================================================
def main():
    today = pd.Timestamp.now(tz="Asia/Seoul").strftime("%Y-%m-%d")
    c = cfg()

    print(f"🚀 미너비니 스크리닝 시작 (모드: {c['mode']})")
    sp_list = get_sp500_tickers()
    nd_list = get_nasdaq100_tickers()
    ru_list, russell_source = get_russell2000_tickers()
    raw_tickers = sorted(set(sp_list + nd_list + ru_list))
    if not raw_tickers:
        send_telegram_message("❌ 스크리닝 실패: 종목 명단 수집 실패")
        sys.exit(1)

    official_map = get_official_listed_universe()
    tickers, _, _ = validate_candidate_universe(raw_tickers, official_map)

    market_state, market_status = passes_market_filter()
    if market_state != "GREEN": send_telegram_message(f"⚠️ 시장 필터 경고\n{market_status}")

    raw_data = pd.DataFrame()
    for i in range(0, len(tickers), CHUNK_SIZE):
        chunk = tickers[i:i + CHUNK_SIZE]
        print(f"   ↳ 다운로드 중... [{i + 1}~{min(i + CHUNK_SIZE, len(tickers))}/{len(tickers)}]")
        try:
            cd = yf.download(chunk, period=PRICE_PERIOD, interval="1d", group_by="ticker", progress=False, threads=False, timeout=40, auto_adjust=True, multi_level_index=True)
            if cd is not None and not cd.empty: raw_data = cd if raw_data.empty else pd.concat([raw_data, cd], axis=1)
        except Exception: pass
        time.sleep(SLEEP_BETWEEN_CHUNKS + random.uniform(0, 2))

    if raw_data.empty:
        send_telegram_message("❌ 스크리닝 실패: 가격 데이터 다운로드 전체 실패")
        sys.exit(1)

    price_data = {}
    for t in tickers:
        df = prepare_price_dataframe(get_ticker_dataframe(raw_data, t))
        if df is not None: price_data[t] = df

    rs_map = calculate_rs_scores(price_data)

    passed_trend = []
    for t, df in price_data.items():
        rs = rs_map.get(t, {"rs_rating": 0})
        if passes_trend_template(t, df, rs): passed_trend.append((t, df, rs))

    vcp_candidates = []
    for t, df, rs in passed_trend:
        ok_v, v = check_vcp_pattern(t, df)
        if ok_v: vcp_candidates.append((t, df, rs, v))

    vcp_candidates = sorted(vcp_candidates, key=lambda x: (0 if "🚀" in x[3]["setup_type"] else (1 if "🎯" in x[3]["setup_type"] else 2), -x[3]["vcp_score"], -x[2].get("rs_rating", 0)))

    final, tech_only = [], []
    for idx, (t, df, rs, v) in enumerate(vcp_candidates[:MAX_FUNDAMENTAL_CALLS], start=1):
        status, f = get_fundamental_info(t)
        base = {
            "ticker": t, "setup_type": v["setup_type"], "vcp_score": v["vcp_score"], "pullback_score": v["pullback_score"],
            "current_price": v["current_price"], "action": v["action"], "action_reason": v["action_reason"],
            "entry": v["entry"], "entry_zone_high": v["entry_zone_high"], "stop": v["stop"], "risk": v["risk"],
            "expected_loss": v["expected_loss"], "rs_rating": rs.get("rs_rating", 0), "fundamental_status": status,
        }
        if status == "PASS": final.append(base)
        else: tech_only.append(base)
        time.sleep(FUNDAMENTAL_SLEEP + random.uniform(0, 1))

    for t, df, rs, v in vcp_candidates[MAX_FUNDAMENTAL_CALLS:]:
        tech_only.append({
            "ticker": t, "setup_type": v["setup_type"], "vcp_score": v["vcp_score"], "pullback_score": v["pullback_score"],
            "current_price": v["current_price"], "action": v["action"], "action_reason": v["action_reason"],
            "entry": v["entry"], "entry_zone_high": v["entry_zone_high"], "stop": v["stop"], "risk": v["risk"],
            "expected_loss": v["expected_loss"], "rs_rating": rs.get("rs_rating", 0), "fundamental_status": "NOT_CHECKED",
        })

    all_candidates = final + tech_only
    category_order = ["🚀돌파(Breakout)", "🎯눌림목(Pullback)", "🔥돌파임박(NearPivot)", "👀Near 눌림목(NearPullback)"]
    rank_map = {name: i for i, name in enumerate(category_order)}[span_1](start_span)[span_1](end_span)
    all_candidates.sort(key=lambda x: (rank_map.get(x.get("setup_type"), 99), -x.get("vcp_score", 0), -x.get("pullback_score", 0), -x.get("rs_rating", 0)))

    # =========================================================================
    # 📝 [핵심 수정 구간] 카테고리별 TOP 5 추출 및 텔레그램 포맷 적용
    # =========================================================================
    top_candidates_by_category = defaultdict(list)
    for item in all_candidates:
        cat = item.get("setup_type", "분류없음")
        if len(top_candidates_by_category[cat]) < TOP_TELEGRAM_COUNT:  # 각 유형별 최대 5개 (기본값 세팅 연동)
            top_candidates_by_category[cat].append(item)

    # 텍스트 파일 생성용 데이터 준비 (전체 목록)
    txt_file = f"minervini_all_candidates_{today}.txt"
    with open(txt_file, "w", encoding="utf-8") as out:
        out.write(f"[{today}] 미너비니 전체 후보 V11.3 (유형별 Top5)\n")
        out.write(f"시장: {market_status}\n후보 수: {len(all_candidates)}\n\n")
        for item in all_candidates:
            out.write("=" * 70 + "\n")
            out.write(f"{item['ticker']} | {item['setup_type']} | {item['action']}\n")
            out.write(f"VCP 점수: {item.get('vcp_score',0)}/8 | 눌림목 점수: {item.get('pullback_score',0)}/8 | RS: {item.get('rs_rating',0)}\n")
            out.write(f"현재가: ${item['current_price']} | 진입구간: ${item['entry']} ~ ${item['entry_zone_high']}\n")
            out.write(f"손절: ${item['stop']} (-{item['risk']}%)\n")
            out.write(f"행동설명: {item['action_reason']}\n")

    # 텔레그램 메시지 발송 로직
    msg = (f"🔔 [{today}] K-미너비니 유형별 TOP 5\n"
           f"시장: {market_status}\n"
           f"전체후보 {len(all_candidates)}개 (유니버스 {len(tickers)} → 추세 {len(passed_trend)})\n\n")

    if not all_candidates:
        msg += "금일 조건을 만족하는 후보가 없습니다.\n(시장 폭락 등으로 인해 VCP 차트 형태가 무너졌을 확률이 높습니다.)\n"
    else:
        for cat in category_order:
            cat_items = top_candidates_by_category.get(cat, [])
            if not cat_items:
                continue
            msg += f"🔥 [{cat} TOP {len(cat_items)}]\n"
            for item in cat_items:
                msg += (f"▪ {item['ticker']} [{item['action']}]\n"
                        f"  └ VCP {item.get('vcp_score',0)} | 눌림 {item.get('pullback_score',0)} | RS {item['rs_rating']}\n"
                        f"  └ 현재 ${item['current_price']} | 진입 ${item['entry']}~${item['entry_zone_high']}\n"
                        f"  └ 손절 ${item['stop']} (-{item['risk']}%)\n")
            msg += "\n"

    msg += "전체 후보 상세내용은 첨부 TXT를 확인하세요."
    send_telegram_message(msg)
    send_telegram_file(txt_file, f"[{today}] 미너비니 전체 후보 {len(all_candidates)}개")
    print("🎯 전체 스크리닝 완료 및 텔레그램 발송 완료")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        error_detail = traceback.format_exc()
        try:
            send_telegram_message(f"❌ 스크리닝 실패\n{type(e).__name__}: {e}")
        except Exception:
            pass
        sys.exit(1)
