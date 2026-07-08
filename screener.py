
import os
import sys
import time
import io
import csv
import re
import pandas as pd
import requests
import yfinance as yf

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

MIN_RS_RATING = 70
MIN_AVG_VOLUME = 150000
MIN_DOLLAR_VOLUME = 2000000
MIN_EPS_GROWTH = 0.20
MIN_REV_GROWTH = 0.15
MAX_RISK_PCT = 8.0
PIVOT_NEAR_PCT = 5.0
VCP_MAX_LAST_CONTRACTION = 0.08
MARKET_FILTER_ENABLED = True
PRICE_PERIOD = "18mo"
CHUNK_SIZE = 150
SLEEP_BETWEEN_CHUNKS = 2


def send_telegram_message(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("❌ 에러: 텔레그램 환경변수 TELEGRAM_TOKEN 또는 CHAT_ID가 없습니다.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        res = requests.post(url, json={"chat_id": CHAT_ID, "text": message}, timeout=15)
        if res.status_code != 200:
            print(f"⚠️ 텔레그램 전송 실패: {res.text}")
    except Exception as e:
        print(f"⚠️ 텔레그램 전송 에러: {e}")


def clean_ticker(ticker):
    if ticker is None:
        return ""
    ticker = str(ticker).strip().replace("\ufeff", "").replace('"', "")
    ticker = ticker.replace(".", "-")
    return ticker


def is_valid_us_ticker(ticker):
    ticker = clean_ticker(ticker)
    if not ticker:
        return False
    if ticker.upper() in ["CASH", "USD", "-", "N/A", "VALUE"]:
        return False
    if " " in ticker:
        return False
    if ticker.startswith("RTY") or ticker.startswith("RTYM"):
        return False
    if len(ticker) > 8:
        return False
    return bool(re.match(r"^[A-Z0-9][A-Z0-9\-]*$", ticker))


def get_html_with_header(url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
    res = requests.get(url, headers=headers, timeout=20)
    res.raise_for_status()
    return res.text


def get_sp500_tickers():
    try:
        html = get_html_with_header("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
        df = pd.read_html(io.StringIO(html))[0]
        tickers = df["Symbol"].astype(str).map(clean_ticker).tolist()
        tickers = sorted(set(t for t in tickers if t))
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
        tickers = dfs[0]["Ticker"].astype(str).map(clean_ticker).tolist()
        tickers = sorted(set(t for t in tickers if t))
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
    """CSV/TSV/공백형 텍스트에서 Ticker 또는 Symbol 컬럼을 최대한 안전하게 추출."""
    text = text.replace("\x00", "").replace("\ufeff", "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    # 1) iShares CSV처럼 Ticker/Name 헤더가 있는 경우
    start_idx = None
    delimiter = ","
    for i, line in enumerate(lines):
        norm = line.replace('"', '').lower()
        if ("ticker" in norm or "symbol" in norm) and ("name" in norm or "sector" in norm or "asset class" in norm):
            start_idx = i
            delimiter = "\t" if line.count("\t") > line.count(",") else ","
            break

    if start_idx is not None:
        data_text = "\n".join(lines[start_idx:])
        reader = csv.reader(io.StringIO(data_text), delimiter=delimiter)
        try:
            header = next(reader)
        except StopIteration:
            header = []
        normalized_header = [clean_ticker(h).lower().replace(" ", "_") for h in header]
        ticker_col = None
        for idx, h in enumerate(normalized_header):
            if h in ["ticker", "symbol", "constituents"] or "ticker" in h or "symbol" in h:
                ticker_col = idx
                break
        if ticker_col is None:
            ticker_col = 0
        tickers = []
        for row in reader:
            if not row or len(row) <= ticker_col:
                continue
            first_col = clean_ticker(row[0])
            if first_col.startswith("The") or "BlackRock" in first_col or first_col.startswith("©"):
                break
            ticker = clean_ticker(row[ticker_col])
            if is_valid_us_ticker(ticker):
                tickers.append(ticker)
        return sorted(set(tickers))

    # 2) GitHub CSV처럼 컬럼명이 단순한 경우 pandas로 재시도
    try:
        df = pd.read_csv(io.StringIO(text))
        candidates = []
        for col in df.columns:
            c = str(col).lower()
            if "ticker" in c or "symbol" in c or "constituents" in c:
                candidates.append(col)
        if not candidates and len(df.columns) > 0:
            candidates = [df.columns[0]]
        tickers = []
        for col in candidates[:1]:
            for x in df[col].dropna().astype(str).tolist():
                t = clean_ticker(x)
                if is_valid_us_ticker(t) and t != "^RUT":
                    tickers.append(t)
        if tickers:
            return sorted(set(tickers))
    except Exception:
        pass

    # 3) 최후: 각 줄 첫 토큰에서 티커처럼 생긴 것만 추출
    tickers = []
    for line in lines:
        token = clean_ticker(re.split(r"[,\t ]+", line)[0])
        if is_valid_us_ticker(token) and token.upper() not in ["TICKER", "SYMBOL", "NO"]:
            tickers.append(token)
    return sorted(set(tickers))


def get_russell2000_tickers():
    """
    1순위: iShares IWM 공식 보유종목 CSV
    2순위: 공개 GitHub Russell2000 CSV fallback
    iShares가 GitHub Actions에서 인코딩/헤더 구조 문제로 0개가 나오는 경우까지 대응.
    """
    sources = [
        ("iShares IWM", "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund"),
        ("iShares IWM alt1", "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM"),
        ("iShares IWM alt2", "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv"),
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
            if len(tickers) >= 1500:
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
        r3 = safe_return(close, 63)
        r6 = safe_return(close, 126)
        r12 = safe_return(close, 252)
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
        return True, "비활성화"
    try:
        data = yf.download(["^GSPC", "^IXIC"], period="1y", interval="1d", group_by="ticker", progress=False, threads=False, timeout=30, auto_adjust=True)
        oks = []
        for idx in ["^GSPC", "^IXIC"]:
            df = data[idx].copy() if isinstance(data.columns, pd.MultiIndex) and idx in data.columns.get_level_values(0) else data.copy()
            df = df.dropna(subset=["Close"]).copy()
            if len(df) < 220:
                return False, "시장지수 데이터 부족"
            df["MA50"] = df["Close"].rolling(50).mean()
            df["MA200"] = df["Close"].rolling(200).mean()
            oks.append(df["Close"].iloc[-1] > df["MA50"].iloc[-1] > df["MA200"].iloc[-1])
        return (True, "양호: S&P500/Nasdaq 모두 상승 추세") if all(oks) else (False, "주의: S&P500 또는 Nasdaq 상승 추세 미충족")
    except Exception as e:
        print(f"⚠️ 시장 환경 필터 확인 실패: {e}")
        return False, "시장 환경 확인 실패"


def passes_trend_template(ticker, df, rs_info):
    try:
        cp = df["Close"].iloc[-1]
        ma50 = df["MA50"].iloc[-1]
        ma150 = df["MA150"].iloc[-1]
        ma200 = df["MA200"].iloc[-1]
        ma200_22 = df["MA200"].iloc[-22]
        ma200_44 = df["MA200"].iloc[-44]
        low52 = df["Close"].tail(252).min()
        high52 = df["Close"].tail(252).max()
        avg_vol = df["Vol_MA50"].iloc[-1]
        avg_dvol = df["DollarVol_MA50"].iloc[-1]
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


def get_fundamental_info(ticker):
    try:
        info = yf.Ticker(ticker).info
        eps = info.get("earningsGrowth")
        rev = info.get("revenueGrowth")
        if eps is None or rev is None or eps < MIN_EPS_GROWTH or rev < MIN_REV_GROWTH:
            return False, {}
        return True, {"eps_growth": eps, "rev_growth": rev, "sector": info.get("sector", ""), "industry": info.get("industry", "")}
    except Exception as e:
        print(f"⚠️ {ticker} 실적 조회 오류: {e}")
        return False, {}


def check_vcp_pattern(ticker, df):
    try:
        recent = df.tail(65).copy()
        if len(recent) < 65:
            return False, {}
        seg1, seg2, seg3 = recent.iloc[0:25], recent.iloc[25:45], recent.iloc[45:65]
        def cr(seg):
            low = seg["Low"].min()
            high = seg["High"].max()
            return None if low <= 0 else (high - low) / low
        r1, r2, r3 = cr(seg1), cr(seg2), cr(seg3)
        if r1 is None or r2 is None or r3 is None or not (r1 > r2 > r3) or r3 > VCP_MAX_LAST_CONTRACTION:
            return False, {}
        vol_early = recent["Volume"].iloc[0:30].mean()
        vol_recent = recent["Volume"].iloc[-10:].mean()
        vol_ma50 = df["Vol_MA50"].iloc[-1]
        if pd.isna(vol_early) or pd.isna(vol_recent) or pd.isna(vol_ma50) or not (vol_recent < vol_early):
            return False, {}
        current = df["Close"].iloc[-1]
        current_vol = df["Volume"].iloc[-1]
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
            "current_price": round(current, 2), "entry": round(pivot, 2), "stop": round(stop, 2), "risk": round(risk, 1),
            "distance_from_pivot": round(dist, 1), "vcp_r1": round(r1 * 100, 1), "vcp_r2": round(r2 * 100, 1), "vcp_r3": round(r3 * 100, 1),
            "vol_decline": round((1 - vol_recent / vol_early) * 100, 1), "breakout_volume_ratio": round(current_vol / vol_ma50, 2)
        }
    except Exception as e:
        print(f"⚠️ {ticker} VCP 검사 오류: {e}")
        return False, {}


def main():
    print("📦 1. 미국 시장 종목 명단 수집 시작...")
    sp_list = get_sp500_tickers()
    nd_list = get_nasdaq100_tickers()
    ru_list = get_russell2000_tickers()
    tickers = sorted(set(sp_list + nd_list + ru_list))
    print(f"📊 수집 완료 -> S&P500: {len(sp_list)}개 | Nasdaq100: {len(nd_list)}개 | Russell2000/IWM: {len(ru_list)}개")
    print(f"🚀 총 스캔 대상, 중복 제거 기준: {len(tickers)}개 종목")
    if len(ru_list) == 0:
        print("⚠️ 경고: Russell2000 수집 실패. S&P500 + Nasdaq100만 스크리닝합니다.")
    if not tickers:
        sys.exit(1)

    print("🌎 2. 시장 환경 필터 확인...")
    market_ok, market_status = passes_market_filter()
    print(f"📌 시장 환경: {market_status}")

    print("📥 3. 주가 데이터 분할 다운로드 시작...")
    raw_data = pd.DataFrame()
    for i in range(0, len(tickers), CHUNK_SIZE):
        chunk = tickers[i:i + CHUNK_SIZE]
        print(f"   ↳ 다운로드 중... [{i + 1}~{min(i + CHUNK_SIZE, len(tickers))}/{len(tickers)}] ({len(chunk)}개 종목)")
        try:
            cd = yf.download(chunk, period=PRICE_PERIOD, interval="1d", group_by="ticker", progress=False, threads=False, timeout=30, auto_adjust=True)
            if cd is not None and not cd.empty:
                raw_data = cd if raw_data.empty else pd.concat([raw_data, cd], axis=1)
        except Exception as e:
            print(f"⚠️ 일부 청크 다운로드 실패: {e}")
        time.sleep(SLEEP_BETWEEN_CHUNKS)
    if raw_data.empty:
        print("❌ 에러: 주가 데이터를 전혀 다운로드하지 못했습니다.")
        sys.exit(1)

    print("🧮 4. 종목별 가격 데이터 정리 및 RS Rating 계산...")
    price_data = {}
    for t in tickers:
        df = prepare_price_dataframe(get_ticker_dataframe(raw_data, t))
        if df is not None:
            price_data[t] = df
    print(f"✅ 유효 가격 데이터 확보: {len(price_data)}개")
    rs_map = calculate_rs_scores(price_data)
    print(f"✅ RS Rating 계산 완료: {len(rs_map)}개")

    print("📈 5. 미너비니 트렌드 템플릿 필터 가동...")
    passed = []
    for t, df in price_data.items():
        rs = rs_map.get(t, {"rs_rating": 0})
        if passes_trend_template(t, df, rs):
            passed.append((t, df, rs))
            print(f"✅ 트렌드 템플릿 통과: {t} | RS {rs.get('rs_rating', 0)}")
    print(f"🎯 트렌드 템플릿 통과: {len(passed)}개 종목")

    print("🧬 6. 펀더멘탈 필터 및 VCP 검사 시작...")
    final = []
    for idx, (t, df, rs) in enumerate(passed, start=1):
        print(f"   ↳ [{idx}/{len(passed)}] {t} 실적/VCP 검사 중...")
        ok_f, f = get_fundamental_info(t)
        if not ok_f:
            time.sleep(0.4)
            continue
        ok_v, v = check_vcp_pattern(t, df)
        if not ok_v:
            time.sleep(0.4)
            continue
        item = {
            "ticker": t, "setup_type": v["setup_type"], "current_price": v["current_price"], "entry": v["entry"], "stop": v["stop"], "risk": v["risk"],
            "distance_from_pivot": v["distance_from_pivot"], "rs_rating": rs.get("rs_rating", 0),
            "r3_return_pct": round(rs.get("r3", 0) * 100, 1), "r6_return_pct": round(rs.get("r6", 0) * 100, 1), "r12_return_pct": round(rs.get("r12", 0) * 100, 1),
            "eps_growth_pct": round(f["eps_growth"] * 100, 1), "rev_growth_pct": round(f["rev_growth"] * 100, 1), "sector": f.get("sector", ""), "industry": f.get("industry", ""),
            "vcp_r1_pct": v["vcp_r1"], "vcp_r2_pct": v["vcp_r2"], "vcp_r3_pct": v["vcp_r3"], "vol_decline_pct": v["vol_decline"], "breakout_volume_ratio": v["breakout_volume_ratio"]
        }
        final.append(item)
        print(f"🔥 최종 통과: {t} | {item['setup_type']} | RS {item['rs_rating']} | Risk {item['risk']}%")
        time.sleep(0.4)

    final = sorted(final, key=lambda x: (0 if x["setup_type"] == "BREAKOUT" else 1, -x["rs_rating"], abs(x["distance_from_pivot"])))
    today = pd.Timestamp.now(tz="Asia/Seoul").strftime("%Y-%m-%d")
    cols = ["ticker", "setup_type", "current_price", "entry", "stop", "risk", "distance_from_pivot", "rs_rating", "r3_return_pct", "r6_return_pct", "r12_return_pct", "eps_growth_pct", "rev_growth_pct", "sector", "industry", "vcp_r1_pct", "vcp_r2_pct", "vcp_r3_pct", "vol_decline_pct", "breakout_volume_ratio"]
    pd.DataFrame(final, columns=cols).to_csv(f"minervini_result_{today}.csv", index=False)
    print(f"🔥 최종 미너비니 후보: {len(final)}개")

    if final:
        lines = []
        for item in final[:15]:
            lines.append(f"• {item['ticker']} [{item['setup_type']}]\n  현재가 {item['current_price']}$ | 진입가 {item['entry']}$ | 손절가 {item['stop']}$ | 리스크 -{item['risk']}%\n  RS {item['rs_rating']} | EPS {item['eps_growth_pct']}% | 매출 {item['rev_growth_pct']}% | 피벗거리 {item['distance_from_pivot']}%")
        target_text = "\n".join(lines)
        if len(final) > 15:
            target_text += f"\n\n외 {len(final)-15}개 후보는 CSV에서 확인하세요."
    else:
        target_text = "조건을 모두 만족하는 미너비니 후보가 없습니다."

    msg = (
        f"🔔 [{today}] 미너비니 정밀 스크리닝 결과\n"
        f"------------------------------------\n"
        f"📊 S&P500: {len(sp_list)}개\n"
        f"📊 Nasdaq100: {len(nd_list)}개\n"
        f"📊 Russell2000/IWM: {len(ru_list)}개, 수집상태: {'성공' if ru_list else '실패'}\n"
        f"🚀 총 스캔 대상: {len(tickers)}개\n"
        f"🌎 시장 환경: {market_status}\n"
        f"📈 유효 가격 데이터: {len(price_data)}개\n"
        f"✅ 트렌드 템플릿 통과: {len(passed)}개\n"
        f"🔥 최종 실적+VCP 통과: {len(final)}개\n\n"
        f"{target_text}\n"
        f"------------------------------------\n"
        f"※ 투자 추천이 아닌 자동 선별 결과입니다.\n"
        f"※ 실제 매수 전 차트, 거래량, 뉴스, 실적 발표일을 반드시 확인하세요."
    )
    send_telegram_message(msg)
    print("🎯 전체 스크리닝 및 텔레그램 발송 프로세스 완료")


if __name__ == "__main__":
    main()
