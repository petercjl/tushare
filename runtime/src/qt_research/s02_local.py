"""Offline S02 adapter: reuse preserved PTrade strategy; replay causal minute bars."""
from pathlib import Path
from types import SimpleNamespace as NS
from decimal import Decimal
import hashlib
import importlib.util
import json
import math
import os
import pandas as pd
import pyarrow.parquet as pq
from .orders import OrderBook, OrderRequest, SecurityRule
from .execution import BarMatcher, Bar, ExecutionConfig
from .event_engine import RunResult
from .core_report import write_report
from .nav_availability import load_evidence, apply_evidence
from .reference_audit import audit as audit_reference
from .strategy_source import verify as verify_strategy_source

from qt_research.paths import ROOT, ENGINE
SYMBOLS = ['518880.SH','159985.SZ','501018.SH','161226.SZ','513100.SH','159915.SZ','511220.SH','511880.SH']


def native(symbol):
    return symbol.replace('.SS','.SH')


def broker(symbol):
    return symbol.replace('.SH','.SS')


def checked_file(root, filename, digest):
    root = Path(root).resolve()
    file = (root/filename).resolve()
    if not file.is_relative_to(root) or hashlib.sha256(file.read_bytes()).hexdigest()!=digest:
        raise ValueError('Unsafe data path or checksum mismatch: '+filename)
    return file


