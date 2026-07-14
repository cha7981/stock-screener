import io
import os
import re
import sys
import time
import random
import traceback
from datetime import datetime, timezone

import pandas as pd
import requests
import yfinance as yf

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# ============================================================
# V11.4 SETTINGS
# ============================================================
STRICT_MODE = False
PRICE_PERIOD = "18mo"
CHUNK_SIZE = 50
SLEEP_BETWEEN_CHUNKS = 8
MAX_DOWNLOAD_RETRIES = 2
MAX_FUNDAMENTAL_CALLS = 60
FUNDAMENTAL_SLEEP = 2
FUNDAMENTAL_RETRY_SLEEP = 30
TOP_TELEGRAM_COUNT = int(os.environ.get("TOP_TELEGRAM_COUNT", "10"))

ACCOUNT_SIZE = float(os.environ.get("ACCOUNT_SIZE", "100000"))
ACCOUNT_RISK_PCT = float(os.environ.get("ACCOUNT_RISK_PCT", "0.005"))
MAX_POSITION_PCT = float(os.environ.get("MAX_POSITION_PCT", "0.15"))

MIN_PRICE = 10.0
MIN_AVG_VOLUME = 200_000
MIN_DOLLAR_VOLUME = 3_000_000
MIN_RS_BALANCED = 80
MIN_RS_STRICT = 90
HIGH52_BALANCED = 0.75
HIGH52_STRICT = 0.85
MIN_EPS_GROWTH = 0.20
MIN_REV_GROWTH = 0.15

ENTRY_BUFFER_PCT = 0.001
ENTRY_ZONE_PCT = 0.02
MAX_CHASE_PCT = 0.05
MAX_STRUCTURE_RISK_PCT = 7.0
BREAKOUT_VOLUME_RATIO = 1.5
EARNINGS_BLOCK_DAYS = 7

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "text/html,text/csv,application/csv,text/plain,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

CATEGORY_EXPLANATIONS = {
    "READY": "실제 피벗 돌파와 거래량 조건까지 충족. 실적일과 주문 가격을 최종 확인한 뒤 진입 검토.",
    "SETUP": "패턴 품질은 기준을 통과했지만 아직 피벗 돌파 전. 알림을 걸고 기다리는 종목.",
    "PULLBACK_SETUP": "10일선/20일선 부근의 건전한 눌림목. 안전하게 피벗 돌파를 기다리는 후보.",
    "WATCH": "일부 조건은 좋지만 VCP, 눌림목, 펀더멘털 또는 이격 조건이 부족한 관찰 후보.",
    "REJECT": "구조적 손절 과다, 추격 구간, 패턴 미달 등으로 현재 신규 진입 대상에서 제외.",
}

SETUP_EXPLANATIONS = {
    "🚀돌파(Breakout)": "피벗을 거래량과 함께 돌파한 상태",
    "🔥돌파임박(NearPivot)": "피벗 아래 4% 이내에서 돌파를 준비하는 상태",
    "🎯눌림목(Pullback)": "상승 추세 안에서 이동평균선 부근으로 건전하게 조정받는 상태",
    "👀Near 눌림목(NearPullback)": "눌림목 일부 조건만 충족하여 추가 확인이 필요한 상태",
}


def cfg():
    if STRICT_MODE:
        return {"mode": "STRICT", "rs": MIN_RS_STRICT, "high52": HIGH52_STRICT}
    return {"mode": "BALANCED", "rs": MIN_RS_BALANCED, "high52": HIGH52_BALANCED}


