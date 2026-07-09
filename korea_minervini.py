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
# K-Minervini v1 Settings
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# GitHub Actions runtime control
MAX_UNIVERSE = int(os.environ.get("KOREA_MAX_UNIVERSE", "600"))
SLEEP_SEC = float(os.environ.get("KOREA_SLEEP_SEC", "0.15"))

# Liquidity / price filters
MIN_PRICE = 2000
MIN_AVG_TURNOVER = 3_000_000_000      # 20일 평균 거래대금 30억
GOOD_TURNOVER = 5_000_000_000         # 50억
STRONG_TURNOVER = 10_000_000_000      # 100억

# Score thresholds
BREAKOUT_SCORE = 80
PULLBACK_SCORE = 75
WATCH_SCORE = 70

# Pullback settings
PULLBACK_MIN_DD = -12.0
PULLBACK_MAX_DD = -3.0
NEAR_MA20_PCT = 4.0
NEAR_MA60_PCT = 6.0

# Breakout settings
PIVOT_LOOKBACK = 30
BREAKOUT_NEAR_PCT = 3.0
MAX_RISK_PCT = 8.0

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
                json={
                    "chat_id": CHAT_ID,
                    "text": chunk,
                },
                timeout=20,
            )

            if res.status_code != 200:
                ok = False
                print(f"⚠️ 텔레그램 전송 실패: {res.status_code} {res.text}")

        return ok

    except Exception as e:
        print(f"⚠️ 텔레그램 전송 에러: {e}")
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


def fmt_krw_eok(x):
    """
    원 단위 금액을 억원 단위로 변환해서 표시합니다.
    예:
    12,345,000,000원 -> 123.5억
    -3,456,000,000원 -> -34.6억
    """
    try:
        if pd.isna(x):
            return "-"
        value = float(x) / 100_000_000
        return f"{value:,.1f}억"
    except Exception:
        return "-"


def safe_col(df, candidates, default=None):
    for c in candidates:
        if c in df.columns:
            return c
    return default


# ============================================================
# Date helpers
# ============================================================
def date_str(dt):
    return dt.strftime("%Y%m%d")


def get_recent_business_day(max_back=10):
    """
    최근 KRX 거래일을 찾습니다.
    장 마감 전, 주말, 공휴일에도 최대한 안정적으로 동작합니다.
    """
    now = datetime.now(KST)

    for i in range(max_back):
        d = now - timedelta(days=i)
        ds = date_str(d)

        try:
            df = stock.get_market_ohlcv_by_ticker(ds, market="KOSPI")
            if df is not None and not df.empty:
                return ds
        except Exception:
            pass

        time.sleep(0.1)

    raise RuntimeError("최근 거래일을 찾지 못했습니다.")


def add_days_ago(base_yyyymmdd, days):
    base = datetime.strptime(base_yyyymmdd, "%Y%m%d")
    return date_str(base - timedelta(days=days))


