import os
import sys
import time
import io
import csv
import math
import pandas as pd
import requests
import yfinance as yf


# ============================================================
# 설정값
# ============================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# 미너비니 조건 설정
MIN_RS_RATING = 70             # RS Rating 유사 점수 최소값
MIN_AVG_VOLUME = 150000        # 50일 평균 거래량
MIN_DOLLAR_VOLUME = 2000000    # 50일 평균 거래대금
MIN_EPS_GROWTH = 0.20          # EPS 성장률 20%
MIN_REV_GROWTH = 0.15          # 매출 성장률 15%
MAX_RISK_PCT = 8.0             # 진입가 대비 손절 리스크 최대 8%
PIVOT_NEAR_PCT = 5.0           # 피벗가 대비 5% 이내 접근
VCP_MAX_LAST_CONTRACTION = 0.08 # 마지막 수축폭 8% 이하
MARKET_FILTER_ENABLED = True   # 시장 환경 필터 사용 여부

# 다운로드 설정
PRICE_PERIOD = "18mo"
CHUNK_SIZE = 150
SLEEP_BETWEEN_CHUNKS = 2


# ============================================================
# 텔레그램
# ============================================================

def send_telegram_message(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("❌ 에러: 텔레그램 환경변수 TELEGRAM_TOKEN 또는 CHAT_ID가 없습니다.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    payload = {
        "chat_id": CHAT_ID,
        "text": message
    }

    try:
        response = requests.post(url, json=payload, timeout=15)
        if response.status_code != 200:
            print(f"⚠️ 텔레그램 전송 실패: {response.text}")
    except Exception as e:
        print(f"⚠️ 텔레그램 전송 에러: {e}")


# ============================================================
# 종목 리스트 수집
# ============================================================

def get_html_with_header(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    return response.text


def clean_ticker(ticker):
    if ticker is None:
        return ""

    ticker = str(ticker).strip()
    ticker = ticker.replace(".", "-")

    return ticker


def get_sp500_tickers():
    try:
        html = get_html_with_header(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        )

        tickers = (
            pd.read_html(html)[0]["Symbol"]
            .astype(str)
            .map(clean_ticker)
            .tolist()
        )

        tickers = sorted(set([t for t in tickers if t]))
        print(f"✅ S&P500 수집 성공: {len(tickers)}개")
        return tick tickers

    except Exception as e:
        print(f"❌ S&P500 수집 실패: {e}")
        return []


def get_nasdaq100_tickers():
    try:
        html = get_html_with_header("https://en.wikipedia.org/wiki/Nasdaq-100")

        df_list = pd.read_html(html, attrs={"id": "constituents"})

        if not df_list:
            print("❌ Nasdaq100 테이블을 찾지 못했습니다.")
            return []

        tickers = (
            df_list[0]["Ticker"]
            .astype(str)
            .map(clean_ticker)
            .tolist()
        )

        tickers = sorted(set([t for t in tickers if t]))
        print(f"✅ Nasdaq100 수집 성공: {len(tickers)}개")
        return tickers

    except Exception as e:
        print(f"❌ Nasdaq100 수집 실패: {e}")
        return []


def get_russell2000_tickers():
    """
    Russell2000 직접 구성종목 대신
    iShares Russell 2000 ETF(IWM)의 보유종목을 사용합니다.
    """

    urls = [
        "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM",
        "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv"
    ]

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36"
        ),
        "Accept": "text/csv,application/csv,text/plain,*/*",
        "Referer": "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf"
    }

    for url in urls:
        try:
            print("📥 Russell2000/IWM 보유종목 수집 시도 중...")

            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()

            text = response.text
            lines = [line for line in text.splitlines() if line.strip()]

            start_idx = None

            for i, line in enumerate(lines):
                if "Ticker" in line and "Name" in line:
                    start_idx = i
                    break

            if start_idx is None:
                print("⚠️ IWM CSV에서 Ticker 헤더를 찾지 못했습니다.")
                continue

            data_text = "\n".join(lines[start_idx:])
            reader = csv.DictReader(io.StringIO(data_text))

            tickers = []

            for row in reader:
                ticker = row.get("Ticker", "").strip()

                if not ticker:
                    continue

                if ticker in ["-", "Cash", "CASH", "USD"]:
                    continue

                if " " in ticker:
                    continue

                if ticker.startswith("RTY") or ticker.startswith("RTYM"):
                    continue

                if ticker.startswith("The") or "BlackRock" in ticker:
                    break

                ticker = clean_ticker(ticker)

                if 1 <= len(ticker) <= 8:
                    tickers.append(ticker)

            tickers = sorted(set(tickers))

            if len(tickers) >= 1500:
                print(f"✅ Russell2000/IWM 티커 수집 성공: {len(tickers)}개")
                return tickers
            else:
                print(f"⚠️ Russell2000/IWM 티커 수가 비정상적으로 적습니다: {len(tickers)}개")

        except Exception as e:
            print(f"⚠️ IWM Russell2000 수집 실패: {e}")

    print("❌ Russell2000 티커 수집 최종 실패")
    return []


