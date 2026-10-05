"""Reconcile immutable minute aggregates with independent daily references."""
import pandas as pd


def audit(symbol, daily, reference, policy='strict'):
    if policy not in ('strict', 'warn'):
        raise ValueError('Unknown reference mismatch policy')
    joined = daily.join(reference[['close', 'vol', 'amount']], rsuffix='_reference')
    price = (joined.close - joined.close_reference).abs()
    volume = (joined.volume - joined.vol * 100).abs()
    amount = (joined.amount - joined.amount_reference * 1000).abs()
    vr = volume / (joined.vol * 100).clip(lower=1)
    ar = amount / (joined.amount_reference * 1000).clip(lower=1)
    missing = joined[['close_reference', 'vol', 'amount_reference']].isna().any(axis=1)
    different = (price > 1e-8) | (vr > 1e-6) | (ar > 1e-6)
    issues = []
    for day in joined.index[missing | different]:
        row = joined.loc[day]
        issues.append(dict(event='daily_reference_missing' if missing.loc[day] else 'daily_reference_mismatch',
                           symbol=symbol, date=str(day.date()),
                           minute=dict(close=float(row.close), volume_shares=float(row.volume), amount_yuan=float(row.amount)),
                           reference=None if missing.loc[day] else dict(close=float(row.close_reference), volume_shares=float(row.vol*100), amount_yuan=float(row.amount_reference*1000)),
                           policy=policy, data_used='original immutable minute bars; no correction inferred'))
    if issues and policy == 'strict':
        raise ValueError('Offline minute units/price reconciliation failed: '+symbol+' ('+str(len(issues))+' dates)')
    maximum = lambda values: float(values.max()) if values.notna().any() else None
    summary = dict(price_max_error=maximum(price), volume_max_error_shares=maximum(volume),
                   amount_max_error_yuan=maximum(amount), volume_relative_error=maximum(vr),
                   amount_relative_error=maximum(ar), reference_mismatch_days=int(different.sum()),
                   reference_missing_days=int(missing.sum()))
    return summary, issues