# ============================================================
# TELEGRAM / GENERAL UTILITIES
# ============================================================
def send_telegram_message(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("텔레그램 환경변수가 없어 메시지 전송을 건너뜁니다.")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        response = requests.post(url, json={"chat_id": CHAT_ID, "text": message[:4096]}, timeout=20)
        if response.status_code != 200:
            print(f"텔레그램 메시지 실패: {response.status_code} {response.text[:300]}")
            return False
        return True
    except Exception as exc:
        print(f"텔레그램 메시지 오류: {exc}")
        return False


def send_telegram_file(file_path, caption=""):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("텔레그램 환경변수가 없어 파일 전송을 건너뜁니다.")
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
            print(f"텔레그램 파일 실패: {response.status_code} {response.text[:300]}")
            return False
        return True
    except Exception as exc:
        print(f"텔레그램 파일 오류: {exc}")
        return False


def get_text(url, timeout=40):
    response = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.text


def clean_ticker(value):
    if value is None:
        return ""
    return str(value).strip().replace("\ufeff", "").replace('"', "").replace("/", "-").replace(".", "-").upper()


def valid_ticker(value):
    ticker = clean_ticker(value)
    return bool(re.fullmatch(r"[A-Z][A-Z0-9]*(?:-[A-Z])?", ticker)) and len(ticker) <= 8


def normalize_columns(frame):
    frame = frame.copy()
    frame.columns = [str(c).strip().replace("\ufeff", "") for c in frame.columns]
    return frame


# ============================================================
# UNIVERSE: S&P500 + NASDAQ100 + RUSSELL2000
# ============================================================
def get_sp500_tickers():
    try:
        frame = pd.read_html(io.StringIO(get_text("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")))[0]
        values = sorted({clean_ticker(x) for x in frame["Symbol"].dropna() if valid_ticker(x)})
        print(f"S&P500 수집: {len(values)}개")
        return values
    except Exception as exc:
        print(f"S&P500 실패: {exc}")
        return []


def get_nasdaq100_tickers():
    try:
        for frame in pd.read_html(io.StringIO(get_text("https://en.wikipedia.org/wiki/Nasdaq-100"))):
            frame = normalize_columns(frame)
            for column in frame.columns:
                if str(column).lower() not in {"ticker", "symbol"}:
                    continue
                values = sorted({clean_ticker(x) for x in frame[column].dropna() if valid_ticker(x)})
                if 90 <= len(values) <= 110:
                    print(f"Nasdaq100 수집: {len(values)}개")
                    return values
        print("Nasdaq100 표를 찾지 못함. 다른 지수로 계속합니다.")
    except Exception as exc:
        print(f"Nasdaq100 실패: {exc}")
    return []


def decode_response(response):
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "latin1"):
        try:
            text = response.content.decode(encoding).replace("\x00", "")
            if text.strip():
                return text
        except Exception:
            pass
    return response.text.replace("\x00", "")


def parse_holdings_tickers(text):
    lines = [x for x in text.replace("\ufeff", "").splitlines() if x.strip()]
    for start in range(min(15, len(lines))):
        for separator in (",", "\t", ";"):
            try:
                frame = pd.read_csv(io.StringIO("\n".join(lines[start:])), sep=separator, engine="python", on_bad_lines="skip")
                frame = normalize_columns(frame)
                column = next((c for c in frame.columns if "ticker" in c.lower() or "symbol" in c.lower()), None)
                if column is None:
                    continue
                values = sorted({clean_ticker(x) for x in frame[column].dropna() if valid_ticker(x)})
                if len(values) >= 1000:
                    return values
            except Exception:
                pass
    return []


def get_russell2000_tickers():
    sources = [
        ("iShares IWM official", "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund", 1500),
        ("GitHub quanthero", "https://raw.githubusercontent.com/quanthero/US_Indices_Constituents/main/Russell2000.csv", 1400),
        ("GitHub ikoniaris", "https://raw.githubusercontent.com/ikoniaris/Russell2000/master/russell_2000_components.csv", 1400),
    ]
    for name, url, minimum in sources:
        try:
            response = requests.get(url, headers=REQUEST_HEADERS, timeout=45)
            response.raise_for_status()
            values = parse_holdings_tickers(decode_response(response))
            if len(values) >= minimum:
                print(f"Russell2000 수집: {len(values)}개 ({name})")
                return values, name
        except Exception as exc:
            print(f"Russell2000 {name} 실패: {exc}")
    return [], "failed"


def parse_pipe(text):
    lines = [x.strip() for x in text.splitlines() if x.strip() and not x.startswith("File Creation Time")]
    return pd.read_csv(io.StringIO("\n".join(lines)), sep="|") if lines else pd.DataFrame()


def get_official_universe():
    result = {}
    sources = [
        ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", "Symbol", "NASDAQ"),
        ("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", "ACT Symbol", None),
    ]
    for url, symbol_column, fixed_exchange in sources:
        try:
            frame = parse_pipe(get_text(url))
            for _, row in frame.iterrows():
                ticker = clean_ticker(row.get(symbol_column, ""))
                if not valid_ticker(ticker):
                    continue
                result[ticker] = {
                    "name": str(row.get("Security Name", "")),
                    "exchange": fixed_exchange or str(row.get("Exchange", "")),
                    "etf": str(row.get("ETF", "N")),
                    "test": str(row.get("Test Issue", "N")),
                }
        except Exception as exc:
            print(f"공식 상장 리스트 실패: {exc}")
    print(f"공식 상장 리스트: {len(result)}개")
    return result


def is_common_stock(meta):
    name = str(meta.get("name", "")).lower()
    blocked = (" etf", "etn", "warrant", " right", " unit", "preferred", "depositary share", "note due", "bond", "debenture", "closed end fund")
    return meta.get("etf", "N").upper() != "Y" and meta.get("test", "N").upper() != "Y" and not any(x in name for x in blocked)


def validate_universe(tickers, official):
    included, excluded = [], []
    for ticker in sorted(set(clean_ticker(x) for x in tickers)):
        meta = official.get(ticker)
        if not valid_ticker(ticker):
            excluded.append({"ticker": ticker, "reason": "BAD_FORMAT"})
        elif meta is None:
            excluded.append({"ticker": ticker, "reason": "NOT_OFFICIAL"})
        elif not is_common_stock(meta):
            excluded.append({"ticker": ticker, "reason": "NON_COMMON", **meta})
        else:
            included.append(ticker)
    return included, excluded


