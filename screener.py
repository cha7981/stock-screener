
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
# V8 핵심 설정
# ============================================================
# 너무 Severe 하지 않도록 기본값은 BALANCED입니다.
# STRICT_MODE=True로 바꾸면 더 정석 VCP에 가까워지지만 후보가 크게 줄 수 있습니다.
STRICT_MODE = False

# Trend Template
MIN_RS_RATING_BALANCED = 80
MIN_RS_RATING_STRICT = 90
MIN_PRICE = 5.0
MIN_AVG_VOLUME = 150000
MIN_DOLLAR_VOLUME = 2000000
HIGH_52W_NEAR_BALANCED = 0.85   # 현재가가 52주 고점의 85% 이상
HIGH_52W_NEAR_STRICT = 0.90     # 현재가가 52주 고점의 90% 이상

# Fundamentals
MIN_EPS_GROWTH = 0.20
MIN_REV_GROWTH = 0.15
MAX_FUNDAMENTAL_CALLS = 40
FUNDAMENTAL_SLEEP = 3
FUNDAMENTAL_RETRY_SLEEP = 45

# VCP Balanced / Strict thresholds
VCP_LAST_CONTRACTION_BALANCED = 0.07    # 마지막 수축폭 7% 이하
VCP_LAST_CONTRACTION_STRICT = 0.05      # 마지막 수축폭 5% 이하
VCP_CONTRACTION_RATIO_BALANCED = 0.85   # r2 <= r1*0.85, r3 <= r2*0.85
VCP_CONTRACTION_RATIO_STRICT = 0.75
VOLUME_DRYUP_BALANCED = 0.85            # 최근 거래량 <= 초기 거래량 85%
VOLUME_DRYUP_STRICT = 0.70
ATR_DRYUP_BALANCED = 0.90               # ATR10 <= ATR30*0.90
ATR_DRYUP_STRICT = 0.70
MAX_RISK_BALANCED = 8.0
MAX_RISK_STRICT = 6.0
PIVOT_NEAR_PCT_BALANCED = 6.0
PIVOT_NEAR_PCT_STRICT = 4.0
MIN_VCP_SCORE_BALANCED = 6              # 8점 만점 중 6점 이상
MIN_VCP_SCORE_STRICT = 7                # 8점 만점 중 7점 이상

