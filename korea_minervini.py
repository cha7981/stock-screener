
import os
import time
import random
import traceback
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests
from pykrx import stock

# ============================================================
# K-Minervini v2.5 Settings
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

MAX_UNIVERSE = int(os.environ.get("KOREA_MAX_UNIVERSE", "600"))
SLEEP_SEC = float(os.environ.get("KOREA_SLEEP_SEC", "0.15"))
SUPPLY_DETAIL_LIMIT = int(os.environ.get("KOREA_SUPPLY_DETAIL_LIMIT", "120"))

MIN_PRICE = 2000
MIN_AVG_TURNOVER = 3_000_000_000
GOOD_TURNOVER = 5_000_000_000
STRONG_TURNOVER = 10_000_000_000

BREAKOUT_TODAY_SCORE = 82
BREAKOUT_SCORE = 80
PULLBACK_SCORE = 75
WATCH_SCORE = 70
AVOID_MIN_SCORE = 70

PULLBACK_MIN_DD = -12.0
PULLBACK_MAX_DD = -3.0
NEAR_MA20_PCT = 4.0
NEAR_MA60_PCT = 6.0

PIVOT_LOOKBACK = 30
BREAKOUT_NEAR_PCT = 3.0
MAX_RISK_PCT = 8.0
AVOID_RISK_PCT = 10.0

VOLUME_EXPLOSION_RATIO = 1.5
TURNOVER_EXPLOSION_RATIO = 1.5

OVERHEAT_5D_RETURN = 30.0
OVERHEAT_10D_RETURN = 50.0
OVERHEAT_MA20_GAP = 25.0

# V2.5 relaxed quality thresholds
VCP_READY_SCORE = 4.5
PULLBACK_QUALITY_READY_SCORE = 5.5
PULLBACK_QUALITY_WATCH_SCORE = 4.0

# Near Ready display thresholds
NEAR_BREAKOUT_VCP = 4.0
NEAR_PULLBACK_PB = 5.0
NEAR_READY_SCORE = 75
NEAR_READY_LIMIT = 5

KST = timezone(timedelta(hours=9))