class OfflineData:
    def __init__(self, minute_root, snapshot, start, end, nav_evidence=None, reference_mismatch='strict', dividend_root=None):
        self.start,self.end = pd.Timestamp(start),pd.Timestamp(end)
        if self.start > self.end: raise ValueError('Start must not exceed end')
        minute_root,snapshot = Path(minute_root),Path(snapshot)
        report = json.loads((snapshot/'report.json').read_text())
        self.nav_provider = ','.join(sorted({item.get('provider', 'legacy_sxsc_snapshot') for item in report['results'] if item['api'] == 'fund_nav' and item['state'] == 'nonempty'}))
        tables={name:[] for name in ['trade_cal','fund_nav','fund_adj','fund_daily']}
        for item in report['results']:
            if item['api'] in tables and item['state']=='nonempty':
                file=checked_file(snapshot,item['file'],item['sha256'])
                tables[item['api']].append(pd.read_parquet(file))
        tables={k:pd.concat(v,ignore_index=True) for k,v in tables.items()}
        cal=tables['trade_cal'].drop_duplicates('cal_date').sort_values('cal_date')
        self.calendar=pd.DatetimeIndex(pd.to_datetime(cal.loc[cal.is_open==1,'cal_date'],format='%Y%m%d'))
        self.days=self.calendar[(self.calendar>=self.start)&(self.calendar<=self.end)]
        if len(self.days)<2 or self.calendar.min()>self.start-pd.Timedelta(days=45):
            raise ValueError('Calendar or warm-up coverage insufficient')
        self.nav=tables['fund_nav']
        if self.nav.duplicated(['ts_code','nav_date']).any():raise ValueError('Duplicate NAV')
        self.nav_evidence = load_evidence(nav_evidence, self.nav) if nav_evidence else {}
        self.daily={};self.sessions={};self.coverage=[];self.provenance={};self.reference_issues=[]
        manifest=json.loads((minute_root/'manifest.json').read_text())
        for symbol in SYMBOLS:
            item=next(x for x in manifest['files'] if x['symbol']==symbol)
            file=checked_file(minute_root,item['file'],item['sha256'])
            frame=pq.read_table(file,filters=[('trade_date','>=',self.calendar.min()),('trade_date','<=',self.end)]).to_pandas(ignore_metadata=True)
            frame=frame.sort_values('trade_time')
            if frame.duplicated('trade_time').any() or not frame.ts_code.eq(symbol).all():raise ValueError('Invalid minute identity')
            daily=frame.groupby('trade_date').agg(close=('close','last'),volume=('vol','sum'),amount=('amount','sum'),factor=('adj_factor','first'))
            factors=tables['fund_adj'].loc[tables['fund_adj'].ts_code==symbol].copy()
            factors['day']=pd.to_datetime(factors.trade_date,format='%Y%m%d')
            factors=factors.set_index('day').adj_factor
            daily['factor']=factors.reindex(daily.index)
            if daily.factor.isna().any():raise ValueError('Adjustment factor gap: '+symbol)
            old=tables['fund_daily'].loc[tables['fund_daily'].ts_code==symbol].copy()
            old['day']=pd.to_datetime(old.trade_date,format='%Y%m%d');old=old.set_index('day')
            reference_summary, issues = audit_reference(symbol, daily, old, reference_mismatch)
            self.reference_issues.extend(issues)
            self.daily[broker(symbol)]=daily
            sessions={day:part.set_index('trade_time') for day,part in frame.groupby('trade_date') if self.start<=day<=self.end}
            for day in self.days:
                if day not in sessions:raise ValueError('Missing session: '+symbol+str(day))
                expected=pd.date_range(day+pd.Timedelta(hours=9,minutes=30),day+pd.Timedelta(hours=11,minutes=30),freq='min').append(pd.date_range(day+pd.Timedelta(hours=13,minutes=1),day+pd.Timedelta(hours=15),freq='min'))
                if not sessions[day].index.equals(expected):raise ValueError('Incomplete minute grid: '+symbol+str(day))
            self.sessions[broker(symbol)]=sessions
            self.provenance[symbol]=item['sha256']
            self.coverage.append(dict(symbol=symbol,days=len(sessions),**reference_summary))
        self.dividends=[]
        for symbol in SYMBOLS:
            folder=Path(dividend_root or ROOT/'data')/('s02-fund-div-'+symbol.split('.')[0]+'-v1')
            meta=json.loads((folder/'manifest.json').read_text())
            div=pd.read_parquet(checked_file(folder,'data.parquet',meta['sha256']))
            self.hashes_div = getattr(self, 'hashes_div', {})
            self.hashes_div[symbol] = meta['sha256']
            for row in div.to_dict('records'):
                if row.get('div_proc')!='实施' or not row.get('ex_date'):continue
                ex=pd.Timestamp(row['ex_date'])
                if not self.start<=ex<=self.end:continue
                if row['ts_code']!=symbol or not row.get('pay_date') or not row.get('record_date'):raise ValueError('Dividend dates unknown')
                ann,record,pay=map(pd.Timestamp,[row['ann_date'],row['record_date'],row['pay_date']])
                value=float(row['div_cash'])
                if not math.isfinite(value) or value<0 or not ann<=record<ex<=pay:raise ValueError('Dividend chronology/value invalid')
                self.dividends.append(dict(symbol=broker(symbol),id=symbol+str(ex.date()),ann=ann,record=record,ex=ex,pay=pay,per_share=value))
        self.hashes={'minute_manifest':hashlib.sha256((minute_root/'manifest.json').read_bytes()).hexdigest(),'nav_snapshot':hashlib.sha256((snapshot/'report.json').read_bytes()).hexdigest()}
        if nav_evidence:
            self.hashes['nav_publication_evidence'] = hashlib.sha256((Path(nav_evidence)/'manifest.json').read_bytes()).hexdigest()


