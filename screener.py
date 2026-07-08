import os
import time
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
  payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
  try:
    requests.post(url, json=payload, timeout=10)
  except Exception as e:
    print(f"텔레그램 전송 에러: {e}")


def get_html_with_header(url):
  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
          " like Gecko) Chrome/120.0.0.0 Safari/537.36"
      )
  }
  return requests.get(url, headers=headers, timeout=15).text


def get_sp500_tickers():
  try:
    html = get_html_with_header(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    )
    return (
        pd.read_html(html)[0]["Symbol"]
        .str.replace(".", "-", regex=False)
        .tolist()
    )
  except Exception:
    return []


def get_nasdaq100_tickers():
  try:
    html = get_html_with_header("https://en.wikipedia.org/wiki/Nasdaq-100")
    df_list = pd.read_html(html, attrs={"id": "constituents"})
    return (
        df_list[0]["Ticker"].str.replace(".", "-", regex=False).tolist()
        if df_list
        else []
    )
  except Exception:
    return []


def get_russell2000_tickers():
  """안정적인 다른 오픈소스 금융 데이터 가공처에서 러셀 2000 명단 수집"""
  try:
    # 종목 리스트가 비교적 잘 유지되는 대체 주소 사용
    url = "https://raw.githubusercontent.com/mrgnprime/russell-2000-tickers/main/russell2000.csv"
    df = pd.read_csv(url)
    # 컬럼명이 'Ticker'나 'Symbol' 등 다를 수 있으므로 첫 번째 컬럼을 타겟팅
    tickers = df.iloc[:, 0].dropna().tolist()
    return [str(t).strip().replace(".", "-") for t in tickers if len(str(t)) < 6]
  except Exception as e:
    print(f"⚠️ 러셀 2000 기본 수집 실패 ({e}), 백업 주소 시도...")
    try:
      # 백업 데이터셋 주소
      url_bak = "https://raw.githubusercontent.com/anthonymandelli/Russell-2000-Ticker-History/master/russell2000_current.csv"
      df = pd.read_csv(url_bak)
      tickers = df.iloc[:, 0].dropna().tolist()
      return [
          str(t).strip().replace(".", "-") for t in tickers if len(str(t)) < 6
      ]
    except Exception as e2:
      print(f"❌ 러셀 2000 백업 수집도 실패 ({e2})")
      return []