# ============================================================
# PRICE DATA / MARKET / TREND
# ============================================================
def get_ticker_frame(raw_data, ticker):
    try:
        if raw_data is None or not isinstance(raw_data, pd.DataFrame) or raw_data.empty:
            return None
        if not isinstance(raw_data.columns, pd.MultiIndex):
            return raw_data.copy() if {"High", "Low", "Close", "Volume"}.issubset(raw_data.columns) else None
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
        return frame if {"High", "Low", "Close", "Volume"}.issubset(frame.columns) else None
    except Exception as exc:
        print(f"{ticker} 데이터 구조 오류: {exc}")
        return None


def prepare_frame(frame):
    if frame is None or frame.empty:
        return None
    frame = frame.dropna(subset=["Close"]).copy()
    if len(frame) < 260:
        return None
    if getattr(frame.index, "tz", None) is not None:
        frame.index = frame.index.tz_localize(None)
    for period in (10, 20, 50, 150, 200):
        frame[f"MA{period}"] = frame["Close"].rolling(period).mean()
    frame["Vol_MA10"] = frame["Volume"].rolling(10).mean()
    frame["Vol_MA50"] = frame["Volume"].rolling(50).mean()
    frame["Vol_Median50"] = frame["Volume"].rolling(50).median()
    frame["DollarVol_MA50"] = (frame["Close"] * frame["Volume"]).rolling(50).mean()
    true_range = pd.concat([
        frame["High"] - frame["Low"],
        (frame["High"] - frame["Close"].shift()).abs(),
        (frame["Low"] - frame["Close"].shift()).abs(),
    ], axis=1).max(axis=1)
    frame["ATR10"] = true_range.rolling(10).mean()
    frame["ATR30"] = true_range.rolling(30).mean()
    return frame


def download_prices(tickers, official):
    price_data, failures = {}, []
    for index in range(0, len(tickers), CHUNK_SIZE):
        chunk = tickers[index:index + CHUNK_SIZE]
        print(f"가격 다운로드 [{index+1}~{min(index+CHUNK_SIZE, len(tickers))}/{len(tickers)}]")
        raw, error = pd.DataFrame(), ""
        for attempt in range(1, MAX_DOWNLOAD_RETRIES + 1):
            try:
                raw = yf.download(chunk, period=PRICE_PERIOD, interval="1d", group_by="ticker", progress=False,
                                  threads=False, timeout=40, auto_adjust=True, multi_level_index=True)
                if raw is not None and not raw.empty:
                    break
                error = "EMPTY_DATAFRAME"
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                print(f"  {attempt}차 실패: {error}")
            if attempt < MAX_DOWNLOAD_RETRIES:
                time.sleep(7)
        success = 0
        for ticker in chunk:
            frame = prepare_frame(get_ticker_frame(raw, ticker))
            if frame is None:
                failures.append({"ticker": ticker, "reason": error or "PRICE_DATA_FAILED_OR_INSUFFICIENT", "name": official.get(ticker, {}).get("name", "")})
            else:
                price_data[ticker] = frame
                success += 1
        print(f"  청크 유효 {success}/{len(chunk)} | 누적 {len(price_data)}")
        if index >= CHUNK_SIZE and not price_data:
            raise RuntimeError("첫 100개 가격 데이터가 전부 실패했습니다.")
        time.sleep(SLEEP_BETWEEN_CHUNKS + random.uniform(0, 2))
    return price_data, failures


def market_filter():
    try:
        raw = yf.download(["SPY", "QQQ"], period=PRICE_PERIOD, group_by="ticker", progress=False,
                          threads=False, auto_adjust=True, multi_level_index=True, timeout=40)
        metrics = {}
        for ticker in ("SPY", "QQQ"):
            frame = get_ticker_frame(raw, ticker)
            if frame is None:
                return "UNKNOWN", f"{ticker} 데이터 없음"
            close = frame["Close"].dropna()
            metrics[ticker] = {
                "price": float(close.iloc[-1]),
                "ma50": float(close.rolling(50).mean().iloc[-1]),
                "ma200": float(close.rolling(200).mean().iloc[-1]),
                "ma50_old": float(close.rolling(50).mean().iloc[-10]),
            }
        spy, qqq = metrics["SPY"], metrics["QQQ"]
        if spy["price"] < spy["ma200"] or (spy["price"] < spy["ma50"] and qqq["price"] < qqq["ma50"]):
            state = "RED"
        elif spy["price"] > spy["ma50"] > spy["ma200"] and qqq["price"] > qqq["ma50"] > qqq["ma200"] and spy["ma50"] > spy["ma50_old"]:
            state = "GREEN"
        else:
            state = "CAUTION"
        return state, f"SPY {spy['price']:.2f}/50MA {spy['ma50']:.2f}/200MA {spy['ma200']:.2f} | QQQ {qqq['price']:.2f}/50MA {qqq['ma50']:.2f}/200MA {qqq['ma200']:.2f} | {state}"
    except Exception as exc:
        return "UNKNOWN", f"시장 필터 실패: {exc}"