class S02Runtime:
    def __init__(self, data, source, cash, participation='0.25'):
        self.data=data;self.time=pd.Timestamp(data.start)+pd.Timedelta(hours=9)
        self.events=[];self.errors=[];self.prices={};self.sequence=0;self.cursor=0
        for issue in getattr(data, 'reference_issues', []):
            row=dict(event='data_validation',time=self.time.isoformat()+'+08:00',level='ERROR',
                     message='DATA_REFERENCE_ERROR|'+json.dumps(issue,ensure_ascii=False),detail=issue)
            self.events.append(row);self.errors.append(row)
            print(self.time,'ERROR',row['message'],flush=True)
        self.cashflows=[];self.receivables={};self.entitlements={};self.dividend_cost_adjustments={}
        # T+1 conservatively configured for all; S02 never rebuys a stopped symbol on the same day.
        self.book=OrderBook(cash,{s:SecurityRule() for s in data.sessions})
        self.config=ExecutionConfig(commission_rate='0.00005',minimum_commission='5',participation=participation,slippage='0.0001',commission_exempt_symbols=('511880.SS',))
        self.matcher=BarMatcher(self.book,self.config)
        spec=importlib.util.spec_from_file_location('s02_preserved_runtime',source)
        self.s=importlib.util.module_from_spec(spec)
        services=dict(is_trade=lambda:False,get_frequency=lambda:'minute',log_info=self.log,
            get_stock_info=lambda security,field:{security:{'listed_date':str(data.daily[security].index.min().date())}},
            check_limit=lambda security:{security:0},get_trade_days=self.trade_days,get_history=self.history,
            get_price=self.get_price,fetch_nav_window=self.fetch_nav,order=self.order,get_orders=self.orders,
            get_open_orders=lambda security=None:[o for o in self.orders() if o.status in ('accepted','partially_filled') and (security is None or o.security==security)],
            affordable_shares=self.affordable)
        services.update({name:lambda *a,**kw:None for name in ['set_universe','set_benchmark','set_commission','set_slippage','record']})
        mode=os.environ.get('STRATEGY_RUNTIME','local')
        if mode!='local':raise ValueError('Local replay requires STRATEGY_RUNTIME=local; ptrade mode runs in the broker client')
        self.s._RUNTIME_MODE_OVERRIDE=mode;self.s._LOCAL_API=services
        self.s._NAV_SOURCE_OVERRIDE=getattr(data,'nav_provider','offline_snapshot')+'.fund_nav'
        spec.loader.exec_module(self.s)
        s=self.s;s.g=NS()
        if getattr(s,'SINGLE_SOURCE_CONTRACT',None)==1:
            verify_strategy_source(source.read_text())
        else:
            # Preserve compatibility with immutable pre-1.4 source snapshots.
            for name,fn in services.items():
                if name!='log_info':setattr(s,name,fn)
            s.log=NS(info=self.log);s.NAV_SOURCE=self.s._NAV_SOURCE_OVERRIDE
        self.context=NS(blotter=NS(current_dt=self.time.to_pydatetime()),previous_date=data.calendar[data.calendar<data.start][-1].date(),portfolio=NS())
        self.sync();s.initialize(self.context)

    def log(self, message):
        level='INFO'
        if message.startswith('HUMAN|净值缺失|') or message.startswith('HUMAN|溢价取数异常|') or '"event": "nav_provider_error"' in message:
            level='ERROR'
        row={'event':'strategy_log','time':self.time.isoformat()+'+08:00','level':level,'message':message}
        if level=='ERROR':self.errors.append(row);print(self.time,'ERROR',message,flush=True)
        self.events.append(row)

    def sync(self):
        positions={}
        for symbol,p in self.book.ledger.positions.items():
            adjusted=p.cost-self.dividend_cost_adjustments.get(symbol,Decimal('0'))
            positions[symbol]=NS(amount=p.quantity,enable_amount=self.book.available_quantity(symbol),last_sale_price=float(self.prices[symbol]),cost_basis=float(max(Decimal('0'),adjusted)/p.quantity))
        self.context.portfolio=NS(cash=float(self.book.ledger.cash),positions=positions,portfolio_value=float(self.book.ledger.equity(self.prices)+sum(self.receivables.values(),Decimal('0'))))
        self.context.blotter.current_dt=self.time.to_pydatetime()

    def trade_days(self,start_date=None,end_date=None):
        end=pd.Timestamp(end_date)
        if end>=self.time.normalize():raise ValueError('Calendar query beyond previous session')
        start=pd.Timestamp(start_date)
        return [d.strftime('%Y%m%d') for d in self.data.calendar if start<=d<=end]

    def daily_view(self,security,fq):
        df=self.data.daily[security].loc[lambda f:f.index<self.time.normalize()].copy()
        if fq is not None:
            current=float(self.data.daily[security].loc[self.time.normalize(),'factor'])
            df['close']=df.close*df.factor/current
        df['is_open']=1
        return df

    def history(self,count,frequency,field,security_list,fq=None,include=False,is_dict=False):
        if frequency=='1d':
            if include:raise ValueError('Current daily bar is unavailable')
            return self.daily_view(security_list,fq)[field].tail(count)
        if frequency=='1m':
            day=self.data.sessions[security_list][self.time.normalize()]
            return day.loc[day.index<=self.time].rename(columns={'vol':'volume'})[field].tail(count)
        raise ValueError('Unsupported history frequency')

    def get_price(self,security,start_date,end_date,frequency,fields,fq=None,is_dict=False):
        start,end=pd.Timestamp(start_date),pd.Timestamp(end_date)
        if end>=self.time.normalize():raise ValueError('Future/current-day daily access blocked')
        return self.daily_view(security,fq).loc[start:end,fields]

    def fetch_nav(self,security,start,end):
        frame=self.data.nav
        selected=frame.loc[(frame.ts_code==native(security))&(frame.nav_date.astype(str)>=pd.Timestamp(start).strftime('%Y%m%d'))&(frame.nav_date.astype(str)<=pd.Timestamp(end).strftime('%Y%m%d'))]
        result = self.s.parse_nav_window(selected,security,start,end,self.s.g.nav_asof)
        for date, record in result.items():
            witness = getattr(self.data, 'nav_evidence', {}).get((native(security), pd.Timestamp(date).strftime('%Y%m%d')))
            if witness:
                corrected = apply_evidence(record, witness, self.s.g.nav_asof)
                if corrected != record:
                    self.events.append(dict(event='nav_publication_verified', time=self.time.isoformat()+'+08:00',
                                            security=security, valuation_date=date,
                                            provider_announcement_date=record.get('announcement_date'), **witness))
                    result[date] = corrected
        return result

    def orders(self):
        return [NS(id=o.request.order_id,security=o.request.symbol,filled=o.filled,status=o.status) for o in self.book.orders.values()]

    def adjust_distribution_cost_after_sell(self, symbol, before_quantity):
        pos = self.book.ledger.positions.get(symbol)
        after = pos.quantity if pos else 0
        if before_quantity and after < before_quantity:
            old = self.dividend_cost_adjustments.get(symbol, Decimal('0'))
            self.dividend_cost_adjustments[symbol] = old * after / before_quantity

    def affordable(self,context,security,price):
        limit=Decimal(str(price))*Decimal('1.01')
        qty=int(self.book.available_cash/limit)//100*100
        while qty and qty*limit+self.config.fee_total(qty*limit, security)>self.book.available_cash:qty-=100
        return qty

    def order(self,security,amount):
        self.sequence+=1;side='buy' if amount>0 else 'sell';qty=abs(amount)
        limit=self.prices[security]*(Decimal('1.01') if side=='buy' else Decimal('0.99'))
        fee=self.config.fee_total(limit*qty*(Decimal('1') if side=='buy' else Decimal('1.1')), security)
        request=OrderRequest(f's02-{self.sequence:06d}',security,side,qty,limit,fee)
        result=self.matcher.submit(request,self.time.isoformat()+'+08:00')
        self.events.append(dict(event='submit',time=self.time.isoformat()+'+08:00',symbol=security,quantity=qty,side=side,order_id=request.order_id,status=result.status,reason=result.reason))
        return request.order_id

    def drain(self):
        for row in self.book.events[self.cursor:]:self.events.append(dict(row,time=row.get('time',self.time.isoformat()+'+08:00')))
        self.cursor=len(self.book.events)

    def distributions(self,day):
        for action in self.data.dividends:
            if action['ex']==day:
                qty=self.entitlements.get(action['id'],0)
                value=Decimal(str(action['per_share']))*qty
                if value:
                    self.receivables[action['id']]=value
                    symbol=action['symbol']
                    self.dividend_cost_adjustments[symbol]=self.dividend_cost_adjustments.get(symbol,Decimal('0'))+value
                    row=dict(event='dividend_entitlement',time=self.time.isoformat()+'+08:00',symbol=symbol,amount=str(value),record_date=str(action['record'].date()),quantity=qty,pay_date=str(action['pay'].date()),id=action['id'])
                    self.cashflows.append(row);self.events.append(row)
            if action['pay']<=day and action['id'] in self.receivables:
                value=self.receivables.pop(action['id']);self.book.ledger.credit_cash(action['id'],value)
                self.events.append(dict(event='dividend_paid',time=self.time.isoformat()+'+08:00',symbol=action['symbol'],amount=str(value),id=action['id']))

    def run(self):
        equity=[dict(time=str(self.data.start.date())+'T00:00:00+08:00',cash=str(self.book.ledger.cash),equity=str(self.book.ledger.cash),reserved_cash='0',nav=1.,baseline=True)]
        holdings=[];signals=[]
        for n,day in enumerate(self.data.days):
            self.time=day+pd.Timedelta(hours=9,minutes=30)
            for order in self.book.active_orders:self.book.cancel(order.request.order_id,'session_expired')
            self.book.start_session(str(day.date()));self.distributions(day)
            self.context.previous_date=self.data.calendar[self.data.calendar<day][-1].date()
            if n%20==0:print(json.dumps({'progress_day':n+1,'total_days':len(self.data.days),'date':str(day.date())}),flush=True)
            timestamps=[day+pd.Timedelta(hours=9,minutes=35),*pd.date_range(day+pd.Timedelta(hours=14),day+pd.Timedelta(hours=15),freq='min')]
            for time in timestamps:
                self.time=time;bars={}
                for symbol,sessions in self.data.sessions.items():
                    row=sessions[day].loc[time];price=Decimal(str(row.close));volume=Decimal(str(row.vol))
                    if volume!=volume.to_integral_value():raise ValueError('Noninteger share volume')
                    prior=self.book.ledger.positions.get(symbol)
                    self.matcher.match(Bar(time.isoformat()+'+08:00',symbol,price,int(volume)))
                    self.adjust_distribution_cost_after_sell(symbol, prior.quantity if prior else 0)
                    self.prices[symbol]=price
                    bars[symbol]=NS(dt=time.to_pydatetime(),price=float(price),is_open=1)
                self.sync();self.drain();self.s.handle_data(self.context,bars);self.drain()
                if time.hour==14 and time.minute==0:
                    plan=self.s.g.execution['rotation'];signals.append(dict(time=time.isoformat()+'+08:00',targets=plan['targets'],unknown=plan['unknown'],excluded=plan['excluded']))
                    for symbol in self.dividend_cost_adjustments:
                        if symbol not in self.book.ledger.positions:self.dividend_cost_adjustments[symbol]=Decimal('0')
            for order in self.book.active_orders:self.book.cancel(order.request.order_id,'day_order_expired')
            self.drain();self.sync();self.s.after_trading_end(self.context,bars)
            value=Decimal(str(self.context.portfolio.portfolio_value))
            equity.append(dict(time=self.time.isoformat()+'+08:00',cash=str(self.book.ledger.cash),equity=str(value),reserved_cash=str(self.book.reserved_cash),nav=float(value/self.book.ledger.initial_cash),baseline=False))
            for symbol,p in self.book.ledger.positions.items():holdings.append(dict(time=self.time.isoformat()+'+08:00',symbol=native(symbol),quantity=p.quantity,cost=str(p.cost),market_value=str(self.prices[symbol]*p.quantity),available=self.book.available_quantity(symbol)))
            for action in self.data.dividends:
                if action['record']==day:
                    pos=self.book.ledger.positions.get(action['symbol']);self.entitlements[action['id']]=pos.quantity if pos else 0
        result=RunResult(self.book,equity,holdings,signals,self.events)
        result.cashflows=self.cashflows
        return result


