import io
import os
import re
import sys
import time
import random
import traceback
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# ============================================================
# V11.1 configuration
# ============================================================
STRICT_MODE = False
PRICE_PERIOD = "18mo"
CHUNK_SIZE = 50
SLEEP_BETWEEN_CHUNKS = 8
MAX_DOWNLOAD_RETRIES = 2
MARKET_FILTER_ENABLED = True

MIN_PRICE = 10.0
MIN_AVG_VOLUME = 200_000
MIN_DOLLAR_VOLUME = 3_000_000
MIN_RS_BALANCED = 80
MIN_RS_STRICT = 90
HIGH52_BALANCED = 0.75
HIGH52_STRICT = 0.85

MIN_EPS_GROWTH = 0.20
MIN_REV_GROWTH = 0.15
MAX_FUNDAMENTAL_CALLS = 40
FUNDAMENTAL_SLEEP = 3
FUNDAMENTAL_RETRY_SLEEP = 45

LAST_CONTRACTION_BALANCED = 0.08
LAST_CONTRACTION_STRICT = 0.05
CONTRACTION_RATIO_BALANCED = 0.85
CONTRACTION_RATIO_STRICT = 0.75
VOLUME_DRYUP_BALANCED = 0.85
VOLUME_DRYUP_STRICT = 0.70
ATR_DRYUP_BALANCED = 0.90
ATR_DRYUP_STRICT = 0.70
MAX_RISK_BALANCED = 10.0
MAX_RISK_STRICT = 6.0
PIVOT_NEAR_BALANCED = 6.0
PIVOT_NEAR_STRICT = 4.0
MIN_VCP_SCORE_BALANCED = 6  # 10점 만점
MIN_VCP_SCORE_STRICT = 8
RANGE_TOLERANCE = 0.98

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
        return {"mode": "STRICT", "rs": MIN_RS_STRICT, "high52": HIGH52_STRICT,
                "last_contraction": LAST_CONTRACTION_STRICT,
                "contraction_ratio": CONTRACTION_RATIO_STRICT,
                "volume_dryup": VOLUME_DRYUP_STRICT, "atr_dryup": ATR_DRYUP_STRICT,
                "max_risk": MAX_RISK_STRICT, "pivot_near": PIVOT_NEAR_STRICT,
                "min_vcp_score": MIN_VCP_SCORE_STRICT}
    return {"mode": "BALANCED", "rs": MIN_RS_BALANCED, "high52": HIGH52_BALANCED,
            "last_contraction": LAST_CONTRACTION_BALANCED,
            "contraction_ratio": CONTRACTION_RATIO_BALANCED,
            "volume_dryup": VOLUME_DRYUP_BALANCED, "atr_dryup": ATR_DRYUP_BALANCED,
            "max_risk": MAX_RISK_BALANCED, "pivot_near": PIVOT_NEAR_BALANCED,
            "min_vcp_score": MIN_VCP_SCORE_BALANCED}


