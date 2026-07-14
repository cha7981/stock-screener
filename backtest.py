import os
import time
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
from pykrx import stock

# ============================================================
# 백테스트 환경 설정
# ============================================================
START_BACKTEST = "20230714"  # 3년 전
END_BACKTEST = "20260714"    # 오늘 기준

INIT_CASH = 10_000_000       # 초기 자본금 1,000만 원
MAX_POSITIONS = 5            # 최대 동시 보유 종목 수
SLOT_SIZE = INIT_CASH / MAX_POSITIONS  # 종목당 고정 투자금 (200만 원)

# 최종 청산 지지선 설정 ('MA10' 또는 'MA20' 중 선택 가능)
EXIT_MA_LINE = "MA20"        

MIN_PRICE = 2000
MIN_AVG_TURNOVER = 3_000_000_000  # 20일 평균 거래대금 최소 30억

print(f"📈 K-Minervini v2.5 백테스트 엔진 가동 (자본금: {INIT_CASH:,}원 / 청산기준: {EXIT_MA_LINE} 붕괴)")

# ============================================================
# 0. 최근 거래일 탐색 헬퍼 (주말/휴일 에러 방지)
# ============================================================
def get_valid_end_date(target_date_str):
    target_date = datetime.strptime(target_date_str, "%Y%m%d")
    for i in range(10):
        check_date = (target_date - timedelta(days=i)).strftime("%Y%m%d")
        try:
            df = stock.get_market_ohlcv_by_ticker(check_date, market="KOSPI")
            if df is not None and not df.empty:
                return check_date
        except Exception:
            pass
    return target_date_str

# ============================================================
# 1. 백테스트 대상 유니버스 선정 (현재 유동성 상위 종목)
# ============================================================
def get_backtest_universe():
    valid_end_date = get_valid_end_date(END_BACKTEST)
    print(f"📦 백테스트 유니버스 기준일: {valid_end_date}")
    
    try:
        kospi = stock.get_market_ohlcv_by_ticker(valid_end_date, market="KOSPI")
        kosdaq = stock.get_market_ohlcv_by_ticker(valid_end_date, market="KOSDAQ")
        snap = pd.concat([kospi, kosdaq])
    except Exception as e:
        print(f"⚠️ 마켓 스냅샷 로드 실패: {e}")
        return []
    
    if snap.empty:
        print("⚠️ 유니버스 데이터를 불러오지 못했습니다.")
        return []

    snap = snap[snap["종가"] >= MIN_PRICE]
    
    # 거래대금 상위 150개 종목 추출
    if "거래대금" in snap.columns:
        tickers = snap.sort_values("거래대금", ascending=False).index[:150].tolist()
    else:
        tickers = snap.index[:150].tolist()
        
    return tickers