# ============================================================
# 데이터 처리 함수
# ============================================================

def get_ticker_dataframe(raw_data, ticker):
    try:
        if raw_data is None or raw_data.empty:
            return None

        if isinstance(raw_data.columns, pd.MultiIndex):
            if ticker in raw_data.columns.get_level_values(0):
                df = raw_data[ticker].copy()
                return df
            return None

        if "Close" in raw_data.columns:
            return raw_data.copy()

        return None

    except Exception:
        return None


def prepare_price_dataframe(df):
    if df is None or df.empty:
        return None

    required_cols = ["Open", "High", "Low", "Close", "Volume"]
    for col in required_cols:
        if col not in df.columns:
            return None

    df = df.dropna(subset=["Close"]).copy()

    if len(df) < 260:
        return None

    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_localize(None)

    df["MA50"] = df["Close"].rolling(window=50).mean()
    df["MA150"] = df["Close"].rolling(window=150).mean()
    df["MA200"] = df["Close"].rolling(window=200).mean()
    df["Vol_MA50"] = df["Volume"].rolling(window=50).mean()
    df["DollarVol_MA50"] = df["Close"].rolling(window=50).mean() * df["Vol_MA50"]

    return df


def safe_return(close_series, days):
    try:
        if len(close_series) <= days:
            return None

        start_price = close_series.iloc[-days]
        end_price = close_series.iloc[-1]

        if start_price is None or start_price <= 0:
            return None

        return (end_price / start_price) - 1

    except Exception:
        return None


def calculate_rs_scores(price_data_by_ticker):
    """
    공식 IBD RS Rating은 아니지만,
    전체 스캔 종목 내 3개월, 6개월, 12개월 수익률을 가중합산해
    0~99점의 유사 RS Rating을 계산합니다.

    가중치:
    - 3개월 수익률: 40%
    - 6개월 수익률: 30%
    - 12개월 수익률: 30%
    """

    rows = []

    for ticker, df in price_data_by_ticker.items():
        try:
            close = df["Close"].dropna()

            r3 = safe_return(close, 63)
            r6 = safe_return(close, 126)
            r12 = safe_return(close, 252)

            if r3 is None or r6 is None or r12 is None:
                continue

            weighted_return = (r3 * 0.4) + (r6 * 0.3) + (r12 * 0.3)

            rows.append(
                {
                    "ticker": ticker,
                    "r3": r3,
                    "r6": r6,
                    "r12": r12,
                    "weighted_return": weighted_return
                }
            )

        except Exception:
            continue

    if not rows:
        return {}

    rs_df = pd.DataFrame(rows)
    rs_df["rs_rating"] = (
        rs_df["weighted_return"].rank(pct=True) * 99
    ).round(0).astype(int)

    rs_map = {}
    for _, row in rs_df.iterrows():
        rs_map[row["ticker"]] = {
            "rs_rating": int(row["rs_rating"]),
            "r3": float(row["r3"]),
            "r6": float(row["r6"]),
            "r12": float(row["r12"]),
            "weighted_return": float(row["weighted_return"])
        }

    return rs_map