# ============================================================
# Universe and market data
# ============================================================
def get_market_snapshot(end_date, market):
    """
    특정 일자의 KOSPI/KOSDAQ 전체 종목 OHLCV snapshot.
    """
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

    # 1차 필터: 당일 거래대금 기준 유동성 상위 종목
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
        "시가": "Open",
        "고가": "High",
        "저가": "Low",
        "종가": "Close",
        "거래량": "Volume",
        "거래대금": "Turnover",
        "등락률": "ChangePct",
    }

    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    required = ["Open", "High", "Low", "Close", "Volume"]

    if not all(c in df.columns for c in required):
        return None

    if "Turnover" not in df.columns:
        df["Turnover"] = df["Close"] * df["Volume"]

    df = df.dropna(subset=["Close"]).copy()

    if len(df) < 160:
        return None

    for c in ["Open", "High", "Low", "Close", "Volume", "Turnover"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

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
# Supply data: foreign / institution
# ============================================================
def get_net_purchase_by_investor(start_date, end_date, investor):
    """
    KOSPI + KOSDAQ 투자자별 순매수 금액을 ticker 단위로 병합.
    investor 예:
    - 외국인
    - 기관합계
    """
    frames = []

    for market in ["KOSPI", "KOSDAQ"]:
        try:
            df = stock.get_market_net_purchases_of_equities(
                start_date,
                end_date,
                market,
                investor,
            )

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

    net_col = safe_col(
        merged,
        ["순매수거래대금", "순매수", "순매수금액", "거래대금"],
    )

    if net_col is None:
        numeric_cols = merged.select_dtypes(include=[np.number]).columns.tolist()
        net_col = numeric_cols[-1] if numeric_cols else None

    if net_col is None:
        merged[f"{investor}_net"] = 0
    else:
        merged[f"{investor}_net"] = pd.to_numeric(
            merged[net_col],
            errors="coerce",
        ).fillna(0)

    return merged[["ticker", f"{investor}_net"]]


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
    """
    외국인 수급 점수.
    strong_base = 20일 평균 거래대금 * 20
    """
    if pd.isna(value):
        return 0

    if value > strong_base * 0.03:
        return 15

    if value > 0:
        return 10

    return 0


def institution_score(value, strong_base):
    """
    기관 수급 점수.
    """
    if pd.isna(value):
        return 0

    if value > strong_base * 0.02:
        return 10

    if value > 0:
        return 6

    return 0


def setup_scores(df):
    """
    Breakout / Pullback 판단용 setup score.
    """
    last = df.iloc[-1]
    close = last["Close"]
    ma20 = last["MA20"]
    ma60 = last["MA60"]

    high60 = df["High"].tail(60).max()
    pivot = df["High"].tail(PIVOT_LOOKBACK).max()
    recent_high = high60

    drawdown = (close / recent_high - 1) * 100 if recent_high > 0 else np.nan
    dist_to_pivot = (close / pivot - 1) * 100 if pivot > 0 else np.nan

    vol_recent = df["Volume"].tail(5).mean()
    vol_ma20 = last["VolMA20"]

    volume_dryup = vol_ma20 > 0 and vol_recent < vol_ma20 * 0.8

    atr10 = last.get("ATR10", np.nan)
    atr30 = last.get("ATR30", np.nan)

    atr_dryup = (
        not pd.isna(atr10)
        and not pd.isna(atr30)
        and atr30 > 0
        and atr10 <= atr30 * 0.9
    )

    near_ma20 = not pd.isna(ma20) and abs(close / ma20 - 1) * 100 <= NEAR_MA20_PCT
    near_ma60 = not pd.isna(ma60) and abs(close / ma60 - 1) * 100 <= NEAR_MA60_PCT

    pullback = (
        PULLBACK_MIN_DD <= drawdown <= PULLBACK_MAX_DD
        and (near_ma20 or near_ma60)
        and volume_dryup
    )

    breakout_near = (
        abs(dist_to_pivot) <= BREAKOUT_NEAR_PCT
        or (-BREAKOUT_NEAR_PCT <= dist_to_pivot <= 1.5)
    )

    score = 0
    reasons = []

    if breakout_near:
        score += 5
        reasons.append("피봇근처")

    if pullback:
        score += 5
        reasons.append("눌림목")

    if volume_dryup:
        score += 3
        reasons.append("거래량감소")

    if atr_dryup:
        score += 2
        reasons.append("ATR감소")

    recent_low = df["Low"].tail(20).min()

    if pullback:
        stop = min(recent_low, close * 0.97)
    else:
        stop = max(recent_low, close * 0.94)

    if stop <= 0 or stop >= close:
        stop = close * 0.94

    risk = (close / stop - 1) * 100

    return {
        "setup_score": min(score, 15),
        "setup_reason": ";".join(reasons),
        "pivot": pivot,
        "recent_high": recent_high,
        "drawdown_pct": drawdown,
        "dist_to_pivot_pct": dist_to_pivot,
        "pullback_ready": bool(pullback),
        "breakout_ready": bool(breakout_near and risk <= MAX_RISK_PCT),
        "volume_dryup": bool(volume_dryup),
        "atr_dryup": bool(atr_dryup),
        "stop": stop,
        "risk_pct": risk,
    }


# ============================================================
# Main analysis
# ============================================================
def analyze():
    end_date = get_recent_business_day()
    start_date = add_days_ago(end_date, 430)
    supply_start = add_days_ago(end_date, 35)
    today_label = datetime.now(KST).strftime("%Y-%m-%d")

    print(f"🇰🇷 K-Minervini v1 실행일: {today_label}, 기준거래일: {end_date}")

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

            row = {
                "ticker": ticker,
                "name": name,
                "market": market,
                "close": close,
                "avg_turnover_20d": avg_turnover,
                "trend_score": ts,
                "trend_reason": trend_reasons,
                "liquidity_score": liq,
                "setup_score": setup["setup_score"],
                "setup_reason": setup["setup_reason"],
                "r3_pct": r3 * 100,
                "r6_pct": r6 * 100,
                "r12_pct": r12 * 100,
                "weighted_return": weighted_return,
                "drawdown_52w_pct": dd52,
                "up_from_52w_low_pct": up_low,
                **setup,
            }

            rows.append(row)

        except Exception as e:
            print(f"⚠️ {ticker} {name} 분석 실패: {e}")

        if idx % 50 == 0:
            print(f"   ↳ 진행 {idx}/{len(tickers)}")

        time.sleep(SLEEP_SEC + random.uniform(0, 0.05))

    if not rows:
        raise RuntimeError("분석 결과가 없습니다.")

    result = pd.DataFrame(rows)
    result = result.merge(supply, on="ticker", how="left")

    if "외국인_net" not in result.columns:
        result["외국인_net"] = 0

    if "기관합계_net" not in result.columns:
        result["기관합계_net"] = 0

    result["외국인_net"] = result["외국인_net"].fillna(0)
    result["기관합계_net"] = result["기관합계_net"].fillna(0)

    # RS percentile score
    result["rs_percentile"] = result["weighted_return"].rank(pct=True) * 100
    result["rs_score"] = (result["rs_percentile"] / 100 * 20).round(1)

    # Supply score base = avg_turnover_20d * 20 days
    result["supply_base"] = result["avg_turnover_20d"] * 20

    result["foreign_score"] = result.apply(
        lambda r: supply_score(r["외국인_net"], r["supply_base"]),
        axis=1,
    )

    result["institution_score"] = result.apply(
        lambda r: institution_score(r["기관합계_net"], r["supply_base"]),
        axis=1,
    )

    result["total_score"] = (
        result["trend_score"]
        + result["rs_score"]
        + result["foreign_score"]
        + result["institution_score"]
        + result["liquidity_score"]
        + result["setup_score"]
    ).round(1)

    # Classify
    result["category"] = "WATCH"

    result.loc[
        (result["total_score"] >= PULLBACK_SCORE)
        & (result["pullback_ready"]),
        "category",
    ] = "PULLBACK_READY"

    result.loc[
        (result["total_score"] >= BREAKOUT_SCORE)
        & (result["breakout_ready"]),
        "category",
    ] = "BREAKOUT_READY"

    result = result[result["total_score"] >= WATCH_SCORE].copy()

    result = result.sort_values(
        ["total_score", "rs_percentile", "avg_turnover_20d"],
        ascending=False,
    )

    # Save CSVs
    result.to_csv(
        f"korea_all_candidates_{today_label}.csv",
        index=False,
        encoding="utf-8-sig",
    )

    result[result["category"] == "BREAKOUT_READY"].to_csv(
        f"korea_breakout_{today_label}.csv",
        index=False,
        encoding="utf-8-sig",
    )

    result[result["category"] == "PULLBACK_READY"].to_csv(
        f"korea_pullback_{today_label}.csv",
        index=False,
        encoding="utf-8-sig",
    )

    result[result["category"] == "WATCH"].to_csv(
        f"korea_watch_{today_label}.csv",
        index=False,
        encoding="utf-8-sig",
    )

    return result, today_label, end_date, len(tickers)


# ============================================================
# Telegram formatting
# ============================================================
def format_section(df, title, limit=10):
    if df.empty:
        return f"{title}\n없음"

    lines = [title]

    for _, r in df.head(limit).iterrows():
        foreign_mark = "+" if r.get("외국인_net", 0) > 0 else "-"
        inst_mark = "+" if r.get("기관합계_net", 0) > 0 else "-"

        lines.append(
            f"• {r['name']}({r['ticker']}) [{r['market']}] 점수 {r['total_score']:.1f}\n"
            f"  현재가 {fmt_int(r['close'])}원 | 피봇 {fmt_int(r['pivot'])}원 | 손절 {fmt_int(r['stop'])}원 | 리스크 {r['risk_pct']:.1f}%\n"
            f"  RS {r['rs_percentile']:.0f} | 52주고점대비 {fmt_pct(r['drawdown_52w_pct'])} | 최근고점대비 {fmt_pct(r['drawdown_pct'])}\n"
            f"  외국인 {foreign_mark} {fmt_krw_eok(r.get('외국인_net', 0))} | 기관 {inst_mark} {fmt_krw_eok(r.get('기관합계_net', 0))}\n"
            f"  20일평균거래대금 {fmt_krw_eok(r['avg_turnover_20d'])} | {r.get('setup_reason', '')}"
        )

    if len(df) > limit:
        lines.append(f"외 {len(df) - limit}개는 CSV 확인")

    return "\n".join(lines)


def build_message(result, today_label, end_date, universe_count):
    breakout = result[result["category"] == "BREAKOUT_READY"]
    pullback = result[result["category"] == "PULLBACK_READY"]
    watch = result[result["category"] == "WATCH"]

    msg = (
        f"🇰🇷 K-Minervini v1 결과\n"
        f"기준일: {today_label} / KRX 거래일: {end_date}\n"
        f"------------------------------------\n"
        f"분석대상: 유동성 상위 {universe_count}개\n"
        f"🔥 Breakout Ready: {len(breakout)}개\n"
        f"🟢 Pullback Ready: {len(pullback)}개\n"
        f"👀 Watch: {len(watch)}개\n"
        f"------------------------------------\n\n"
        f"{format_section(breakout, '🔥 Breakout Ready', 8)}\n\n"
        f"{format_section(pullback, '🟢 Pullback Ready', 8)}\n\n"
        f"{format_section(watch, '👀 Watch', 8)}\n\n"
        f"------------------------------------\n"
        f"※ 투자 추천이 아닌 자동 선별 결과입니다.\n"
        f"※ 한국형은 돌파보다 눌림목/수급/거래대금 확인이 중요합니다.\n"
        f"※ 실제 매수 전 차트, 뉴스, 공시, 실적, 시장 분위기를 확인하세요."
    )

    return msg


def main():
    try:
        result, today_label, end_date, universe_count = analyze()
        msg = build_message(result, today_label, end_date, universe_count)
        send_telegram_message(msg)
        print("🎯 K-Minervini v1 완료")

    except Exception as e:
        print(traceback.format_exc())
        send_telegram_message(
            f"❌ K-Minervini v1 실행 실패\n"
            f"{e}\n\n"
            f"로그를 GitHub Actions에서 확인하세요."
        )
        raise


if __name__ == "__main__":
    main()