if __name__ == "__main__":
  print("📦 1. 미국 시장 전 종목 명단 수집 시작...")
  sp_list = get_sp500_tickers()
  nd_list = get_nasdaq100_tickers()
  ru_list = get_russell2000_tickers()

  tickers = list(set(sp_list + nd_list + ru_list))
  print(
      f"📊 수집 완료 -> S&P500: {len(sp_list)}개 | 나스닥100: {len(nd_list)}개 |"
      f" 러셀2000: {len(ru_list)}개"
  )
  print(f"🚀 총 스캔 대상(중복 제거): {len(tickers)}개 종목")

  if not tickers:
    print("❌ 에러: 종목 명단을 수집하지 못해 프로그램을 종료합니다.")
    exit()

  print("📥 2. 주가 데이터 분할 다운로드 시작 (IP 차단 방지)...")
  # 2,500개가 넘는 종목을 한 번에 받으면 야후 파이낸스에서 에러를 뱉으므로, 300개씩 쪼개서 다운로드
  chunk_size = 300
  raw_data = pd.DataFrame()

  for i in range(0, len(tickers), chunk_size):
    chunk_tickers = tickers[i : i + chunk_size]
    print(
      f"   ↳ 다운로드 중... [{i}/{len(tickers)}] ({len(chunk_tickers)}개 종목)"
    )
    try:
      chunk_data = yf.download(
          chunk_tickers, period="1y", group_by="ticker", progress=False
      )
      if raw_data.empty:
        raw_data = chunk_data
      else:
        raw_data = pd.concat([raw_data, chunk_data], axis=1)
    except Exception as e:
      print(f"⚠️ 일부 청크 다운로드 실패: {e}")
    time.sleep(2)  # 야후 서버를 위한 휴식 시간

  print("📈 3. 1차 기술적 필터 (트렌드 템플릿) 가동...")
  spy_df = yf.Ticker("^GSPC").history(period="1y")
  spy_close = spy_df["Close"].copy().tz_localize(None)

  passed_technicals = []

  for ticker in tickers:
    try:
      # MultiIndex 구조에서 해당 ticker 데이터가 존재하는지 안전하게 확인
      if ticker in raw_data.columns.levels[0]:
        df = raw_data[ticker].dropna(subset=["Close"]).copy()
        if len(df) < 200:
          continue

        df["MA50"] = df["Close"].rolling(window=50).mean()
        df["MA150"] = df["Close"].rolling(window=150).mean()
        df["MA200"] = df["Close"].rolling(window=200).mean()
        df["Vol_MA50"] = df["Volume"].rolling(window=50).mean()

        current_price = df["Close"].iloc[-1]
        ma50 = df["MA50"].iloc[-1]
        ma150 = df["MA150"].iloc[-1]
        ma200 = df["MA200"].iloc[-1]
        ma200_20days_ago = df["MA200"].iloc[-20]
        low_52week = df["Close"].min()
        high_52week = df["Close"].max()

        # 미너비니 7대 기술적 조건 필터링
        if not (current_price > ma150 and current_price > ma200):
          continue
        if not (ma150 > ma200 and ma200 > ma200_20days_ago):
          continue
        if not (ma50 > ma150 and ma50 > ma200 and current_price > ma50):
          continue
        if not (
            current_price >= (low_52week * 1.30)
            and current_price >= (high_52week * 0.75)
        ):
          continue
        if not (df["Vol_MA50"].iloc[-1] > 150000):
          continue  # 거래량 기준 최소 15만 주 이상

        # RS 대략 검증
        stock_close = df["Close"].copy().tz_localize(None)
        combined = pd.DataFrame({"Stock": stock_close, "SPY": spy_close}).dropna()
        combined["RS_Line"] = combined["Stock"] / combined["SPY"]
        if (
            combined["RS_Line"].iloc[-1]
            > combined["RS_Line"].rolling(window=50).mean().iloc[-1]
        ):
          passed_technicals.append((ticker, df))
    except Exception:
      pass

  print(f"🎯 기술적 필터 통과: {len(passed_technicals)}개 종목")

  print("🧬 4. 2차 펀더멘탈 필터 가동 (살아남은 종목만 실적 정밀 검사)...")
  final_vcp_targets = []

  for ticker, df in passed_technicals:
    try:
      # 기술적 필터를 통과한 극소수(보통 30~50개 내외)만 펀더멘탈 API 호출하므로 안전함
      t_info = yf.Ticker(ticker).info

      # 미너비니 핵심: 최근 분기 EPS 및 매출 성장률 검증
      eps_growth = t_info.get("earningsGrowth", 0.25)
      rev_growth = t_info.get("revenueGrowth", 0.20)

      if (eps_growth is not None and eps_growth >= 0.20) and (
          rev_growth is not None and rev_growth >= 0.15
      ):

        # 5. VCP 변동성 축소 패턴 최종 검증
        recent_df = df.tail(30)
        seg1, seg2, seg3 = (
            recent_df.iloc[0:10],
            recent_df.iloc[10:20],
            recent_df.iloc[20:30],
        )
        r1 = (seg1["High"].max() - seg1["Low"].min()) / seg1["Low"].min()
        r2 = (seg2["High"].max() - seg2["Low"].min()) / seg2["Low"].min()
        r3 = (seg3["High"].max() - seg3["Low"].min()) / seg3["Low"].min()

        if r1 > r2 and r2 > r3 and r3 < 0.08:
          if recent_df["Volume"].tail(5).mean() < recent_df["Volume"].mean():
            entry = round(seg3["High"].max(), 2)
            stop = round(seg3["Low"].min(), 2)
            risk = round(((entry - stop) / entry) * 100, 1)
            final_vcp_targets.append(
                {"ticker": ticker, "entry": entry, "stop": stop, "risk": risk}
            )
      time.sleep(0.5)  # 연속 호출 차단 방지용 미세 딜레이
    except Exception:
      pass

  # 6. 최종 결과 발송
  today_str = pd.Timestamp.now().strftime("%Y-%m-%d")
  t3_text = (
      "\n".join([
          f"• *{item['ticker']}* ➔ 진입가: {item['entry']}$ | 손절가:"
          f" {item['stop']}$ (-{item['risk']}%)"
          for item in final_vcp_targets
      ])
      if final_vcp_targets
      else "차트와 실적을 동시 만족하는 매수 임박 종목이 없습니다."
  )

  msg = (
      f"🔔 *[{today_str}] 미너비니 콤보 스크리닝 결과*\n"
      "------------------------------------\n"
      f"📊 *대상:* S&P500 + 나스닥100 + 러셀2000 (총 {len(tickers)}개)\n"
      f"📈 *1차 추세+거래량 필터 통과:* {len(passed_technicals)}개\n"
      f"🔥 *최종 실적 + VCP 패턴 통과:* {len(final_vcp_targets)}개\n\n"
      f"{t3_text}\n"
      "------------------------------------"
  )
  send_telegram_message(msg)
  print("🎯 전체 스크리닝 및 텔레그램 발송 프로세스가 정상 완료되었습니다!")
