#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
K-Minervini v2.5 point-in-time backtest

핵심 원칙
1) 신호일 당일 고가를 피봇에서 제외한다.
2) 신호는 종가 확정 후 생성하고, 다음 거래일부터만 체결한다.
3) 데이터는 로컬 cache에 저장하여 중단 후 재실행할 수 있다.
4) 월별 과거 상장종목 목록의 합집합을 사용해 생존편향을 줄인다.
5) 매매비용, 포지션 한도, 계좌 위험 한도를 반영한다.

주의
- 투자 조언이 아니라 전략 검증용 연구 코드다.
- 일봉에서는 같은 날 손절과 목표가의 선후를 알 수 없으므로 기본값은 손절 우선이다.
- pykrx 데이터 제공 상태에 따라 일부 종목/날짜가 누락될 수 있다.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from pykrx import stock

# --------------------------- 기본 설정 ---------------------------
INITIAL_CAPITAL = 10_000_000
MAX_UNIVERSE = 600
MIN_PRICE = 2_000
MIN_AVG_TURNOVER = 3_000_000_000
GOOD_TURNOVER = 5_000_000_000
STRONG_TURNOVER = 10_000_000_000

BREAKOUT_TODAY_SCORE = 82
BREAKOUT_SCORE = 80
PULLBACK_SCORE = 75

PULLBACK_MIN_DD = -12.0
PULLBACK_MAX_DD = -3.0
NEAR_MA20_PCT = 4.0
NEAR_MA60_PCT = 6.0
PIVOT_LOOKBACK = 30
BREAKOUT_NEAR_PCT = 3.0
MAX_RISK_PCT = 8.0

VOLUME_EXPLOSION_RATIO = 1.5
TURNOVER_EXPLOSION_RATIO = 1.5
VCP_READY_SCORE = 4.5
PULLBACK_QUALITY_READY_SCORE = 5.5

DEFAULT_COMMISSION = 0.00015       # 편도 0.015%
DEFAULT_SELL_TAX = 0.0020          # 단순 기본값. CLI에서 변경 가능
DEFAULT_SLIPPAGE = 0.0010          # 편도 0.10%

CACHE_VERSION = "1.0"


@dataclass
class Position:
    ticker: str
    name: str
    market: str
    signal_type: str
    signal_date: str
    entry_date: str
    entry_price: float
    shares: int
    remaining: int
    stop: float
    initial_stop: float
    risk_per_share: float
    target_1r: float
    target_2r: float
    half_sold: bool = False
    one_r_touched: bool = False
    entry_cost: float = 0.0
    realized_pnl: float = 0.0


@dataclass
class Order:
    ticker: str
    name: str
    market: str
    signal_type: str
    signal_date: str
    valid_until_idx: int
    trigger: float
    stop_from_signal: float
    score: float


def ymd(x) -> str:
    return pd.Timestamp(x).strftime("%Y%m%d")


def safe_col(df: pd.DataFrame, names: List[str]) -> Optional[str]:
    for c in names:
        if c in df.columns:
            return c
    return None


def retry_call(func, *args, retries=3, wait=1.0, **kwargs):
    last = None
    for n in range(retries):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            last = exc
            time.sleep(wait * (2 ** n))
    raise last


def cache_read(path: Path) -> Optional[pd.DataFrame]:
    if not path.exists():
        return None
    try:
        df = pd.read_pickle(path)
        return df if isinstance(df, pd.DataFrame) else None
    except Exception:
        return None