# ============================================================
# 미너비니 조건
# ============================================================

def passes_market_filter():
    """
    시장 환경 필터.
    S&P500과 Nasdaq이 모두 상승 추세일 때만 적극적으로 후보를 냅니다.
    """

    if not MARKET_FILTER_ENABLED:
        return True, "비활성화"

    try:
        index_data = yf.download(
            ["^GSPC", "^IXIC"],
            period="1y",
            interval="1d",
            group_by="ticker",
            progress=False,
            threads=False,
            timeout=30,
            auto_adjust=True
        )

        status_list = []

        for index_ticker in ["^GSPC", "^IXIC"]:
            if isinstance(index_data.columns, pd.MultiIndex):
                df = index_data[index_ticker].copy()
            else:
                df = index_data.copy()

            df = df.dropna(subset=["Close"]).copy()

            if len(df) < 220:
                return False, "시장지수 데이터 부족"

            df["MA50"] = df["Close"].rolling(50).mean()
            df["MA200"] = df["Close"].rolling(200).mean()

            current = df["Close"].iloc[-1]
            ma50 = df["MA50"].iloc[-1]
            ma200 = df["MA200"].iloc[-1]

            ok = current > ma50 and current > ma200 and ma50 > ma200

            status_list.append((index_ticker, ok, current, ma50, ma200))

        market_ok = all([x[1] for x in status_list])

        if market_ok:
            return True, "양호: S&P500/Nasdaq 모두 상승 추세"
        else:
            return False, "주의: S&P500 또는 Nasdaq 상승 추세 미충족"

    except Exception as e:
        print(f"⚠️ 시장 환경 필터 확인 실패: {e}")
        return False, "시장 환경 확인 실패"


def passes_trend_template(ticker, df, rs_info):
    """
    미너비니 트렌드 템플릿 강화 버전.
    """

    try:
        current_price = df["Close"].iloc[-1]
        ma50 = df["MA50"].iloc[-1]
        ma150 = df["MA150"].iloc[-1]
        ma200 = df["MA200"].iloc[-1]

        ma200_22days_ago = df["MA200"].iloc[-22]
        ma200_44days_ago = df["MA200"].iloc[-44]

        low_52week = df["Close"].tail(252).min()
        high_52week = df["Close"].tail(252).max()

        avg_vol50 = df["Vol_MA50"].iloc[-1]
        avg_dollar_vol = df["DollarVol_MA50"].iloc[-1]

        rs_rating = rs_info.get("rs_rating", 0)

        values = [
            current_price, ma50, ma150, ma200,
            ma200_22days_ago, ma200_44days_ago,
            low_52week, high_52week, avg_vol, avg_dollar_vol
        ]

        if any(pd.isna(x) for x in values):
            return False, {}

        # 1. 현재가 > 150일선, 200일선
        if not (current_price > ma150 and current_price > ma200):
            return False, {}

        # 2. 150일선 > 200일선
        if not (ma150 > ma200):
            return False, {}

        # 3. 200일선 상승: 1개월 전 및 2개월 전보다 높아야 함
        if not (ma200 > ma200_22days_ago and ma200 > ma200_44days_ago):
            return False, {}

        # 4. 50일선 > 150일선, 200일선
        if not (ma50 > ma150 and ma50 > ma200):
            return False, {}

        # 5. 현재가 > 50일선
        if not (current_price > ma50):
            return False, {}

        # 6. 현재가가 52주 저점 대비 30% 이상
        if not (current_price >= low_52week * 1.30):
            return False, {}

        # 7. 현재가가 52주 고점 대비 25% 이내
        if not (current_price >= high_52week * 0.75):
            return False, {}

        # 8. RS Rating 유사 점수
        if rs_rating < MIN_RS_RATING:
            return False, {}

        # 9. 유동성 필터
        if avg_vol < MIN_AVG_VOLUME:
            return False, {}

        if avg_dollar_vol < MIN_DOLLAR_VOLUME:
            return False, {}

        details = {
            "price": round(current_price, 2),
            "ma50": round(ma50, 2),
            "ma150": round(ma150, 2),
            "ma200": round(ma200, 2),
            "low_52w": round(low_52week, 2),
            "high_52w": round(high_52week, 2),
            "rs_rating": rs_rating,
            "avg_vol50": int(avg_vol),
            "avg_dollar_vol50": int(avg_dollar_vol)
        }

        return True, details

    except Exception as e:
        print(f"⚠️ {ticker} 트렌드 템플릿 오류: {e}")
        return False, {}


