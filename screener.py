import os
import sys
import time
import io
import csv
import pandas as pd
import requests
import yfinance as yf


TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")


def send_telegram_message(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("❌ 에러: 텔레그램 환경변수 설정을 확인하세요.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    payload = {
        "chat_id": CHAT_ID,
        "text": message
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"⚠️ 텔레그램 전송 실패: {response.text}")
    except Exception as e:
        print(f"텔레그램 전송 에러: {e}")


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


def get_sp500_tickers():
    try:
        html = get_html_with_header(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        )

        tickers = (
            pd.read_html(html)[0]["Symbol"]
            .astype(str)
            .str.replace(".", "-", regex=False)
            .str.strip()
            .tolist()
        )

        tickers = sorted(set([t for t in tickers if t]))
        print(f"✅ S&P500 수집 성공: {len(tickers)}개")
        return tickers

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
            .str.replace(".", "-", regex=False)
            .str.strip()
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

    IWM은 Russell 2000 Index를 추종하는 대표 ETF이며,
    약 2,000개 전후의 보유종목을 제공합니다.

    yfinance 호환을 위해 MOG.A 같은 티커는 MOG-A 형식으로 변환합니다.
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

                ticker = ticker.replace(".", "-").strip()

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


def get_ticker_dataframe(raw_data, ticker):
    try:
        if isinstance(raw_data.columns, pd.MultiIndex):
            if ticker in raw_data.columns.get_level_values(0):
                return raw_data[ticker].copy()
            return None

        if "Close" in raw_data.columns:
            return raw_data.copy()

        return None

    except Exception:
        return None


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

    print("📥 2. 주가 데이터 분할 다운로드 시작...")

    chunk_size = 150
    raw_data = pd.DataFrame()

    for i in range(0, len(tickers), chunk_size):
        chunk_tickers = tickers[i:i + chunk_size]

        print(
            f"   ↳ 다운로드 중... "
            f"[{i + 1}~{min(i + chunk_size, len(tickers))}/{len(tickers)}] "
            f"({len(chunk_tickers)}개 종목)"
        )

        try:
            chunk_data = yf.download(
                chunk_tickers,
                period="1y",
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

        time.sleep(2)

    if raw_data.empty:
        print("❌ 에러: 주가 데이터를 전혀 다운로드하지 못했습니다.")
        sys.exit(1)

    print("📈 3. 1차 기술적 필터, 미너비니 트렌드 템플릿 가동...")

    try:
        spy_df = yf.Ticker("^GSPC").history(period="1y", auto_adjust=True)
        spy_close = spy_df["Close"].copy()
        spy_close.index = spy_close.index.tz_localize(None)
    except Exception as e:
        print(f"❌ S&P500 지수 데이터 다운로드 실패: {e}")
        sys.exit(1)

    passed_technicals = []

    for ticker in tickers:
        try:
            df = get_ticker_dataframe(raw_data, ticker)

            if df is None or df.empty:
                continue

            if "Close" not in df.columns or "Volume" not in df.columns:
                continue

            df = df.dropna(subset=["Close"]).copy()

            if len(df) < 220:
                continue

            if getattr(df.index, "tz", None) is not None:
                df.index = df.index.tz_localize(None)

            df["MA50"] = df["Close"].rolling(window=50).mean()
            df["MA150"] = df["Close"].rolling(window=150).mean()
            df["MA200"] = df["Close"].rolling(window=200).mean()
            df["Vol_MA50"] = df["Volume"].rolling(window=50).mean()

            current_price = df["Close"].iloc[-1]
            ma50 = df["MA50"].iloc[-1]
            ma150 = df["MA150"].iloc[-1]
            ma200 = df["MA200"].iloc[-1]
            ma200_20days_ago = df["MA200"].iloc[-20]

            low_52week = df["Close"].tail(252).min()
            high_52week = df["Close"].tail(252).max()

            if pd.isna(ma50) or pd.isna(ma150) or pd.isna(ma200) or pd.isna(ma200_20days_ago):
                continue

            # 미너비니 트렌드 템플릿 주요 조건
            if not (current_price > ma150 and current_price > ma200):
                continue

            if not (ma150 > ma200 and ma200 > ma200_20days_ago):
                continue

            if not (ma50 > ma150 and ma50 > ma200 and current_price > ma50):
                continue

            if not (
                current_price >= low_52week * 1.30
                and current_price >= high_52week * 0.75
            ):
                continue

            # 최소 거래량 조건
            if not (df["Vol_MA50"].iloc[-1] > 150000):
                continue

            # RS 라인 간이 검증: 종목 / S&P500
            stock_close = df["Close"].copy()
            combined = pd.DataFrame(
                {
                    "Stock": stock_close,
                    "SP500": spy_close
                }
            ).dropna()

            if len(combined) < 60:
                continue

            combined["RS_Line"] = combined["Stock"] / combined["SP500"]
            rs_current = combined["RS_Line"].iloc[-1]
            rs_ma50 = combined["RS_Line"].rolling(window=50).mean().iloc[-1]

            if pd.isna(rs_ma50):
                continue

            if rs_current > rs_ma50:
                passed_technicals.append((ticker, df))
                print(f"✅ 기술적 필터 통과: {ticker}")

        except Exception as e:
            print(f"⚠️ {ticker} 처리 중 오류: {e}")

    print(f"🎯 기술적 필터 통과: {len(passed_technicals)}개 종목")

    print("🧬 4. 2차 펀더멘탈 필터 및 VCP 패턴 검사 시작...")

    final_vcp_targets = []

    for ticker, df in passed_technicals:
        try:
            t_info = yf.Ticker(ticker).info

            eps_growth = t_info.get("earningsGrowth", None)
            rev_growth = t_info.get("revenueGrowth", None)

            if eps_growth is None or rev_growth is None:
                continue

            if not (eps_growth >= 0.20 and rev_growth >= 0.15):
                continue

            recent_df = df.tail(30).copy()

            if len(recent_df) < 30:
                continue

            seg1 = recent_df.iloc[0:10]
            seg2 = recent_df.iloc[10:20]
            seg3 = recent_df.iloc[20:30]

            r1 = (seg1["High"].max() - seg1["Low"].min()) / seg1["Low"].min()
            r2 = (seg2["High"].max() - seg2["Low"].min()) / seg2["Low"].min()
            r3 = (seg3["High"].max() - seg3["Low"].min()) / seg3["Low"].min()

            # VCP: 변동성 축소
            if r1 > r2 > r3 and r3 < 0.08:
                # 최근 거래량 감소 여부
                if recent_df["Volume"].tail(5).mean() < recent_df["Volume"].mean():
                    entry = round(seg3["High"].max(), 2)
                    stop = round(seg3["Low"].min(), 2)
                    risk = round(((entry - stop) / entry) * 100, 1)

                    final_vcp_targets.append(
                        {
                            "ticker": ticker,
                            "entry": entry,
                            "stop": stop,
                            "risk": risk,
                            "eps_growth": round(eps_growth * 100, 1),
                            "rev_growth": round(rev_growth * 100, 1)
                        }
                    )

                    print(f"🔥 최종 통과: {ticker}")

            time.sleep(0.5)

        except Exception as e:
            print(f"⚠️ {ticker} 펀더멘탈/VCP 검사 중 오류: {e}")

    today_str = pd.Timestamp.now(tz="Asia/Seoul").strftime("%Y-%m-%d")

    result_file = f"minervini_result_{today_str}.csv"

    if final_vcp_targets:
        pd.DataFrame(final_vcp_targets).to_csv(result_file, index=False)
        print(f"💾 결과 CSV 저장 완료: {result_file}")
    else:
        pd.DataFrame(
            columns=["ticker", "entry", "stop", "risk", "eps_growth", "rev_growth"]
        ).to_csv(result_file, index=False)
        print(f"💾 빈 결과 CSV 저장 완료: {result_file}")

    russell_status = "성공" if len(ru_list) > 0 else "실패"

    if final_vcp_targets:
        target_text = "\n".join(
            [
                (
                    f"• {item['ticker']} | "
                    f"진입가: {item['entry']}$ | "
                    f"손절가: {item['stop']}$ | "
                    f"리스크: -{item['risk']}% | "
                    f"EPS성장: {item['eps_growth']}% | "
                    f"매출성장: {item['rev_growth']}%"
                )
                for item in final_vcp_targets
            ]
        )
    else:
        target_text = "차트와 실적을 동시에 만족하는 매수 임박 후보가 없습니다."

    msg = (
        f"🔔 [{today_str}] 미너비니 콤보 스크리닝 결과\n"
        f"------------------------------------\n"
        f"📊 S&P500: {len(sp_list)}개\n"
        f"📊 Nasdaq100: {len(nd_list)}개\n"
        f"📊 Russell2000/IWM: {len(ru_list)}개, 수집상태: {russell_status}\n"
        f"🚀 총 스캔 대상: {len(tickers)}개\n"
        f"📈 1차 추세+거래량 필터 통과: {len(passed_technicals)}개\n"
        f"🔥 최종 실적+VCP 패턴 통과: {len(final_vcp_targets)}개\n\n"
        f"{target_text}\n"
        f"------------------------------------\n"
        f"※ 투자 추천이 아닌 자동 선별 결과입니다."
    )

    send_telegram_message(msg)

    print("🎯 전체 스크리닝 및 텔레그램 발송 프로세스 완료")