# Download stability
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
# Common utilities
# ============================================================
def send_telegram_message(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("❌ 텔레그램 환경변수 TELEGRAM_TOKEN 또는 CHAT_ID가 없습니다.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        res = requests.post(url, json={"chat_id": CHAT_ID, "text": message}, timeout=15)
        if res.status_code != 200:
            print(f"⚠️ 텔레그램 전송 실패: {res.text}")
    except Exception as e:
        print(f"⚠️ 텔레그램 전송 에러: {e}")


def clean_ticker(ticker):
    if ticker is None:
        return ""
    return str(ticker).strip().replace("\ufeff", "").replace('"', "").replace("/", "-").replace(".", "-").upper()


def is_valid_us_ticker(ticker):
    ticker = clean_ticker(ticker)
    if not ticker:
        return False
    if ticker in {"CASH", "USD", "-", "N/A", "VALUE", "TICKER", "SYMBOL", "NO", "CONSTITUENTS"}:
        return False
    if " " in ticker or len(ticker) > 8:
        return False
    if ticker.endswith(("-TO", "-OL", "-DE", "-L", "-PA", "-AS", "-SW", "-VI", "-F")):
        return False
    if ticker.startswith("RTY") or ticker.startswith("RTYM"):
        return False
    if re.search(r"\d{3,}", ticker):
        return False
    return bool(re.match(r"^[A-Z][A-Z0-9]*(?:-[A-Z])?$", ticker))


def is_probably_common_stock(name):
    if not name:
        return True
    n = str(name).lower()
    bad = [
        " etf", "exchange traded fund", "etn", "exchange traded note",
        "warrant", "right", "unit", "preferred", "preference",
        "depositary share", "depositary shares", "note due", "notes due",
        "bond", "debenture", "income fund", "closed end fund",
        "preferred stock", "preferred shares"
    ]
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
                if "Ticker" in text or "Symbol" in text or "Name" in text:
                    return text.replace("\x00", "")
            except Exception:
                pass
    for enc in ["utf-8-sig", "utf-8", "latin1"]:
        try:
            text = content.decode(enc, errors="ignore").replace("\x00", "")
            if text.strip():
                return text
        except Exception:
            pass
    return response.text.replace("\x00", "")


# ============================================================
# Universe
# ============================================================
def get_sp500_tickers():
    try:
        html = get_text("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
        df = pd.read_html(io.StringIO(html))[0]
        tickers = sorted(set(clean_ticker(x) for x in df["Symbol"].dropna().tolist()))
        print(f"✅ S&P500 수집 성공: {len(tickers)}개")
        return tickers
    except Exception as e:
        print(f"❌ S&P500 수집 실패: {e}")
        return []


def get_nasdaq100_tickers():
    try:
        html = get_text("https://en.wikipedia.org/wiki/Nasdaq-100")
        dfs = pd.read_html(io.StringIO(html), attrs={"id": "constituents"})
        if not dfs:
            return []
        tickers = sorted(set(clean_ticker(x) for x in dfs[0]["Ticker"].dropna().tolist()))
        print(f"✅ Nasdaq100 수집 성공: {len(tickers)}개")
        return tickers
    except Exception as e:
        print(f"❌ Nasdaq100 수집 실패: {e}")
        return []


def normalize_columns(df):
    df = df.copy()
    df.columns = [str(c).strip().replace("\ufeff", "") for c in df.columns]
    return df


def find_ticker_column(df):
    for c in df.columns:
        lc = str(c).strip().lower().replace(" ", "_")
        if lc in ["ticker", "symbol", "holding_ticker", "constituents"] or "ticker" in lc or "symbol" in lc:
            return c
    return None


def parse_iwm_text_to_tickers(text):
    text = text.replace("\x00", "").replace("\ufeff", "")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    header_rows = []
    for i, line in enumerate(lines[:100]):
        norm = line.replace('"', '').lower()
        if "ticker" in norm and ("name" in norm or "sector" in norm or "asset class" in norm or "weight" in norm):
            header_rows.append(i)
    for start in header_rows + list(range(0, min(12, len(lines)))):
        for sep in [",", "\t", ";"]:
            try:
                df = pd.read_csv(io.StringIO("\n".join(lines[start:])), sep=sep, engine="python", on_bad_lines="skip")
                if df.empty or len(df.columns) < 2:
                    continue
                df = normalize_columns(df)
                col = find_ticker_column(df)
                if col is None:
                    continue
                tickers = []
                for x in df[col].dropna().astype(str).tolist():
                    t = clean_ticker(x)
                    if t.startswith("THE") or "BLACKROCK" in t:
                        break
                    if is_valid_us_ticker(t):
                        tickers.append(t)
                tickers = sorted(set(tickers))
                if len(tickers) >= 1000:
                    return tickers
            except Exception:
                pass
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
            print("📥 Russell2000 최신 후보: iShares IWM 공식 holdings CSV 시도")
            res = requests.get(url, headers=headers, timeout=45)
            res.raise_for_status()
            tickers = parse_iwm_text_to_tickers(decode_response_text_safely(res))
            if len(tickers) >= 1500:
                return tickers, f"iShares IWM official CSV ({len(tickers)} tickers)"
            print(f"⚠️ iShares CSV 파싱 결과 적음: {len(tickers)}개")
        except Exception as e:
            print(f"⚠️ iShares CSV 수집 실패: {e}")
    return [], "iShares official failed"


def parse_generic_csv_tickers(text):
    try:
        df = pd.read_csv(io.StringIO(text.replace("\x00", "").replace("\ufeff", "")))
        df = normalize_columns(df)
        col = find_ticker_column(df)
        if col is None and len(df.columns) > 0:
            col = df.columns[0]
        tickers = sorted(set(clean_ticker(x) for x in df[col].dropna().astype(str).tolist() if is_valid_us_ticker(clean_ticker(x))))
        return tickers
    except Exception:
        return []


def get_russell2000_tickers():
    tickers, source = get_iwm_official_holdings()
    if len(tickers) >= 1500:
        print(f"✅ Russell2000/IWM 최신 소스 성공: {source}")
        return tickers, source
    fallbacks = [
        ("GitHub quanthero fallback", "https://raw.githubusercontent.com/quanthero/US_Indices_Constituents/main/Russell2000.csv"),
        ("GitHub ikoniaris fallback", "https://raw.githubusercontent.com/ikoniaris/Russell2000/master/russell_2000_components.csv"),
    ]
    for name, url in fallbacks:
        try:
            print(f"📥 Russell2000 fallback 수집 시도: {name}")
            res = requests.get(url, headers=REQUEST_HEADERS, timeout=35)
            res.raise_for_status()
            tickers = parse_generic_csv_tickers(decode_response_text_safely(res))
            if len(tickers) >= 1400:
                print(f"✅ Russell2000 fallback 수집 성공({name}): {len(tickers)}개")
                return tickers, f"{name} + official listing validation"
        except Exception as e:
            print(f"⚠️ {name} 수집 실패: {e}")
    return [], "Russell2000 source failed"


def parse_pipe_text(text):
    lines = [ln.strip() for ln in text.replace("\ufeff", "").splitlines() if ln.strip()]
    useful = [ln for ln in lines if not ln.startswith("File Creation Time")]
    return pd.read_csv(io.StringIO("\n".join(useful)), sep="|") if useful else pd.DataFrame()


def get_official_listed_universe():
    official = {}
    try:
        print("📥 공식 상장 리스트 수집: Nasdaq Trader nasdaqlisted.txt")
        res = requests.get("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", headers=REQUEST_HEADERS, timeout=30)
        res.raise_for_status()
        df = parse_pipe_text(res.text)
        for _, row in df.iterrows():
            sym = clean_ticker(row.get("Symbol", ""))
            if not sym or sym.startswith("FILE"):
                continue
            official[sym] = {"symbol": sym, "name": str(row.get("Security Name", "")), "exchange": "NASDAQ", "etf": str(row.get("ETF", "N")), "test_issue": str(row.get("Test Issue", "N")), "financial_status": str(row.get("Financial Status", "N")), "source": "nasdaqlisted.txt"}
    except Exception as e:
        print(f"⚠️ Nasdaq Trader nasdaqlisted 실패: {e}")
    try:
        print("📥 공식 상장 리스트 수집: Nasdaq Trader otherlisted.txt")
        res = requests.get("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", headers=REQUEST_HEADERS, timeout=30)
        res.raise_for_status()
        df = parse_pipe_text(res.text)
        for _, row in df.iterrows():
            sym = clean_ticker(row.get("ACT Symbol", ""))
            if not sym or sym.startswith("FILE"):
                continue
            official[sym] = {"symbol": sym, "name": str(row.get("Security Name", "")), "exchange": str(row.get("Exchange", "")), "etf": str(row.get("ETF", "N")), "test_issue": str(row.get("Test Issue", "N")), "financial_status": "N", "source": "otherlisted.txt"}
    except Exception as e:
        print(f"⚠️ Nasdaq Trader otherlisted 실패: {e}")
    print(f"✅ 공식 상장 리스트 확보: {len(official)}개")
    return official


def validate_candidate_universe(tickers, official_map):
    included, excluded_not_listed, excluded_non_common = [], [], []
    seen = set()
    for raw in tickers:
        t = clean_ticker(raw)
        if not t or t in seen:
            continue
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
        if raw_data is None or raw_data.empty:
            return None
        if isinstance(raw_data.columns, pd.MultiIndex):
            return raw_data[ticker].copy() if ticker in raw_data.columns.get_level_values(0) else None
        if "Close" in raw_data.columns:
            return raw_data.copy()
    except Exception:
        return None
    return None


def prepare_price_dataframe(df):
    if df is None or df.empty:
        return None
    for col in ["High", "Low", "Close", "Volume"]:
        if col not in df.columns:
            return None
    df = df.dropna(subset=["Close"]).copy()
    if len(df) < 260:
        return None
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_localize(None)
    df["MA50"] = df["Close"].rolling(50).mean()
    df["MA150"] = df["Close"].rolling(150).mean()
    df["MA200"] = df["Close"].rolling(200).mean()
    df["Vol_MA50"] = df["Volume"].rolling(50).mean()
    df["DollarVol_MA50"] = df["Close"].rolling(50).mean() * df["Vol_MA50"]
    tr1 = df["High"] - df["Low"]
    tr2 = (df["High"] - df["Close"].shift(1)).abs()
    tr3 = (df["Low"] - df["Close"].shift(1)).abs()
    df["TR"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["ATR10"] = df["TR"].rolling(10).mean()
    df["ATR30"] = df["TR"].rolling(30).mean()
    return df


def safe_return(close, days):
    if len(close) <= days:
        return None
    start, end = close.iloc[-days], close.iloc[-1]
    if start is None or start <= 0:
        return None
    return end / start - 1


def calculate_rs_scores(price_data):
    rows = []
    for ticker, df in price_data.items():
        close = df["Close"].dropna()
        r3, r6, r12 = safe_return(close, 63), safe_return(close, 126), safe_return(close, 252)
        if r3 is None or r6 is None or r12 is None:
            continue
        wr = r3 * 0.4 + r6 * 0.3 + r12 * 0.3
        rows.append({"ticker": ticker, "r3": r3, "r6": r6, "r12": r12, "weighted_return": wr})
    if not rows:
        return {}
    rs_df = pd.DataFrame(rows)
    rs_df["rs_rating"] = (rs_df["weighted_return"].rank(pct=True) * 99).round(0).astype(int)
    return {r["ticker"]: {"rs_rating": int(r["rs_rating"]), "r3": float(r["r3"]), "r6": float(r["r6"]), "r12": float(r["r12"])} for _, r in rs_df.iterrows()}


def passes_market_filter():
    if not MARKET_FILTER_ENABLED:
        return True, "비활성화: Yahoo SSL/rate limit 회피"
    return True, "활성화되지 않음"


def passes_trend_template(ticker, df, rs_info):
    c = cfg()
    try:
        cp = df["Close"].iloc[-1]
        ma50, ma150, ma200 = df["MA50"].iloc[-1], df["MA150"].iloc[-1], df["MA200"].iloc[-1]
        ma200_22, ma200_44 = df["MA200"].iloc[-22], df["MA200"].iloc[-44]
        low52, high52 = df["Close"].tail(252).min(), df["Close"].tail(252).max()
        avg_vol, avg_dvol = df["Vol_MA50"].iloc[-1], df["DollarVol_MA50"].iloc[-1]
        rs = rs_info.get("rs_rating", 0)
        vals = [cp, ma50, ma150, ma200, ma200_22, ma200_44, low52, high52, avg_vol, avg_dvol]
        if any(pd.isna(x) for x in vals):
            return False
        return all([
            cp >= MIN_PRICE,
            cp > ma150 and cp > ma200,
            ma150 > ma200,
            ma200 > ma200_22 and ma200 > ma200_44,
            ma50 > ma150 and ma50 > ma200,
            cp > ma50,
            cp >= low52 * 1.30,
            cp >= high52 * c["high52"],
            rs >= c["rs"],
            avg_vol >= MIN_AVG_VOLUME,
            avg_dvol >= MIN_DOLLAR_VOLUME,
        ])
    except Exception as e:
        print(f"⚠️ {ticker} 트렌드 템플릿 오류: {e}")
        return False


def check_vcp_pattern(ticker, df):
    c = cfg()
    try:
        recent = df.tail(75).copy()
        if len(recent) < 75:
            return False, {}
        seg1, seg2, seg3 = recent.iloc[0:30], recent.iloc[30:55], recent.iloc[55:75]
        def cr(seg):
            low, high = seg["Low"].min(), seg["High"].max()
            return None if low <= 0 else (high - low) / low
        r1, r2, r3 = cr(seg1), cr(seg2), cr(seg3)
        if r1 is None or r2 is None or r3 is None:
            return False, {}

        vol_early = recent["Volume"].iloc[0:35].mean()
        vol_recent = recent["Volume"].iloc[-10:].mean()
        vol_ma50 = df["Vol_MA50"].iloc[-1]
        atr10, atr30 = df["ATR10"].iloc[-1], df["ATR30"].iloc[-1]
        current, current_vol = df["Close"].iloc[-1], df["Volume"].iloc[-1]
        pivot = recent["High"].tail(20).max()
        stop = max(recent["Low"].tail(20).min(), df["MA50"].iloc[-1] * 0.97)
        risk = (pivot - stop) / pivot * 100
        dist = (pivot - current) / pivot * 100

        near_pivot = current <= pivot * 1.03 and current >= pivot * (1 - c["pivot_near"] / 100)
        breakout = current > pivot and current_vol >= vol_ma50 * 1.5
        tight_closes = recent["Close"].tail(10).std() / recent["Close"].tail(10).mean()
        high_proximity = current >= recent["High"].max() * 0.95

        score_items = {
            "range_contracts": r1 > r2 > r3,
            "meaningful_contracts": r2 <= r1 * c["contraction_ratio"] and r3 <= r2 * c["contraction_ratio"],
            "last_contraction_ok": r3 <= c["last_contraction"],
            "volume_dryup": vol_recent <= vol_early * c["volume_dryup"],
            "atr_dryup": False if pd.isna(atr10) or pd.isna(atr30) or atr30 <= 0 else atr10 <= atr30 * c["atr_dryup"],
            "near_pivot_or_breakout": near_pivot or breakout,
            "risk_ok": 0 < risk <= c["max_risk"],
            "tight_closes": tight_closes <= 0.025,
            "high_proximity": high_proximity,
        }
        # 9개 중 점수. high_proximity는 정석성 보조 조건으로 포함.
        vcp_score = sum(1 for v in score_items.values() if v)

        # 필수 조건: 수축 방향, 피벗 근처, 리스크
        essential = score_items["range_contracts"] and score_items["near_pivot_or_breakout"] and score_items["risk_ok"]
        if not essential:
            return False, {}
        if vcp_score < c["min_vcp_score"]:
            return False, {}

        quality = "STRICT_LIKE" if vcp_score >= 8 and r3 <= VCP_LAST_CONTRACTION_STRICT else "BALANCED"
        details = {
            "setup_type": "BREAKOUT" if breakout else "WATCH",
            "vcp_quality": quality,
            "vcp_score": vcp_score,
            "vcp_checks": "; ".join([k for k, v in score_items.items() if v]),
            "current_price": round(current, 2),
            "entry": round(pivot, 2),
            "stop": round(stop, 2),
            "risk": round(risk, 1),
            "distance_from_pivot": round(dist, 1),
            "vcp_r1": round(r1 * 100, 1),
            "vcp_r2": round(r2 * 100, 1),
            "vcp_r3": round(r3 * 100, 1),
            "vol_decline": round((1 - vol_recent / vol_early) * 100, 1) if vol_early > 0 else None,
            "atr10_atr30_ratio": round(atr10 / atr30, 2) if not pd.isna(atr10) and not pd.isna(atr30) and atr30 > 0 else None,
            "tight_close_pct": round(tight_closes * 100, 2),
            "breakout_volume_ratio": round(current_vol / vol_ma50, 2) if vol_ma50 and vol_ma50 > 0 else None,
        }
        return True, details
    except Exception as e:
        print(f"⚠️ {ticker} VCP 검사 오류: {e}")
        return False, {}


# ============================================================
# Fundamentals
# ============================================================
def build_fundamental_reason(status, eps, rev, raw_reason):
    eps_txt = "없음" if eps is None else f"{eps * 100:.1f}%"
    rev_txt = "없음" if rev is None else f"{rev * 100:.1f}%"
    eps_req = f"{MIN_EPS_GROWTH * 100:.0f}%"
    rev_req = f"{MIN_REV_GROWTH * 100:.0f}%"
    if status == "PASS":
        return f"통과: EPS {eps_txt} ≥ {eps_req}, 매출 {rev_txt} ≥ {rev_req}"
    if status == "FAIL":
        parts = []
        if eps is None:
            parts.append(f"EPS 데이터 없음, 기준 {eps_req}")
        elif eps < MIN_EPS_GROWTH:
            parts.append(f"EPS {eps_txt} < 기준 {eps_req}")
        if rev is None:
            parts.append(f"매출 데이터 없음, 기준 {rev_req}")
        elif rev < MIN_REV_GROWTH:
            parts.append(f"매출 {rev_txt} < 기준 {rev_req}")
        return "미통과: " + "; ".join(parts)
    return f"미확인: {raw_reason}; EPS {eps_txt}, 매출 {rev_txt}"


def get_fundamental_info(ticker):
    for _ in range(2):
        try:
            info = yf.Ticker(ticker).info
            eps, rev = info.get("earningsGrowth"), info.get("revenueGrowth")
            sector, industry = info.get("sector", ""), info.get("industry", "")
            if eps is None or rev is None:
                return "UNKNOWN", {"eps_growth": eps, "rev_growth": rev, "sector": sector, "industry": industry, "reason": build_fundamental_reason("UNKNOWN", eps, rev, "yfinance 실적 데이터 없음")}
            if eps >= MIN_EPS_GROWTH and rev >= MIN_REV_GROWTH:
                return "PASS", {"eps_growth": eps, "rev_growth": rev, "sector": sector, "industry": industry, "reason": build_fundamental_reason("PASS", eps, rev, "")}
            return "FAIL", {"eps_growth": eps, "rev_growth": rev, "sector": sector, "industry": industry, "reason": build_fundamental_reason("FAIL", eps, rev, "성장률 기준 미달")}
        except Exception as e:
            msg = str(e)
            if "Too Many Requests" in msg or "Rate limited" in msg:
                print(f"⚠️ {ticker} 실적 조회 rate limit, {FUNDAMENTAL_RETRY_SLEEP}초 대기 후 재시도")
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

    print("📦 1. 미국 시장 종목 명단 수집 시작...")
    sp_list = get_sp500_tickers()
    nd_list = get_nasdaq100_tickers()
    ru_list, russell_source = get_russell2000_tickers()
    raw_tickers = sorted(set(sp_list + nd_list + ru_list))
    print(f"📊 원본 수집 완료 -> S&P500: {len(sp_list)}개 | Nasdaq100: {len(nd_list)}개 | Russell2000: {len(ru_list)}개")
    print(f"📌 Russell2000 소스: {russell_source}")
    print(f"🚀 원본 총 스캔 대상: {len(raw_tickers)}개")
    if not raw_tickers:
        sys.exit(1)

    print("🧾 2. 공식 상장 리스트 대조 시작...")
    official_map = get_official_listed_universe()
    tickers, excluded_not_listed, excluded_non_common = validate_candidate_universe(raw_tickers, official_map)
    print(f"✅ 공식 상장 보통주 후보: {len(tickers)}개")
    print(f"🧹 공식 리스트 미등록 제외: {len(excluded_not_listed)}개")
    print(f"🧹 ETF/비보통주/형식 제외: {len(excluded_non_common)}개")

    pd.DataFrame(excluded_not_listed).to_csv(f"excluded_not_currently_listed_{today}.csv", index=False)
    pd.DataFrame(excluded_non_common).to_csv(f"excluded_non_common_{today}.csv", index=False)
    pd.DataFrame([{
        "date": today,
        "mode": c["mode"],
        "russell2000_source": russell_source,
        "sp500_count": len(sp_list),
        "nasdaq100_count": len(nd_list),
        "russell2000_count": len(ru_list),
        "raw_universe_count": len(raw_tickers),
        "official_common_universe_count": len(tickers),
        "excluded_not_listed_count": len(excluded_not_listed),
        "excluded_non_common_count": len(excluded_non_common),
        "min_rs_rating": c["rs"],
        "high52_threshold": c["high52"],
        "last_contraction_threshold": c["last_contraction"],
        "min_vcp_score": c["min_vcp_score"],
    }]).to_csv(f"universe_source_summary_{today}.csv", index=False)

    _, market_status = passes_market_filter()
    print(f"🌎 시장 환경: {market_status}")

    print("📥 3. 주가 데이터 분할 다운로드 시작...")
    raw_data = pd.DataFrame()
    for i in range(0, len(tickers), CHUNK_SIZE):
        chunk = tickers[i:i + CHUNK_SIZE]
        print(f"   ↳ 다운로드 중... [{i + 1}~{min(i + CHUNK_SIZE, len(tickers))}/{len(tickers)}] ({len(chunk)}개 종목)")
        try:
            cd = yf.download(chunk, period=PRICE_PERIOD, interval="1d", group_by="ticker", progress=False, threads=False, timeout=40, auto_adjust=True)
            if cd is not None and not cd.empty:
                raw_data = cd if raw_data.empty else pd.concat([raw_data, cd], axis=1)
        except Exception as e:
            print(f"⚠️ 청크 다운로드 실패: {e}")
        time.sleep(SLEEP_BETWEEN_CHUNKS + random.uniform(0, 2))

    if raw_data.empty:
        print("❌ 가격 데이터를 전혀 받지 못했습니다.")
        sys.exit(1)

    print("🧮 4. 가격 데이터 정리 및 RS Rating 계산...")
    price_data = {}
    yfinance_failed_but_listed = []
    for t in tickers:
        df = prepare_price_dataframe(get_ticker_dataframe(raw_data, t))
        if df is not None:
            price_data[t] = df
        else:
            meta = official_map.get(t, {})
            yfinance_failed_but_listed.append({"ticker": t, "reason": "LISTED_BUT_YFINANCE_PRICE_DATA_FAILED_OR_INSUFFICIENT", "name": meta.get("name", ""), "exchange": meta.get("exchange", ""), "source": meta.get("source", "")})
    pd.DataFrame(yfinance_failed_but_listed).to_csv(f"yfinance_failed_but_listed_{today}.csv", index=False)

    rs_map = calculate_rs_scores(price_data)
    print(f"✅ 유효 가격 데이터: {len(price_data)}개 / RS 계산: {len(rs_map)}개")
    print(f"⚠️ 공식 상장이나 yfinance 가격 미확보: {len(yfinance_failed_but_listed)}개")

    print("📈 5. 트렌드 템플릿 검사...")
    passed_trend = []
    for t, df in price_data.items():
        rs = rs_map.get(t, {"rs_rating": 0})
        if passes_trend_template(t, df, rs):
            passed_trend.append((t, df, rs))
    print(f"🎯 트렌드 템플릿 통과: {len(passed_trend)}개")

    print("📉 6. VCP 정석 조건 검사")
    vcp_candidates = []
    for t, df, rs in passed_trend:
        ok_v, v = check_vcp_pattern(t, df)
        if ok_v:
            vcp_candidates.append((t, df, rs, v))
            print(f"✅ VCP 후보: {t} | {v['setup_type']} | {v['vcp_quality']} | Score {v['vcp_score']} | Risk {v['risk']}% | RS {rs.get('rs_rating', 0)}")
    vcp_candidates = sorted(vcp_candidates, key=lambda x: (0 if x[3]["setup_type"] == "BREAKOUT" else 1, -x[3]["vcp_score"], -x[2].get("rs_rating", 0), abs(x[3]["distance_from_pivot"])))
    print(f"🎯 VCP 후보: {len(vcp_candidates)}개")

    print("🧬 7. VCP 후보에 대해서만 펀더멘탈 검사")
    final, tech_only = [], []
    for idx, (t, df, rs, v) in enumerate(vcp_candidates[:MAX_FUNDAMENTAL_CALLS], start=1):
        print(f"   ↳ [{idx}/{min(len(vcp_candidates), MAX_FUNDAMENTAL_CALLS)}] {t} 실적 조회")
        status, f = get_fundamental_info(t)
        eps_pct = None if f.get("eps_growth") is None else round(f["eps_growth"] * 100, 1)
        rev_pct = None if f.get("rev_growth") is None else round(f["rev_growth"] * 100, 1)
        meta = official_map.get(t, {})
        base = {
            "ticker": t,
            "setup_type": v["setup_type"],
            "vcp_quality": v["vcp_quality"],
            "vcp_score": v["vcp_score"],
            "vcp_checks": v["vcp_checks"],
            "fundamental_status": status,
            "fundamental_reason": f.get("reason", ""),
            "official_name": meta.get("name", ""),
            "official_exchange": meta.get("exchange", ""),
            "current_price": v["current_price"],
            "entry": v["entry"],
            "stop": v["stop"],
            "risk": v["risk"],
            "distance_from_pivot": v["distance_from_pivot"],
            "rs_rating": rs.get("rs_rating", 0),
            "r3_return_pct": round(rs.get("r3", 0) * 100, 1),
            "r6_return_pct": round(rs.get("r6", 0) * 100, 1),
            "r12_return_pct": round(rs.get("r12", 0) * 100, 1),
            "eps_growth_pct": eps_pct,
            "rev_growth_pct": rev_pct,
            "sector": f.get("sector", ""),
            "industry": f.get("industry", ""),
            "vcp_r1_pct": v["vcp_r1"],
            "vcp_r2_pct": v["vcp_r2"],
            "vcp_r3_pct": v["vcp_r3"],
            "vol_decline_pct": v["vol_decline"],
            "atr10_atr30_ratio": v["atr10_atr30_ratio"],
            "tight_close_pct": v["tight_close_pct"],
            "breakout_volume_ratio": v["breakout_volume_ratio"],
        }
        if status == "PASS":
            final.append(base)
        else:
            tech_only.append(base)
        print(f"      실적결과: {status} | {base['fundamental_reason']}")
        time.sleep(FUNDAMENTAL_SLEEP + random.uniform(0, 1))

    # 실적 조회 한도 밖 VCP 후보도 watch로 저장
    for t, df, rs, v in vcp_candidates[MAX_FUNDAMENTAL_CALLS:]:
        meta = official_map.get(t, {})
        tech_only.append({
            "ticker": t, "setup_type": v["setup_type"], "vcp_quality": v["vcp_quality"], "vcp_score": v["vcp_score"], "vcp_checks": v["vcp_checks"],
            "fundamental_status": "NOT_CHECKED", "fundamental_reason": "실적 조회 호출 제한으로 미확인",
            "official_name": meta.get("name", ""), "official_exchange": meta.get("exchange", ""),
            "current_price": v["current_price"], "entry": v["entry"], "stop": v["stop"], "risk": v["risk"], "distance_from_pivot": v["distance_from_pivot"],
            "rs_rating": rs.get("rs_rating", 0), "r3_return_pct": round(rs.get("r3", 0) * 100, 1), "r6_return_pct": round(rs.get("r6", 0) * 100, 1), "r12_return_pct": round(rs.get("r12", 0) * 100, 1),
            "eps_growth_pct": None, "rev_growth_pct": None, "sector": "", "industry": "",
            "vcp_r1_pct": v["vcp_r1"], "vcp_r2_pct": v["vcp_r2"], "vcp_r3_pct": v["vcp_r3"], "vol_decline_pct": v["vol_decline"], "atr10_atr30_ratio": v["atr10_atr30_ratio"], "tight_close_pct": v["tight_close_pct"], "breakout_volume_ratio": v["breakout_volume_ratio"],
        })

    cols = [
        "ticker", "setup_type", "vcp_quality", "vcp_score", "vcp_checks", "fundamental_status", "fundamental_reason", "official_name", "official_exchange",
        "current_price", "entry", "stop", "risk", "distance_from_pivot", "rs_rating",
        "r3_return_pct", "r6_return_pct", "r12_return_pct", "eps_growth_pct", "rev_growth_pct",
        "sector", "industry", "vcp_r1_pct", "vcp_r2_pct", "vcp_r3_pct", "vol_decline_pct", "atr10_atr30_ratio", "tight_close_pct", "breakout_volume_ratio"
    ]
    strict_file = f"minervini_strict_{today}.csv"
    watch_file = f"minervini_watchlist_{today}.csv"
    pd.DataFrame(final, columns=cols).to_csv(strict_file, index=False)
    pd.DataFrame(tech_only, columns=cols).to_csv(watch_file, index=False)
    print(f"🔥 최종 실적 확인 통과: {len(final)}개")
    print(f"👀 기술+VCP 후보/실적 미확인 또는 미달: {len(tech_only)}개")
    print(f"💾 CSV 저장: {strict_file}, {watch_file}")

    def fmt(items, title, limit=10):
        if not items:
            return f"{title}: 없음"
        lines = [title]
        for item in items[:limit]:
            reason = item.get("fundamental_reason", "")
            if len(reason) > 85:
                reason = reason[:82] + "..."
            lines.append(
                f"• {item['ticker']} [{item['setup_type']}/{item['vcp_quality']}] Score {item['vcp_score']} | 현재 {item['current_price']}$ | 진입 {item['entry']}$ | 손절 {item['stop']}$ | 리스크 {item['risk']}% | RS {item['rs_rating']}\n"
                f"  VCP: {item['vcp_r1_pct']}%→{item['vcp_r2_pct']}%→{item['vcp_r3_pct']}%, ATR비율 {item['atr10_atr30_ratio']}, 거래량감소 {item['vol_decline_pct']}%\n"
                f"  실적: {item['fundamental_status']} | 사유: {reason}"
            )
        if len(items) > limit:
            lines.append(f"외 {len(items)-limit}개는 CSV 확인")
        return "\n".join(lines)

    msg = (
        f"🔔 [{today}] 미너비니 정밀 스크리닝 결과 v8\n"
        f"------------------------------------\n"
        f"⚙️ 모드: {c['mode']} | RS≥{c['rs']} | 52주고점 {int(c['high52']*100)}% 이상 | VCP Score≥{c['min_vcp_score']}\n"
        f"📌 Russell2000 소스: {russell_source}\n"
        f"📊 원본: S&P500 {len(sp_list)}개 | Nasdaq100 {len(nd_list)}개 | Russell2000 {len(ru_list)}개\n"
        f"🧾 공식 상장 보통주 후보: {len(tickers)}개\n"
        f"🧹 공식 미등록 제외: {len(excluded_not_listed)}개 | ETF/비보통주 제외: {len(excluded_non_common)}개\n"
        f"📈 유효 가격 데이터: {len(price_data)}개 | yfinance 실패: {len(yfinance_failed_but_listed)}개\n"
        f"✅ 트렌드 템플릿 통과: {len(passed_trend)}개\n"
        f"✅ VCP 후보: {len(vcp_candidates)}개\n"
        f"🔥 실적까지 확인 통과: {len(final)}개\n"
        f"👀 기술+VCP 후보: {len(tech_only)}개\n\n"
        f"{fmt(final, '🔥 Strict 후보')}\n\n"
        f"{fmt(tech_only, '👀 Watch 후보')}\n"
        f"------------------------------------\n"
        f"※ 투자 추천이 아닌 자동 선별 결과입니다.\n"
        f"※ v8은 너무 Severe하지 않도록 BALANCED 모드에서 VCP 점수제를 사용합니다."
    )
    send_telegram_message(msg)
    print("🎯 전체 스크리닝 완료")


if __name__ == "__main__":
    main()