def get_fundamental_info(ticker):
    """
    yfinance info 기반 기본 실적 필터.
    데이터가 없으면 보수적으로 탈락 처리.
    """

    try:
        info = yf.Ticker(ticker).info

        eps_growth = info.get("earningsGrowth", None)
        rev_growth = info.get("revenueGrowth", None)
        sector = info.get("sector", "")
        industry = info.get("industry", "")

        if eps_growth is None or rev_growth is None:
            return False, {
                "eps_growth": None,
                "rev_growth": None,
                "sector": sector,
                "industry": industry,
                "reason": "실적 데이터 없음"
            }

        if eps_growth < MIN_EPS_GROWTH:
            return False, {
                "eps_growth": eps_growth,
                "rev_growth": rev_growth,
                "sector": sector,
                "industry": industry,
                "reason": "EPS 성장률 미달"
            }

        if rev_growth < MIN_REV_GROWTH:
            return False, {
                "eps_growth": eps_growth,
                "rev_growth": rev_growth,
                "sector": sector,
                "industry": industry,
                "reason": "매출 성장률 미달"
            }

        return True, {
            "eps_growth": eps_growth,
            "rev_growth": rev_growth,
            "sector": sector,
            "industry": industry,
            "reason": "통과"
        }

    except Exception as e:
        return False, {
            "eps_growth": None,
            "rev_growth": None,
            "sector": "",
            "industry": "",
            "reason": f"실적 조회 오류: {e}"
        }