def safe_return(close, days):
    return float(close.iloc[-1] / close.iloc[-days] - 1) if len(close) > days and close.iloc[-days] > 0 else None


def calculate_rs_scores(price_data):
    rows = []
    for ticker, frame in price_data.items():
        close = frame["Close"].dropna()
        r3, r6, r12 = safe_return(close, 63), safe_return(close, 126), safe_return(close, 252)
        if None not in (r3, r6, r12):
            rows.append({"ticker": ticker, "r3": r3, "r6": r6, "r12": r12, "weighted": r3*0.4 + r6*0.3 + r12*0.3})
    if not rows:
        return {}
    frame = pd.DataFrame(rows)
    frame["rs_rating"] = (frame["weighted"].rank(pct=True) * 99).round().astype(int)
    return frame.set_index("ticker")[["rs_rating", "r3", "r6", "r12"]].to_dict("index")


def passes_trend_template(frame, rs):
    c = cfg()
    cp = float(frame["Close"].iloc[-1])
    ma50, ma150, ma200 = (float(frame[x].iloc[-1]) for x in ("MA50", "MA150", "MA200"))
    low52, high52 = float(frame["Close"].tail(252).min()), float(frame["Close"].tail(252).max())
    return all([
        cp >= MIN_PRICE, cp > ma50 > ma150 > ma200,
        ma200 > frame["MA200"].iloc[-22] and ma200 > frame["MA200"].iloc[-44],
        cp >= low52 * 1.30, cp >= high52 * c["high52"], rs.get("rs_rating", 0) >= c["rs"],
        frame["Vol_MA50"].iloc[-1] >= MIN_AVG_VOLUME,
        frame["Vol_Median50"].iloc[-1] >= MIN_AVG_VOLUME * 0.4,
        frame["Vol_MA10"].iloc[-1] >= MIN_AVG_VOLUME * 0.3,
        frame["DollarVol_MA50"].iloc[-1] >= MIN_DOLLAR_VOLUME,
    ])


