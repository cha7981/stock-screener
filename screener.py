
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

# ===== Minervini / Screener Settings =====
MIN_RS_RATING = 70
MIN_AVG_VOLUME = 150000
MIN_DOLLAR_VOLUME = 2000000
MIN_EPS_GROWTH = 0.20
MIN_REV_GROWTH = 0.15
MAX_RISK_PCT = 8.0
PIVOT_NEAR_PCT = 5.0
VCP_MAX_LAST_CONTRACTION = 0.08

PRICE_PERIOD = "18mo"
CHUNK_SIZE = 50
SLEEP_BETWEEN_CHUNKS = 8
FUNDAMENTAL_SLEEP = 3
FUNDAMENTAL_RETRY_SLEEP = 45
MAX_FUNDAMENTAL_CALLS = 40
MARKET_FILTER_ENABLED = False


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
    return str(ticker).strip().replace("\ufeff", "").replace('"', "").replace(".", "-")


def is_valid_us_ticker(ticker):
    ticker = clean_ticker(ticker)
    if not ticker:
        return False
    bad_values = {"CASH", "USD", "-", "N/A", "VALUE", "TICKER", "SYMBOL", "NO", "CONSTITUENTS"}
    if ticker.upper() in bad_values:
        return False
    if " " in ticker or len(ticker) > 8:
        return False
    bad_suffixes = ("-TO", "-OL", "-DE", "-L", "-PA", "-AS", "-SW", "-VI", "-F")
    if ticker.endswith(bad_suffixes):
        return False
    if ticker.startswith("RTY") or ticker.startswith("RTYM"):
        return False
    if re.search(r"\d{3,}", ticker):
        return False
    return bool(re.match(r"^[A-Z][A-Z0-9]*(?:-[A-Z])?$", ticker))