def check_vcp_pattern(ticker, df):
    """
    VCP 후보 탐지.
    실제 차트 패턴을 100% 자동 판별할 수는 없지만,
    아래 조건을 조합해 미너비니식 VCP에 가깝게 선별합니다.

    조건:
    1. 최근 65일 변동성 수축: r1 > r2 > r3
    2. 마지막 수축폭 8% 이하
    3. 최근 거래량이 이전 거래량보다 감소
    4. 현재가가 피벗 가격 5% 이내
    5. 진입가 대비 손절 리스크 8% 이하
    6. 오늘 이미 피벗 돌파했고 거래량이 50일 평균의 1.5배 이상이면 BREAKOUT 표시
    """

    try:
        if len(df) < 260:
            return False, {}

        recent = df.tail(65).copy()

        if len(recent) < 65:
            return False, {}

        # 세 구간으로 나누어 변동성 축소 확인
        seg1 = recent.iloc[0:25]
        seg2 = recent.iloc[25:45]
        seg3 = recent.iloc[45:65]

        def contraction_range(seg):
            low = seg["Low"].min()
            high = seg["High"].max()
            if low <= 0:
                return None
            return (high - low) / low

        r1 = contraction_range(seg1)
        r2 = contraction_range(seg2)
        r3 = contraction_range(seg3)

        if r1 is None or r2 is None or r3 is None:
            return False, {}

        if not (r1 > r2 > r3):
            return False, {}

        if r3 > VCP_MAX_LAST_CONTRACTION:
            return False, {}

        # 거래량 감소 확인
        vol_early = recent["Volume"].iloc[0:30].mean()
        vol_recent = recent["Volume"].iloc[-10:].mean()
        vol_ma50 = df["Vol_MA50"].iloc[-1]

        if pd.isna(vol_early) or pd.isna(vol_recent) or pd.isna(vol_ma50):
            return False, {}

        if not (vol_recent < vol_early):
            return False, {}

        current_price = df["Close"].iloc[-1]
        current_volume = df["Volume"].iloc[-1]

        # 피벗: 최근 20일 고점
        pivot = recent["High"].tail(20).max()

        # 손절: 최근 20일 저점 또는 50일선 중 낮지 않은 쪽을 참고
        recent_low = recent["Low"].tail(20).min()
        ma50 = df["MA50"].iloc[-1]

        stop = max(recent_low, ma50 * 0.97)
        risk_pct = ((pivot - stop) / pivot) * 100

        if risk_pct <= 0:
            return False, {}

        if risk_pct > MAX_RISK_PCT:
            return False, {}

        # 피벗 근접 여부
        distance_from_pivot = ((pivot - current_price) / pivot) * 100

        near_pivot = (
            current_price <= pivot * 1.03
            and current_price >= pivot * (1 - PIVOT_NEAR_PCT / 100)
        )

        # 당일 돌파 여부
        breakout_today = (
            current_price > pivot
            and current_volume >= vol_ma50 * 1.5
        )

        if not near_pivot and not breakout_today:
            return False, {}

        setup_type = "BREAKOUT" if breakout_today else "WATCH"

        details = {
            "setup_type": setup_type,
            "entry": round(pivot, 2),
            "stop": round(stop, 2),
            "risk": round(risk_pct, 1),
            "current_price": round(current_price, 2),
            "distance_from_pivot": round(distance_from_pivot, 1),
            "vcp_r1": round(r1 * 100, 1),
            "vcp_r2": round(r2 * 100, 1),
            "vcp_r3": round(r(r3 * 100, 1),
            "vol_decline": round((1 - vol_recent / vol_early) * 100, 1),
            "breakout_volume_ratio": round(current_volume / vol_ma50, 2)
        }

        return True, details

    except Exception as e:
        print(f"⚠️ {ticker} VCP 검사 오류: {e}")
        return False, {}


# ============================================================
# 메인 실행
# ============================================================

if __name__ == "__main__":
    print("📦 1. 미국 시장 종목 명단 수집 시작...")

    sp_list = get_sp500_tickers()
    nd_list = get_nasdaq100_tickers()
    ru_list = get_russell2000_tickers()

    tickers = sorted(set(sp_list + nd_list + ru_list))

    print(
        f"📊 수집 완료 -> "
        f"S&P500: {len(sp_list)}개 | "
        f"Nasdaq100: {len(nd_list)}개 | "
        f"Russell2000/IWM: {len(ru_list)}개"
    )

    print(f"🚀 총 스캔 대상, 중복 제거 기준: {len(tickers)}개 종목")

    if len(ru_list) == 0:
        print("⚠️ 경고: Russell2000 수집 실패. S&P500 + Nasdaq100만 스크리닝합니다.")

    if not tickers:
        print("❌ 에러: 종목 명단을 전혀 수집하지 못해 프로그램을 종료합니다.")
        sys.exit(1)

    print("🌎 2. 시장 환경 필터 확인...")

    market_ok, market_status = passes_market_filter()
    print(f"📌 시장 환경: {market_status}")

    if MARKET_FILTER_ENABLED and not market_ok:
        print("⚠️ 시장 환경이 좋지 않습니다. 후보는 계산하지만 메시지에 경고를 표시합니다.")

    print("📥 3. 주가 데이터 분할 다운로드 시작...")

    raw_data = pd.DataFrame()

    for i in range(0, len(tickers), CHUNK_SIZE):
        chunk_tickers = tickers[i:i + CHUNK_SIZE]

        print(
            f"   ↳ 다운로드 중... "
            f"[{i + 1}~{min(i + CHUNK_SIZE, len(tickers))}/{len(tickers)}] "
            f"({len(chunk_tickers)}개 종목)"
        )

        try:
            chunk_data = yf.download(
                chunk_tickers,
                period=PRICE_PERIOD,
                interval="1d",
                group_by="ticker",
                progress=False,
                threads=False,
                timeout=30,
                auto_adjust=True
            )

            if chunk_data is not None and not chunk_data.empty:
                if raw_data.empty:
                    raw_data = chunk_data
                else:
                    raw_data = pd.concat([raw_data, chunk_data], axis=1)

        except Exception as e:
            print(f"⚠️ 일부 청크 다운로드 실패: {e}")

        time.sleep(SLEEP_BETWEEN_CHUNKS)

    if raw_data.empty:
        print("❌ 에러: 주가 데이터를 전혀 다운로드하지 못했습니다.")
        sys.exit(1)

    print("🧮 4. 종목별 가격 데이터 정리 및 RS Rating 계산...")

    price_data_by_ticker = {}

    for ticker in tickers:
        df = get_ticker_dataframe(raw_data, ticker)
        df = prepare_price_dataframe(df)

        if df is not None:
            price_data_by_ticker[ticker] = df

    print(f"✅ 유효 가격 데이터 확보: {len(price_data_by_ticker)}개")

    if not price_data_by_ticker:
        print("❌ 유효한 가격 데이터가 없습니다.")
        sys.exit(1)

    rs_map = calculate_rs_scores(price_data_by_ticker)

    print(f"✅ RS Rating 계산 완료: {len(rs_map)}개")

    print("📈 5. 미너비니 트렌드 템플릿 필터 가동...")

    passed_trend = []

    for ticker, df in price_data_by_ticker.items():
        rs_info = rs_map.get(ticker, {"rs_rating": 0})

        passed, trend_details = passes_trend_template(ticker, df, rs_info)

        if passed:
            passed_trend.append((ticker, df, trend_details, rs_info))
            print(f"✅ 트렌드 템플릿 통과: {ticker} | RS {rs_info.get('rs_rating', 0)}")

    print(f"🎯 트렌드 템플릿 통과: {len(passed_trend)}개 종목")

    print("🧬 6. 펀더멘탈 필터 및 VCP 검사 시작...")

    final_targets = []

    for idx, item in enumerate(passed_trend, start=1):
        ticker, df, trend_details, rs_info = item

        try:
            print(f"   ↳ [{idx}/{len(passed_trend)}] {ticker} 실적/VCP 검사 중...")

            fundamental_ok, fundamental = get_fundamental_info(ticker)

            if not fundamental_ok:
                time.sleep(0.4)
                continue

            vcp_ok, vcp_details = check_vcp_pattern(ticker, df)

            if not vcp_ok:
                time.sleep(0.4)
                continue

            target = {
                "ticker": ticker,
                "setup_type": vcp_details["setup_type"],
                "current_price": vcp_details["current_price"],
                "entry": vcp_details["entry"],
                "stop": vcp_details["stop"],
                "risk": vcp_details["risk"],
                "distance_from_pivot": vcp_details["distance_from_pivot"],
                "rs_rating": rs_info.get("rs_rating", 0),
                "r3_return_pct": round(rs_info.get("r3", 0) * 100, 1),
                "r6_return_pct": round(rs_info.get("r6", 0) * 100, 1),
                "r12_return_pct": round(rs_info.get("r12", 0) * 100, 1),
                "eps_growth_pct": round(fundamental["eps_growth"] * 100, 1),
                "rev_growth_pct": round(fundamental["rev_growth"] * 100, 1),
                "sector": fundamental.get("sector", ""),
                "industry": fundamental.get("industry", ""),
                "vcp_r1_pct": vcp_details["vcp_r1"],
                "vcp_r2_pct": vcp_details["vcp_r2"],
                "vcp_r3_pct": vcp_details["vcp_r3"],
                "vol_decline_pct": vcp_details["vol_decline"],
                "breakout_volume_ratio": vcp_details["breakout_volume_ratio"]
            }

            final_targets.append(target)

            print(
                f"🔥 최종 통과: {ticker} | "
                f"{target['setup_type']} | "
                f"RS {target['rs_rating']} | "
                f"Risk {target['risk']}%"
            )

            time.sleep(0.4)

        except Exception as e:
            print(f"⚠️ {ticker} 최종 검사 중 오류: {e}")
            time.sleep(0.4)

    # 우선순위 정렬
    # 1. BREAKOUT 우선
    # 2. RS Rating 높은 순
    # 3. 피벗과 가까운 순
    final_targets = sorted(
        final_targets,
        key=lambda x: (
            0 if x["setup_type"] == "BREAKOUT" else 1,
            -x["rs_rating"],
            abs(x["distance_from_pivot"])
        )
    )

    print(f"🔥 최종 미너비니 후보: {len(final_targets)}개")

    today_str = pd.Timestamp.now(tz="Asia/Seoul").strftime("%Y-%m-%d")
    result_file = f"minervini_result_{today_str}.csv"

    result_columns = [
        "ticker",
        "setup_type",
        "current_price",
        "entry",
        "stop",
        "risk",
        "distance_from_pivot",
        "rs_rating",
        "r3_return_pct",
        "r6_return_pct",
        "r12_return_pct",
        "eps_growth_pct",
        "rev_growth_pct",
        "sector",
        "industry",
        "vcp_r1_pct",
        "vcp_r2_pct",
        "vcp_r3_pct",
        "vol_decline_pct",
        "breakout_volume_ratio"
    ]

    if final_targets:
        pd.DataFrame(final_targets)[result_columns].to_csv(result_file, index=False)
        print(f"💾 결과 CSV 저장 완료: {result_file}")
    else:
        pd.DataFrame(columns=result_columns).to_csv(result_file, index=False)
        print(f"💾 빈 결과 CSV 저장 완료: {result_file}")

    russell_status = "성공" if len(ru_list) > 0 else "실패"

    if final_targets:
        display_targets = final_targets[:15]

        target_text = "\n".join(
            [
                (
                    f"• {item['ticker']} [{item['setup_type']}]\n"
                    f"  현재가 {item['current_price']}$ | 진입가 {item['entry']}$ | "
                    f"손절가 {item['stop']}$ | 리스크 -{item['risk']}%\n"
                    f"  RS {item['rs_rating']} | EPS {item['eps_growth_pct']}% | "
                    f"매출 {item['rev_growth_pct']}% | 피벗거리 {item['distance_from_pivot']}%"
                )
                for item in display_targets
            ]
        )

        if len(final_targets) > 15:
            target_text += f"\n\n외 {len(final_targets) - 15}개 후보는 CSV에서 확인하세요."

    else:
        target_text = "조건을 모두 만족하는 미너비니 후보가 없습니다."

    msg = (
        f"🔔 [{today_str}] 미너비니 정밀 스크리닝 결과\n"
        f"------------------------------------\n"
        f"📊 S&P500: {len(sp_list)}개\n"
        f"📊 Nasdaq100: {len(nd_list)}개\n"
        f"📊 Russell2000/IWM: {len(ru_list)}개, 수집상태: {russell_status}\n"
        f"🚀 총 스캔 대상: {len(tickers)}개\n"
        f"🌎 시장 환경: {market_status}\n"
        f"📈 유효 가격 데이터: {len(price_data_by_ticker)}개\n"
        f"✅ 트렌드 템플릿 통과: {len(passed_trend)}개\n"
        f"🔥 최종 실적+VCP 통과: {len(final_targets)}개\n\n"
        f"{target_text}\n"
        f"------------------------------------\n"
        f"※ 투자 추천이 아닌 자동 선별 결과입니다.\n"
        f"※ 실제 매수 전 차트, 거래량, 뉴스, 실적 발표일을 반드시 확인하세요."
    )

    send_telegram_message(msg)

    print("🎯 전체 스크리닝 및 텔레그램 발송 프로세스 완료")