def cache_write(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_pickle(tmp)
    tmp.replace(path)


def month_anchors(start: pd.Timestamp, end: pd.Timestamp) -> List[pd.Timestamp]:
    anchors = list(pd.date_range(start=start, end=end, freq="MS"))
    if not anchors or anchors[0] > start:
        anchors.insert(0, start)
    if anchors[-1] < end:
        anchors.append(end)
    return anchors


def nearest_business_snapshot(day: pd.Timestamp, market: str) -> Tuple[str, List[str]]:
    # 휴일이면 최대 10일 뒤/앞을 탐색한다.
    candidates = [day + pd.Timedelta(days=i) for i in range(0, 8)]
    candidates += [day - pd.Timedelta(days=i) for i in range(1, 8)]
    for d in candidates:
        ds = ymd(d)
        tickers = retry_call(stock.get_market_ticker_list, ds, market=market)
        if tickers:
            return ds, list(tickers)
    return ymd(day), []


def build_historical_universe(start: pd.Timestamp, end: pd.Timestamp, cache_dir: Path) -> pd.DataFrame:
    path = cache_dir / f"universe_{ymd(start)}_{ymd(end)}.pkl"
    old = cache_read(path)
    if old is not None and not old.empty:
        return old

    rows = []
    seen = set()
    for anchor in month_anchors(start, end):
        for market in ("KOSPI", "KOSDAQ"):
            snap_date, tickers = nearest_business_snapshot(anchor, market)
            print(f"[유니버스] {snap_date} {market}: {len(tickers)}개")
            for ticker in tickers:
                key = (ticker, market)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    name = stock.get_market_ticker_name(ticker) or ticker
                except Exception:
                    name = ticker
                rows.append({"ticker": ticker, "market": market, "name": name})
            time.sleep(0.15)

    df = pd.DataFrame(rows).drop_duplicates("ticker")
    cache_write(df, path)
    return df


def download_ohlcv(ticker: str, start: pd.Timestamp, end: pd.Timestamp, cache_dir: Path) -> Optional[pd.DataFrame]:
    path = cache_dir / "ohlcv" / f"{ticker}_{ymd(start)}_{ymd(end)}.pkl"
    old = cache_read(path)
    if old is not None:
        return old
    try:
        df = retry_call(stock.get_market_ohlcv_by_date, ymd(start), ymd(end), ticker)
        if df is None or df.empty:
            return None
        rename = {"시가":"Open", "고가":"High", "저가":"Low", "종가":"Close",
                  "거래량":"Volume", "거래대금":"Turnover", "등락률":"ChangePct"}
        df = df.rename(columns={k:v for k,v in rename.items() if k in df.columns}).copy()
        if "Turnover" not in df.columns:
            df["Turnover"] = df["Close"] * df["Volume"]
        need = ["Open", "High", "Low", "Close", "Volume", "Turnover"]
        if not all(c in df.columns for c in need):
            return None
        df.index = pd.to_datetime(df.index)
        df = df[need].apply(pd.to_numeric, errors="coerce").dropna(subset=["Close"])
        cache_write(df, path)
        return df
    except Exception as exc:
        print(f"[경고] {ticker} OHLCV 실패: {exc}")
        return None


def download_supply(ticker: str, start: pd.Timestamp, end: pd.Timestamp, cache_dir: Path) -> Optional[pd.DataFrame]:
    path = cache_dir / "supply" / f"{ticker}_{ymd(start)}_{ymd(end)}.pkl"
    old = cache_read(path)
    if old is not None:
        return old
    try:
        df = retry_call(stock.get_market_trading_value_by_date, ymd(start), ymd(end), ticker)
        if df is None or df.empty:
            return None
        df.index = pd.to_datetime(df.index)
        fcol = safe_col(df, ["외국인합계", "외국인"])
        icol = safe_col(df, ["기관합계", "기관"])
        out = pd.DataFrame(index=df.index)
        out["Foreign"] = pd.to_numeric(df[fcol], errors="coerce").fillna(0) if fcol else 0.0
        out["Institution"] = pd.to_numeric(df[icol], errors="coerce").fillna(0) if icol else 0.0
        cache_write(out, path)
        return out
    except Exception as exc:
        print(f"[경고] {ticker} 수급 실패: {exc}")
        return None


def add_indicators(raw: pd.DataFrame) -> pd.DataFrame:
    d = raw.copy()
    for n in (10, 20, 60, 120):
        d[f"MA{n}"] = d["Close"].rolling(n).mean()
    d["VolMA20"] = d["Volume"].rolling(20).mean()
    d["TurnoverMA20"] = d["Turnover"].rolling(20).mean()
    tr = pd.concat([(d["High"]-d["Low"]),
                    (d["High"]-d["Close"].shift(1)).abs(),
                    (d["Low"]-d["Close"].shift(1)).abs()], axis=1).max(axis=1)
    d["ATR10"] = tr.rolling(10).mean()
    d["ATR30"] = tr.rolling(30).mean()
    return d


def safe_return(s: pd.Series, days: int) -> float:
    if len(s) <= days or s.iloc[-days] <= 0:
        return np.nan
    return s.iloc[-1] / s.iloc[-days] - 1


def liquidity_score(x: float) -> int:
    if x >= STRONG_TURNOVER: return 15
    if x >= GOOD_TURNOVER: return 10
    if x >= MIN_AVG_TURNOVER: return 5
    return 0


def trend_score(d: pd.DataFrame) -> int:
    x = d.iloc[-1]
    score = 0
    if x.Close > x.MA20: score += 5
    if x.Close > x.MA60: score += 5
    if x.MA60 > x.MA120: score += 5
    if len(d) >= 141 and x.MA120 > d["MA120"].iloc[-21]: score += 5
    high52 = d["Close"].tail(252).max()
    if high52 > 0 and x.Close >= high52 * 0.85: score += 5
    return score


def detect_vcp(d: pd.DataFrame) -> float:
    # 원본의 취지를 보존한 결정론적 축약: 변동폭/거래량/ATR 수축과 higher low
    if len(d) < 80: return 0.0
    early = d.iloc[-60:-40]
    mid = d.iloc[-40:-20]
    final = d.iloc[-20:]
    def rng(x):
        m = x["Close"].mean()
        return (x["High"].max()-x["Low"].min())/m if m > 0 else np.nan
    re, rm, rf = rng(early), rng(mid), rng(final)
    score = 0.0
    if np.isfinite(re) and np.isfinite(rm) and rm < re: score += 1.5
    if np.isfinite(rm) and np.isfinite(rf) and rf < rm: score += 1.5
    if final["Volume"].mean() < d["Volume"].iloc[-60:-20].mean() * 0.8: score += 1.0
    if d["ATR10"].iloc[-1] <= d["ATR30"].iloc[-1] * 0.9: score += 1.0
    if final["Low"].tail(10).min() >= final["Low"].head(10).min(): score += 1.0
    if np.isfinite(rf) and rf <= 0.12: score += 1.0
    return min(score, 7.0)


def setup_at(d: pd.DataFrame) -> Dict[str, float]:
    x = d.iloc[-1]
    close = float(x.Close)
    high60 = d["High"].tail(60).max()

    # 중요 수정: 신호일 당일 High를 피봇에서 제외한다.
    prior = d.iloc[:-1]
    pivot = float(prior["High"].tail(PIVOT_LOOKBACK).max())
    recent_low = float(prior["Low"].tail(20).min())

    drawdown = (close / high60 - 1) * 100
    dist = (close / pivot - 1) * 100
    vol_recent = d["Volume"].tail(5).mean()
    vol_ma20 = float(x.VolMA20)
    turn_ratio = float(x.Turnover / x.TurnoverMA20) if x.TurnoverMA20 > 0 else np.nan
    vol_ratio = float(x.Volume / x.VolMA20) if x.VolMA20 > 0 else np.nan
    volume_dryup = vol_recent < vol_ma20 * 0.8
    atr_dryup = x.ATR10 <= x.ATR30 * 0.9
    near20 = abs(close/x.MA20 - 1)*100 <= NEAR_MA20_PCT
    near60 = abs(close/x.MA60 - 1)*100 <= NEAR_MA60_PCT
    pull_basic = PULLBACK_MIN_DD <= drawdown <= PULLBACK_MAX_DD and (near20 or near60) and volume_dryup
    vcp = detect_vcp(d)
    breakout_near = -BREAKOUT_NEAR_PCT <= dist <= 1.5
    breakout_today = close >= pivot and vol_ratio >= VOLUME_EXPLOSION_RATIO and turn_ratio >= TURNOVER_EXPLOSION_RATIO and vcp >= 3

    prelim_entry = close if pull_basic else pivot
    prelim_stop = max(recent_low, close*0.95) if pull_basic else max(recent_low, pivot*0.94)
    if prelim_stop <= 0 or prelim_stop >= prelim_entry:
        prelim_stop = prelim_entry * 0.95
    risk_pct = (prelim_entry-prelim_stop)/prelim_entry*100

    pb_quality = 0.0
    if PULLBACK_MIN_DD <= drawdown <= PULLBACK_MAX_DD: pb_quality += 2
    if near20 or near60: pb_quality += 1.5
    if close >= x.MA60: pb_quality += 1
    if close >= x.MA20: pb_quality += 1
    if volume_dryup: pb_quality += 1.5
    if atr_dryup: pb_quality += 1
    if risk_pct <= MAX_RISK_PCT: pb_quality += 1
    pull_ready = pull_basic and pb_quality >= PULLBACK_QUALITY_READY_SCORE and risk_pct <= MAX_RISK_PCT

    setup_score = 0
    if breakout_today: setup_score += 8
    elif breakout_near and vcp >= VCP_READY_SCORE: setup_score += 6
    elif breakout_near: setup_score += 2
    if vcp >= VCP_READY_SCORE: setup_score += 4
    elif vcp >= 3: setup_score += 2
    if pull_ready: setup_score += 6
    elif pull_basic: setup_score += 3
    if volume_dryup: setup_score += 3
    if atr_dryup: setup_score += 2
    if vol_ratio >= VOLUME_EXPLOSION_RATIO: setup_score += 3
    if turn_ratio >= TURNOVER_EXPLOSION_RATIO: setup_score += 2

    return dict(close=close, pivot=pivot, stop=prelim_stop, risk_pct=risk_pct,
                vcp_score=vcp, pb_quality=pb_quality, setup_score=min(setup_score,26),
                pullback_ready=bool(pull_ready),
                breakout_ready=bool(breakout_near and vcp >= VCP_READY_SCORE and risk_pct <= MAX_RISK_PCT),
                breakout_today=bool(breakout_today and risk_pct <= MAX_RISK_PCT))


def supply_points(s: Optional[pd.DataFrame], date: pd.Timestamp, base: float) -> Tuple[int, int, int]:
    if s is None or s.empty or date not in s.index:
        return 0, 0, 0
    h20 = s.loc[:date].tail(20)
    h5 = s.loc[:date].tail(5)
    f = float(h20["Foreign"].sum())
    i = float(h20["Institution"].sum())
    fs = 15 if f > base*0.03 else (10 if f > 0 else 0)
    ins = 10 if i > base*0.02 else (6 if i > 0 else 0)
    momentum = (2 if (h5["Foreign"] > 0).sum() >= 3 else 0)
    momentum += (2 if (h5["Institution"] > 0).sum() >= 3 else 0)
    momentum += (1 if ((h5["Foreign"]>0)&(h5["Institution"]>0)).sum() >= 2 else 0)
    return fs, ins, momentum


def screen_date(date: pd.Timestamp, data: Dict[str,pd.DataFrame], meta: pd.DataFrame,
                supply: Dict[str,pd.DataFrame], include_supply: bool) -> pd.DataFrame:
    rows = []
    # 먼저 그 날짜의 유동성 상위 600개를 point-in-time으로 선정
    liquid = []
    for ticker, d in data.items():
        if date not in d.index: continue
        hist = d.loc[:date]
        if len(hist) < 260: continue
        x = hist.iloc[-1]
        if x.Close >= MIN_PRICE and x.TurnoverMA20 >= MIN_AVG_TURNOVER:
            liquid.append((ticker, float(x.TurnoverMA20)))
    liquid = sorted(liquid, key=lambda z:z[1], reverse=True)[:MAX_UNIVERSE]

    temp = []
    for ticker, avg_turn in liquid:
        hist = data[ticker].loc[:date]
        st = setup_at(hist)
        close_series = hist["Close"].dropna()
        wr = safe_return(close_series,63)*0.4 + safe_return(close_series,126)*0.3 + safe_return(close_series,252)*0.3
        temp.append((ticker, avg_turn, hist, st, wr))
    if not temp: return pd.DataFrame()

    wrs = pd.Series({t[0]:t[4] for t in temp})
    ranks = wrs.rank(pct=True)*100
    meta_idx = meta.set_index("ticker")
    for ticker, avg_turn, hist, st, wr in temp:
        rs_pct = float(ranks.get(ticker,0))
        rs_score = round(rs_pct/100*20,1)
        base = avg_turn*20
        fs, ins, mom = supply_points(supply.get(ticker), date, base) if include_supply else (0,0,0)
        total = trend_score(hist)+liquidity_score(avg_turn)+st["setup_score"]+rs_score+fs+ins+mom
        # 수급을 생략한 빠른 모드는 최대 30점이 빠지므로 동일 비율로 문턱을 보정한다.
        adjust = 0 if include_supply else 25
        category = None
        if total >= PULLBACK_SCORE-adjust and st["pullback_ready"]: category = "PULLBACK_READY"
        if total >= BREAKOUT_SCORE-adjust and st["breakout_ready"]: category = "BREAKOUT_READY"
        if total >= BREAKOUT_TODAY_SCORE-adjust and st["breakout_today"]: category = "BREAKOUT_TODAY"
        if category:
            m = meta_idx.loc[ticker]
            rows.append(dict(date=date,ticker=ticker,name=m["name"],market=m["market"],
                             category=category,total_score=round(total,1),rs_percentile=rs_pct,**st))
    return pd.DataFrame(rows).sort_values(["total_score","rs_percentile"],ascending=False) if rows else pd.DataFrame()


def execution_price(side: str, raw: float, slippage: float) -> float:
    return raw*(1+slippage) if side == "buy" else raw*(1-slippage)


def sell_cost(value: float, commission: float, sell_tax: float) -> float:
    return value*(commission+sell_tax)


def run_backtest(args, data, meta, supply, calendar):
    cash = float(args.capital)
    positions: Dict[str,Position] = {}
    orders: List[Order] = []
    trades = []
    equity_rows = []

    for di, date in enumerate(calendar):
        # 1) 기존 포지션 관리. 동일 일봉 내 충돌은 보수적으로 손절 우선.
        for ticker in list(positions):
            p = positions[ticker]
            d = data[ticker]
            if date not in d.index: continue
            bar = d.loc[date]
            exit_reason = None
            exit_price = None

            if bar.Open <= p.stop:
                exit_reason, exit_price = "갭손절", execution_price("sell", float(bar.Open), args.slippage)
            elif bar.Low <= p.stop:
                exit_reason, exit_price = "손절", execution_price("sell", p.stop, args.slippage)
            else:
                if (not p.one_r_touched) and bar.High >= p.target_1r:
                    p.one_r_touched = True
                    p.stop = max(p.stop, p.entry_price)
                if (not p.half_sold) and bar.High >= p.target_2r:
                    qty = max(1, p.remaining//2)
                    px = execution_price("sell", p.target_2r, args.slippage)
                    value = qty*px
                    cost = sell_cost(value,args.commission,args.sell_tax)
                    cash += value-cost
                    p.remaining -= qty
                    p.half_sold = True
                    p.realized_pnl += value-cost-qty*p.entry_price
                    trades.append(dict(ticker=ticker,name=p.name,signal_type=p.signal_type,
                        entry_date=p.entry_date,exit_date=str(date.date()),qty=qty,entry_price=p.entry_price,
                        exit_price=px,reason="2R 절반익절",pnl=value-cost-qty*p.entry_price))
                # 잔여 수량은 전일 확정 MA20 이탈을 오늘 시가에 정리
                prev = d.loc[:date].iloc[:-1]
                if len(prev) and prev.iloc[-1].Close < prev.iloc[-1].MA20:
                    exit_reason, exit_price = "MA20 이탈", execution_price("sell", float(bar.Open), args.slippage)

            if exit_reason and p.remaining > 0:
                qty = p.remaining
                value = qty*exit_price
                cost = sell_cost(value,args.commission,args.sell_tax)
                cash += value-cost
                pnl = value-cost-qty*p.entry_price
                trades.append(dict(ticker=ticker,name=p.name,signal_type=p.signal_type,
                    entry_date=p.entry_date,exit_date=str(date.date()),qty=qty,entry_price=p.entry_price,
                    exit_price=exit_price,reason=exit_reason,pnl=pnl))
                del positions[ticker]

        # 2) 전일 신호 주문 체결
        new_orders = []
        for o in orders:
            if o.ticker in positions or di > o.valid_until_idx: continue
            if len(positions) >= args.max_positions:
                new_orders.append(o); continue
            d = data[o.ticker]
            if date not in d.index:
                new_orders.append(o); continue
            bar = d.loc[date]
            raw_entry = None
            if o.signal_type == "PULLBACK_READY":
                raw_entry = float(bar.Open)
            else:
                # 돌파 주문: 시가가 피봇 위면 시가, 장중 돌파면 피봇
                if bar.Open >= o.trigger: raw_entry = float(bar.Open)
                elif bar.High >= o.trigger: raw_entry = o.trigger
            if raw_entry is None:
                new_orders.append(o); continue
            buy_px = execution_price("buy",raw_entry,args.slippage)
            stop = min(o.stop_from_signal, buy_px*0.999)
            risk_ps = buy_px-stop
            if risk_ps <= 0 or risk_ps/buy_px > MAX_RISK_PCT/100: continue
            equity_now = cash + sum(p.remaining*float(data[t].loc[:date].iloc[-1].Close) for t,p in positions.items())
            qty_risk = math.floor(equity_now*args.account_risk/risk_ps)
            qty_alloc = math.floor(equity_now*args.max_position_pct/buy_px)
            qty_cash = math.floor(cash/(buy_px*(1+args.commission)))
            qty = max(0,min(qty_risk,qty_alloc,qty_cash))
            if qty < 1: continue
            value=qty*buy_px; fee=value*args.commission; cash-=value+fee
            positions[o.ticker]=Position(o.ticker,o.name,o.market,o.signal_type,o.signal_date,
                str(date.date()),buy_px,qty,qty,stop,stop,risk_ps,buy_px+risk_ps,buy_px+2*risk_ps,
                entry_cost=fee)
        orders = new_orders

        # 3) 오늘 종가로 신호 생성. 체결은 반드시 다음 거래일부터.
        signals = screen_date(date,data,meta,supply,args.include_supply)
        if not signals.empty:
            existing = {x.ticker for x in orders}|set(positions)
            for _,r in signals.iterrows():
                if r.ticker in existing: continue
                valid_days = 1 if r.category == "PULLBACK_READY" else args.order_valid_days
                orders.append(Order(r.ticker,r["name"],r.market,r.category,str(date.date()),
                                    di+valid_days,float(r.pivot),float(r.stop),float(r.total_score)))
                existing.add(r.ticker)

        # 4) 일별 평가자산
        market_value=0.0
        for ticker,p in positions.items():
            h=data[ticker].loc[:date]
            if not h.empty: market_value += p.remaining*float(h.iloc[-1].Close)
        equity_rows.append(dict(date=date,cash=cash,market_value=market_value,equity=cash+market_value,
                                positions=len(positions),open_orders=len(orders)))
        if di%50==0: print(f"[백테스트] {date.date()} {di+1}/{len(calendar)} 자산={cash+market_value:,.0f}")

    # 마지막 날 종가 강제청산
    last=calendar[-1]
    for ticker,p in list(positions.items()):
        px=execution_price("sell",float(data[ticker].loc[:last].iloc[-1].Close),args.slippage)
        qty=p.remaining; value=qty*px; cost=sell_cost(value,args.commission,args.sell_tax)
        cash += value-cost
        trades.append(dict(ticker=ticker,name=p.name,signal_type=p.signal_type,entry_date=p.entry_date,
            exit_date=str(last.date()),qty=qty,entry_price=p.entry_price,exit_price=px,
            reason="기간종료",pnl=value-cost-qty*p.entry_price))
    equity=pd.DataFrame(equity_rows).set_index("date")
    equity.iloc[-1,equity.columns.get_loc("cash")]=cash
    equity.iloc[-1,equity.columns.get_loc("market_value")]=0
    equity.iloc[-1,equity.columns.get_loc("equity")]=cash
    return equity,pd.DataFrame(trades)


def performance(equity: pd.DataFrame, trades: pd.DataFrame, initial: float) -> Dict[str,float]:
    e=equity["equity"]
    total=e.iloc[-1]/initial-1
    years=max((e.index[-1]-e.index[0]).days/365.25,1/365.25)
    cagr=(e.iloc[-1]/initial)**(1/years)-1
    dd=e/e.cummax()-1
    daily=e.pct_change().dropna()
    annual_vol=daily.std()*np.sqrt(252) if len(daily)>1 else np.nan
    sharpe=(daily.mean()/daily.std()*np.sqrt(252)) if len(daily)>1 and daily.std()>0 else np.nan
    pnl=trades["pnl"] if not trades.empty else pd.Series(dtype=float)
    wins=pnl[pnl>0]; losses=pnl[pnl<0]
    return {"initial_capital":initial,"final_equity":float(e.iloc[-1]),"total_return_pct":total*100,
            "cagr_pct":cagr*100,"mdd_pct":float(dd.min()*100),"annual_vol_pct":float(annual_vol*100),
            "sharpe":float(sharpe),"trade_legs":int(len(trades)),"win_rate_pct":float((pnl>0).mean()*100) if len(pnl) else 0,
            "profit_factor":float(wins.sum()/abs(losses.sum())) if len(losses) and losses.sum()!=0 else np.nan}


def parse_args():
    p=argparse.ArgumentParser(description="K-Minervini 과거 백테스트")
    today=pd.Timestamp.today().normalize()
    p.add_argument("--start",default=(today-pd.DateOffset(years=5)).strftime("%Y-%m-%d"))
    p.add_argument("--end",default=today.strftime("%Y-%m-%d"))
    p.add_argument("--capital",type=float,default=INITIAL_CAPITAL)
    p.add_argument("--max-positions",type=int,default=5)
    p.add_argument("--max-position-pct",type=float,default=0.20)
    p.add_argument("--account-risk",type=float,default=0.01)
    p.add_argument("--commission",type=float,default=DEFAULT_COMMISSION)
    p.add_argument("--sell-tax",type=float,default=DEFAULT_SELL_TAX)
    p.add_argument("--slippage",type=float,default=DEFAULT_SLIPPAGE)
    p.add_argument("--order-valid-days",type=int,default=5)
    p.add_argument("--include-supply",action="store_true",help="외국인/기관 수급까지 반영. 더 느리지만 원본에 가까움")
    p.add_argument("--cache-dir",default="backtest_cache")
    p.add_argument("--output-dir",default="backtest_output")
    p.add_argument("--sleep",type=float,default=0.12)
    return p.parse_args()


def main():
    args=parse_args()
    start=pd.Timestamp(args.start); end=pd.Timestamp(args.end)
    warmup=start-pd.Timedelta(days=550)
    cache_dir=Path(args.cache_dir); out=Path(args.output_dir); out.mkdir(parents=True,exist_ok=True)
    print("1/4 과거 유니버스 구성")
    meta=build_historical_universe(warmup,end,cache_dir)
    print(f"고유 종목 수: {len(meta):,}")

    print("2/4 가격 데이터 다운로드/캐시")
    data={}; supply={}
    for n,row in enumerate(meta.itertuples(index=False),1):
        raw=download_ohlcv(row.ticker,warmup,end,cache_dir)
        if raw is not None and len(raw)>=260:
            data[row.ticker]=add_indicators(raw)
            if args.include_supply:
                s=download_supply(row.ticker,warmup,end,cache_dir)
                if s is not None: supply[row.ticker]=s
        if n%50==0: print(f"  {n}/{len(meta)} 완료, 사용 가능 {len(data)}")
        time.sleep(args.sleep)

    print("3/4 거래일 구성 및 백테스트")
    dates=sorted(set().union(*[set(d.index[(d.index>=start)&(d.index<=end)]) for d in data.values()]))
    calendar=pd.DatetimeIndex(dates)
    if len(calendar)<30: raise RuntimeError("백테스트 거래일이 부족합니다.")
    equity,trades=run_backtest(args,data,meta,supply,calendar)
    stats=performance(equity,trades,args.capital)

    print("4/4 결과 저장")
    equity.to_csv(out/"equity_curve.csv",encoding="utf-8-sig")
    trades.to_csv(out/"trades.csv",index=False,encoding="utf-8-sig")
    yearly=equity["equity"].resample("YE").last().pct_change()
    if len(yearly): yearly.iloc[0]=equity["equity"].resample("YE").last().iloc[0]/args.capital-1
    yearly.rename("return").to_csv(out/"yearly_returns.csv",encoding="utf-8-sig")
    with open(out/"summary.json","w",encoding="utf-8") as f: json.dump(stats,f,ensure_ascii=False,indent=2)
    with open(out/"run_config.json","w",encoding="utf-8") as f: json.dump(vars(args),f,ensure_ascii=False,indent=2)
    print(json.dumps(stats,ensure_ascii=False,indent=2))
    print(f"결과 폴더: {out.resolve()}")


if __name__ == "__main__":
    main()