def send_telegram_message(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("텔레그램 환경변수가 없어 전송을 건너뜁니다.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        res = requests.post(url, json={"chat_id": CHAT_ID, "text": message}, timeout=20)
        if res.status_code != 200:
            print(f"텔레그램 전송 실패: {res.status_code} {res.text[:300]}")
    except Exception as exc:
        print(f"텔레그램 전송 오류: {exc}")


def get_text(url, timeout=35):
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


def get_sp500_tickers():
    try:
        tables = pd.read_html(io.StringIO(get_text("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")))
        values = sorted({clean_ticker(x) for x in tables[0]["Symbol"].dropna() if valid_ticker(x)})
        print(f"S&P500: {len(values)}개")
        return values
    except Exception as exc:
        print(f"S&P500 수집 실패: {exc}")
        return []


def get_nasdaq100_tickers():
    """HTML id에 의존하지 않고 90~110개 Ticker/Symbol 표를 찾는다."""
    try:
        tables = pd.read_html(io.StringIO(get_text("https://en.wikipedia.org/wiki/Nasdaq-100")))
        for frame in tables:
            frame = frame.copy()
            frame.columns = [str(c).strip() for c in frame.columns]
            for col in frame.columns:
                if str(col).lower() not in {"ticker", "symbol"}:
                    continue
                values = sorted({clean_ticker(x) for x in frame[col].dropna() if valid_ticker(x)})
                if 90 <= len(values) <= 110:
                    print(f"Nasdaq100: {len(values)}개")
                    return values
        print("Nasdaq100 구성 종목 표를 찾지 못했습니다. 중복 지수로 간주하고 계속합니다.")
    except Exception as exc:
        print(f"Nasdaq100 수집 실패: {exc}")
    return []


def decode_response(response):
    content = response.content
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "latin1"):
        try:
            text = content.decode(encoding).replace("\x00", "")
            if text.strip():
                return text
        except Exception:
            pass
    return response.text.replace("\x00", "")


def parse_csv_tickers(text):
    lines = [line for line in text.splitlines() if line.strip()]
    for start in range(min(15, len(lines))):
        for sep in (",", "\t", ";"):
            try:
                frame = pd.read_csv(io.StringIO("\n".join(lines[start:])), sep=sep, engine="python", on_bad_lines="skip")
                frame.columns = [str(c).strip().replace("\ufeff", "") for c in frame.columns]
                col = next((c for c in frame.columns if "ticker" in c.lower() or "symbol" in c.lower()), None)
                if col is None:
                    continue
                values = sorted({clean_ticker(x) for x in frame[col].dropna() if valid_ticker(x)})
                if len(values) >= 1000:
                    return values
            except Exception:
                pass
    return []


def get_russell2000_tickers():
    urls = [
        ("iShares IWM", "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund"),
        ("GitHub fallback", "https://raw.githubusercontent.com/quanthero/US_Indices_Constituents/main/Russell2000.csv"),
    ]
    for name, url in urls:
        try:
            response = requests.get(url, headers=REQUEST_HEADERS, timeout=45)
            response.raise_for_status()
            values = parse_csv_tickers(decode_response(response))
            if len(values) >= 1400:
                print(f"Russell2000: {len(values)}개 ({name})")
                return values, name
        except Exception as exc:
            print(f"Russell2000 {name} 실패: {exc}")
    return [], "failed"


def parse_pipe(text):
    lines = [x.strip() for x in text.splitlines() if x.strip() and not x.startswith("File Creation Time")]
    return pd.read_csv(io.StringIO("\n".join(lines)), sep="|") if lines else pd.DataFrame()


def official_universe():
    result = {}
    sources = [
        ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", "Symbol", "Security Name", "NASDAQ"),
        ("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", "ACT Symbol", "Security Name", None),
    ]
    for url, symbol_col, name_col, fixed_exchange in sources:
        try:
            frame = parse_pipe(get_text(url))
            for _, row in frame.iterrows():
                symbol = clean_ticker(row.get(symbol_col, ""))
                if not valid_ticker(symbol):
                    continue
                result[symbol] = {
                    "name": str(row.get(name_col, "")),
                    "exchange": fixed_exchange or str(row.get("Exchange", "")),
                    "etf": str(row.get("ETF", "N")),
                    "test": str(row.get("Test Issue", "N")),
                }
        except Exception as exc:
            print(f"공식 상장 리스트 실패: {exc}")
    print(f"공식 상장 리스트: {len(result)}개")
    return result


def is_common(meta):
    name = str(meta.get("name", "")).lower()
    bad = (" etf", "etn", "warrant", " warrant", "right", " unit", "preferred", "depositary share",
           "note due", "notes due", "bond", "debenture", "closed end fund")
    return meta.get("etf", "N").upper() != "Y" and meta.get("test", "N").upper() != "Y" and not any(x in name for x in bad)


def validate_universe(tickers, official):
    included, excluded = [], []
    for ticker in sorted(set(map(clean_ticker, tickers))):
        meta = official.get(ticker)
        if not valid_ticker(ticker):
            excluded.append({"ticker": ticker, "reason": "BAD_FORMAT"})
        elif meta is None:
            excluded.append({"ticker": ticker, "reason": "NOT_IN_OFFICIAL_LIST"})
        elif not is_common(meta):
            excluded.append({"ticker": ticker, "reason": "NON_COMMON", **meta})
        else:
            included.append(ticker)
    return included, excluded


def market_filter():
    if not MARKET_FILTER_ENABLED:
        return "UNKNOWN", "시장 필터 비활성화"
    try:
        # repair=True 제거: yfinance 1.5.1에서 scipy 의존 오류가 발생할 수 있음
        data = yf.download("SPY", period=PRICE_PERIOD, progress=False, auto_adjust=True,
                           multi_level_index=True, timeout=40)
        close = data["Close"].dropna()
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        if len(close) < 210:
            return "UNKNOWN", "SPY 데이터 부족 → 신규 진입 보류"
        price = float(close.iloc[-1])
        ma50 = float(close.rolling(50).mean().iloc[-1])
        ma200 = float(close.rolling(200).mean().iloc[-1])
        state = "GREEN" if price > ma50 and price > ma200 else "CAUTION"
        return state, f"SPY {price:.2f} / MA50 {ma50:.2f} / MA200 {ma200:.2f} / {state}"
    except Exception as exc:
        return "UNKNOWN", f"SPY 조회 실패: {exc} → 신규 진입 보류"


def get_ticker_frame(raw, ticker):
    try:
        if raw is None or raw.empty:
            return None
        if not isinstance(raw.columns, pd.MultiIndex):
            return raw.copy() if "Close" in raw.columns else None
        if ticker in raw.columns.get_level_values(0):
            return raw[ticker].copy()
        if ticker in raw.columns.get_level_values(1):
            return raw.xs(ticker, axis=1, level=1).copy()
    except Exception:
        pass
    return None


def prepare_frame(frame):
    if frame is None or frame.empty or any(c not in frame for c in ("High", "Low", "Close", "Volume")):
        return None
    frame = frame.dropna(subset=["Close"]).copy()
    if len(frame) < 260:
        return None
    frame.index = frame.index.tz_localize(None) if getattr(frame.index, "tz", None) is not None else frame.index
    for n in (10, 20, 50, 150, 200):
        frame[f"MA{n}"] = frame["Close"].rolling(n).mean()
    frame["Vol_MA10"] = frame["Volume"].rolling(10).mean()
    frame["Vol_MA50"] = frame["Volume"].rolling(50).mean()
    frame["Vol_Median50"] = frame["Volume"].rolling(50).median()
    frame["DollarVol_MA50"] = (frame["Close"] * frame["Volume"]).rolling(50).mean()
    tr = pd.concat([(frame["High"] - frame["Low"]),
                    (frame["High"] - frame["Close"].shift()).abs(),
                    (frame["Low"] - frame["Close"].shift()).abs()], axis=1).max(axis=1)
    frame["ATR10"] = tr.rolling(10).mean()
    frame["ATR30"] = tr.rolling(30).mean()
    return frame


def ret(close, days):
    return float(close.iloc[-1] / close.iloc[-days] - 1) if len(close) > days and close.iloc[-days] > 0 else None


def rs_scores(price_data):
    rows = []
    for ticker, frame in price_data.items():
        close = frame["Close"].dropna()
        r3, r6, r12 = ret(close, 63), ret(close, 126), ret(close, 252)
        if None not in (r3, r6, r12):
            rows.append({"ticker": ticker, "r3": r3, "r6": r6, "r12": r12,
                         "weighted": r3 * 0.4 + r6 * 0.3 + r12 * 0.3})
    if not rows:
        return {}
    frame = pd.DataFrame(rows)
    frame["rs_rating"] = (frame["weighted"].rank(pct=True) * 99).round().astype(int)
    return frame.set_index("ticker")[["rs_rating", "r3", "r6", "r12"]].to_dict("index")


def trend_template(frame, rs):
    c = cfg()
    cp = float(frame["Close"].iloc[-1])
    ma50, ma150, ma200 = (float(frame[x].iloc[-1]) for x in ("MA50", "MA150", "MA200"))
    low52, high52 = float(frame["Close"].tail(252).min()), float(frame["Close"].tail(252).max())
    numbers = [cp, ma50, ma150, ma200, low52, high52, frame["Vol_MA50"].iloc[-1], frame["DollarVol_MA50"].iloc[-1]]
    if any(pd.isna(x) for x in numbers):
        return False
    return all((cp >= MIN_PRICE, cp > ma50 > ma150 > ma200,
                ma200 > frame["MA200"].iloc[-22] and ma200 > frame["MA200"].iloc[-44],
                cp >= low52 * 1.30, cp >= high52 * c["high52"], rs.get("rs_rating", 0) >= c["rs"],
                frame["Vol_MA50"].iloc[-1] >= MIN_AVG_VOLUME,
                frame["Vol_Median50"].iloc[-1] >= MIN_AVG_VOLUME * 0.4,
                frame["Vol_MA10"].iloc[-1] >= MIN_AVG_VOLUME * 0.3,
                frame["DollarVol_MA50"].iloc[-1] >= MIN_DOLLAR_VOLUME))


def vcp_plan(frame):
    c = cfg()
    recent = frame.tail(75)
    if len(recent) < 75:
        return False, {}

    def contraction(part):
        low, high = float(part["Low"].min()), float(part["High"].max())
        return (high - low) / low if low > 0 else None

    r1, r2, r3 = contraction(recent.iloc[:30]), contraction(recent.iloc[30:55]), contraction(recent.iloc[55:])
    if None in (r1, r2, r3):
        return False, {}
    current, current_vol = float(frame["Close"].iloc[-1]), float(frame["Volume"].iloc[-1])
    vol50 = float(frame["Vol_MA50"].iloc[-1])
    atr10, atr30 = float(frame["ATR10"].iloc[-1]), float(frame["ATR30"].iloc[-1])
    pivot = float(frame["High"].iloc[-21:-1].max())  # 오늘 봉 제외
    entry = pivot * (1 + ENTRY_BUFFER_PCT)
    entry_high = pivot * (1 + ENTRY_ZONE_PCT)
    max_chase = pivot * (1 + MAX_CHASE_PCT)
    last_low = float(frame["Low"].iloc[-11:-1].min())
    stop = max(last_low - atr10 * 0.25, entry * (1 - MAX_STOP_LOSS_PCT))
    if stop >= entry:
        return False, {}
    rps = entry - stop
    risk = rps / entry * 100
    target1, target2 = entry + rps * 2, entry + rps * 3
    distance = (pivot - current) / pivot * 100
    vol_ratio = current_vol / vol50 if vol50 > 0 else 0
    early_vol = float(recent["Volume"].iloc[:35].median())
    recent_vol = float(recent["Volume"].iloc[-10:].median())
    tight10 = float(recent["Close"].tail(10).std() / recent["Close"].tail(10).mean())
    last5 = frame.iloc[-6:-1]
    tight5 = float((last5["High"].max() - last5["Low"].min()) / last5["Close"].mean())
    higher_low = float(frame["Low"].iloc[-11:-1].min()) >= float(frame["Low"].iloc[-21:-11].min()) * 0.98
    near_ma = min(abs(current / float(frame["MA10"].iloc[-1]) - 1), abs(current / float(frame["MA20"].iloc[-1]) - 1)) < 0.03
    pullback = 0.5 <= distance <= 8 and near_ma and recent_vol < early_vol * 0.85
    breakout = entry <= current <= max_chase and vol_ratio >= BREAKOUT_VOLUME_RATIO
    setup = "🚀돌파" if breakout else "🎯눌림목" if pullback else "🔥돌파임박" if 0 <= distance <= c["pivot_near"] else "👀관찰"

    if current < entry:
        action, reason = "대기", f"${entry:.2f} 이상 돌파와 거래량 증가 확인"
    elif current <= entry_high and vol_ratio >= BREAKOUT_VOLUME_RATIO:
        action, reason = "진입 가능", "권장 진입 구간과 거래량 조건 충족"
    elif current <= max_chase and vol_ratio >= BREAKOUT_VOLUME_RATIO:
        action, reason = "소액만 검토", "피벗에서 다소 상승하여 수량 축소 필요"
    elif current > max_chase:
        action, reason = "추격 금지", "피벗에서 5% 이상 상승"
    else:
        action, reason = "돌파 확인 대기", "가격은 피벗 위지만 거래량 조건 미달"

    checks = {
        "setup_ready": setup != "👀관찰", "risk_ok": MIN_STOP_LOSS_PCT * 100 <= risk <= c["max_risk"],
        "range_contracts": r1 >= r2 * RANGE_TOLERANCE and r2 >= r3 * RANGE_TOLERANCE,
        "meaningful_contracts": r2 <= r1 * c["contraction_ratio"] and r3 <= r2 * c["contraction_ratio"],
        "last_contraction": r3 <= c["last_contraction"],
        "volume_dryup": recent_vol <= early_vol * c["volume_dryup"],
        "atr_dryup": atr30 > 0 and atr10 <= atr30 * c["atr_dryup"],
        "tight10": tight10 <= 0.03, "tight5": tight5 <= 0.05, "higher_low": higher_low,
    }
    score = sum(checks.values())
    by_risk = int(ACCOUNT_SIZE * ACCOUNT_RISK_PCT / rps)
    by_value = int(ACCOUNT_SIZE * MAX_POSITION_PCT / entry)
    shares = max(0, min(by_risk, by_value))
    details = {
        "setup_type": setup, "action": action, "action_reason": reason, "vcp_score": score,
        "vcp_checks": ";".join(k for k, v in checks.items() if v), "current_price": round(current, 2),
        "pivot": round(pivot, 2), "entry": round(entry, 2), "entry_zone_high": round(entry_high, 2),
        "max_chase": round(max_chase, 2), "stop": round(stop, 2), "risk": round(risk, 1),
        "risk_per_share": round(rps, 2), "target1": round(target1, 2), "target2": round(target2, 2),
        "position_shares": shares, "position_value": round(shares * entry, 2),
        "expected_loss": round(shares * rps, 2), "distance_from_pivot": round(distance, 1),
        "breakout_volume_ratio": round(vol_ratio, 2), "vcp_r1_pct": round(r1 * 100, 1),
        "vcp_r2_pct": round(r2 * 100, 1), "vcp_r3_pct": round(r3 * 100, 1),
        "entry_explanation": f"${entry:.2f}~${entry_high:.2f}, 거래량 50일 평균 {BREAKOUT_VOLUME_RATIO:.1f}배 이상. ${max_chase:.2f} 초과 추격 금지.",
        "stop_explanation": f"마지막 수축 저점과 ATR을 반영한 ${stop:.2f}를 종가 기준 이탈 시 손절.",
        "target_explanation": f"${target1:.2f}(2R)에서 30~50% 분할매도, ${target2:.2f}(3R) 또는 10일선 종가 이탈 시 잔량 관리.",
    }
    passed = checks["setup_ready"] and checks["risk_ok"] and score >= c["min_vcp_score"]
    return passed, details


def fundamental(ticker):
    for attempt in range(2):
        try:
            info = yf.Ticker(ticker).info
            eps, rev = info.get("earningsGrowth"), info.get("revenueGrowth")
            if eps is None or rev is None:
                return "UNKNOWN", eps, rev, info.get("sector", ""), info.get("industry", ""), "EPS 또는 매출 데이터 없음"
            status = "PASS" if eps >= MIN_EPS_GROWTH and rev >= MIN_REV_GROWTH else "FAIL"
            return status, eps, rev, info.get("sector", ""), info.get("industry", ""), f"EPS {eps*100:.1f}% / 매출 {rev*100:.1f}%"
        except Exception as exc:
            if attempt == 0 and ("rate" in str(exc).lower() or "429" in str(exc)):
                time.sleep(FUNDAMENTAL_RETRY_SLEEP)
                continue
            return "UNKNOWN", None, None, "", "", f"조회 오류: {exc}"


def download_prices(tickers):
    frames = {}
    failures = []
    for i in range(0, len(tickers), CHUNK_SIZE):
        chunk = tickers[i:i + CHUNK_SIZE]
        print(f"가격 다운로드 {i+1}~{min(i+CHUNK_SIZE, len(tickers))}/{len(tickers)}")
        raw = None
        for attempt in range(MAX_DOWNLOAD_RETRIES):
            try:
                # repair=True를 사용하지 않는다. scipy가 없어도 동작해야 한다.
                raw = yf.download(chunk, period=PRICE_PERIOD, interval="1d", group_by="ticker",
                                  progress=False, threads=False, timeout=40, auto_adjust=True,
                                  multi_level_index=True)
                if raw is not None and not raw.empty:
                    break
            except Exception as exc:
                print(f"청크 {attempt+1}차 실패: {exc}")
            time.sleep(5 + attempt * 5)
        for ticker in chunk:
            frame = prepare_frame(get_ticker_frame(raw, ticker))
            if frame is None:
                failures.append({"ticker": ticker, "reason": "PRICE_DATA_FAILED_OR_INSUFFICIENT"})
            else:
                frames[ticker] = frame
        time.sleep(SLEEP_BETWEEN_CHUNKS + random.uniform(0, 2))
    return frames, failures


def main():
    today = pd.Timestamp.now(tz="Asia/Seoul").strftime("%Y-%m-%d")
    c = cfg()
    print(f"미너비니 스크리너 V11.1 / {c['mode']}")
    print(f"Python {sys.version.split()[0]} / pandas {pd.__version__} / yfinance {getattr(yf, '__version__', 'unknown')}")

    sp500, nasdaq100 = get_sp500_tickers(), get_nasdaq100_tickers()
    russell, russell_source = get_russell2000_tickers()
    raw_tickers = sorted(set(sp500 + nasdaq100 + russell))
    if not raw_tickers:
        raise RuntimeError("종목 유니버스를 수집하지 못했습니다.")

    official = official_universe()
    tickers, excluded = validate_universe(raw_tickers, official)
    pd.DataFrame(excluded).to_csv(f"excluded_universe_{today}.csv", index=False)
    pd.DataFrame([{"date": today, "mode": c["mode"], "sp500": len(sp500), "nasdaq100": len(nasdaq100),
                   "russell2000": len(russell), "russell_source": russell_source,
                   "validated_universe": len(tickers)}]).to_csv(f"universe_summary_{today}.csv", index=False)
    if not tickers:
        raise RuntimeError("공식 상장 검증 후 남은 종목이 없습니다.")

    market_state, market_status = market_filter()
    print(f"시장: {market_status}")
    prices, failures = download_prices(tickers)
    pd.DataFrame(failures).to_csv(f"price_failures_{today}.csv", index=False)
    if not prices:
        raise RuntimeError("가격 데이터가 전혀 없습니다. yfinance 오류와 설치 패키지를 확인하세요.")

    rs_map = rs_scores(prices)
    trend = [(t, f, rs_map.get(t, {})) for t, f in prices.items() if trend_template(f, rs_map.get(t, {}))]
    candidates, near_miss = [], []
    for ticker, frame, rs in trend:
        passed, plan = vcp_plan(frame)
        if passed:
            candidates.append((ticker, rs, plan))
        elif plan and plan.get("vcp_score", 0) >= c["min_vcp_score"] - 1:
            near_miss.append({"ticker": ticker, "rs_rating": rs.get("rs_rating", 0), **plan})
    pd.DataFrame(near_miss).to_csv(f"vcp_near_miss_{today}.csv", index=False)
    candidates.sort(key=lambda x: (-x[2]["vcp_score"], -x[1].get("rs_rating", 0)))

    final, watch = [], []
    for idx, (ticker, rs, plan) in enumerate(candidates):
        if idx < MAX_FUNDAMENTAL_CALLS:
            status, eps, rev, sector, industry, reason = fundamental(ticker)
            time.sleep(FUNDAMENTAL_SLEEP + random.uniform(0, 1))
        else:
            status, eps, rev, sector, industry, reason = "NOT_CHECKED", None, None, "", "", "호출 한도 초과"
        row = {"ticker": ticker, **plan, "rs_rating": rs.get("rs_rating", 0),
               "r3_return_pct": round(rs.get("r3", 0) * 100, 1),
               "r6_return_pct": round(rs.get("r6", 0) * 100, 1),
               "r12_return_pct": round(rs.get("r12", 0) * 100, 1),
               "fundamental_status": status, "fundamental_reason": reason,
               "eps_growth_pct": None if eps is None else round(eps * 100, 1),
               "rev_growth_pct": None if rev is None else round(rev * 100, 1),
               "sector": sector, "industry": industry, "market_state": market_state,
               "market_status": market_status, "official_name": official.get(ticker, {}).get("name", "")}
        if market_state != "GREEN":
            row.update({"action": "신규 진입 보류", "action_reason": market_status,
                        "position_shares": 0, "position_value": 0.0, "expected_loss": 0.0})
        (final if status == "PASS" else watch).append(row)

    pd.DataFrame(final).to_csv(f"minervini_v11_1_strict_{today}.csv", index=False)
    pd.DataFrame(watch).to_csv(f"minervini_v11_1_watchlist_{today}.csv", index=False)

    all_rows = final + watch
    message = (f"[{today}] 미너비니 V11.1\n시장: {market_status}\n"
               f"유효가격 {len(prices)} / 추세통과 {len(trend)} / VCP후보 {len(candidates)} / 실적통과 {len(final)}\n\n")
    if not all_rows:
        message += "조건을 만족하는 후보가 없습니다.\n"
    for row in all_rows[:20]:
        message += (f"{row['ticker']} [{row['action']}] 현재 ${row['current_price']} / 피벗 ${row['pivot']}\n"
                    f"진입 ${row['entry']}~${row['entry_zone_high']} / 추격금지 ${row['max_chase']}\n"
                    f"손절 ${row['stop']}(-{row['risk']}%) / 1차 ${row['target1']} / 2차 ${row['target2']}\n"
                    f"수량 {row['position_shares']}주 / 예상손실 ${row['expected_loss']} / RS {row['rs_rating']}\n"
                    f"설명: {row['action_reason']}\n\n")
    message += "자동 선별 결과이며 투자 추천이 아닙니다. 주문 전 차트와 실적 일정을 확인하세요."
    send_telegram_message(message)
    print("스크리닝 완료")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"스크리닝 실패: {exc}")
        print(traceback.format_exc())
        send_telegram_message(f"스크리닝 실패\n{type(exc).__name__}: {exc}")
        sys.exit(1)
