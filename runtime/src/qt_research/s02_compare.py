"""Offline S02 cross-platform comparison, retaining native and common metrics."""
from pathlib import Path
import ast
import hashlib
import html
import json
import re
from decimal import Decimal, ROUND_HALF_UP
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from .core_report import overview
from .s02_local import checked_file, native
from .local_cli import inspect_result


def symbol(value):
    return native(value.replace('.XSHG', '.SH').replace('.XSHE', '.SZ'))


def target_rows(lines):
    result = {}
    for line in lines:
        line = html.unescape(line)
        if 'HUMAN|轮动共同目标|' not in line:
            continue
        match = re.search(r'买入目标=(\[[^\]]*\])', line)
        if not match:
            raise ValueError('Malformed S02 target log')
        day = line[:10]
        targets = sorted(symbol(x) for x in ast.literal_eval(match.group(1)))
        if day in result:
            raise ValueError('Duplicate S02 daily target')
        result[day] = targets
    return result


def aligned_dates(left, right):
    if left.duplicated().any() or right.duplicated().any() or list(left) != list(right):
        raise ValueError('Comparison dates differ or contain duplicates')


def observed_jq_fill_price(close, side):
    """Empirical S02 pricing contract, independently checked on all 199 fills."""
    if side not in ('buy', 'sell'):
        raise ValueError('Unknown order side')
    sign = 1 if side == 'buy' else -1
    return float((Decimal(str(close)) * (Decimal(1) + sign * Decimal('0.00005'))).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP))


def market_comparison(logfile, minute_root, transactions):
    payload = json.loads(Path(logfile).read_text())
    if payload.get('max') or payload.get('state') != '2':
        raise ValueError('Market probe logs are incomplete')
    records = []
    for line in payload['logs']:
        text = html.unescape(line)
        if 'JQ_MARKET_CHECK|' not in text:
            continue
        row = json.loads(text.split('JQ_MARKET_CHECK|', 1)[1])
        if 'error' in row:
            raise ValueError('JQ historical minute probe failed')
        frame = row['frame']
        expected = pd.date_range(row['day']+' 13:59',row['day']+' 14:02',freq='min')
        # JQ's old pandas serializes naive exchange clocks with trailing Z.
        # Bind them to explicitly requested exchange wall clocks, not UTC.
        clocks = pd.to_datetime([x[:19] for x in frame['index']])
        if not clocks.equals(expected):
            raise ValueError('Unexpected exchange clock in minute probe')
        f = pd.DataFrame(frame['data'],columns=frame['columns'])
        f['time'] = clocks;f['symbol']=symbol(row['code'])
        records.append(f)
    prices=pd.concat(records,ignore_index=True)
    if prices.duplicated(['symbol','time']).any():
        raise ValueError('Duplicate minute observations')
    root=Path(minute_root);meta=json.loads((root/'manifest.json').read_text());parts=[]
    for code, part in prices.groupby('symbol'):
        item=next(x for x in meta['files'] if x['symbol']==code)
        file=checked_file(root,item['file'],item['sha256'])
        f=pq.read_table(file,filters=[('trade_date','>=',part.time.min().normalize()),('trade_date','<=',part.time.max().normalize())],columns=['trade_time','close','vol']).to_pandas(ignore_metadata=True)
        f=f.rename(columns={'trade_time':'time','close':'local_close','vol':'local_volume'});f['symbol']=code;parts.append(f)
    combined=prices.merge(pd.concat(parts),on=['symbol','time'],how='left',validate='one_to_one')
    if combined.local_close.isna().any():
        raise ValueError('Local minute coverage gap')
    combined['quote_difference']=combined.local_close-combined.close
    t=transactions.copy();t['time']=pd.to_datetime(t.date+' '+t.order_time)
    result=t.merge(combined,on=['symbol','time'],validate='one_to_one')
    result['predicted_jq_price']=[observed_jq_fill_price(r.close,r.side) for r in result.itertuples()]
    result['pricing_verified']=np.isclose(result.predicted_jq_price,result.jq_price,atol=1e-9,rtol=0)
    return combined, result


