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

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# ============================================================
# V11.3 최종 안정화 설정
# ============================================================
# 기본값은 BALANCED. (V9에서 STRICT_MODE=True가 기본값이라
# 완화해둔 BALANCED 수치들이 전부 무시되던 버그를 수정했습니다.)
# STRICT_MODE=True로 바꾸면 훨씬 더 적은 후보만 통과합니다.
STRICT_MODE = False

# Trend Template
MIN_RS_RATING_BALANCED = 80
MIN_RS_RATING_STRICT = 90
MIN_PRICE = 10.0
MIN_AVG_VOLUME = 200000
MIN_DOLLAR_VOLUME = 3000000
HIGH_52W_NEAR_BALANCED = 0.75   # 미너비니 원 기준: 52주 고점의 75% 이상
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
RANGE_TOLERANCE = 0.98          # r1>=r2*0.98 식으로 노이즈 허용 (완전 엄격부등호 대체)
MIN_VCP_SCORE_BALANCED = 5      # 8점 만점(필수 2개 포함) 중 5점
MIN_VCP_SCORE_STRICT = 7        # 8점 만점 중 7점

# Download stability
PRICE_PERIOD = "18mo"
CHUNK_SIZE = 50
SLEEP_BETWEEN_CHUNKS = 8
MARKET_FILTER_ENABLED = True
TOP_TELEGRAM_COUNT = int(os.environ.get("TOP_TELEGRAM_COUNT", "10"))
ACCOUNT_SIZE = float(os.environ.get("ACCOUNT_SIZE", "100000"))
ACCOUNT_RISK_PCT = float(os.environ.get("ACCOUNT_RISK_PCT", "0.005"))
MAX_POSITION_PCT = float(os.environ.get("MAX_POSITION_PCT", "0.15"))
ENTRY_BUFFER_PCT = 0.001
ENTRY_ZONE_PCT = 0.02
MAX_CHASE_PCT = 0.05
MAX_STOP_LOSS_PCT = 0.07
MIN_STOP_LOSS_PCT = 0.02
BREAKOUT_VOLUME_RATIO = 1.5
MAX_DOWNLOAD_RETRIES = 2

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
        res = requests.post(url, json={"chat_id": CHAT_ID, "text": message[:4096]}, timeout=15)
        if res.status_code != 200:
            print(f"⚠️ 텔레그램 전송 실패: {res.text}")
    except Exception as e:
        print(f"⚠️ 텔레그램 전송 에러: {e}")


def send_telegram_file(file_path, caption=""):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("텔레그램 환경변수가 없어 TXT 전송을 건너뜁니다.")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
        with open(file_path, "rb") as handle:
            response = requests.post(
                url,
                data={"chat_id": CHAT_ID, "caption": caption[:1024]},
                files={"document": (os.path.basename(file_path), handle, "text/plain")},
                timeout=60,
            )
        if response.status_code != 200:
            print(f"텔레그램 TXT 전송 실패: {response.status_code} {response.text[:300]}")
            return False
        return True
    except Exception as exc:
        print(f"텔레그램 TXT 전송 오류: {exc}")
        return False


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
        for frame in pd.read_html(io.StringIO(html)):
            frame = normalize_columns(frame)
            for column in frame.columns:
                if str(column).strip().lower() not in {"ticker", "symbol"}:
                    continue
                tickers = sorted({clean_ticker(x) for x in frame[column].dropna()
                                  if is_valid_us_ticker(clean_ticker(x))})
                if 90 <= len(tickers) <= 110:
                    print(f"Nasdaq100 수집 성공: {len(tickers)}개")
                    return tickers
        print("Nasdaq100 구성표를 찾지 못함. 다른 지수로 계속합니다.")
    except Exception as exc:
        print(f"Nasdaq100 수집 실패: {exc}")
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
        if raw_data is None or not isinstance(raw_data, pd.DataFrame) or raw_data.empty:
            return None
        if not isinstance(raw_data.columns, pd.MultiIndex):
            required = {"High", "Low", "Close", "Volume"}
            return raw_data.copy() if required.issubset(set(raw_data.columns)) else None
        level0 = [str(x) for x in raw_data.columns.get_level_values(0)]
        level1 = [str(x) for x in raw_data.columns.get_level_values(1)]
        if ticker in level0:
            frame = raw_data.xs(ticker, axis=1, level=0, drop_level=True).copy()
        elif ticker in level1:
            frame = raw_data.xs(ticker, axis=1, level=1, drop_level=True).copy()
        else:
            return None
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = [str(c[-1]) for c in frame.columns]
        frame.columns = [str(c).strip() for c in frame.columns]
        return frame if {"High", "Low", "Close", "Volume"}.issubset(set(frame.columns)) else None
    except Exception as exc:
        print(f"{ticker} 데이터 구조 처리 실패: {type(exc).__name__}: {exc}")
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
    # 주의: 이 유니버스(S&P500+나스닥100+러셀2000) 안에서의 상대 순위입니다.
    # 이미 우량한 종목끼리 경쟁하는 셈이라, 전체 시장 기준 IBD RS Rating보다
    # 실질적인 커트라인이 더 빡빡하게 작동할 수 있습니다.
    rs_df["rs_rating"] = (rs_df["weighted_return"].rank(pct=True) * 99).round(0).astype(int)
    return {r["ticker"]: {"rs_rating": int(r["rs_rating"]), "r3": float(r["r3"]), "r6": float(r["r6"]), "r12": float(r["r12"])} for _, r in rs_df.iterrows()}