# ============================================================
# Telegram
# ============================================================
def send_telegram_message(message: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("❌ TELEGRAM_TOKEN 또는 CHAT_ID가 없습니다. GitHub Secrets를 확인하세요.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    try:
        chunks = []
        text = message
        while len(text) > 3900:
            cut = text.rfind("\n", 0, 3900)
            if cut == -1:
                cut = 3900
            chunks.append(text[:cut])
            text = text[cut:].lstrip()
        chunks.append(text)

        ok = True
        for chunk in chunks:
            res = requests.post(
                url,
                json={"chat_id": CHAT_ID, "text": chunk},
                timeout=20,
            )
            if res.status_code != 200:
                ok = False
                print(f"⚠️ 텔레그램 전송 실패: {res.status_code} {res.text}")
        return ok

    except Exception as e:
        print(f"⚠️ 텔레그램 전송 에러: {e}")
        return False


def send_telegram_document(file_path: str, caption: str = ""):
    """텔레그램으로 일반 파일을 전송합니다."""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("❌ TELEGRAM_TOKEN 또는 CHAT_ID가 없습니다. GitHub Secrets를 확인하세요.")
        return False

    if not os.path.exists(file_path):
        print(f"⚠️ 전송할 파일이 없습니다: {file_path}")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"

    try:
        with open(file_path, "rb") as f:
            res = requests.post(
                url,
                data={"chat_id": CHAT_ID, "caption": caption},
                files={"document": f},
                timeout=60,
            )
        if res.status_code != 200:
            print(f"⚠️ 텔레그램 파일 전송 실패: {res.status_code} {res.text}")
            return False
        return True
    except Exception as e:
        print(f"⚠️ 텔레그램 파일 전송 에러: {e}")
        return False


# ============================================================
# Format helpers
# ============================================================
def fmt_int(x):
    try:
        if pd.isna(x):
            return "-"
        return f"{int(round(float(x))):,}"
    except Exception:
        return "-"


def fmt_pct(x):
    try:
        if pd.isna(x):
            return "-"
        return f"{float(x):.1f}%"
    except Exception:
        return "-"


def fmt_float(x, decimals=2):
    try:
        if pd.isna(x):
            return "-"
        return f"{float(x):.{decimals}f}"
    except Exception:
        return "-"


def fmt_krw_eok(x):
    try:
        if pd.isna(x):
            return "-"
        return f"{float(x) / 100_000_000:,.1f}억"
    except Exception:
        return "-"


def fmt_eok_plain(x):
    """원 단위 금액을 억원 단위 정수로 표시. 예: +570억"""
    try:
        if pd.isna(x):
            return "0억"
        return f"{float(x) / 100_000_000:+.0f}억"
    except Exception:
        return "0억"


def safe_col(df, candidates, default=None):
    for c in candidates:
        if c in df.columns:
            return c
    return default


def circled_number(i):
    nums = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"]
    if 1 <= i <= len(nums):
        return nums[i - 1]
    return f"{i}."


# ============================================================
# Date helpers
# ============================================================
def date_str(dt):
    return dt.strftime("%Y%m%d")


def get_recent_business_day(max_back=10):
    now = datetime.now(KST)
    
    # [핵심 방어 로직] 정규장 개장(오전 9시) 이전인가?
    # 오전 9시 이전이면 장전 시간외 거래가 있더라도 무조건 오늘(당일)을 건너뛰고 '어제'부터 탐색
    start_offset = 1 if now.hour < 9 else 0
    
    for i in range(start_offset, max_back + start_offset):
        d = now - timedelta(days=i)
        ds = date_str(d)
        try:
            df = stock.get_market_ohlcv_by_ticker(ds, market="KOSPI")
            
            if df is not None and not df.empty:
                # 거래대금이 존재하는지 확인 (공휴일/주말 필터링)
                if "거래대금" in df.columns and df["거래대금"].sum() > 0:
                    return ds
                elif "Amount" in df.columns and df["Amount"].sum() > 0:
                    return ds
                elif "거래대금" not in df.columns and "Amount" not in df.columns:
                    return ds
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("최근 거래일을 찾지 못했습니다.")



def add_days_ago(base_yyyymmdd, days):
    base = datetime.strptime(base_yyyymmdd, "%Y%m%d")
    return date_str(base - timedelta(days=days))


# ============================================================
# Universe and price data
# ============================================================
def get_market_snapshot(end_date, market):
    df = stock.get_market_ohlcv_by_ticker(end_date, market=market)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["ticker"] = df.index.astype(str)
    df["market"] = market
    df["name"] = df["ticker"].map(lambda t: stock.get_market_ticker_name(t))
    return df.reset_index(drop=True)


def build_liquid_universe(end_date):
    print("📦 KOSPI/KOSDAQ 전체 snapshot 수집")
    kospi = get_market_snapshot(end_date, "KOSPI")
    kosdaq = get_market_snapshot(end_date, "KOSDAQ")
    snap = pd.concat([kospi, kosdaq], ignore_index=True)

    if snap.empty:
        raise RuntimeError("KOSPI/KOSDAQ snapshot이 비어 있습니다.")

    close_col = safe_col(snap, ["종가", "Close"])
    value_col = safe_col(snap, ["거래대금", "Amount", "거래대금(원)"])
    volume_col = safe_col(snap, ["거래량", "Volume"])

    snap["close"] = pd.to_numeric(snap[close_col], errors="coerce")
    snap["turnover"] = pd.to_numeric(snap[value_col], errors="coerce") if value_col else 0
    snap["volume"] = pd.to_numeric(snap[volume_col], errors="coerce") if volume_col else 0

    snap = snap[(snap["close"] >= MIN_PRICE) & (snap["turnover"] > 0)].copy()
    snap = snap.sort_values("turnover", ascending=False).head(MAX_UNIVERSE).copy()
    print(f"✅ 1차 유동성 후보: {len(snap)}개 / MAX_UNIVERSE={MAX_UNIVERSE}")
    return snap


def get_history(ticker, start_date, end_date):
    df = stock.get_market_ohlcv_by_date(start_date, end_date, ticker)
    if df is None or df.empty:
        return None
    df = df.copy()

    rename = {
        "시가": "Open", "고가": "High", "저가": "Low", "종가": "Close",
        "거래량": "Volume", "거래대금": "Turnover", "등락률": "ChangePct",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    if not all(c in df.columns for c in ["Open", "High", "Low", "Close", "Volume"]):
        return None

    if "Turnover" not in df.columns:
        df["Turnover"] = df["Close"] * df["Volume"]

    df = df.dropna(subset=["Close"]).copy()
    if len(df) < 160:
        return None

    for c in ["Open", "High", "Low", "Close", "Volume", "Turnover"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df["MA10"] = df["Close"].rolling(10).mean()
    df["MA20"] = df["Close"].rolling(20).mean()
    df["MA60"] = df["Close"].rolling(60).mean()
    df["MA120"] = df["Close"].rolling(120).mean()
    df["VolMA20"] = df["Volume"].rolling(20).mean()
    df["TurnoverMA20"] = df["Turnover"].rolling(20).mean()

    tr1 = df["High"] - df["Low"]
    tr2 = (df["High"] - df["Close"].shift(1)).abs()
    tr3 = (df["Low"] - df["Close"].shift(1)).abs()
    df["TR"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["ATR10"] = df["TR"].rolling(10).mean()
    df["ATR30"] = df["TR"].rolling(30).mean()
    return df


def safe_return(close, days):
    if len(close) <= days:
        return np.nan
    start = close.iloc[-days]
    end = close.iloc[-1]
    if start <= 0 or pd.isna(start) or pd.isna(end):
        return np.nan
    return end / start - 1


# ============================================================
# Supply data
# ============================================================
def get_net_purchase_by_investor(start_date, end_date, investor):
    frames = []
    for market in ["KOSPI", "KOSDAQ"]:
        try:
            df = stock.get_market_net_purchases_of_equities(start_date, end_date, market, investor)
            if df is None or df.empty:
                continue
            df = df.copy()
            df["ticker"] = df.index.astype(str)
            df["market"] = market
            frames.append(df.reset_index(drop=True))
            time.sleep(0.2)
        except Exception as e:
            print(f"⚠️ {investor} {market} 순매수 조회 실패: {e}")

    if not frames:
        return pd.DataFrame(columns=["ticker", f"{investor}_net"])

    merged = pd.concat(frames, ignore_index=True)
    net_col = safe_col(merged, ["순매수거래대금", "순매수", "순매수금액", "거래대금"])
    if net_col is None:
        numeric_cols = merged.select_dtypes(include=[np.number]).columns.tolist()
        net_col = numeric_cols[-1] if numeric_cols else None

    merged[f"{investor}_net"] = pd.to_numeric(merged[net_col], errors="coerce").fillna(0) if net_col else 0
    return merged[["ticker", f"{investor}_net"]]


def get_recent_supply_counts(ticker, start_date, end_date):
    out = {
        "foreign_buy_days_5d": 0,
        "institution_buy_days_5d": 0,
        "co_buy_days_5d": 0,
        "foreign_net_5d": 0,
        "institution_net_5d": 0,
    }
    try:
        df = stock.get_market_trading_value_by_date(start_date, end_date, ticker)
        if df is None or df.empty:
            return out
        df = df.tail(5).copy()
        fcol = safe_col(df, ["외국인", "외국인합계"])
        icol = safe_col(df, ["기관합계", "기관"])

        if fcol:
            f = pd.to_numeric(df[fcol], errors="coerce").fillna(0)
            out["foreign_buy_days_5d"] = int((f > 0).sum())
            out["foreign_net_5d"] = float(f.sum())
        if icol:
            inst = pd.to_numeric(df[icol], errors="coerce").fillna(0)
            out["institution_buy_days_5d"] = int((inst > 0).sum())
            out["institution_net_5d"] = float(inst.sum())
        if fcol and icol:
            out["co_buy_days_5d"] = int(((f > 0) & (inst > 0)).sum())
    except Exception as e:
        print(f"⚠️ {ticker} 최근 5일 수급 상세 조회 실패: {e}")
    return out


# ============================================================
# Scoring
# ============================================================
def trend_score(df):
    last = df.iloc[-1]
    score = 0
    reasons = []
    close = last["Close"]
    ma20 = last["MA20"]
    ma60 = last["MA60"]
    ma120 = last["MA120"]
    high52 = df["Close"].tail(252).max()
    low52 = df["Close"].tail(252).min()
    ma120_prev = df["MA120"].iloc[-21] if len(df) >= 141 else np.nan

    if close > ma20:
        score += 5
        reasons.append("종가>MA20")
    if close > ma60:
        score += 5
        reasons.append("종가>MA60")
    if ma60 > ma120:
        score += 5
        reasons.append("MA60>MA120")
    if not pd.isna(ma120_prev) and ma120 > ma120_prev:
        score += 5
        reasons.append("MA120상승")
    if high52 > 0 and close >= high52 * 0.85:
        score += 5
        reasons.append("52주고점15%이내")

    dd_from_high = (close / high52 - 1) * 100 if high52 > 0 else np.nan
    up_from_low = (close / low52 - 1) * 100 if low52 > 0 else np.nan
    return score, ";".join(reasons), dd_from_high, up_from_low


def liquidity_score(avg_turnover):
    if avg_turnover >= STRONG_TURNOVER:
        return 15
    if avg_turnover >= GOOD_TURNOVER:
        return 10
    if avg_turnover >= MIN_AVG_TURNOVER:
        return 5
    return 0


def supply_score(value, strong_base):
    if pd.isna(value):
        return 0
    if value > strong_base * 0.03:
        return 15
    if value > 0:
        return 10
    return 0


def institution_score(value, strong_base):
    if pd.isna(value):
        return 0
    if value > strong_base * 0.02:
        return 10
    if value > 0:
        return 6
    return 0


def risk_grade(risk):
    if pd.isna(risk):
        return "-"
    if risk <= 5:
        return "좋음"
    if risk <= 8:
        return "가능"
    if risk <= 10:
        return "주의"
    return "제외"


def detect_vcp(df):
    out = {
        "vcp_score": 0,
        "vcp_ready": False,
        "vcp_reason": "",
        "vcp_range_early": np.nan,
        "vcp_range_mid": np.nan,
        "vcp_range_final": np.nan,
        "vcp_volume_ratio": np.nan,
        "vcp_atr_ratio": np.nan,
        "vcp_final_tight": False,
        "vcp_higher_low": False,
    }
    if df is None or len(df) < 80:
        return out

    d = df.copy()
    last = d.iloc[-1]
    close = last["Close"]
    w = d.tail(60).copy()
    if len(w) < 60:
        return out

    early = w.iloc[0:20]
    mid = w.iloc[20:40]
    final = w.iloc[40:60]

    def range_pct(x):
        hi = x["High"].max()
        lo = x["Low"].min()
        if hi <= 0 or pd.isna(hi) or pd.isna(lo):
            return np.nan
        return (hi - lo) / hi * 100

    r1 = range_pct(early)
    r2 = range_pct(mid)
    r3 = range_pct(final)
    out["vcp_range_early"] = r1
    out["vcp_range_mid"] = r2
    out["vcp_range_final"] = r3

    score = 0
    reasons = []
    if not pd.isna(r1) and not pd.isna(r2) and not pd.isna(r3):
        if r1 > r2 > r3:
            score += 2
            reasons.append("수축연속")
        elif r2 > r3 and r3 <= 10:
            score += 1
            reasons.append("최종수축")

    if not pd.isna(r3) and r3 <= 8:
        score += 1
        reasons.append("최종8%이내")
        out["vcp_final_tight"] = True
    elif not pd.isna(r3) and r3 <= 10:
        score += 0.5
        reasons.append("최종10%이내")
        out["vcp_final_tight"] = True

    prev_low = w.iloc[0:40]["Low"].min()
    final_low = final["Low"].min()
    if final_low > prev_low:
        score += 1
        reasons.append("고점부높은저점")
        out["vcp_higher_low"] = True

    vol5 = d["Volume"].tail(5).mean()
    vol20 = d["Volume"].tail(20).mean()
    vol_ratio = vol5 / vol20 if vol20 and vol20 > 0 else np.nan
    out["vcp_volume_ratio"] = vol_ratio
    if not pd.isna(vol_ratio) and vol_ratio <= 0.80:
        score += 1
        reasons.append("거래량DryUp")

    atr10 = last.get("ATR10", np.nan)
    atr30 = last.get("ATR30", np.nan)
    atr_ratio = atr10 / atr30 if not pd.isna(atr10) and not pd.isna(atr30) and atr30 > 0 else np.nan
    out["vcp_atr_ratio"] = atr_ratio
    if not pd.isna(atr_ratio) and atr_ratio <= 0.90:
        score += 1
        reasons.append("ATR수축")

    pivot = d["High"].tail(PIVOT_LOOKBACK).max()
    dist_to_pivot = (close / pivot - 1) * 100 if pivot > 0 else np.nan
    if not pd.isna(dist_to_pivot) and -BREAKOUT_NEAR_PCT <= dist_to_pivot <= 1.5:
        score += 1
        reasons.append("피봇근처")

    ret10 = safe_return(d["Close"].dropna(), 10) * 100
    ma20 = last.get("MA20", np.nan)
    ma20_gap = (close / ma20 - 1) * 100 if ma20 and ma20 > 0 else np.nan
    if (not pd.isna(ret10) and ret10 >= 20) or (not pd.isna(ma20_gap) and ma20_gap >= 18):
        score -= 1
        reasons.append("급등후감점")

    score = max(0, min(7, score))
    out["vcp_score"] = round(score, 1)
    out["vcp_ready"] = bool(score >= VCP_READY_SCORE)
    out["vcp_reason"] = ";".join(reasons)
    return out


def detect_pullback_quality(df, drawdown, near_ma20, near_ma60, volume_dryup, atr_dryup, risk):
    out = {
        "pullback_quality_score": 0,
        "pullback_quality_ready": False,
        "pullback_quality_reason": "",
        "pullback_higher_low": False,
        "pullback_above_ma60": False,
        "pullback_above_ma20": False,
        "pullback_volume_dryup": bool(volume_dryup),
        "pullback_atr_dryup": bool(atr_dryup),
    }
    if df is None or len(df) < 50:
        return out

    last = df.iloc[-1]
    close = last["Close"]
    ma20 = last.get("MA20", np.nan)
    ma60 = last.get("MA60", np.nan)

    score = 0
    reasons = []
    if not pd.isna(drawdown) and PULLBACK_MIN_DD <= drawdown <= PULLBACK_MAX_DD:
        score += 2
        reasons.append("적정조정")
    if near_ma20 or near_ma60:
        score += 2
        reasons.append("이평선근처")
    if volume_dryup:
        score += 1
        reasons.append("거래량감소")
    if atr_dryup:
        score += 1
        reasons.append("ATR감소")

    recent_low_10 = df["Low"].tail(10).min()
    prev_low_20 = df["Low"].iloc[-30:-10].min() if len(df) >= 30 else np.nan
    if not pd.isna(recent_low_10) and not pd.isna(prev_low_20) and recent_low_10 > prev_low_20:
        score += 1
        reasons.append("높은저점")
        out["pullback_higher_low"] = True
    if not pd.isna(ma60) and close > ma60:
        score += 1
        reasons.append("60일선위")
        out["pullback_above_ma60"] = True
    if not pd.isna(ma20) and close >= ma20 * 0.97:
        score += 0.5
        reasons.append("20일선방어")
        out["pullback_above_ma20"] = True
    if not pd.isna(risk) and risk <= MAX_RISK_PCT:
        score += 0.5
        reasons.append("리스크양호")

    score = max(0, min(9, score))
    out["pullback_quality_score"] = round(score, 1)
    out["pullback_quality_ready"] = bool(score >= PULLBACK_QUALITY_READY_SCORE)
    out["pullback_quality_reason"] = ";".join(reasons)
    return out


def setup_scores(df):
    last = df.iloc[-1]
    close = last["Close"]
    ma20 = last["MA20"]
    ma60 = last["MA60"]
    ma10 = last.get("MA10", np.nan)

    high60 = df["High"].tail(60).max()
    pivot = df["High"].tail(PIVOT_LOOKBACK).max()
    recent_high = high60

    drawdown = (close / recent_high - 1) * 100 if recent_high > 0 else np.nan
    dist_to_pivot = (close / pivot - 1) * 100 if pivot > 0 else np.nan

    vol_recent = df["Volume"].tail(5).mean()
    vol_ma20 = last["VolMA20"]
    turnover_today = df["Turnover"].tail(1).mean()
    turnover_ma20 = last["TurnoverMA20"]

    volume_ratio = vol_recent / vol_ma20 if vol_ma20 and vol_ma20 > 0 else np.nan
    turnover_ratio = turnover_today / turnover_ma20 if turnover_ma20 and turnover_ma20 > 0 else np.nan

    volume_dryup = vol_ma20 > 0 and vol_recent < vol_ma20 * 0.8
    volume_explosion = not pd.isna(volume_ratio) and volume_ratio >= VOLUME_EXPLOSION_RATIO
    turnover_explosion = not pd.isna(turnover_ratio) and turnover_ratio >= TURNOVER_EXPLOSION_RATIO

    atr10 = last.get("ATR10", np.nan)
    atr30 = last.get("ATR30", np.nan)
    atr_dryup = not pd.isna(atr10) and not pd.isna(atr30) and atr30 > 0 and atr10 <= atr30 * 0.9

    near_ma20 = not pd.isna(ma20) and abs(close / ma20 - 1) * 100 <= NEAR_MA20_PCT
    near_ma60 = not pd.isna(ma60) and abs(close / ma60 - 1) * 100 <= NEAR_MA60_PCT

    pullback_basic = PULLBACK_MIN_DD <= drawdown <= PULLBACK_MAX_DD and (near_ma20 or near_ma60) and volume_dryup
    breakout_near = abs(dist_to_pivot) <= BREAKOUT_NEAR_PCT or (-BREAKOUT_NEAR_PCT <= dist_to_pivot <= 1.5)

    vcp = detect_vcp(df)
    vcp_ready = vcp["vcp_ready"]
    breakout_today = close >= pivot and volume_explosion and turnover_explosion and vcp["vcp_score"] >= 3

    recent_low = df["Low"].tail(20).min()
    prelim_entry = close if pullback_basic else pivot
    prelim_stop = max(recent_low, close * 0.95) if pullback_basic else max(recent_low, pivot * 0.94)
    if prelim_stop <= 0 or prelim_stop >= prelim_entry:
        prelim_stop = prelim_entry * 0.95
    prelim_risk = (prelim_entry - prelim_stop) / prelim_entry * 100 if prelim_entry > 0 else np.nan

    pullback_quality = detect_pullback_quality(
        df=df,
        drawdown=drawdown,
        near_ma20=near_ma20,
        near_ma60=near_ma60,
        volume_dryup=volume_dryup,
        atr_dryup=atr_dryup,
        risk=prelim_risk,
    )
    pullback = bool(pullback_basic and pullback_quality["pullback_quality_score"] >= PULLBACK_QUALITY_READY_SCORE)

    score = 0
    reasons = []
    if breakout_today:
        score += 8
        reasons.append("오늘돌파")
    elif breakout_near and vcp_ready:
        score += 6
        reasons.append("VCP피봇근처")
    elif breakout_near:
        score += 2
        reasons.append("피봇근처_수축부족")

    if vcp_ready:
        score += 4
        reasons.append("VCP수축")
    elif vcp["vcp_score"] >= 3:
        score += 2
        reasons.append("VCP부분충족")

    if pullback:
        score += 6
        reasons.append("고품질눌림목")
    elif pullback_basic:
        score += 3
        reasons.append("눌림목_품질부족")

    if volume_dryup:
        score += 3
        reasons.append("거래량감소")
    if atr_dryup:
        score += 2
        reasons.append("ATR감소")
    if volume_explosion:
        score += 3
        reasons.append("거래량증가")
    if turnover_explosion:
        score += 2
        reasons.append("거래대금증가")

    if pullback:
        entry = close
        stop = max(recent_low, close * 0.95)
        trade_type = "PULLBACK"
    else:
        entry = pivot
        stop = max(recent_low, entry * 0.94)
        trade_type = "BREAKOUT"

    if stop <= 0:
        stop = entry * 0.95
    if stop >= entry:
        stop = entry * 0.95

    risk_amount = entry - stop
    risk = (risk_amount / entry) * 100 if entry > 0 else np.nan
    target_1r = entry + risk_amount
    target_2r = entry + risk_amount * 2
    target_3r = entry + risk_amount * 3
    breakeven_trigger = target_1r
    breakeven_stop = entry

    if pullback:
        take_profit_plan = "1R 도달 시 본전스탑, 피봇/전고점 부근 20~30% 익절, 2R 추가익절, 잔여 20일선 추적"
        trailing_rule = "잔여물량은 20일선 이탈 또는 피봇 재이탈 시 정리"
    else:
        take_profit_plan = "1R 도달 시 본전스탑, 1.5~2R에서 30~50% 익절, 잔여 10일/20일선 추적"
        trailing_rule = "피봇 재이탈 시 실패돌파로 정리, 잔여물량은 10일선 또는 20일선 이탈 시 정리"

    ret5 = safe_return(df["Close"].dropna(), 5) * 100
    ret10 = safe_return(df["Close"].dropna(), 10) * 100
    ma20_gap = (close / ma20 - 1) * 100 if ma20 and ma20 > 0 else np.nan

    overheat = False
    overheat_reasons = []
    if not pd.isna(ret5) and ret5 >= OVERHEAT_5D_RETURN:
        overheat = True
        overheat_reasons.append("5일급등")
    if not pd.isna(ret10) and ret10 >= OVERHEAT_10D_RETURN:
        overheat = True
        overheat_reasons.append("10일급등")
    if not pd.isna(ma20_gap) and ma20_gap >= OVERHEAT_MA20_GAP:
        overheat = True
        overheat_reasons.append("MA20과열")
    if risk > AVOID_RISK_PCT:
        overheat = True
        overheat_reasons.append("리스크과대")

    return {
        "setup_score": min(score, 26),
        "setup_reason": ";".join(reasons),
        "trade_type": trade_type,
        "entry": entry,
        "pivot": pivot,
        "recent_high": recent_high,
        "drawdown_pct": drawdown,
        "dist_to_pivot_pct": dist_to_pivot,
        "pullback_basic": bool(pullback_basic),
        "pullback_ready": bool(pullback and risk <= MAX_RISK_PCT),
        "breakout_ready": bool(breakout_near and vcp_ready and risk <= MAX_RISK_PCT),
        "breakout_today": bool(breakout_today and risk <= MAX_RISK_PCT),
        "volume_dryup": bool(volume_dryup),
        "atr_dryup": bool(atr_dryup),
        "volume_ratio": volume_ratio,
        "turnover_ratio": turnover_ratio,
        "ma10": ma10,
        "ma20": ma20,
        "ma60": ma60,
        "stop": stop,
        "risk_amount": risk_amount,
        "risk_pct": risk,
        "risk_grade": risk_grade(risk),
        "target_1r": target_1r,
        "target_2r": target_2r,
        "target_3r": target_3r,
        "breakeven_trigger": breakeven_trigger,
        "breakeven_stop": breakeven_stop,
        "take_profit_plan": take_profit_plan,
        "trailing_rule": trailing_rule,
        "ret5_pct": ret5,
        "ret10_pct": ret10,
        "ma20_gap_pct": ma20_gap,
        "overheat": bool(overheat),
        "overheat_reason": ";".join(overheat_reasons),
        **vcp,
        **pullback_quality,
    }


# ============================================================
# Main analysis
# ============================================================
def analyze():
    end_date = get_recent_business_day()
    start_date = add_days_ago(end_date, 430)
    supply_start = add_days_ago(end_date, 35)
    supply5_start = add_days_ago(end_date, 10)
    today_label = datetime.now(KST).strftime("%Y-%m-%d")

    print(f"🇰🇷 K-Minervini v2.5 실행일: {today_label}, 기준거래일: {end_date}")
    universe = build_liquid_universe(end_date)
    tickers = universe["ticker"].tolist()

    print("💰 외국인/기관 20일 순매수 수집")
    foreign = get_net_purchase_by_investor(supply_start, end_date, "외국인")
    inst = get_net_purchase_by_investor(supply_start, end_date, "기관합계")
    supply = pd.merge(foreign, inst, on="ticker", how="outer")

    rows = []
    print(f"📈 가격 데이터 및 점수 계산 시작: {len(tickers)}개")

    for idx, ticker in enumerate(tickers, start=1):
        name = universe.loc[universe["ticker"] == ticker, "name"].iloc[0]
        market = universe.loc[universe["ticker"] == ticker, "market"].iloc[0]
        try:
            df = get_history(ticker, start_date, end_date)
            if df is None:
                continue

            last = df.iloc[-1]
            close = last["Close"]
            avg_turnover = last["TurnoverMA20"]
            if close < MIN_PRICE or pd.isna(avg_turnover) or avg_turnover < MIN_AVG_TURNOVER:
                continue

            close_series = df["Close"].dropna()
            r3 = safe_return(close_series, 63)
            r6 = safe_return(close_series, 126)
            r12 = safe_return(close_series, 252)
            weighted_return = r3 * 0.4 + r6 * 0.3 + r12 * 0.3

            ts, trend_reasons, dd52, up_low = trend_score(df)
            liq = liquidity_score(avg_turnover)
            setup = setup_scores(df)

            rows.append(
                {
                    "ticker": ticker,
                    "name": name,
                    "market": market,
                    "close": close,
                    "avg_turnover_20d": avg_turnover,
                    "trend_score": ts,
                    "trend_reason": trend_reasons,
                    "liquidity_score": liq,
                    "r3_pct": r3 * 100,
                    "r6_pct": r6 * 100,
                    "r12_pct": r12 * 100,
                    "weighted_return": weighted_return,
                    "drawdown_52w_pct": dd52,
                    "up_from_52w_low_pct": up_low,
                    **setup,
                }
            )
        except Exception as e:
            print(f"⚠️ {ticker} {name} 분석 실패: {e}")

        if idx % 50 == 0:
            print(f"   ↳ 진행 {idx}/{len(tickers)}")
        time.sleep(SLEEP_SEC + random.uniform(0, 0.05))

    if not rows:
        raise RuntimeError("분석 결과가 없습니다.")

    result = pd.DataFrame(rows)
    result = result.merge(supply, on="ticker", how="left")

    for col in ["외국인_net", "기관합계_net"]:
        if col not in result.columns:
            result[col] = 0
        result[col] = result[col].fillna(0)

    result["rs_percentile"] = result["weighted_return"].rank(pct=True) * 100
    result["rs_score"] = (result["rs_percentile"] / 100 * 20).round(1)
    result["supply_base"] = result["avg_turnover_20d"] * 20
    result["foreign_score"] = result.apply(lambda r: supply_score(r["외국인_net"], r["supply_base"]), axis=1)
    result["institution_score"] = result.apply(lambda r: institution_score(r["기관합계_net"], r["supply_base"]), axis=1)

    result["total_score"] = (
        result["trend_score"]
        + result["rs_score"]
        + result["foreign_score"]
        + result["institution_score"]
        + result["liquidity_score"]
        + result["setup_score"]
    ).round(1)

    result = result.sort_values(["total_score", "rs_percentile", "avg_turnover_20d"], ascending=False).copy()

    detail_target = result.head(SUPPLY_DETAIL_LIMIT).copy()
    detail_rows = []
    print(f"🔎 최근 5일 수급 연속성 조회: 상위 {len(detail_target)}개")
    for _, row in detail_target.iterrows():
        ticker = row["ticker"]
        d = get_recent_supply_counts(ticker, supply5_start, end_date)
        d["ticker"] = ticker
        detail_rows.append(d)
        time.sleep(SLEEP_SEC + 0.05)

    if detail_rows:
        detail_df = pd.DataFrame(detail_rows)
        result = result.merge(detail_df, on="ticker", how="left")

    for col in ["foreign_buy_days_5d", "institution_buy_days_5d", "co_buy_days_5d", "foreign_net_5d", "institution_net_5d"]:
        if col not in result.columns:
            result[col] = 0
        result[col] = result[col].fillna(0)

    result["supply_momentum_score"] = 0
    result.loc[result["foreign_buy_days_5d"] >= 3, "supply_momentum_score"] += 2
    result.loc[result["institution_buy_days_5d"] >= 3, "supply_momentum_score"] += 2
    result.loc[result["co_buy_days_5d"] >= 2, "supply_momentum_score"] += 1
    result["total_score"] = (result["total_score"] + result["supply_momentum_score"]).round(1)

    # Classification
    result["category"] = "WATCH"
    result.loc[(result["total_score"] >= PULLBACK_SCORE) & (result["pullback_ready"]), "category"] = "PULLBACK_READY"
    result.loc[(result["total_score"] >= BREAKOUT_SCORE) & (result["breakout_ready"]), "category"] = "BREAKOUT_READY"
    result.loc[(result["total_score"] >= BREAKOUT_TODAY_SCORE) & (result["breakout_today"]), "category"] = "BREAKOUT_TODAY"
    result.loc[(result["total_score"] >= AVOID_MIN_SCORE) & (result["risk_pct"] > AVOID_RISK_PCT), "category"] = "AVOID"
    result.loc[(result["total_score"] >= AVOID_MIN_SCORE) & (result["overheat"]) & (result["category"] == "WATCH"), "category"] = "AVOID"

    result = result[result["total_score"] >= WATCH_SCORE].copy()
    result = result.sort_values(["category", "total_score", "rs_percentile"], ascending=[True, False, False])

    result.to_csv(f"korea_all_candidates_{today_label}.csv", index=False, encoding="utf-8-sig")
    result[result["category"] == "BREAKOUT_TODAY"].to_csv(f"korea_breakout_today_{today_label}.csv", index=False, encoding="utf-8-sig")
    result[result["category"] == "BREAKOUT_READY"].to_csv(f"korea_breakout_{today_label}.csv", index=False, encoding="utf-8-sig")
    result[result["category"] == "PULLBACK_READY"].to_csv(f"korea_pullback_{today_label}.csv", index=False, encoding="utf-8-sig")
    result[result["category"] == "WATCH"].to_csv(f"korea_watch_{today_label}.csv", index=False, encoding="utf-8-sig")
    result[result["category"] == "AVOID"].to_csv(f"korea_avoid_{today_label}.csv", index=False, encoding="utf-8-sig")

    return result, today_label, end_date, len(tickers)


# ============================================================
# Telegram formatting and Watch detail file
# ============================================================
def format_section(df, title, limit=8):
    if df.empty:
        return f"{title}\n없음"

    lines = [title]
    for _, r in df.head(limit).iterrows():
        foreign_mark = "+" if r.get("외국인_net", 0) > 0 else "-"
        inst_mark = "+" if r.get("기관합계_net", 0) > 0 else "-"

        special = ""
        if r.get("overheat", False):
            special = f"\n  ⚠️ 과열/주의: {r.get('overheat_reason', '')}"

        volume_ratio = r.get("volume_ratio", np.nan)
        turnover_ratio = r.get("turnover_ratio", np.nan)
        volume_ratio_text = "-" if pd.isna(volume_ratio) else f"{volume_ratio:.2f}"
        turnover_ratio_text = "-" if pd.isna(turnover_ratio) else f"{turnover_ratio:.2f}"

        lines.append(
            f"• {r['name']}({r['ticker']}) [{r['market']}] 점수 {r['total_score']:.1f}\n"
            f"  현재가 {fmt_int(r['close'])}원 | 진입 {fmt_int(r['entry'])}원 | 피봇 {fmt_int(r['pivot'])}원 | 손절 {fmt_int(r['stop'])}원\n"
            f"  리스크 {r['risk_pct']:.1f}%({r['risk_grade']}) | RS {r['rs_percentile']:.0f} | VCP {r.get('vcp_score', 0):.1f}/7 | PB품질 {r.get('pullback_quality_score', 0):.1f}/9\n"
            f"  1R {fmt_int(r['target_1r'])}원 | 2R {fmt_int(r['target_2r'])}원 | 3R {fmt_int(r['target_3r'])}원\n"
            f"  매도전략: 1R 도달 시 본전스탑, 2R 부근 일부익절, 잔여 추세추종\n"
            f"  52주고점대비 {fmt_pct(r['drawdown_52w_pct'])} | 최근고점대비 {fmt_pct(r['drawdown_pct'])}\n"
            f"  외국인20일 {foreign_mark} {fmt_krw_eok(r.get('외국인_net', 0))} | 기관20일 {inst_mark} {fmt_krw_eok(r.get('기관합계_net', 0))}\n"
            f"  최근5일: 외국인 {int(r.get('foreign_buy_days_5d', 0))}/5일, 기관 {int(r.get('institution_buy_days_5d', 0))}/5일, 동시 {int(r.get('co_buy_days_5d', 0))}/5일\n"
            f"  거래대금 {fmt_krw_eok(r['avg_turnover_20d'])} | 거래량비율 {volume_ratio_text} | 거래대금비율 {turnover_ratio_text}\n"
            f"  VCP: {r.get('vcp_reason', '')} | 최종수축 {fmt_pct(r.get('vcp_range_final', np.nan))}\n"
            f"  PB: {r.get('pullback_quality_reason', '')}\n"
            f"  메모: {r.get('setup_reason', '')}{special}"
        )

    if len(df) > limit:
        lines.append(f"외 {len(df) - limit}개는 CSV 확인")
    return "\n".join(lines)


def format_watch_card_section(df, title, limit=5):
    if df.empty:
        return f"{title}\n없음"

    lines = [title, ""]
    for idx, (_, r) in enumerate(df.head(limit).iterrows(), start=1):
        name = str(r.get("name", ""))
        score = float(r.get("total_score", 0))
        rs = float(r.get("rs_percentile", 0))
        vcp = float(r.get("vcp_score", 0))
        pb = float(r.get("pullback_quality_score", 0))
        risk = r.get("risk_pct", np.nan)
        risk_text = "-" if pd.isna(risk) else f"{float(risk):.1f}%"

        foreign_net = r.get("외국인_net", 0)
        inst_net = r.get("기관합계_net", 0)
        foreign_days = int(r.get("foreign_buy_days_5d", 0))
        inst_days = int(r.get("institution_buy_days_5d", 0))

        lines.append(
            f"{circled_number(idx)} {name}\n"
            f"점수 {score:.1f} | RS {rs:.0f}\n"
            f"VCP {vcp:.1f} | PB {pb:.1f} | Risk {risk_text}\n"
            f"외인 {fmt_eok_plain(foreign_net)} | 기관 {fmt_eok_plain(inst_net)}\n"
            f"최근5일 F{foreign_days} I{inst_days}"
        )
    return "\n\n".join(lines)


def build_watch_summary_file(watch_df, today_label, end_date):
    file_name = f"korea_watch_summary_{today_label}.txt"

    lines = []
    lines.append("🇰🇷 K-Minervini Watch 전체 상세 정리")
    lines.append(f"기준일: {today_label} / KRX 거래일: {end_date}")
    lines.append("=" * 70)
    lines.append("")
    lines.append("※ 본 파일은 투자 추천이 아니라 Watch 후보 정리용입니다.")
    lines.append("※ 실제 매매 전 차트, 수급, 공시, 실적을 반드시 확인하세요.")
    lines.append("")
    lines.append("해석 기준")
    lines.append("- VCP 4.5 이상: 돌파대기 근접")
    lines.append("- PB품질 5.5 이상: 눌림목 근접")
    lines.append("- Risk 8% 이하: 진입 가능 범위")
    lines.append("- 외인/기관 금액: 최근 약 20일 누적 순매수, 억원")
    lines.append("- 최근5일 F/I: 최근 5거래일 중 순매수 일수")
    lines.append("=" * 70)
    lines.append("")

    if watch_df.empty:
        lines.append("Watch 종목 없음")
    else:
        show = watch_df.sort_values(
            ["total_score", "rs_percentile", "vcp_score", "pullback_quality_score"],
            ascending=False,
        ).copy()

        for idx, (_, r) in enumerate(show.iterrows(), start=1):
            name = str(r.get("name", ""))
            ticker = str(r.get("ticker", ""))
            market = str(r.get("market", ""))
            score = float(r.get("total_score", 0))
            rs = float(r.get("rs_percentile", 0))
            vcp = float(r.get("vcp_score", 0))
            pb = float(r.get("pullback_quality_score", 0))
            risk = r.get("risk_pct", np.nan)
            risk_text = "-" if pd.isna(risk) else f"{float(risk):.1f}%"

            close = r.get("close", np.nan)
            entry = r.get("entry", np.nan)
            pivot = r.get("pivot", np.nan)
            stop = r.get("stop", np.nan)
            target_1r = r.get("target_1r", np.nan)
            target_2r = r.get("target_2r", np.nan)
            target_3r = r.get("target_3r", np.nan)
            avg_turnover = r.get("avg_turnover_20d", np.nan)
            volume_ratio = r.get("volume_ratio", np.nan)
            turnover_ratio = r.get("turnover_ratio", np.nan)
            foreign_net = r.get("외국인_net", 0)
            inst_net = r.get("기관합계_net", 0)
            foreign_days = int(r.get("foreign_buy_days_5d", 0))
            inst_days = int(r.get("institution_buy_days_5d", 0))
            co_days = int(r.get("co_buy_days_5d", 0))
            foreign_net_5d = r.get("foreign_net_5d", 0)
            institution_net_5d = r.get("institution_net_5d", 0)
            r3 = r.get("r3_pct", np.nan)
            r6 = r.get("r6_pct", np.nan)
            r12 = r.get("r12_pct", np.nan)
            dd52 = r.get("drawdown_52w_pct", np.nan)
            dd_recent = r.get("drawdown_pct", np.nan)
            dist_to_pivot = r.get("dist_to_pivot_pct", np.nan)
            ma10 = r.get("ma10", np.nan)
            ma20 = r.get("ma20", np.nan)
            ma60 = r.get("ma60", np.nan)
            ret5 = r.get("ret5_pct", np.nan)
            ret10 = r.get("ret10_pct", np.nan)
            ma20_gap = r.get("ma20_gap_pct", np.nan)
            vcp_range_early = r.get("vcp_range_early", np.nan)
            vcp_range_mid = r.get("vcp_range_mid", np.nan)
            vcp_range_final = r.get("vcp_range_final", np.nan)
            vcp_volume_ratio = r.get("vcp_volume_ratio", np.nan)
            vcp_atr_ratio = r.get("vcp_atr_ratio", np.nan)
            setup_reason = r.get("setup_reason", "")
            trend_reason = r.get("trend_reason", "")
            vcp_reason = r.get("vcp_reason", "")
            pb_reason = r.get("pullback_quality_reason", "")
            overheat = r.get("overheat", False)
            overheat_reason = r.get("overheat_reason", "")
            take_profit_plan = r.get("take_profit_plan", "")
            trailing_rule = r.get("trailing_rule", "")

            lines.append("")
            lines.append("=" * 70)
            lines.append(f"[{idx}] {name}({ticker}) [{market}]")
            lines.append("=" * 70)
            lines.append("")
            lines.append("[종합]")
            lines.append(f"점수 {score:.1f} | RS {rs:.0f} | VCP {vcp:.1f}/7 | PB품질 {pb:.1f}/9 | Risk {risk_text}")
            lines.append("")
            lines.append("[가격 / 진입 / 손절]")
            lines.append(f"현재가 {fmt_int(close)}원 | 진입 {fmt_int(entry)}원 | 피봇 {fmt_int(pivot)}원 | 손절 {fmt_int(stop)}원")
            lines.append("")
            lines.append("[익절 목표]")
            lines.append(f"1R {fmt_int(target_1r)}원 | 2R {fmt_int(target_2r)}원 | 3R {fmt_int(target_3r)}원")
            lines.append("")
            lines.append("[수급]")
            lines.append(f"외국인20일 {fmt_krw_eok(foreign_net)} | 기관20일 {fmt_krw_eok(inst_net)}")
            lines.append(f"외국인5일 {fmt_krw_eok(foreign_net_5d)} | 기관5일 {fmt_krw_eok(institution_net_5d)}")
            lines.append(f"최근5일 순매수일수: 외국인 F{foreign_days}/5 | 기관 I{inst_days}/5 | 동시 {co_days}/5")
            lines.append("")
            lines.append("[거래대금 / 거래량]")
            lines.append(f"20일 평균 거래대금 {fmt_krw_eok(avg_turnover)} | 거래량비율 {fmt_float(volume_ratio)} | 거래대금비율 {fmt_float(turnover_ratio)}")
            lines.append("")
            lines.append("[수익률 / 위치]")
            lines.append(f"3개월 {fmt_pct(r3)} | 6개월 {fmt_pct(r6)} | 12개월 {fmt_pct(r12)}")
            lines.append(f"52주고점대비 {fmt_pct(dd52)} | 최근고점대비 {fmt_pct(dd_recent)} | 피봇대비 {fmt_pct(dist_to_pivot)}")
            lines.append("")
            lines.append("[이동평균]")
            lines.append(f"MA10 {fmt_int(ma10)}원 | MA20 {fmt_int(ma20)}원 | MA60 {fmt_int(ma60)}원")
            lines.append("")
            lines.append("[단기 과열]")
            lines.append(f"5일 상승률 {fmt_pct(ret5)} | 10일 상승률 {fmt_pct(ret10)} | 20일선 이격 {fmt_pct(ma20_gap)}")
            lines.append(f"과열/주의: {overheat_reason if overheat else '없음'}")
            lines.append("")
            lines.append("[VCP 수축패턴]")
            lines.append(f"초기수축 {fmt_pct(vcp_range_early)} | 중간수축 {fmt_pct(vcp_range_mid)} | 최종수축 {fmt_pct(vcp_range_final)}")
            lines.append(f"VCP 거래량비율 {fmt_float(vcp_volume_ratio)} | VCP ATR비율 {fmt_float(vcp_atr_ratio)}")
            lines.append(f"VCP 판단: {vcp_reason}")
            lines.append("")
            lines.append("[눌림목 품질]")
            lines.append(f"PB 판단: {pb_reason}")
            lines.append("")
            lines.append("[Setup / Trend]")
            lines.append(f"Setup: {setup_reason}")
            lines.append(f"Trend: {trend_reason}")
            lines.append("")
            lines.append("[매도전략]")
            lines.append(f"익절: {take_profit_plan}")
            lines.append(f"추적청산: {trailing_rule}")
            lines.append("")
            lines.append("[간단 판단 메모]")
            if vcp >= VCP_READY_SCORE and not pd.isna(risk) and risk <= MAX_RISK_PCT:
                lines.append("→ 돌파대기 근접 후보. 피봇 돌파와 거래량 증가 확인 필요.")
            elif pb >= PULLBACK_QUALITY_READY_SCORE and not pd.isna(risk) and risk <= MAX_RISK_PCT:
                lines.append("→ 눌림목 근접 후보. 지지 확인과 수급 유지 확인 필요.")
            elif not pd.isna(risk) and risk > MAX_RISK_PCT:
                lines.append("→ 리스크가 높아 진입 보류가 적절.")
            else:
                lines.append("→ 아직 Watch 단계. 추가 수축, 눌림목 완성, 수급 개선 대기.")
            lines.append("")
            lines.append("=" * 70)
            lines.append(f"[{idx}] {name}({ticker}) 끝")
            lines.append("=" * 70)
            lines.append("")

    with open(file_name, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return file_name


def build_message(result, today_label, end_date, universe_count):
    bt = result[result["category"] == "BREAKOUT_TODAY"]
    br = result[result["category"] == "BREAKOUT_READY"]
    pb = result[result["category"] == "PULLBACK_READY"]
    watch = result[result["category"] == "WATCH"]
    avoid = result[result["category"] == "AVOID"]

    near_breakout = watch[
        (watch["vcp_score"] >= NEAR_BREAKOUT_VCP)
        & (watch["risk_pct"] <= MAX_RISK_PCT)
        & (watch["total_score"] >= NEAR_READY_SCORE)
    ].copy()
    near_breakout = near_breakout.sort_values(["vcp_score", "total_score", "rs_percentile"], ascending=False)
    near_breakout_tickers = set(near_breakout["ticker"].astype(str).tolist())

    near_pullback = watch[
        (watch["pullback_quality_score"] >= NEAR_PULLBACK_PB)
        & (watch["risk_pct"] <= MAX_RISK_PCT)
        & (watch["total_score"] >= NEAR_READY_SCORE)
        & (~watch["ticker"].astype(str).isin(near_breakout_tickers))
    ].copy()
    near_pullback = near_pullback.sort_values(["pullback_quality_score", "total_score", "rs_percentile"], ascending=False)

    near_count = len(near_breakout.head(NEAR_READY_LIMIT)) + len(near_pullback.head(NEAR_READY_LIMIT))
    other_watch_count = max(0, len(watch) - near_count)

    msg = (
        f"🇰🇷 K-Minervini v2.5 결과\n"
        f"기준일: {today_label} / KRX 거래일: {end_date}\n"
        f"------------------------------------\n"
        f"분석대상: 유동성 상위 {universe_count}개\n"
        f"🚀 Breakout Today: {len(bt)}개\n"
        f"🔥 Breakout Ready: {len(br)}개\n"
        f"🟢 Pullback Ready: {len(pb)}개\n"
        f"👀 Watch: {len(watch)}개\n"
        f"⚠️ Avoid: {len(avoid)}개\n"
        f"------------------------------------\n\n"
        f"{format_section(bt, '🚀 Breakout Today', 6)}\n\n"
        f"{format_section(br, '🔥 Breakout Ready', 6)}\n\n"
        f"{format_section(pb, '🟢 Pullback Ready', 8)}\n\n"
        f"{format_watch_card_section(near_breakout, '⏳ Near Breakout TOP5', NEAR_READY_LIMIT)}\n\n"
        f"{format_watch_card_section(near_pullback, '⏳ Near Pullback TOP5', NEAR_READY_LIMIT)}\n\n"
        f"👀 기타 Watch\n"
        f"{other_watch_count}개는 첨부 정리파일 또는 CSV 확인\n\n"
        f"{format_section(avoid, '⚠️ Avoid / 과열주의', 4)}\n\n"
        f"------------------------------------\n"
        f"※ 투자 추천이 아닌 자동 선별 결과입니다.\n"
        f"※ Near Breakout: VCP 4.0 이상, Risk 8% 이하, 점수 75 이상\n"
        f"※ Near Pullback: PB 5.0 이상, Risk 8% 이하, 점수 75 이상\n"
        f"※ Watch 전체 상세는 첨부 TXT 파일과 CSV에서 확인하세요.\n"
        f"※ 실제 매매 전 차트, 수급, 공시, 실적을 반드시 확인하세요."
    )
    return msg


def main():
    try:
        result, today_label, end_date, universe_count = analyze()
        msg = build_message(result, today_label, end_date, universe_count)
        send_telegram_message(msg)

        watch = result[result["category"] == "WATCH"].copy()
        watch_file = build_watch_summary_file(watch, today_label, end_date)
        send_telegram_document(watch_file, caption=f"👀 Watch 전체 상세 정리파일 / {today_label}")

        print("🎯 K-Minervini v2.5 완료")
    except Exception as e:
        print(traceback.format_exc())
        send_telegram_message(f"❌ K-Minervini v2.5 실행 실패\n{e}\n\n로그를 GitHub Actions에서 확인하세요.")
        raise


if __name__ == "__main__":
    main()