def compare(args):
    out, local, jq = map(Path, [args.out, args.local_out, args.jq_dir])
    if out.exists():
        raise FileExistsError('Comparison output exists')
    inspect_result(local)
    manifest = json.loads((local/'manifest.json').read_text())
    result_payload = json.loads((jq/'jq-result.json').read_text())
    data = result_payload['data']['result']
    jqmetrics = json.loads((jq/'jq-show.json').read_text())['metrics']
    # Both chart API and web CSV return rounded percentages. Audit logs preserve
    # full daily account equity; reconcile both independently.
    def curve(key):
        item = data[key]
        return pd.Series(item['value'], index=pd.to_datetime(item['time'], unit='ms', utc=True).tz_convert('Asia/Shanghai').strftime('%Y-%m-%d')) / 100
    returns, bench = curve('overallReturn'), curve('benchmark')
    aligned_dates(pd.Series(returns.index), pd.Series(bench.index))
    le = pd.read_csv(local/'equity.csv')
    aligned_dates(le.date, pd.Series(returns.index))
    capital = float(manifest['initial_cash'])
    if json.loads((jq/'jq-show.json').read_text())['status'] != 'done':
        raise ValueError('JQ run has not finished')
    jt = pd.read_csv(jq/'export/clean/transactions.normalized.csv')
    for name in ['transactions', 'positions']:
        raw = json.loads((jq/('jq-'+name+'.json')).read_text())
        if not raw['complete']:
            raise ValueError('Incomplete JoinQuant '+name)
    accounts = [json.loads(s) for s in (jq/'export/clean/logs.audit.jsonl').read_text().splitlines()]
    accounts = [x for x in accounts if x['event'] == 'daily_account']
    exact = pd.Series([x['equity']/capital-1 for x in accounts], index=[x['dt'][:10] for x in accounts])
    aligned_dates(pd.Series(exact.index), pd.Series(returns.index))
    if not np.allclose(exact, returns, atol=0.000051, rtol=0):
        raise ValueError('JQ daily account / chart mismatch')
    returns = exact
    if not np.isclose(float(returns.iloc[-1]), jqmetrics['algorithm_return'], atol=1e-9, rtol=0):
        raise ValueError('JQ result / metrics mismatch')
    jp = pd.read_csv(jq/'export/clean/positions.normalized.csv')
    jp['date'] = jp.date.astype(str)
    jwealth = jp.groupby('date', sort=True).market_value.sum()
    aligned_dates(pd.Series(jwealth.index), pd.Series(returns.index))
    if not np.allclose(jwealth.to_numpy(), (1+returns.to_numpy())*capital, atol=0.1, rtol=0):
        raise ValueError('JQ positions do not reconcile with configured initial cash / curve')
    events = [json.loads(s) for s in (local/'events.jsonl').read_text().splitlines()]
    jlogs = html.unescape((jq/'export/clean/logs.raw.txt').read_text()).splitlines()
    jtargets = target_rows(jlogs)
    ltargets = target_rows([e['time'][:10]+' '+e['message'] for e in events if e.get('event') == 'strategy_log'])
    if set(jtargets) != set(returns.index) or set(ltargets) != set(returns.index):
        raise ValueError('Incomplete daily target logs')
    targets = pd.DataFrame([dict(date=d, jq_targets=jtargets[d], local_targets=ltargets[d], equal=jtargets[d]==ltargets[d]) for d in returns.index])
    navlogs = html.unescape(Path(args.jq_nav_log).read_text()).splitlines()
    navrows = [json.loads(s.split('JQ_NAV_CHECK|',1)[1]) for s in navlogs if 'JQ_NAV_CHECK|' in s]
    if any(x['event']=='error' and x.get('api')=='get_extras' for x in navrows):
        raise ValueError('JQ unit NAV probe failed')
    jf = pd.DataFrame([dict(ts_code=symbol(x['code']), nav_date=x['day'].replace('-',''), jq_nav=x['value']) for x in navrows if x['event']=='unit_nav'])
    if jf.duplicated(['ts_code','nav_date']).any() or set(jf.ts_code) != set(manifest['minute_file_hashes']):
        raise ValueError('Duplicate or incomplete NAV probe symbols')
    snapshot = Path(args.snapshot)
    meta = json.loads((snapshot/'report.json').read_text())
    sf = pd.concat([pd.read_parquet(checked_file(snapshot,x['file'],x['sha256'])) for x in meta['results'] if x['api']=='fund_nav' and x['state']=='nonempty'])
    sf['nav_date']=sf.nav_date.astype(str)
    if sf.duplicated(['ts_code','nav_date']).any():
        raise ValueError('Duplicate SDK NAV')
    nav = jf.merge(sf[['ts_code','nav_date','unit_nav','ann_date']], on=['ts_code','nav_date'], how='outer', indicator=True)
    nav['difference'] = nav.jq_nav - nav.unit_nav
    nav['equal'] = nav._merge.eq('both') & nav.difference.abs().le(1e-8)
    # Only actual calls can affect premium decisions; pool-wide probe rows are separate.
    queried = set()
    for line in jlogs:
        match = re.search(r'标的=(\S+) 日期=(\d{4}-\d{2}-\d{2}) 阶段=成功',line)
        if match: queried.add((symbol(match.group(1)),match.group(2).replace('-','')))
    nav['used_by_jq_strategy']=[(r.ts_code,r.nav_date) in queried for r in nav.itertuples()]
    fills = pd.read_excel(local/'transactions.xlsx', sheet_name='成交')
    submissions = {x['order_id']:x for x in events if x.get('event')=='submit'}
    rows=[]
    for oid, part in fills.groupby('order_id', sort=False):
        sub=submissions[oid]; qty=int(part.quantity.sum())
        rows.append(dict(order_id=oid,date=sub['time'][:10],symbol=symbol(part.symbol.iloc[0]),side=part.side.iloc[0],local_quantity=qty,
                         local_price=float((part.quantity*part.price).sum()/qty),local_fee=float(part.fee.sum()),local_first_fill=part.time.iloc[0],local_last_fill=part.time.iloc[-1],local_fill_count=len(part)))
    lt=pd.DataFrame(rows)
    jt['symbol']=jt.code.map(symbol);jt['side']=jt.side.map({'买':'buy','卖':'sell'})
    jt['jq_quantity']=jt.filled_amount.abs();jt['jq_price']=jt.filled_price;jt['jq_fee']=jt.fee
    quotes, market = (None, None)
    if getattr(args,'jq_market_log',None):
        if not args.minutes:raise ValueError('Market comparison requires --minutes')
        quotes,market=market_comparison(args.jq_market_log,args.minutes,jt)
    keys=['date','symbol','side']
    lt['pair']=lt.groupby(keys).cumcount();jt['pair']=jt.groupby(keys).cumcount()
    trades=lt.merge(jt[keys+['pair','order_time','jq_quantity','jq_price','jq_fee']],on=keys+['pair'],how='outer',indicator=True)
    trades['price_difference']=trades.local_price-trades.jq_price
    trades['quantity_difference']=trades.local_quantity-trades.jq_quantity
    holdings=pd.read_excel(local/'transactions.xlsx',sheet_name='持仓')
    holdings['date']=holdings.time.str[:10];holdings['symbol']=holdings.symbol.map(symbol)
    holdings['local_close']=holdings.market_value/holdings.quantity
    jp=jp[jp.code.notna()].copy();jp['symbol']=jp.code.map(symbol)
    closes=holdings[['date','symbol','local_close']].merge(jp[['date','symbol','close_price']],on=['date','symbol'],how='outer',indicator=True)
    closes['difference']=closes.local_close-closes.close_price
    equity=pd.DataFrame(dict(date=returns.index,jq_equity=(1+returns.to_numpy())*capital,local_equity=le.equity.to_numpy(),jq_return=returns.to_numpy(),local_return=le.strategy_return.to_numpy()))
    equity['difference_yuan']=equity.local_equity-equity.jq_equity
    common=[]
    benchmark=dict(zip(returns.index,1+bench.to_numpy()))
    for label, values in [('JoinQuant',equity.jq_equity),('Local',equity.local_equity)]:
        observations=[dict(time=d+'T15:00:00+08:00',equity=v,baseline=False) for d,v in zip(equity.date,values)]
        metrics,_,_=overview(observations,capital,[],benchmark,250,0)
        common.append(dict(platform=label,**{k:v for k,v in metrics.items() if k not in {'trade_win_rate','profit_loss_ratio','winning_trades','losing_trades','flat_trades','closed_trades'}}))
    errors=[e for e in events if e.get('level')=='ERROR']
    summary=dict(schema=1,start=equity.date.iloc[0],end=equity.date.iloc[-1],initial_cash=capital,days=len(equity),jq_backtest_id=result_payload['id'],
                 target_mismatch_days=int((~targets.equal).sum()),nav_common_rows=int(nav._merge.eq('both').sum()),nav_mismatches=int((nav._merge.eq('both')&~nav.equal).sum()),
                 nav_mismatches_used=int((nav._merge.eq('both')&~nav.equal&nav.used_by_jq_strategy).sum()),sdk_only_nav_rows=int(nav._merge.eq('right_only').sum()),
                 local_orders=len(lt),jq_orders=len(jt),local_fills=len(fills),partial_local_orders=int((lt.local_fill_count>1).sum()),
                 paired_orders=int(trades._merge.eq('both').sum()),unpaired_orders=int((~trades._merge.eq('both')).sum()),
                 jq_native_metrics=jqmetrics,common_metrics=common,final_difference_yuan=float(equity.difference_yuan.iloc[-1]),
                 data_errors=len(errors),local_performance_valid=manifest['performance_valid'],close_price_max_difference=float(closes.difference.abs().max()),
                 local_total_fees=float(lt.local_fee.sum()),jq_total_fees=float(jt.jq_fee.sum()),
                 first_difference=equity.loc[equity.difference_yuan.abs()>0.01].head(1).to_dict('records'),
                 native_metric_policy='JQ observed annualization 250 days, native Sharpe=(annualized return-0.04)/annual volatility; local native 252 days and arithmetic daily-return Sharpe. Common table uses 250 days, zero risk-free, identical formulas.',
                 limitations=['Matching, sizing buffer and execution schedule differ; paired-order differences are descriptive, not independent causal attribution.', 'JQ historical get_extras has no first-publication timestamp; it cannot establish earlier public availability.', 'Strict local publication errors remain visible in result validity.'])
    out.mkdir(parents=True,mode=0o700)
    tables={'equity':equity,'targets':targets,'nav':nav,'trades':trades,'close_prices':closes,'common_metrics':pd.DataFrame(common),'nav_errors':pd.DataFrame(errors)}
    if market is not None:
        tables.update(minute_quotes=quotes,market_matching=market)
        summary.update(minute_quote_pairs=len(quotes),minute_close_differences=int(quotes.quote_difference.abs().gt(1e-9).sum()),minute_close_max_difference=float(quotes.quote_difference.abs().max()),
                       jq_pricing_verified_orders=int(market.pricing_verified.sum()),jq_pricing_total_orders=len(market),
                       jq_orders_exceeding_current_minute_25pct=int((market.jq_quantity>market.volume*.25).sum()),
                       observed_jq_matching='Same callback minute JQ close, +/- half of configured 0.0001 spread, rounded to 0.001; empirical S02 sample, not universal platform guarantee')
    for name,frame in tables.items():frame.to_csv(out/(name+'.csv'),index=False,mode='x')
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False))
    import plotly.graph_objects as go
    fig=go.Figure()
    for key,label in [('jq_return','聚宽'),('local_return','本地')]:fig.add_trace(go.Scatter(x=equity.date,y=equity[key]*100,name=label))
    fig.update_layout(title='S02 同期同资金对照（严格本地净值验证待完成）',yaxis_title='累计收益 %',xaxis_rangeslider_visible=True)
    page=fig.to_html(full_html=False,include_plotlyjs=True)
    page+='<h2>对照摘要</h2><pre>'+html.escape(json.dumps(summary,ensure_ascii=False,indent=2))+'</pre>'
    for name in ['common_metrics','nav_errors']:page+='<h2>'+name+'</h2>'+tables[name].to_html(index=False)
    (out/'report.html').write_text('<!doctype html><meta charset="utf-8">'+page)
    inputs=[local/'manifest.json',jq/'jq-result.json',jq/'jq-show.json',jq/'export/clean/logs.raw.txt',jq/'export/clean/logs.audit.jsonl',Path(args.jq_nav_log),snapshot/'report.json']
    if market is not None:inputs.extend([Path(args.jq_market_log),Path(args.minutes)/'manifest.json'])
    (out/'provenance.json').write_text(json.dumps({str(p.resolve()):hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs},indent=2))
    checks={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir() if p.is_file()}
    (out/'checksums.json').write_text(json.dumps(checks,indent=2))
    return summary