# ============================================================
# 2. 지표 계산 엔진 (KeyError 방지 보완)
# ============================================================
def calculate_historical_indicators(df):
    df = df.copy()
    
    # [오류 수정] pykrx 기간 조회에 거래대금이 없는 경우 종가*거래량으로 자체 계산
    if "Turnover" not in df.columns or df["Turnover"].dropna().empty:
        df["Turnover"] = df["Close"] * df["Volume"]
        
    df["MA10"] = df["Close"].rolling(10).mean()
    df["MA20"] = df["Close"].rolling(20).mean()
    df["MA60"] = df["Close"].rolling(60).mean()
    df["VolMA20"] = df["Volume"].rolling(20).mean()
    df["TurnoverMA20"] = df["Turnover"].rolling(20).mean()
    
    # ATR 계산
    tr1 = df["High"] - df["Low"]
    tr2 = (df["High"] - df["Close"].shift(1)).abs()
    tr3 = (df["Low"] - df["Close"].shift(1)).abs()
    df["TR"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["ATR10"] = df["TR"].rolling(10).mean()
    
    return df

# ============================================================
# 3. 메인 시뮬레이터
# ============================================================
def run_backtest():
    tickers = get_backtest_universe()
    if not tickers:
        print("❌ 대상 종목이 없습니다.")
        return

    all_data = {}
    
    print("📥 종목별 3년치 데이터 로드 및 지표 연산 중...")
    for idx, t in enumerate(tickers):
        try:
            df = stock.get_market_ohlcv_by_date(START_BACKTEST, END_BACKTEST, t)
            if df is not None and len(df) >= 100:
                df = df.rename(columns={"시가":"Open", "고가":"High", "저가":"Low", "종가":"Close", "거래량":"Volume", "거래대금":"Turnover"})
                all_data[t] = calculate_historical_indicators(df)
        except Exception as e:
            # 에러 추적용 파악 문구 (필요 시 주석 해제)
            # print(f"Ticker {t} 소스 에러: {e}")
            pass
        time.sleep(0.05)

    if not all_data:
        print("❌ 데이터 로드 실패: 모든 종목의 데이터 가공에 실패했습니다.")
        return
    
    trading_days = sorted(list(all_data[list(all_data.keys())[0]].index))
    trading_days = [d for d in trading_days if d >= pd.to_datetime(START_BACKTEST)]
    
    cash = INIT_CASH
    portfolio = []  
    history = []
    
    print(f"🚀 시뮬레이션 가동 시작 ({len(trading_days)} 거래일)")

    for current_day in trading_days:
        # A. 보유 종목 매도 조건 체크
        survived_portfolio = []
        for pos in portfolio:
            t = pos["ticker"]
            df = all_data[t]
            if current_day not in df.index:
                survived_portfolio.append(pos)
                continue
                
            day_row = df.loc[current_day]
            low_p = day_row["Low"]
            high_p = day_row["High"]
            close_p = day_row["Close"]
            
            if high_p > pos["highest_price"]:
                pos["highest_price"] = high_p
            
            # 1) 원 오리지널 손절선 이탈 체크 (Stop Loss)
            if low_p <= pos["stop_loss"]:
                exit_price = pos["stop_loss"]
                pnl = (exit_price - pos["entry_price"]) / pos["entry_price"]
                cash += SLOT_SIZE * (1 + pnl)
                history.append({"ticker": t, "pnl": pnl, "reason": "StopLoss", "date": current_day})
                continue
            
            # 2) v2.5 매도 규칙 익절 시뮬레이션 (1R 도달 시 본전 스탑 상향)
            if pos["highest_price"] >= pos["target_1r"]:
                pos["stop_loss"] = pos["entry_price"] 
                
            # 3) 지정한 이평선 지지선 최종 붕괴 시 청산
            if close_p < day_row.get(EXIT_MA_LINE, 0):
                exit_price = close_p
                pnl = (exit_price - pos["entry_price"]) / pos["entry_price"]
                cash += SLOT_SIZE * (1 + pnl)
                history.append({"ticker": t, "pnl": pnl, "reason": f"{EXIT_MA_LINE}_Break", "date": current_day})
                continue
                
            survived_portfolio.append(pos)
        
        portfolio = survived_portfolio
        
        # B. 신규 종목 매수 진입 시뮬레이션
        if len(portfolio) < MAX_POSITIONS and cash >= SLOT_SIZE:
            candidates = []
            
            for t, df in all_data.items():
                if current_day not in df.index: continue
                idx_list = df.index.get_loc(current_day)
                if idx_list < 60: continue 
                
                sub_df = df.iloc[:idx_list+1]
                last = sub_df.iloc[-1]
                
                # 기본 조건 필터링
                if last["Close"] < last["MA20"] or last["TurnoverMA20"] < MIN_AVG_TURNOVER: continue
                
                # 피봇 돌파 시그널
                pivot = sub_df["High"].iloc[-20:-1].max()
                if last["Close"] >= pivot and last["Volume"] > last["VolMA20"] * 1.3:
                    # [논리오류 수정] 손절선은 최근 5일간의 '최저가' 지지선으로 잡아야 하므로 min 사용
                    stop_loss = min(sub_df["Low"].iloc[-5:]) if len(sub_df) >= 5 else last["Close"] * 0.95
                    if stop_loss >= last["Close"]:
                        stop_loss = last["Close"] * 0.95
                        
                    risk_pct = (last["Close"] - stop_loss) / last["Close"]
                    
                    if 0.02 <= risk_pct <= 0.08:
                        candidates.append({
                            "ticker": t,
                            "price": last["Close"],
                            "stop_loss": stop_loss,
                            "target_1r": last["Close"] + (last["Close"] - stop_loss)
                        })
            
            for cand in candidates:
                if len(portfolio) >= MAX_POSITIONS or cash < SLOT_SIZE: break
                if any(pos["ticker"] == cand["ticker"] for pos in portfolio): continue
                
                portfolio.append({
                    "ticker": cand["ticker"],
                    "entry_price": cand["price"],
                    "stop_loss": cand["stop_loss"],
                    "target_1r": cand["target_1r"],
                    "highest_price": cand["price"]
                })
                cash -= SLOT_SIZE

    # 최종 자산 평가
    final_asset = cash + (len(portfolio) * SLOT_SIZE)
    total_return = (final_asset / INIT_CASH - 1) * 100
    
    print("\n" + "="*50)
    print("📊 K-미너비니 v2.5 맞춤형 백테스트 결과")
    print("="*50)
    print(f"• 초기 투자금: {INIT_CASH:,} 원 (종목당 {int(SLOT_SIZE):,} 원 고정)")
    print(f"• 최종 자산 평가액: {int(final_asset):,} 원")
    print(f"• **누적 수익률**: {total_return:.2f}%")
    
    if history:
        df_hist = pd.DataFrame(history)
        win_rate = (df_hist["pnl"] > 0).sum() / len(df_hist) * 100
        print(f"• 총 매매 횟수: {len(df_hist)} 회")
        print(f"• 매매 승률: {win_rate:.1f}%")
        print(f"• 평균 1회 매매 수익률: {df_hist['pnl'].mean() * 100:.2f}%")
        print(f"\n[청산 유형별 횟수]\n{df_hist['reason'].value_counts().to_string()}")
    print("="*50)

if __name__ == "__main__":
    run_backtest()