# ============================================================
# VCP / PULLBACK / TRADE PLAN
# ============================================================
def analyze_pattern(frame, rs_rating):
    recent = frame.tail(75)
    if len(recent) < 75:
        return None

    def contraction(part):
        low, high = float(part["Low"].min()), float(part["High"].max())
        return (high - low) / low * 100 if low > 0 else None

    r1, r2, r3 = contraction(recent.iloc[:30]), contraction(recent.iloc[30:55]), contraction(recent.iloc[55:])
    if None in (r1, r2, r3):
        return None

    current = float(frame["Close"].iloc[-1])
    current_vol = float(frame["Volume"].iloc[-1])
    vol50 = float(frame["Vol_MA50"].iloc[-1])
    atr10, atr30 = float(frame["ATR10"].iloc[-1]), float(frame["ATR30"].iloc[-1])
    ma10, ma20, ma50 = (float(frame[x].iloc[-1]) for x in ("MA10", "MA20", "MA50"))
    pivot = float(frame["High"].iloc[-21:-1].max())
    entry = pivot * (1 + ENTRY_BUFFER_PCT)
    entry_high = pivot * (1 + ENTRY_ZONE_PCT)
    max_chase = pivot * (1 + MAX_CHASE_PCT)

    last_contraction_low = float(frame["Low"].iloc[-11:-1].min())
    structure_stop = last_contraction_low - atr10 * 0.25
    structure_risk = (entry - structure_stop) / entry * 100
    stop = structure_stop
    risk_per_share = entry - stop
    target1, target2 = entry + 2*risk_per_share, entry + 3*risk_per_share
    distance = (pivot - current) / pivot * 100
    volume_ratio = current_vol / vol50 if vol50 > 0 else 0

    early_volume = float(recent["Volume"].iloc[:35].median())
    recent_volume = float(recent["Volume"].iloc[-10:].median())
    tight10 = float(recent["Close"].tail(10).std() / recent["Close"].tail(10).mean() * 100)
    last5 = frame.iloc[-6:-1]
    tight5 = float((last5["High"].max() - last5["Low"].min()) / last5["Close"].mean() * 100)
    higher_low = last_contraction_low >= float(frame["Low"].iloc[-21:-11].min()) * 0.98

    vcp_checks = {
        "순차수축": r1 > r2 > r3,
        "의미있는수축": r2 <= r1*0.85 and r3 <= r2*0.85,
        "마지막수축10%이하": r3 <= 10.0,
        "거래량감소": recent_volume <= early_volume*0.85,
        "ATR감소": atr30 > 0 and atr10 <= atr30*0.90,
        "10일밀집": tight10 <= 3.0,
        "5일밀집": tight5 <= 5.0,
        "저점상승": higher_low,
    }
    pullback_checks = {
        "10일선근접": abs(current/ma10 - 1) <= 0.03,
        "20일선근접": abs(current/ma20 - 1) <= 0.03,
        "50일선위": current > ma50,
        "거래량감소": recent_volume <= early_volume*0.85,
        "저점상승": higher_low,
        "피벗10%이내": 0 <= distance <= 10.0,
        "구조손절7%이하": 2.0 <= structure_risk <= MAX_STRUCTURE_RISK_PCT,
        "5일밀집": tight5 <= 6.0,
    }
    vcp_score, pullback_score = sum(vcp_checks.values()), sum(pullback_checks.values())

    breakout = entry <= current <= max_chase and volume_ratio >= BREAKOUT_VOLUME_RATIO
    if breakout:
        setup_type = "🚀돌파(Breakout)"
    elif 0 <= distance <= 4.0:
        setup_type = "🔥돌파임박(NearPivot)"
    elif pullback_score >= 7 and 0.5 <= distance <= 8.0:
        setup_type = "🎯눌림목(Pullback)"
    elif pullback_score >= 5 and 0 <= distance <= 12.0:
        setup_type = "👀Near 눌림목(NearPullback)"
    else:
        setup_type = "👀Near 눌림목(NearPullback)"

    # 셋업별 엄격한 기술 등급
    if structure_risk > MAX_STRUCTURE_RISK_PCT or structure_risk < 2.0 or current > max_chase:
        classification = "REJECT"
        reason = "구조적 손절폭이 2~7% 범위를 벗어나거나 피벗 추격 구간입니다."
    elif breakout and vcp_score >= 6 and pullback_score >= 5 and rs_rating >= 85 and structure_risk <= 6.0:
        classification = "READY"
        reason = "피벗 돌파, 거래량, VCP, RS와 손절폭 기준을 모두 충족했습니다."
    elif setup_type == "🔥돌파임박(NearPivot)" and vcp_score >= 5 and pullback_score >= 6 and rs_rating >= 85 and distance <= 4.0 and structure_risk <= 6.0:
        classification = "SETUP"
        reason = "품질 기준을 통과했으며 피벗 돌파만 기다리는 종목입니다."
    elif setup_type == "🎯눌림목(Pullback)" and vcp_score >= 4 and pullback_score >= 7 and rs_rating >= 80 and structure_risk <= 6.0:
        classification = "PULLBACK_SETUP"
        reason = "건전한 눌림목 기준을 통과했습니다. 안전하게 피벗 돌파를 기다립니다."
    elif vcp_score >= 3 or pullback_score >= 5:
        classification = "WATCH"
        reason = "일부 조건은 양호하지만 상위 등급의 필수 기준에는 부족합니다."
    else:
        classification = "REJECT"
        reason = "VCP와 눌림목 품질 점수가 모두 부족합니다."

    account_risk = ACCOUNT_SIZE * ACCOUNT_RISK_PCT
    shares_by_risk = int(account_risk / risk_per_share) if risk_per_share > 0 else 0
    shares_by_value = int(ACCOUNT_SIZE * MAX_POSITION_PCT / entry)
    shares = max(0, min(shares_by_risk, shares_by_value)) if classification != "REJECT" else 0

    if classification == "READY":
        action = "진입 검토"
    elif classification in {"SETUP", "PULLBACK_SETUP"}:
        action = "돌파 대기"
    elif classification == "WATCH":
        action = "관찰"
    else:
        action = "진입 제외"

    return {
        "classification": classification,
        "classification_explanation": CATEGORY_EXPLANATIONS[classification],
        "classification_reason": reason,
        "setup_type": setup_type,
        "setup_explanation": SETUP_EXPLANATIONS[setup_type],
        "action": action,
        "vcp_score": vcp_score,
        "pullback_score": pullback_score,
        "vcp_checks": "; ".join(k for k, v in vcp_checks.items() if v),
        "vcp_missing": "; ".join(k for k, v in vcp_checks.items() if not v),
        "pullback_checks": "; ".join(k for k, v in pullback_checks.items() if v),
        "pullback_missing": "; ".join(k for k, v in pullback_checks.items() if not v),
        "current_price": round(current, 2), "pivot": round(pivot, 2),
        "entry": round(entry, 2), "entry_zone_high": round(entry_high, 2), "max_chase": round(max_chase, 2),
        "stop": round(stop, 2), "structure_risk_pct": round(structure_risk, 1), "risk_per_share": round(risk_per_share, 2),
        "target1": round(target1, 2), "target2": round(target2, 2),
        "position_shares": shares, "position_value": round(shares*entry, 2), "expected_loss": round(shares*risk_per_share, 2),
        "distance_from_pivot": round(distance, 1), "breakout_volume_ratio": round(volume_ratio, 2),
        "vcp_r1_pct": round(r1, 1), "vcp_r2_pct": round(r2, 1), "vcp_r3_pct": round(r3, 1),
        "tight10_pct": round(tight10, 2), "tight5_pct": round(tight5, 2), "atr10_atr30_ratio": round(atr10/atr30, 2) if atr30 > 0 else None,
        "entry_explanation": f"${entry:.2f}~${entry_high:.2f}에서 거래량 50일 평균 {BREAKOUT_VOLUME_RATIO:.1f}배 이상 확인. ${max_chase:.2f} 초과 추격 금지.",
        "stop_explanation": f"마지막 수축 저점과 ATR 기준 ${stop:.2f}. 구조적 위험 {structure_risk:.1f}%.",
        "target_explanation": f"${target1:.2f}(2R)에서 30~50% 분할매도, ${target2:.2f}(3R) 또는 10일선 종가 이탈 시 잔량 관리.",
    }


