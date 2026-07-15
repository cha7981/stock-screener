#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
US Minervini V11.5 기술적 백테스트

원본 screener (6).py의 다음 규칙을 과거 시점별로 재사용한다.
- trend template / RS rating / analyze_pattern
- HOT_SETUP, PULLBACK, BREAKOUT 신호
- 피봇은 원본상 이미 당일 제외: High.iloc[-21:-1]

백테스트 체결 규칙
- 신호는 장 마감 후 생성, 체결은 다음 거래일부터만 가능
- HOT_SETUP/BREAKOUT: entry 이상에서 stop 주문, max_chase 초과 시 미체결
- PULLBACK: 다음 거래일 시가가 entry_zone_high 이하일 때 진입
- 초기 손절은 스크리너 stop
- 2R에서 50% 익절, 잔량은 3R 또는 전일 종가의 MA10 이탈 시 청산
- 같은 날 손절과 목표가가 모두 닿으면 손절 우선
- 시장 RED이면 신규 진입 금지, CAUTION이면 신규 수량 50%

주의: yfinance의 과거 OHLCV는 편리하지만 연구용이다. 현재 구성종목 파일을 쓰면
상장폐지/편출 종목이 빠지는 생존편향이 생긴다. 정확도를 높이려면 사용자 제공
point-in-time 종목 목록 CSV를 --universe-file 로 사용한다.
"""
from __future__ import annotations
import argparse, importlib.util, json, math, time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List
import numpy as np
import pandas as pd
import yfinance as yf

@dataclass
class Order:
    ticker: str
    signal_date: str
    classification: str
    entry: float
    entry_zone_high: float
    max_chase: float
    stop: float
    valid_until: int
    score: float

@dataclass
class Position:
    ticker: str
    classification: str
    signal_date: str
    entry_date: str
    entry: float
    stop: float
    initial_stop: float
    shares: int
    remaining: int
    risk: float
    target1: float
    target2: float
    half_sold: bool = False


def load_screener(path: str):
    p=Path(path)
    if not p.exists(): raise FileNotFoundError(f"스크리너 파일 없음: {p}")
    spec=importlib.util.spec_from_file_location("us_screener",p)
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    for name in ("prepare_frame","passes_trend_template","analyze_pattern"):
        if not hasattr(mod,name): raise RuntimeError(f"원본에 {name} 함수가 없습니다.")
    return mod


def read_universe(args, sc) -> List[str]:
    if args.universe_file:
        df=pd.read_csv(args.universe_file)
        col=next((c for c in df.columns if c.lower() in {"ticker","symbol"}),df.columns[0])
        vals=[sc.clean_ticker(x) for x in df[col].dropna()]
        return sorted({x for x in vals if sc.valid_ticker(x)})
    # 원본 스크리너의 현재 구성종목 수집. 빠른 검증은 SP500, full은 3개 지수 합집합.
    vals=set(sc.get_sp500_tickers())
    if args.universe == "full":
        vals.update(sc.get_nasdaq100_tickers())
        r,_=sc.get_russell2000_tickers(); vals.update(r)
    return sorted(vals)


def extract_ticker(raw,ticker):
    if raw is None or raw.empty: return None
    if not isinstance(raw.columns,pd.MultiIndex):
        return raw.copy()
    l0=[str(x) for x in raw.columns.get_level_values(0)]
    l1=[str(x) for x in raw.columns.get_level_values(1)]
    if ticker in l0: d=raw.xs(ticker,axis=1,level=0,drop_level=True).copy()
    elif ticker in l1: d=raw.xs(ticker,axis=1,level=1,drop_level=True).copy()
    else: return None
    if isinstance(d.columns,pd.MultiIndex): d.columns=[str(x[-1]) for x in d.columns]
    return d


def download_prices(tickers,start,end,cache_dir,chunk_size=50,pause=2.0):
    cache_dir.mkdir(parents=True,exist_ok=True); out={}
    missing=[]
    for t in tickers:
        p=cache_dir/f"{t}_{start}_{end}.pkl"
        if p.exists():
            try: out[t]=pd.read_pickle(p); continue
            except Exception: pass
        missing.append(t)
    for i in range(0,len(missing),chunk_size):
        chunk=missing[i:i+chunk_size]
        print(f"[가격] {i+1}-{min(i+chunk_size,len(missing))}/{len(missing)}")
        raw=pd.DataFrame()
        for attempt in range(3):
            try:
                raw=yf.download(chunk,start=start,end=(pd.Timestamp(end)+pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                    interval="1d",group_by="ticker",auto_adjust=True,actions=False,progress=False,
                    threads=False,timeout=60,multi_level_index=True)
                if not raw.empty: break
            except Exception as e: print(f"  재시도 {attempt+1}: {e}")
            time.sleep(5*(attempt+1))
        for t in chunk:
            d=extract_ticker(raw,t)
            if d is None or d.empty: continue
            if getattr(d.index,"tz",None) is not None: d.index=d.index.tz_localize(None)
            need=["Open","High","Low","Close","Volume"]
            if not all(c in d.columns for c in need): continue
            d=d[need].apply(pd.to_numeric,errors="coerce").dropna(subset=["Close"])
            if len(d)<260: continue
            out[t]=d
            d.to_pickle(cache_dir/f"{t}_{start}_{end}.pkl")
        time.sleep(pause)
    return out


def prepare_all(raw,sc):
    out={}
    for t,d in raw.items():
        x=sc.prepare_frame(d.copy())
        if x is not None: out[t]=x
    return out


def market_state(date,bench):
    try:
        m={}
        for t in ("SPY","QQQ"):
            d=bench[t].loc[:date]
            if len(d)<210:return "UNKNOWN"
            c=d.Close; m[t]=(c.iloc[-1],c.rolling(50).mean().iloc[-1],c.rolling(200).mean().iloc[-1],c.rolling(50).mean().iloc[-10])
        spy,qqq=m["SPY"],m["QQQ"]
        if spy[0]<spy[2] or (spy[0]<spy[1] and qqq[0]<qqq[1]): return "RED"
        if spy[0]>spy[1]>spy[2] and qqq[0]>qqq[1]>qqq[2] and spy[1]>spy[3]: return "GREEN"
        return "CAUTION"
    except Exception:return "UNKNOWN"


def calculate_rs_at(date,data):
    rows=[]
    for t,d in data.items():
        c=d.loc[:date,"Close"].dropna()
        if len(c)>252 and c.iloc[-252]>0:
            r3=c.iloc[-1]/c.iloc[-63]-1; r6=c.iloc[-1]/c.iloc[-126]-1; r12=c.iloc[-1]/c.iloc[-252]-1
            rows.append((t,.4*r3+.3*r6+.3*r12,r3,r6,r12))
    if not rows:return {}
    f=pd.DataFrame(rows,columns=["ticker","weighted","r3","r6","r12"])
    f["rs_rating"]=(f.weighted.rank(pct=True)*99).round().astype(int)
    return f.set_index("ticker")[["rs_rating","r3","r6","r12"]].to_dict("index")


def screen_at(date,data,sc,classes):
    rs=calculate_rs_at(date,data); rows=[]
    for t,r in rs.items():
        d=data[t].loc[:date]
        if len(d)<260:continue
        try:
            if not sc.passes_trend_template(d,r): continue
            p=sc.analyze_pattern(d,int(r["rs_rating"]))
            if p and p["classification"] in classes:
                rows.append({"ticker":t,"rs_rating":r["rs_rating"],**p})
        except Exception:continue
    return sorted(rows,key=lambda x:(x.get("vcp_score",0)+x.get("pullback_score",0),x.get("rs_rating",0)),reverse=True)


def buy_price(raw,slippage):return raw*(1+slippage)
def sell_price(raw,slippage):return raw*(1-slippage)

def run(args,data,bench,sc,calendar):
    cash=float(args.capital); pos:Dict[str,Position]={}; orders:List[Order]=[]; trades=[]; eq=[]; signals=[]
    chosen=set(args.classes.split(","))
    for di,date in enumerate(calendar):
        # 기존 포지션, 손절 우선
        for t in list(pos):
            p=pos[t]; d=data[t]
            if date not in d.index:continue
            b=d.loc[date]; reason=None; px=None
            if b.Open<=p.stop: reason="GAP_STOP"; px=sell_price(float(b.Open),args.slippage)
            elif b.Low<=p.stop: reason="STOP"; px=sell_price(p.stop,args.slippage)
            else:
                if not p.half_sold and b.High>=p.target1:
                    q=max(1,p.remaining//2); x=sell_price(p.target1,args.slippage); value=q*x
                    fee=value*(args.commission+args.sec_fee); cash+=value-fee; p.remaining-=q; p.half_sold=True
                    trades.append(dict(ticker=t,classification=p.classification,signal_date=p.signal_date,entry_date=p.entry_date,
                        exit_date=str(date.date()),qty=q,entry_price=p.entry,exit_price=x,reason="2R_HALF",pnl=value-fee-q*p.entry))
                if b.High>=p.target2: reason="3R"; px=sell_price(p.target2,args.slippage)
                else:
                    prev=d.loc[:date].iloc[:-1]
                    if len(prev) and prev.iloc[-1].Close<prev.iloc[-1].MA10: reason="MA10_EXIT"; px=sell_price(float(b.Open),args.slippage)
            if reason and p.remaining>0:
                q=p.remaining; value=q*px; fee=value*(args.commission+args.sec_fee); cash+=value-fee
                trades.append(dict(ticker=t,classification=p.classification,signal_date=p.signal_date,entry_date=p.entry_date,
                    exit_date=str(date.date()),qty=q,entry_price=p.entry,exit_price=px,reason=reason,pnl=value-fee-q*p.entry))
                del pos[t]
        # 대기 주문 체결
        state=market_state(date,bench)
        keep=[]
        for o in orders:
            if di>o.valid_until or o.ticker in pos:continue
            if len(pos)>=args.max_positions or state in {"RED","UNKNOWN"}:keep.append(o);continue
            d=data[o.ticker]
            if date not in d.index:keep.append(o);continue
            b=d.loc[date]; raw_entry=None
            if o.classification=="PULLBACK":
                if b.Open<=o.entry_zone_high:raw_entry=float(b.Open)
            else:
                if b.Open>o.max_chase: raw_entry=None
                elif b.Open>=o.entry:raw_entry=float(b.Open)
                elif b.High>=o.entry:raw_entry=o.entry
            if raw_entry is None:keep.append(o);continue
            ep=buy_price(raw_entry,args.slippage); stop=min(o.stop,ep*.999); risk=ep-stop
            if risk<=0 or risk/ep>args.max_structure_risk:continue
            equity=cash+sum(p.remaining*float(data[x].loc[:date].iloc[-1].Close) for x,p in pos.items())
            risk_budget=equity*args.account_risk*(.5 if state=="CAUTION" else 1)
            allocation=equity*args.max_position_pct*(.5 if state=="CAUTION" else 1)
            q=min(math.floor(risk_budget/risk),math.floor(allocation/ep),math.floor(cash/(ep*(1+args.commission))))
            if q<1:continue
            value=q*ep; fee=value*args.commission; cash-=value+fee
            pos[o.ticker]=Position(o.ticker,o.classification,o.signal_date,str(date.date()),ep,stop,stop,q,q,risk,ep+2*risk,ep+3*risk)
        orders=keep
        # 신호는 당일 종가 이후 생성
        todays=screen_at(date,data,sc,chosen)
        existing=set(pos)|{o.ticker for o in orders}
        for r in todays:
            signals.append({"date":date,**r})
            if r["ticker"] in existing:continue
            days=1 if r["classification"]=="PULLBACK" else args.order_valid_days
            orders.append(Order(r["ticker"],str(date.date()),r["classification"],float(r["entry"]),float(r["entry_zone_high"]),
                                float(r["max_chase"]),float(r["stop"]),di+days,float(r.get("quality_score",0))))
            existing.add(r["ticker"])
        mv=sum(p.remaining*float(data[t].loc[:date].iloc[-1].Close) for t,p in pos.items())
        eq.append(dict(date=date,cash=cash,market_value=mv,equity=cash+mv,positions=len(pos),market_state=state))
        if di%50==0:print(f"[백테스트] {date.date()} {di+1}/{len(calendar)} ${cash+mv:,.0f}")
    last=calendar[-1]
    for t,p in list(pos.items()):
        x=sell_price(float(data[t].loc[:last].iloc[-1].Close),args.slippage); q=p.remaining; value=q*x; fee=value*(args.commission+args.sec_fee); cash+=value-fee
        trades.append(dict(ticker=t,classification=p.classification,signal_date=p.signal_date,entry_date=p.entry_date,
            exit_date=str(last.date()),qty=q,entry_price=p.entry,exit_price=x,reason="END",pnl=value-fee-q*p.entry))
    e=pd.DataFrame(eq).set_index("date"); e.loc[last,["cash","market_value","equity"]]=[cash,0,cash]
    return e,pd.DataFrame(trades),pd.DataFrame(signals)


def stats(e,t,initial):
    v=e.equity; years=max((v.index[-1]-v.index[0]).days/365.25,1/365.25); ret=v.iloc[-1]/initial-1; dd=v/v.cummax()-1; d=v.pct_change().dropna()
    pnl=t.pnl if not t.empty else pd.Series(dtype=float); wins=pnl[pnl>0]; losses=pnl[pnl<0]
    return {"initial_capital_usd":initial,"final_equity_usd":float(v.iloc[-1]),"total_return_pct":ret*100,
            "cagr_pct":((v.iloc[-1]/initial)**(1/years)-1)*100,"mdd_pct":float(dd.min()*100),
            "sharpe":float(d.mean()/d.std()*np.sqrt(252)) if len(d)>1 and d.std()>0 else None,
            "trade_legs":int(len(t)),"win_rate_pct":float((pnl>0).mean()*100) if len(pnl) else 0,
            "profit_factor":float(wins.sum()/abs(losses.sum())) if len(losses) and losses.sum()!=0 else None}


def args_parser():
    p=argparse.ArgumentParser(description="US Minervini V11.5 backtest")
    today=pd.Timestamp.today().normalize()
    p.add_argument("--screener-file",default="screener (6).py")
    p.add_argument("--start",default=(today-pd.DateOffset(years=5)).strftime("%Y-%m-%d"));p.add_argument("--end",default=today.strftime("%Y-%m-%d"))
    p.add_argument("--capital",type=float,default=100000);p.add_argument("--universe",choices=["sp500","full"],default="sp500")
    p.add_argument("--universe-file",default="",help="ticker 또는 symbol 열이 있는 CSV")
    p.add_argument("--classes",default="HOT_SETUP,PULLBACK,BREAKOUT")
    p.add_argument("--max-positions",type=int,default=6);p.add_argument("--max-position-pct",type=float,default=.15);p.add_argument("--account-risk",type=float,default=.005)
    p.add_argument("--max-structure-risk",type=float,default=.07);p.add_argument("--order-valid-days",type=int,default=5)
    p.add_argument("--commission",type=float,default=0.0005);p.add_argument("--sec-fee",type=float,default=0.0000278);p.add_argument("--slippage",type=float,default=.001)
    p.add_argument("--cache-dir",default="us_backtest_cache");p.add_argument("--output-dir",default="us_backtest_output")
    return p.parse_args()


def main():
    args=args_parser();sc=load_screener(args.screener_file);start=pd.Timestamp(args.start);end=pd.Timestamp(args.end);warm=start-pd.Timedelta(days=550)
    print("1/5 유니버스 구성");tickers=read_universe(args,sc);print(f"종목 {len(tickers):,}개")
    print("2/5 가격 다운로드/캐시");raw=download_prices(sorted(set(tickers+["SPY","QQQ"])),warm.strftime("%Y-%m-%d"),end.strftime("%Y-%m-%d"),Path(args.cache_dir))
    bench={t:raw[t] for t in ("SPY","QQQ") if t in raw};data=prepare_all({t:d for t,d in raw.items() if t not in {"SPY","QQQ"}},sc)
    if len(bench)<2:raise RuntimeError("SPY/QQQ 데이터가 없습니다.")
    print(f"3/5 준비 완료 종목 {len(data):,}개")
    dates=sorted(set().union(*[set(d.index[(d.index>=start)&(d.index<=end)]) for d in data.values()]));calendar=pd.DatetimeIndex(dates)
    print("4/5 백테스트");e,t,s=run(args,data,bench,sc,calendar);summary=stats(e,t,args.capital)
    print("5/5 저장");out=Path(args.output_dir);out.mkdir(parents=True,exist_ok=True)
    e.to_csv(out/"equity_curve.csv",encoding="utf-8-sig");t.to_csv(out/"trades.csv",index=False,encoding="utf-8-sig");s.to_csv(out/"signals.csv",index=False,encoding="utf-8-sig")
    yearly=e.equity.resample("YE").last().pct_change(); yearly.iloc[0]=e.equity.resample("YE").last().iloc[0]/args.capital-1;yearly.rename("return").to_csv(out/"yearly_returns.csv",encoding="utf-8-sig")
    (out/"summary.json").write_text(json.dumps(summary,indent=2,ensure_ascii=False),encoding="utf-8");(out/"run_config.json").write_text(json.dumps(vars(args),indent=2,ensure_ascii=False),encoding="utf-8")
    print(json.dumps(summary,indent=2,ensure_ascii=False));print(f"결과: {out.resolve()}")
if __name__=="__main__":main()