def run(args):
    if Path(args.out).exists():raise FileExistsError('Output exists; select a new directory')
    if args.initial_cash <= 0: raise ValueError('Positive initial cash required')
    source=ROOT/'strategies/S02-local-v1/strategy.py'
    data=OfflineData(args.minutes,args.snapshot,args.start,args.end,getattr(args,'nav_evidence',None),getattr(args,'reference_mismatch','strict'),getattr(args,'dividend_root',None))
    runtime=S02Runtime(data,source,args.initial_cash,args.participation)
    result=runtime.run()
    fallback_successes=sum(e.get('message','').startswith('HUMAN|轮动溢价回退|') and '溢价=None' not in e['message'] for e in runtime.events)
    unresolved=sum(any('F04安全日期溢价不可用' in reason for _,reason in signal['unknown']) for signal in result.signals)
    folder=Path(getattr(args,'benchmark_folder',None) or ROOT/'data/s02-benchmark-000300-v1');meta=json.loads((folder/'manifest.json').read_text())
    frame=pq.read_table(checked_file(folder,'data.parquet',meta['sha256'])).to_pandas(ignore_metadata=True)
    frame.trade_date=pd.to_datetime(frame.trade_date);bench=frame.set_index('trade_date').close.sort_index()
    base=float(bench.loc[bench.index<data.days[0]].iloc[-1]);benchmark={str(day.date()):float(bench.loc[day])/base for day in data.days}
    manifest={'schema':1,'strategy':'S02-local-v1','strategy_source_version':'PTrade 1.3.0','strategy_source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'start':str(data.days[0].date()),'end':str(data.days[-1].date()),'initial_cash':args.initial_cash,'frequency':'1min','benchmark':'000300.SH','benchmark_sha256':meta['sha256'],'data_hashes':data.hashes,'minute_file_hashes':data.provenance,'coverage':data.coverage,'source_sha256':{name:hashlib.sha256((ENGINE/name).read_bytes()).hexdigest() for name in ['s02_local.py','execution.py','ledger.py','orders.py','core_report.py']},'description':'S02本地一分钟迁移：14:00信号，卖出完成后买入，逐分钟续单，历史净值按公告可见性过滤。','execution':{'model':'next-minute close; partial orders remain active; 25% volume participation default','participation':args.participation,'commission_rate':'0.00005','minimum_commission':5,'commission_exempt_symbols':['511880.SS'],'slippage':'0.0001','protected_limit_buffer':'0.01','settlement':'conservative T+1 for all; no same-day stop reentry'},'data_errors':len(runtime.errors),'performance_valid':not runtime.errors,'status':'data_error' if runtime.errors else 'research_completed','cash_dividends':'record-date entitlement; ex-date receivable; pay-date spendable cash; no tax model','limitations':['Historical data revisions and publication intraday timestamps not independently verified','No exchange queue or historical limit-up/down matching','Trading rules conservatively T+1; broker parity not calibrated','Daily wealth includes dividend receivables; fund cash distributions do not imply live parity'],'trading_days':252,'risk_free':0.0}
    manifest['source_sha256']['nav_availability.py'] = hashlib.sha256((ENGINE/'nav_availability.py').read_bytes()).hexdigest()
    manifest['source_sha256']['reference_audit.py'] = hashlib.sha256((ENGINE/'reference_audit.py').read_bytes()).hexdigest()
    manifest['source_sha256']['strategy_source.py'] = hashlib.sha256((ENGINE/'strategy_source.py').read_bytes()).hexdigest()
    manifest.update(strategy_source_version=runtime.s.STRATEGY_VERSION,runtime_mode=getattr(runtime.s,'RUNTIME_MODE','legacy_local'),
                    strategy_build_id=getattr(runtime.s,'STRATEGY_BUILD_ID',None),strategy_core_version=getattr(runtime.s,'STRATEGY_CORE_VERSION',None))
    manifest.update(reference_mismatch_policy=getattr(args,'reference_mismatch','strict'),reference_issues=data.reference_issues)
    manifest.update(nav_provider=data.nav_provider,nav_publication_policy='provider announcement conservative next day; exact-value independent dated disclosures override only with prior-day availability',nav_independent_witnesses=len(data.nav_evidence),dividend_data_hashes=data.hashes_div,nav_fallback_successes=fallback_successes,unresolved_nav_signal_days=unresolved,listing_date_semantics='earliest verified cached observation used only as listed-before-backtest proof',validation_status='local research run; broker matching parity pending')
    summary=write_report(args.out,result,manifest,benchmark)
    return {'out':str(Path(args.out).resolve()),'metrics':summary['metrics'],'orders':summary['orders'],'fills':summary['fills'],'data_errors':len(runtime.errors),'performance_valid':not runtime.errors}