# ============================================================
# FUNDAMENTALS / EARNINGS
# ============================================================
def get_earnings_date(ticker):
    try:
        dates = yf.Ticker(ticker).get_earnings_dates(limit=4)
        if dates is None or dates.empty:
            return None, None, "UNKNOWN"
        now = pd.Timestamp.now(tz="UTC")
        idx = pd.DatetimeIndex(dates.index)
        idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
        future = idx[idx >= now]
        if len(future) == 0:
            return None, None, "UNKNOWN"
        next_date = future.min()
        days = (next_date.normalize() - now.normalize()).days
        risk = "BLOCK" if days <= EARNINGS_BLOCK_DAYS else "LOW"
        return next_date.strftime("%Y-%m-%d"), int(days), risk
    except Exception:
        return None, None, "UNKNOWN"


def get_fundamental_info(ticker):
    for attempt in range(2):
        try:
            info = yf.Ticker(ticker).info
            eps, rev = info.get("earningsGrowth"), info.get("revenueGrowth")
            score = 0
            if eps is not None:
                score += 3 if eps >= 0.50 else 2 if eps >= 0.25 else 1 if eps >= 0.15 else 0
            if rev is not None:
                score += 3 if rev >= 0.30 else 2 if rev >= 0.15 else 1 if rev >= 0.08 else 0
            if eps is None or rev is None:
                status = "UNKNOWN"
            elif eps >= MIN_EPS_GROWTH and rev >= MIN_REV_GROWTH:
                status = "PASS"
            else:
                status = "FAIL"
            earnings_date, days_to_earnings, earnings_risk = get_earnings_date(ticker)
            return {
                "fundamental_status": status, "fundamental_score": score,
                "eps_growth_pct": None if eps is None else round(eps*100, 1),
                "rev_growth_pct": None if rev is None else round(rev*100, 1),
                "sector": info.get("sector", ""), "industry": info.get("industry", ""),
                "earnings_date": earnings_date, "days_to_earnings": days_to_earnings, "earnings_risk": earnings_risk,
            }
        except Exception as exc:
            if attempt == 0 and ("rate" in str(exc).lower() or "429" in str(exc)):
                time.sleep(FUNDAMENTAL_RETRY_SLEEP)
                continue
            return {"fundamental_status": "UNKNOWN", "fundamental_score": 0, "eps_growth_pct": None,
                    "rev_growth_pct": None, "sector": "", "industry": "", "earnings_date": None,
                    "days_to_earnings": None, "earnings_risk": "UNKNOWN"}


def apply_final_classification(row, market_state):
    classification = row["classification"]
    reasons = [row["classification_reason"]]
    if market_state in {"RED", "UNKNOWN"} and classification == "READY":
        classification = "WATCH"
        reasons.append(f"시장 상태가 {market_state}입니다.")
    if row.get("earnings_risk") == "BLOCK" and classification in {"READY", "SETUP", "PULLBACK_SETUP"}:
        classification = "WATCH"
        reasons.append(f"실적 발표까지 {row.get('days_to_earnings')}일로 신규 진입을 보류합니다.")
    if row.get("fundamental_status") == "FAIL" and classification == "READY":
        classification = "WATCH"
        reasons.append("펀더멘털 기준 미달입니다.")
    row["classification"] = classification
    row["classification_explanation"] = CATEGORY_EXPLANATIONS[classification]
    row["final_reason"] = " ".join(reasons)
    if classification == "REJECT" or market_state == "RED" or row.get("earnings_risk") == "BLOCK":
        row["position_shares"] = 0
        row["position_value"] = 0.0
        row["expected_loss"] = 0.0
    return row


# ============================================================
# REPORTING
# ============================================================
def create_summary(rows, today, market_status, universe_count, price_count, trend_count):
    classes = ["READY", "SETUP", "PULLBACK_SETUP", "WATCH", "REJECT"]
    counts = {name: sum(1 for x in rows if x["classification"] == name) for name in classes}
    summary_rows = []
    for name in classes:
        summary_rows.append({
            "date": today, "classification": name, "count": counts[name],
            "explanation": CATEGORY_EXPLANATIONS[name], "market_status": market_status,
            "validated_universe": universe_count, "valid_price_data": price_count, "trend_passed": trend_count,
        })
    pd.DataFrame(summary_rows).to_csv(f"minervini_v11_4_summary_{today}.csv", index=False, encoding="utf-8-sig")
    return counts