def passes_market_filter():
    if not MARKET_FILTER_ENABLED:
        return "UNKNOWN", "시장 필터 비활성화"
    try:
        spy = yf.download("SPY", period=PRICE_PERIOD, progress=False, auto_adjust=True,
                          multi_level_index=True, timeout=40)
        close = spy["Close"].dropna()
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        if len(close) < 210:
            return "UNKNOWN", "SPY 데이터 부족: 신규 진입 보류"
        price = float(close.iloc[-1]); ma50 = float(close.rolling(50).mean().iloc[-1]); ma200 = float(close.rolling(200).mean().iloc[-1])
        state = "GREEN" if price > ma50 and price > ma200 else "CAUTION"
        return state, f"SPY {price:.2f} / MA50 {ma50:.2f} / MA200 {ma200:.2f} / {state}"
    except Exception as exc:
        return "UNKNOWN", f"시장 필터 실패({exc}): 신규 진입 보류"

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
        if any(pd.isna(x) for x in vals):
            return False
        return all([
            cp >= MIN_PRICE,
            cp > ma150 and cp > ma200,
            ma150 > ma200,
            ma200 > ma200_22 and ma200 > ma200_44,  # 200일선 최소 1~2개월 상승 추세
            ma50 > ma150 and ma50 > ma200,
            cp > ma50,
            cp >= low52 * 1.30,
            cp >= high52 * c["high52"],
            rs >= c["rs"],
            avg_vol >= MIN_AVG_VOLUME,
            med_vol >= MIN_AVG_VOLUME * 0.4,    # 거래량 착시(반짝 폭등) 차단
            recent_vol >= MIN_AVG_VOLUME * 0.3,  # 최근 유동성이 죽어있지 않아야 함
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
        def contraction(part):
            low, high = float(part["Low"].min()), float(part["High"].max())
            return None if low <= 0 else (high-low)/low
        r1, r2, r3 = contraction(recent.iloc[:30]), contraction(recent.iloc[30:55]), contraction(recent.iloc[55:])
        if None in (r1,r2,r3): return False, {}
        current=float(df["Close"].iloc[-1]); current_vol=float(df["Volume"].iloc[-1]); vol50=float(df["Vol_MA50"].iloc[-1])
        atr10=float(df["ATR10"].iloc[-1]); atr30=float(df["ATR30"].iloc[-1]); ma10=float(df["MA10"].iloc[-1]); ma20=float(df["MA20"].iloc[-1]); ma50=float(df["MA50"].iloc[-1])
        early_vol=float(recent["Volume"].iloc[:35].median()); recent_vol=float(recent["Volume"].iloc[-10:].median())
        pivot=float(df["High"].iloc[-21:-1].max())
        entry=pivot*(1+ENTRY_BUFFER_PCT); entry_high=pivot*(1+ENTRY_ZONE_PCT); max_chase=pivot*(1+MAX_CHASE_PCT)
        last_low=float(df["Low"].iloc[-11:-1].min()); stop=max(last_low-atr10*0.25, entry*(1-MAX_STOP_LOSS_PCT))
        if stop >= entry: return False, {}
        risk_per_share=entry-stop; risk=risk_per_share/entry*100; target1=entry+2*risk_per_share; target2=entry+3*risk_per_share
        dist=(pivot-current)/pivot*100; vol_ratio=current_vol/vol50 if vol50>0 else 0
        tight10=float(recent["Close"].tail(10).std()/recent["Close"].tail(10).mean())
        last5=df.iloc[-6:-1]; tight5=float((last5["High"].max()-last5["Low"].min())/last5["Close"].mean())
        low20=float(df["Low"].iloc[-21:-11].min()); low10=float(df["Low"].iloc[-11:-1].min()); higher_low=low10>=low20*0.98
        near10=abs(current/ma10-1)<=0.03; near20=abs(current/ma20-1)<=0.03; volume_dry=recent_vol<=early_vol*0.85
        vcp_items={"range_contracts":r1>=r2*RANGE_TOLERANCE and r2>=r3*RANGE_TOLERANCE,"meaningful_contracts":r2<=r1*c["contraction_ratio"] and r3<=r2*c["contraction_ratio"],"last_contraction":r3<=c["last_contraction"],"volume_dryup":recent_vol<=early_vol*c["volume_dryup"],"atr_dryup":atr30>0 and atr10<=atr30*c["atr_dryup"],"tight10":tight10<=0.03,"tight5":tight5<=0.05,"higher_low":higher_low}
        pull_items={"near_ma10":near10,"near_ma20":near20,"above_ma50":current>ma50,"volume_dryup":volume_dry,"higher_low":higher_low,"pivot_distance":0.5<=dist<=10,"risk_ok":MIN_STOP_LOSS_PCT*100<=risk<=c["max_risk"],"tight5":tight5<=0.06}
        vcp_score=sum(vcp_items.values()); pullback_score=sum(pull_items.values())
        breakout=entry<=current<=max_chase and vol_ratio>=BREAKOUT_VOLUME_RATIO
        if breakout: setup="🚀돌파(Breakout)"
        elif pullback_score>=7 and 0.5<=dist<=8: setup="🎯눌림목(Pullback)"
        elif 0<=dist<=c["pivot_near"]: setup="🔥돌파임박(NearPivot)"
        elif pullback_score>=5 and 0<=dist<=12: setup="👀Near 눌림목(NearPullback)"
        else: setup="제외"
        if current<entry: action,reason="대기",f"${entry:.2f} 이상 돌파와 거래량 증가 확인"
        elif current<=entry_high and vol_ratio>=BREAKOUT_VOLUME_RATIO: action,reason="진입 가능","권장 진입 구간과 거래량 조건 충족"
        elif current<=max_chase and vol_ratio>=BREAKOUT_VOLUME_RATIO: action,reason="소액만 검토","피벗에서 다소 상승하여 수량 축소 필요"
        elif current>max_chase: action,reason="추격 금지","피벗에서 5% 이상 상승"
        else: action,reason="돌파 확인 대기","가격은 피벗 위지만 거래량 조건 미달"
        shares=max(0,min(int(ACCOUNT_SIZE*ACCOUNT_RISK_PCT/risk_per_share),int(ACCOUNT_SIZE*MAX_POSITION_PCT/entry)))
        details={"setup_type":setup,"action":action,"action_reason":reason,"vcp_score":vcp_score,"pullback_score":pullback_score,"vcp_checks":"; ".join(k for k,v in vcp_items.items() if v),"pullback_checks":"; ".join(k for k,v in pull_items.items() if v),"current_price":round(current,2),"pivot":round(pivot,2),"entry":round(entry,2),"entry_zone_high":round(entry_high,2),"max_chase":round(max_chase,2),"stop":round(stop,2),"risk":round(risk,1),"risk_per_share":round(risk_per_share,2),"target1":round(target1,2),"target2":round(target2,2),"position_shares":shares,"position_value":round(shares*entry,2),"expected_loss":round(shares*risk_per_share,2),"distance_from_pivot":round(dist,1),"vcp_r1_pct":round(r1*100,1),"vcp_r2_pct":round(r2*100,1),"vcp_r3_pct":round(r3*100,1),"vol_decline_pct":round((1-recent_vol/early_vol)*100,1) if early_vol>0 else None,"atr10_atr30_ratio":round(atr10/atr30,2) if atr30>0 else None,"tight_close_pct":round(tight10*100,2),"breakout_volume_ratio":round(vol_ratio,2),"entry_explanation":f"${entry:.2f}~${entry_high:.2f}, 거래량 50일 평균 {BREAKOUT_VOLUME_RATIO:.1f}배 이상, ${max_chase:.2f} 초과 추격 금지","stop_explanation":f"${stop:.2f} 종가 기준 이탈 시 손절","target_explanation":f"${target1:.2f}(2R) 30~50% 분할매도, ${target2:.2f}(3R) 또는 10일선 이탈 시 잔량 관리"}
        passed=setup!="제외" and MIN_STOP_LOSS_PCT*100<=risk<=c["max_risk"] and (vcp_score>=c["min_vcp_score"] or pullback_score>=5)
        return passed,details
    except Exception as exc:
        print(f"{ticker} 패턴 검사 오류: {type(exc).__name__}: {exc}")
        return False,{}


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
    today=pd.Timestamp.now(tz="Asia/Seoul").strftime("%Y-%m-%d"); c=cfg()
    print(f"미너비니 스크리닝 V11.3 시작 ({c['mode']})")
    print(f"Python {sys.version.split()[0]} | pandas {pd.__version__} | yfinance {getattr(yf,'__version__','unknown')}")
    sp=get_sp500_tickers(); nd=get_nasdaq100_tickers(); ru,russell_source=get_russell2000_tickers(); raw=sorted(set(sp+nd+ru))
    print(f"유니버스 원본: S&P500 {len(sp)} | Nasdaq100 {len(nd)} | Russell2000 {len(ru)} | 합계 {len(raw)}")
    if not raw: raise RuntimeError("종목 유니버스 수집 실패")
    official=get_official_listed_universe(); tickers,excluded_not,excluded_non=validate_candidate_universe(raw,official)
    print(f"공식 검증 보통주: {len(tickers)}개")
    pd.DataFrame(excluded_not).to_csv(f"excluded_not_currently_listed_{today}.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(excluded_non).to_csv(f"excluded_non_common_{today}.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame([{"date":today,"mode":c["mode"],"russell_source":russell_source,"sp500":len(sp),"nasdaq100":len(nd),"russell2000":len(ru),"validated":len(tickers)}]).to_csv(f"universe_summary_{today}.csv",index=False,encoding="utf-8-sig")
    if not tickers: raise RuntimeError("공식 검증 후 종목 0개")
    market_state,market_status=passes_market_filter(); print(f"시장: {market_status}")

    print("가격 데이터 분할 다운로드 시작")
    price_data={}; failed=[]
    for i in range(0,len(tickers),CHUNK_SIZE):
        chunk=tickers[i:i+CHUNK_SIZE]; print(f"  다운로드 [{i+1}~{min(i+CHUNK_SIZE,len(tickers))}/{len(tickers)}]")
        chunk_data=pd.DataFrame(); download_error=""
        for attempt in range(1,MAX_DOWNLOAD_RETRIES+1):
            try:
                chunk_data=yf.download(tickers=chunk,period=PRICE_PERIOD,interval="1d",group_by="ticker",progress=False,threads=False,timeout=40,auto_adjust=True,multi_level_index=True)
                if chunk_data is not None and not chunk_data.empty: break
                download_error="EMPTY_DATAFRAME"
            except Exception as exc:
                download_error=f"{type(exc).__name__}: {exc}"; print(f"    {attempt}차 실패: {download_error}")
            if attempt<MAX_DOWNLOAD_RETRIES: time.sleep(7)
        success=0
        for ticker in chunk:
            try:
                frame=prepare_price_dataframe(get_ticker_dataframe(chunk_data,ticker))
                if frame is None:
                    meta=official.get(ticker,{}); failed.append({"ticker":ticker,"reason":download_error or "PRICE_DATA_FAILED_OR_INSUFFICIENT","name":meta.get("name",""),"exchange":meta.get("exchange","")})
                else: price_data[ticker]=frame; success+=1
            except Exception as exc:
                failed.append({"ticker":ticker,"reason":f"{type(exc).__name__}: {exc}"})
        print(f"    청크 유효 {success}/{len(chunk)} | 누적 {len(price_data)}")
        if i>=CHUNK_SIZE and not price_data: raise RuntimeError("첫 100개 가격 데이터 전부 실패: yfinance 구조 확인 필요")
        time.sleep(SLEEP_BETWEEN_CHUNKS+random.uniform(0,2))
    pd.DataFrame(failed).to_csv(f"yfinance_failed_{today}.csv",index=False,encoding="utf-8-sig")
    print(f"가격 다운로드 완료: 유효 {len(price_data)} | 실패 {len(failed)}")
    if not price_data: raise RuntimeError("유효 가격 데이터 0개")

    print("RS Rating 계산 시작"); rs_map=calculate_rs_scores(price_data); print(f"RS 계산 완료: {len(rs_map)}")
    if not rs_map: raise RuntimeError("RS Rating 결과 0개")
    print("트렌드 템플릿 검사 시작"); passed_trend=[]
    for ticker,frame in price_data.items():
        rs=rs_map.get(ticker,{"rs_rating":0})
        if passes_trend_template(ticker,frame,rs): passed_trend.append((ticker,frame,rs))
    print(f"트렌드 통과: {len(passed_trend)}")
    print("VCP/눌림목 검사 시작"); candidates=[]; near=[]
    for ticker,frame,rs in passed_trend:
        ok,plan=check_vcp_pattern(ticker,frame)
        if ok: candidates.append((ticker,rs,plan))
        elif plan and max(plan.get("vcp_score",0),plan.get("pullback_score",0))>=4: near.append({"ticker":ticker,"rs_rating":rs.get("rs_rating",0),**plan})
    pd.DataFrame(near).to_csv(f"pattern_near_miss_{today}.csv",index=False,encoding="utf-8-sig")
    order={"🚀돌파(Breakout)":0,"🎯눌림목(Pullback)":1,"🔥돌파임박(NearPivot)":2,"👀Near 눌림목(NearPullback)":3}
    candidates.sort(key=lambda x:(order.get(x[2]["setup_type"],99),-x[2]["vcp_score"],-x[2]["pullback_score"],-x[1].get("rs_rating",0)))
    print(f"패턴 후보: {len(candidates)}")

    print("펀더멘털 검사 시작"); rows=[]
    for idx,(ticker,rs,plan) in enumerate(candidates):
        if idx<MAX_FUNDAMENTAL_CALLS:
            status,f=get_fundamental_info(ticker); time.sleep(FUNDAMENTAL_SLEEP+random.uniform(0,1))
        else: status,f="NOT_CHECKED",{"eps_growth":None,"rev_growth":None,"sector":"","industry":"","reason":"호출 한도 초과"}
        meta=official.get(ticker,{})
        row={"ticker":ticker,**plan,"rs_rating":rs.get("rs_rating",0),"r3_return_pct":round(rs.get("r3",0)*100,1),"r6_return_pct":round(rs.get("r6",0)*100,1),"r12_return_pct":round(rs.get("r12",0)*100,1),"fundamental_status":status,"fundamental_reason":f.get("reason",""),"eps_growth_pct":None if f.get("eps_growth") is None else round(f["eps_growth"]*100,1),"rev_growth_pct":None if f.get("rev_growth") is None else round(f["rev_growth"]*100,1),"sector":f.get("sector",""),"industry":f.get("industry",""),"official_name":meta.get("name",""),"market_state":market_state,"market_status":market_status}
        if market_state!="GREEN": row.update({"action":"신규 진입 보류","action_reason":market_status,"position_shares":0,"position_value":0.0,"expected_loss":0.0})
        rows.append(row)
    strict=[x for x in rows if x["fundamental_status"]=="PASS"]; watch=[x for x in rows if x["fundamental_status"]!="PASS"]
    pd.DataFrame(strict).to_csv(f"minervini_v11_3_strict_{today}.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(watch).to_csv(f"minervini_v11_3_watchlist_{today}.csv",index=False,encoding="utf-8-sig")

    txt=f"minervini_all_candidates_{today}.txt"; print("CSV/TXT 생성 시작")
    with open(txt,"w",encoding="utf-8") as out:
        out.write(f"[{today}] 미너비니 전체 후보 V11.3\n시장: {market_status}\n후보: {len(rows)}개\n\n")
        for x in rows:
            out.write("="*70+"\n")
            out.write(f"{x['ticker']} | {x['setup_type']} | {x['action']}\n회사: {x.get('official_name','')} | 섹터: {x.get('sector','')}\n")
            out.write(f"VCP {x['vcp_score']}/8 | 눌림목 {x['pullback_score']}/8 | RS {x['rs_rating']}\n")
            out.write(f"현재 ${x['current_price']} | 피벗 ${x['pivot']} | 이격 {x['distance_from_pivot']}%\n")
            out.write(f"진입 ${x['entry']}~${x['entry_zone_high']} | 추격금지 ${x['max_chase']}\n")
            out.write(f"손절 ${x['stop']} (-{x['risk']}%) | 1차 ${x['target1']} | 2차 ${x['target2']}\n")
            out.write(f"수량 {x['position_shares']}주 | 투자금 ${x['position_value']} | 예상손실 ${x['expected_loss']}\n")
            out.write(f"실적 {x['fundamental_status']} | 거래량비 {x.get('breakout_volume_ratio')}배\n")
            out.write(f"행동: {x['action_reason']}\n진입: {x['entry_explanation']}\n손절: {x['stop_explanation']}\n익절: {x['target_explanation']}\n")
            out.write(f"VCP 충족: {x['vcp_checks']}\n눌림목 충족: {x['pullback_checks']}\n")
        out.write("="*70+"\n자동 선별 결과이며 투자 추천이 아닙니다.\n")
    print("텔레그램 TOP/TXT 전송 시작")
    msg=f"[{today}] 미너비니 V11.3 TOP {min(TOP_TELEGRAM_COUNT,len(rows))}\n시장: {market_status}\n전체후보 {len(rows)}개\n\n"
    for x in rows[:TOP_TELEGRAM_COUNT]:
        msg+=(f"{x['setup_type']} {x['ticker']} [{x['action']}]\nVCP {x['vcp_score']}/8 | 눌림 {x['pullback_score']}/8 | RS {x['rs_rating']}\n현재 ${x['current_price']} | 진입 ${x['entry']}~${x['entry_zone_high']}\n손절 ${x['stop']}(-{x['risk']}%) | 1차 ${x['target1']} | 2차 ${x['target2']}\n↳ {x['action_reason']}\n\n")
    if not rows: msg+="조건 충족 후보가 없습니다.\n"
    msg+="전체 후보는 첨부 TXT를 확인하세요."
    send_telegram_message(msg); send_telegram_file(txt,f"[{today}] 전체 후보 {len(rows)}개")
    print("전체 스크리닝 완료")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        detail=traceback.format_exc()
        print("="*80); print(f"스크리닝 실패: {type(exc).__name__}: {exc}"); print(detail); print("="*80)
        try:
            with open("screener_error_log.txt","w",encoding="utf-8") as handle:
                handle.write(f"Error type: {type(exc).__name__}\nError message: {exc}\n\n{detail}")
            print("오류 로그 저장: screener_error_log.txt")
        except Exception as file_exc:
            print(f"오류 로그 작성 실패: {file_exc}")
        try:
            send_telegram_message(f"미너비니 스크리너 실패\n{type(exc).__name__}: {exc}")
        except Exception as telegram_exc:
            print(f"텔레그램 오류 알림 실패: {telegram_exc}")
        sys.exit(1)