def get_html_with_header(url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
    res = requests.get(url, headers=headers, timeout=20)
    res.raise_for_status()
    return res.text


def get_sp500_tickers():
    try:
        html = get_html_with_header("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
        df = pd.read_html(io.StringIO(html))[0]
        tickers = sorted(set(clean_ticker(x) for x in df["Symbol"].dropna().tolist()))
        print(f"✅ S&P500 수집 성공: {len(tickers)}개")
        return tickers
    except Exception as e:
        print(f"❌ S&P500 수집 실패: {e}")
        return []


def get_nasdaq100_tickers():
    try:
        html = get_html_with_header("https://en.wikipedia.org/wiki/Nasdaq-100")
        dfs = pd.read_html(io.StringIO(html), attrs={"id": "constituents"})
        if not dfs:
            print("❌ Nasdaq100 테이블을 찾지 못했습니다.")
            return []
        tickers = sorted(set(clean_ticker(x) for x in dfs[0]["Ticker"].dropna().tolist()))
        print(f"✅ Nasdaq100 수집 성공: {len(tickers)}개")
        return tickers
    except Exception as e:
        print(f"❌ Nasdaq100 수집 실패: {e}")
        return []


def decode_response_text_safely(response):
    content = response.content
    if content.startswith(b"\xff\xfe") or content.startswith(b"\xfe\xff") or content.count(b"\x00") > max(10, len(content) // 20):
        for enc in ["utf-16", "utf-16-le", "utf-16-be"]:
            try:
                text = content.decode(enc, errors="ignore")
                if "Ticker" in text or "Symbol" in text:
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


def extract_tickers_from_csv_like_text(text):
    text = text.replace("\x00", "").replace("\ufeff", "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    start_idx = None
    delimiter = ","
    for i, line in enumerate(lines):
        norm = line.replace('"', '').lower()
        if ("ticker" in norm or "symbol" in norm or "constituents" in norm) and ("name" in norm or "sector" in norm or "asset class" in norm or "^rut" in norm):
            start_idx = i
            delimiter = "\t" if line.count("\t") > line.count(",") else ","
            break

    if start_idx is not None:
        reader = csv.reader(io.StringIO("\n".join(lines[start_idx:])), delimiter=delimiter)
        try:
            header = next(reader)
        except StopIteration:
            return []
        header_norm = [str(h).lower().strip().replace(" ", "_") for h in header]
        ticker_col = 0
        for idx, h in enumerate(header_norm):
            if "ticker" in h or "symbol" in h or "constituents" in h:
                ticker_col = idx
                break
        tickers = []
        for row in reader:
            if not row or len(row) <= ticker_col:
                continue
            if row[0].startswith("The") or "BlackRock" in row[0] or row[0].startswith("©"):
                break
            t = clean_ticker(row[ticker_col])
            if is_valid_us_ticker(t):
                tickers.append(t)
        return sorted(set(tickers))

    try:
        df = pd.read_csv(io.StringIO(text))
        cols = [c for c in df.columns if any(k in str(c).lower() for k in ["ticker", "symbol", "constituents"])]
        if not cols and len(df.columns) > 0:
            cols = [df.columns[0]]
        tickers = []
        for x in df[cols[0]].dropna().astype(str).tolist():
            t = clean_ticker(x)
            if is_valid_us_ticker(t) and t != "^RUT":
                tickers.append(t)
        return sorted(set(tickers))
    except Exception:
        return []


def get_russell2000_tickers():
    sources = [
        ("iShares IWM", "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund"),
        ("iShares IWM alt1", "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM"),
        ("GitHub quanthero fallback", "https://raw.githubusercontent.com/quanthero/US_Indices_Constituents/main/Russell2000.csv"),
        ("GitHub ikoniaris fallback", "https://raw.githubusercontent.com/ikoniaris/Russell2000/master/russell_2000_components.csv"),
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "text/csv,application/csv,text/plain,application/octet-stream,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf",
    }
    for name, url in sources:
        try:
            print(f"📥 Russell2000 수집 시도: {name}")
            res = requests.get(url, headers=headers, timeout=40)
            res.raise_for_status()
            text = decode_response_text_safely(res)
            tickers = extract_tickers_from_csv_like_text(text)
            if len(tickers) >= 1400:
                print(f"✅ Russell2000 수집 성공({name}): {len(tickers)}개")
                return tickers
            print(f"⚠️ {name} 파싱 결과가 적습니다: {len(tickers)}개, 다음 소스 시도")
        except Exception as e:
            print(f"⚠️ {name} 수집 실패: {e}")
    print("❌ Russell2000 티커 수집 최종 실패")
    return []


def get_ticker_dataframe(raw_data, ticker):
    try:
        if raw_data is None or raw_data.empty:
            return None
        if isinstance(raw_data.columns, pd.MultiIndex):
            if ticker in raw_data.columns.get_level_values(0):
                return raw_data[ticker].copy()
            return None
        if "Close" in raw_data.columns:
            return raw_data.copy()
    except Exception:
        pass
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
    return df


def safe_return(close, days):
    if len(close) <= days:
        return None
    start = close.iloc[-days]
    end = close.iloc[-1]
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
    try:
        data = yf.download(["SPY", "QQQ"], period="1y", interval="1d", group_by="ticker", progress=False, threads=False, timeout=30, auto_adjust=True)
        oks = []
        for idx in ["SPY", "QQQ"]:
            df = data[idx].copy() if isinstance(data.columns, pd.MultiIndex) and idx in data.columns.get_level_values(0) else data.copy()
            df = df.dropna(subset=["Close"]).copy()
            if len(df) < 220:
                return False, "시장 ETF 데이터 부족"
            df["MA50"] = df["Close"].rolling(50).mean()
            df["MA200"] = df["Close"].rolling(200).mean()
            oks.append(df["Close"].iloc[-1] > df["MA50"].iloc[-1] > df["MA200"].iloc[-1])
        return (True, "양호: SPY/QQQ 상승 추세") if all(oks) else (False, "주의: SPY 또는 QQQ 상승 추세 미충족")
    except Exception as e:
        print(f"⚠️ 시장 환경 필터 확인 실패: {e}")
        return False, "시장 환경 확인 실패"


def passes_trend_template(ticker, df, rs_info):
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
            cp > ma150 and cp > ma200,
            ma150 > ma200,
            ma200 > ma200_22 and ma200 > ma200_44,
            ma50 > ma150 and ma50 > ma200,
            cp > ma50,
            cp >= low52 * 1.30,
            cp >= high52 * 0.75,
            rs >= MIN_RS_RATING,
            avg_vol >= MIN_AVG_VOLUME,
            avg_dvol >= MIN_DOLLAR_VOLUME,
        ])
    except Exception as e:
        print(f"⚠️ {ticker} 트렌드 템플릿 오류: {e}")
        return False


def check_vcp_pattern(ticker, df):
    try:
        recent = df.tail(65).copy()
        if len(recent) < 65:
            return False, {}
        seg1, seg2, seg3 = recent.iloc[0:25], recent.iloc[25:45], recent.iloc[45:65]
        def cr(seg):
            low, high = seg["Low"].min(), seg["High"].max()
            return None if low <= 0 else (high - low) / low
        r1, r2, r3 = cr(seg1), cr(seg2), cr(seg3)
        if r1 is None or r2 is None or r3 is None:
            return False, {}
        if not (r1 > r2 > r3) or r3 > VCP_MAX_LAST_CONTRACTION:
            return False, {}
        vol_early, vol_recent, vol_ma50 = recent["Volume"].iloc[0:30].mean(), recent["Volume"].iloc[-10:].mean(), df["Vol_MA50"].iloc[-1]
        if pd.isna(vol_early) or pd.isna(vol_recent) or pd.isna(vol_ma50) or not (vol_recent < vol_early):
            return False, {}
        current, current_vol = df["Close"].iloc[-1], df["Volume"].iloc[-1]
        pivot = recent["High"].tail(20).max()
        stop = max(recent["Low"].tail(20).min(), df["MA50"].iloc[-1] * 0.97)
        risk = (pivot - stop) / pivot * 100
        if risk <= 0 or risk > MAX_RISK_PCT:
            return False, {}
        dist = (pivot - current) / pivot * 100
        near_pivot = current <= pivot * 1.03 and current >= pivot * (1 - PIVOT_NEAR_PCT / 100)
        breakout = current > pivot and current_vol >= vol_ma50 * 1.5
        if not near_pivot and not breakout:
            return False, {}
        return True, {
            "setup_type": "BREAKOUT" if breakout else "WATCH",
            "current_price": round(current, 2),
            "entry": round(pivot, 2),
            "stop": round(stop, 2),
            "risk": round(risk, 1),
            "distance_from_pivot": round(dist, 1),
            "vcp_r1": round(r1 * 100, 1),
            "vcp_r2": round(r2 * 100, 1),
            "vcp_r3": round(r3 * 100, 1),
            "vol_decline": round((1 - vol_recent / vol_early) * 100, 1),
            "breakout_volume_ratio": round(current_vol / vol_ma50, 2),
        }
    except Exception as e:
        print(f"⚠️ {ticker} VCP 검사 오류: {e}")
        return False, {}


def build_fundamental_reason(status, eps, rev, raw_reason):
    eps_txt = "없음" if eps is None else f"{eps * 100:.1f}%"
    rev_txt = "없음" if rev is None else f"{rev * 100:.1f}%"
    eps_req = f"{MIN_EPS_GROWTH * 100:.0f}%"
    rev_req = f"{MIN_REV_GROWTH * 100:.0f}%"
    if status == "PASS":
        return f"통과: EPS {eps_txt} ≥ {eps_req}, 매출 {rev_txt} ≥ {rev_req}"
    if status == "FAIL":
        fail_parts = []
        if eps is None:
            fail_parts.append(f"EPS 데이터 없음, 기준 {eps_req}")
        elif eps < MIN_EPS_GROWTH:
            fail_parts.append(f"EPS {eps_txt} < 기준 {eps_req}")
        if rev is None:
            fail_parts.append(f"매출 데이터 없음, 기준 {rev_req}")
        elif rev < MIN_REV_GROWTH:
            fail_parts.append(f"매출 {rev_txt} < 기준 {rev_req}")
        return "미통과: " + "; ".join(fail_parts)
    return f"미확인: {raw_reason}; EPS {eps_txt}, 매출 {rev_txt}"


def get_fundamental_info(ticker):
    """VCP까지 통과한 소수 종목에만 호출. 실패/미달 사유를 상세 반환."""
    for attempt in range(2):
        try:
            info = yf.Ticker(ticker).info
            eps = info.get("earningsGrowth")
            rev = info.get("revenueGrowth")
            sector = info.get("sector", "")
            industry = info.get("industry", "")
            if eps is None or rev is None:
                reason = build_fundamental_reason("UNKNOWN", eps, rev, "yfinance 실적 데이터 없음")
                return "UNKNOWN", {"eps_growth": eps, "rev_growth": rev, "sector": sector, "industry": industry, "reason": reason}
            if eps >= MIN_EPS_GROWTH and rev >= MIN_REV_GROWTH:
                reason = build_fundamental_reason("PASS", eps, rev, "")
                return "PASS", {"eps_growth": eps, "rev_growth": rev, "sector": sector, "industry": industry, "reason": reason}
            reason = build_fundamental_reason("FAIL", eps, rev, "성장률 기준 미달")
            return "FAIL", {"eps_growth": eps, "rev_growth": rev, "sector": sector, "industry": industry, "reason": reason}
        except Exception as e:
            msg = str(e)
            if "Too Many Requests" in msg or "Rate limited" in msg:
                print(f"⚠️ {ticker} 실적 조회 rate limit, {FUNDAMENTAL_RETRY_SLEEP}초 대기 후 재시도")
                time.sleep(FUNDAMENTAL_RETRY_SLEEP)
                continue
            reason = build_fundamental_reason("UNKNOWN", None, None, f"실적 조회 오류: {e}")
            return "UNKNOWN", {"eps_growth": None, "rev_growth": None, "sector": "", "industry": "", "reason": reason}
    reason = build_fundamental_reason("UNKNOWN", None, None, "rate limit으로 실적 미확인")
    return "UNKNOWN", {"eps_growth": None, "rev_growth": None, "sector": "", "industry": "", "reason": reason}


def main():
    print("📦 1. 미국 시장 종목 명단 수집 시작...")
    sp_list, nd_list, ru_list = get_sp500_tickers(), get_nasdaq100_tickers(), get_russell2000_tickers()
    tickers = sorted(set(sp_list + nd_list + ru_list))
    print(f"📊 수집 완료 -> S&P500: {len(sp_list)}개 | Nasdaq100: {len(nd_list)}개 | Russell2000: {len(ru_list)}개")
    print(f"🚀 총 스캔 대상: {len(tickers)}개")
    if not tickers:
        sys.exit(1)

    market_ok, market_status = passes_market_filter()
    print(f"🌎 시장 환경: {market_status}")

    print("📥 2. 주가 데이터 분할 다운로드 시작...")
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

    print("🧮 3. 가격 데이터 정리 및 RS Rating 계산...")
    price_data = {}
    for t in tickers:
        df = prepare_price_dataframe(get_ticker_dataframe(raw_data, t))
        if df is not None:
            price_data[t] = df
    rs_map = calculate_rs_scores(price_data)
    print(f"✅ 유효 가격 데이터: {len(price_data)}개 / RS 계산: {len(rs_map)}개")

    print("📈 4. 트렌드 템플릿 검사...")
    passed_trend = []
    for t, df in price_data.items():
        rs = rs_map.get(t, {"rs_rating": 0})
        if passes_trend_template(t, df, rs):
            passed_trend.append((t, df, rs))
    print(f"🎯 트렌드 템플릿 통과: {len(passed_trend)}개")

    print("📉 5. VCP 검사 먼저 수행 — 실적 조회 호출 수 최소화...")
    vcp_candidates = []
    for t, df, rs in passed_trend:
        ok_v, v = check_vcp_pattern(t, df)
        if ok_v:
            vcp_candidates.append((t, df, rs, v))
            print(f"✅ VCP 후보: {t} | {v['setup_type']} | Risk {v['risk']}% | RS {rs.get('rs_rating', 0)}")
    vcp_candidates = sorted(vcp_candidates, key=lambda x: (0 if x[3]["setup_type"] == "BREAKOUT" else 1, -x[2].get("rs_rating", 0), abs(x[3]["distance_from_pivot"])))
    print(f"🎯 VCP 후보: {len(vcp_candidates)}개")

    print("🧬 6. VCP 후보에 대해서만 펀더멘탈 검사...")
    final = []
    tech_only = []
    for idx, (t, df, rs, v) in enumerate(vcp_candidates[:MAX_FUNDAMENTAL_CALLS], start=1):
        print(f"   ↳ [{idx}/{min(len(vcp_candidates), MAX_FUNDAMENTAL_CALLS)}] {t} 실적 조회")
        status, f = get_fundamental_info(t)
        eps_pct = None if f.get("eps_growth") is None else round(f["eps_growth"] * 100, 1)
        rev_pct = None if f.get("rev_growth") is None else round(f["rev_growth"] * 100, 1)
        base = {
            "ticker": t,
            "setup_type": v["setup_type"],
            "fundamental_status": status,
            "fundamental_reason": f.get("reason", ""),
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
            "breakout_volume_ratio": v["breakout_volume_ratio"],
        }
        if status == "PASS":
            final.append(base)
        else:
            tech_only.append(base)
        print(f"      실적결과: {status} | {base['fundamental_reason']}")
        time.sleep(FUNDAMENTAL_SLEEP + random.uniform(0, 1))

    for t, df, rs, v in vcp_candidates[MAX_FUNDAMENTAL_CALLS:]:
        tech_only.append({
            "ticker": t,
            "setup_type": v["setup_type"],
            "fundamental_status": "NOT_CHECKED",
            "fundamental_reason": "실적 조회 호출 제한으로 미확인",
            "current_price": v["current_price"],
            "entry": v["entry"],
            "stop": v["stop"],
            "risk": v["risk"],
            "distance_from_pivot": v["distance_from_pivot"],
            "rs_rating": rs.get("rs_rating", 0),
            "r3_return_pct": round(rs.get("r3", 0) * 100, 1),
            "r6_return_pct": round(rs.get("r6", 0) * 100, 1),
            "r12_return_pct": round(rs.get("r12", 0) * 100, 1),
            "eps_growth_pct": None,
            "rev_growth_pct": None,
            "sector": "",
            "industry": "",
            "vcp_r1_pct": v["vcp_r1"],
            "vcp_r2_pct": v["vcp_r2"],
            "vcp_r3_pct": v["vcp_r3"],
            "vol_decline_pct": v["vol_decline"],
            "breakout_volume_ratio": v["breakout_volume_ratio"],
        })

    today = pd.Timestamp.now(tz="Asia/Seoul").strftime("%Y-%m-%d")
    cols = [
        "ticker", "setup_type", "fundamental_status", "fundamental_reason", "current_price", "entry", "stop", "risk", "distance_from_pivot",
        "rs_rating", "r3_return_pct", "r6_return_pct", "r12_return_pct", "eps_growth_pct", "rev_growth_pct", "sector", "industry",
        "vcp_r1_pct", "vcp_r2_pct", "vcp_r3_pct", "vol_decline_pct", "breakout_volume_ratio"
    ]
    strict_file = f"minervini_strict_{today}.csv"
    watch_file = f"minervini_watchlist_{today}.csv"
    pd.DataFrame(final, columns=cols).to_csv(strict_file, index=False)
    pd.DataFrame(tech_only, columns=cols).to_csv(watch_file, index=False)
    print(f"🔥 최종 실적 확인 통과: {len(final)}개")
    print(f"👀 기술+VCP 후보/실적 미확인 또는 미달: {len(tech_only)}개")
    print(f"💾 CSV 저장: {strict_file}, {watch_file}")

    def format_items(items, title, limit=10):
        if not items:
            return f"{title}: 없음"
        lines = [title]
        for item in items[:limit]:
            reason = item.get("fundamental_reason", "")
            if len(reason) > 90:
                reason = reason[:87] + "..."
            lines.append(
                f"• {item['ticker']} [{item['setup_type']}] 현재 {item['current_price']}$ | 진입 {item['entry']}$ | 손절 {item['stop']}$ | 리스크 {item['risk']}% | RS {item['rs_rating']}\n"
                f"  실적: {item['fundamental_status']} | 사유: {reason}"
            )
        if len(items) > limit:
            lines.append(f"외 {len(items)-limit}개는 CSV 확인")
        return "\n".join(lines)

    msg = (
        f"🔔 [{today}] 미너비니 정밀 스크리닝 결과 v5\n"
        f"------------------------------------\n"
        f"📊 S&P500: {len(sp_list)}개 | Nasdaq100: {len(nd_list)}개 | Russell2000: {len(ru_list)}개\n"
        f"🚀 총 스캔 대상: {len(tickers)}개\n"
        f"🌎 시장 환경: {market_status}\n"
        f"📈 유효 가격 데이터: {len(price_data)}개\n"
        f"✅ 트렌드 템플릿 통과: {len(passed_trend)}개\n"
        f"✅ VCP 후보: {len(vcp_candidates)}개\n"
        f"🔥 실적까지 확인 통과: {len(final)}개\n"
        f"👀 기술+VCP 후보(실적 미확인/미달 포함): {len(tech_only)}개\n\n"
        f"{format_items(final, '🔥 Strict 후보')}\n\n"
        f"{format_items(tech_only, '👀 Watch 후보')}\n"
        f"------------------------------------\n"
        f"※ 투자 추천이 아닌 자동 선별 결과입니다.\n"
        f"※ v5는 Watch 후보에 실적 미통과/미확인 사유를 표시합니다."
    )
    send_telegram_message(msg)
    print("🎯 전체 스크리닝 완료")


if __name__ == "__main__":
    main()