def write_all_candidates_txt(rows, counts, today, market_status):
    file_name = f"minervini_v11_4_all_candidates_{today}.txt"
    with open(file_name, "w", encoding="utf-8") as out:
        out.write(f"[{today}] 미너비니 V11.4 전체 후보 보고서\n")
        out.write(f"시장: {market_status}\n\n")
        out.write("[SUMMARY]\n")
        for name in ("READY", "SETUP", "PULLBACK_SETUP", "WATCH", "REJECT"):
            out.write(f"- {name}: {counts[name]}개\n  설명: {CATEGORY_EXPLANATIONS[name]}\n")
        out.write("\n")
        order = {"READY": 0, "SETUP": 1, "PULLBACK_SETUP": 2, "WATCH": 3, "REJECT": 4}
        for row in sorted(rows, key=lambda x: (order[x["classification"]], -x.get("quality_score", 0))):
            out.write("="*78 + "\n")
            out.write(f"{row['ticker']} | {row['classification']} | {row['setup_type']} | 품질점수 {row['quality_score']:.1f}\n")
            out.write(f"구분 설명: {row['classification_explanation']}\n")
            out.write(f"판단 이유: {row['final_reason']}\n")
            out.write(f"셋업 설명: {row['setup_explanation']}\n")
            out.write(f"VCP {row['vcp_score']}/8 | 눌림목 {row['pullback_score']}/8 | 펀더멘털 {row['fundamental_score']}/6 | RS {row['rs_rating']}\n")
            out.write(f"현재 ${row['current_price']} | 피벗 ${row['pivot']} | 이격 {row['distance_from_pivot']}% | 거래량비 {row['breakout_volume_ratio']}배\n")
            out.write(f"진입 ${row['entry']}~${row['entry_zone_high']} | 추격금지 ${row['max_chase']}\n")
            out.write(f"손절 ${row['stop']} ({row['structure_risk_pct']}%) | 1차 ${row['target1']} | 2차 ${row['target2']}\n")
            out.write(f"수량 {row['position_shares']}주 | 투자금 ${row['position_value']} | 예상손실 ${row['expected_loss']}\n")
            out.write(f"실적 상태 {row['fundamental_status']} | 다음 실적 {row.get('earnings_date')} | 남은일 {row.get('days_to_earnings')} | 위험 {row.get('earnings_risk')}\n")
            out.write(f"VCP 충족: {row['vcp_checks']}\nVCP 부족: {row['vcp_missing']}\n")
            out.write(f"눌림목 충족: {row['pullback_checks']}\n눌림목 부족: {row['pullback_missing']}\n")
            out.write(f"진입 설명: {row['entry_explanation']}\n손절 설명: {row['stop_explanation']}\n익절 설명: {row['target_explanation']}\n")
        out.write("="*78 + "\n자동 선별 결과이며 투자 추천이 아닙니다.\n")
    return file_name


def make_telegram_summary(rows, counts, today, market_status):
    message = f"[{today}] 미너비니 V11.4 SUMMARY\n시장: {market_status}\n\n"
    for name in ("READY", "SETUP", "PULLBACK_SETUP", "WATCH", "REJECT"):
        message += f"{name}: {counts[name]}개\n- {CATEGORY_EXPLANATIONS[name]}\n"
    actionable = [x for x in rows if x["classification"] in {"READY", "SETUP", "PULLBACK_SETUP"}]
    actionable.sort(key=lambda x: (0 if x["classification"] == "READY" else 1 if x["classification"] == "SETUP" else 2, -x["quality_score"]))
    message += f"\n[TOP {min(TOP_TELEGRAM_COUNT, len(actionable))}]\n"
    if not actionable:
        message += "현재 즉시 행동 가능한 상위 후보가 없습니다.\n"
    for row in actionable[:TOP_TELEGRAM_COUNT]:
        message += (
            f"{row['classification']} {row['ticker']} | {row['setup_type']}\n"
            f"품질 {row['quality_score']:.1f} | VCP {row['vcp_score']}/8 | 눌림 {row['pullback_score']}/8 | RS {row['rs_rating']}\n"
            f"현재 ${row['current_price']} | 진입 ${row['entry']}~${row['entry_zone_high']}\n"
            f"손절 ${row['stop']}({row['structure_risk_pct']}%) | 1차 ${row['target1']} | 2차 ${row['target2']}\n"
            f"이유: {row['final_reason']}\n\n"
        )
    message += "전체 후보와 상세 설명은 첨부 TXT를 확인하세요."
    return message


# ============================================================
# MAIN
# ============================================================
def main():
    today = pd.Timestamp.now(tz="Asia/Seoul").strftime("%Y-%m-%d")
    print(f"미너비니 V11.4 시작 | Python {sys.version.split()[0]} | pandas {pd.__version__} | yfinance {getattr(yf, '__version__', 'unknown')}")

    sp500, nasdaq100 = get_sp500_tickers(), get_nasdaq100_tickers()
    russell, russell_source = get_russell2000_tickers()
    raw = sorted(set(sp500 + nasdaq100 + russell))
    if not raw:
        raise RuntimeError("유니버스 수집 실패")
    official = get_official_universe()
    tickers, excluded = validate_universe(raw, official)
    pd.DataFrame(excluded).to_csv(f"excluded_universe_{today}.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"date": today, "sp500": len(sp500), "nasdaq100": len(nasdaq100), "russell2000": len(russell),
                   "russell_source": russell_source, "validated": len(tickers)}]).to_csv(f"universe_summary_{today}.csv", index=False, encoding="utf-8-sig")
    print(f"검증 유니버스: {len(tickers)}개")

    market_state, market_status = market_filter()
    print(f"시장: {market_status}")
    price_data, failures = download_prices(tickers, official)
    pd.DataFrame(failures).to_csv(f"price_failures_{today}.csv", index=False, encoding="utf-8-sig")
    if not price_data:
        raise RuntimeError("유효 가격 데이터 0개")

    rs_map = calculate_rs_scores(price_data)
    trend = [(ticker, frame, rs_map.get(ticker, {})) for ticker, frame in price_data.items()
             if passes_trend_template(frame, rs_map.get(ticker, {}))]
    print(f"트렌드 템플릿 통과: {len(trend)}개")

    technical_rows = []
    for ticker, frame, rs in trend:
        analysis = analyze_pattern(frame, rs.get("rs_rating", 0))
        if analysis:
            technical_rows.append({"ticker": ticker, "official_name": official.get(ticker, {}).get("name", ""),
                                   "rs_rating": rs.get("rs_rating", 0), "r3_return_pct": round(rs.get("r3", 0)*100, 1),
                                   "r6_return_pct": round(rs.get("r6", 0)*100, 1), "r12_return_pct": round(rs.get("r12", 0)*100, 1),
                                   **analysis})

    # 펀더멘털 조회 우선순위: 기술 품질이 높은 순
    technical_rows.sort(key=lambda x: (0 if x["classification"] == "READY" else 1 if x["classification"] == "SETUP" else 2 if x["classification"] == "PULLBACK_SETUP" else 3, -x["vcp_score"], -x["pullback_score"], -x["rs_rating"]))
    rows = []
    for index, row in enumerate(technical_rows):
        if index < MAX_FUNDAMENTAL_CALLS:
            fundamental = get_fundamental_info(row["ticker"])
            time.sleep(FUNDAMENTAL_SLEEP + random.uniform(0, 1))
        else:
            fundamental = {"fundamental_status": "NOT_CHECKED", "fundamental_score": 0, "eps_growth_pct": None,
                           "rev_growth_pct": None, "sector": "", "industry": "", "earnings_date": None,
                           "days_to_earnings": None, "earnings_risk": "UNKNOWN"}
        row.update(fundamental)
        row["market_state"] = market_state
        row["market_status"] = market_status
        row["quality_score"] = round(
            row["vcp_score"]*4 + row["pullback_score"]*3 + max(0, row["rs_rating"]-70)*0.5
            + row["fundamental_score"]*4 - row["structure_risk_pct"]*2
            - max(0, row["distance_from_pivot"]-2)*2, 1
        )
        rows.append(apply_final_classification(row, market_state))

    counts = create_summary(rows, today, market_status, len(tickers), len(price_data), len(trend))
    for classification in ("READY", "SETUP", "PULLBACK_SETUP", "WATCH", "REJECT"):
        selected = [x for x in rows if x["classification"] == classification]
        pd.DataFrame(selected).to_csv(f"minervini_v11_4_{classification.lower()}_{today}.csv", index=False, encoding="utf-8-sig")

    txt_file = write_all_candidates_txt(rows, counts, today, market_status)
    send_telegram_message(make_telegram_summary(rows, counts, today, market_status))
    send_telegram_file(txt_file, f"[{today}] V11.4 전체 후보 및 구분 설명")
    print("SUMMARY:", counts)
    print("미너비니 V11.4 완료")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        detail = traceback.format_exc()
        print("="*80)
        print(f"스크리닝 실패: {type(exc).__name__}: {exc}")
        print(detail)
        print("="*80)
        try:
            with open("screener_error_log.txt", "w", encoding="utf-8") as handle:
                handle.write(f"Error type: {type(exc).__name__}\nError message: {exc}\n\n{detail}")
        except Exception:
            pass
        try:
            send_telegram_message(f"미너비니 V11.4 실패\n{type(exc).__name__}: {exc}")
        except Exception:
            pass
        sys.exit(1)
