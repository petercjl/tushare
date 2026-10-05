"""Independent, dated publication witnesses for unit NAV availability.

Retain provider announcement metadata: a later report update must not erase a
verified earlier public value. These witnesses bound publication conservatively
to midnight AFTER the dated disclosure. They never estimate an absent release.
"""
from pathlib import Path
from decimal import Decimal
import hashlib
import json
from urllib.parse import urlsplit
import pandas as pd


def load_evidence(folder, nav):
    folder = Path(folder).resolve()
    meta = json.loads((folder / 'manifest.json').read_text())
    if meta['schema'] != 1:
        raise ValueError('Unsupported NAV evidence schema')
    result = {}
    for row in meta['records']:
        key = (row['ts_code'], row['nav_date'])
        if key in result:
            raise ValueError('Duplicate NAV publication evidence')
        path = (folder / row['witness']).resolve()
        if not path.is_relative_to(folder) or hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
            raise ValueError('NAV witness checksum/path mismatch')
        witness = json.loads(path.read_text())
        allowed = {'official_exchange_publication_table':'www.sge.com.cn', 'official_issuer_pcf':'api.efunds.com.cn'}
        if allowed.get(witness['type']) != urlsplit(witness['source_url']).hostname:
            raise ValueError('Unsupported independent NAV publication source')
        if 'raw_file' in witness:
            raw = (folder / witness['raw_file']).resolve()
            if not raw.is_relative_to(folder) or hashlib.sha256(raw.read_bytes()).hexdigest() != witness['raw_sha256']:
                raise ValueError('Raw publication checksum/path mismatch')
            if witness['type'] == 'official_issuer_pcf':
                payload = json.loads(raw.read_text())
                data = payload['data']; pcf = data['map']
                if payload['status'] != 1 or not data['isMapShow'] or pcf['PUBLISH'] != '是':
                    raise ValueError('PCF was not published')
                if str(pcf['SECURITYID']) != witness['fund_code'] or data['tDate'] != witness['publication_date'] or pcf['PRETRADINGDAY'] != witness['valuation_date'] or Decimal(str(pcf['NAV'])) != Decimal(str(witness['unit_nav'])):
                    raise ValueError('Raw PCF witness mismatch')
        elif witness['type'] == 'official_issuer_pcf':
            raise ValueError('Issuer PCF requires raw response')
        matches = nav.loc[(nav.ts_code == key[0]) & (nav.nav_date.astype(str) == key[1])]
        if len(matches) != 1 or witness['fund_code'] != key[0].split('.')[0]:
            raise ValueError('NAV witness identity mismatch')
        value = Decimal(str(row['unit_nav']))
        if not value.is_finite() or value <= 0 or value != Decimal(str(witness['unit_nav'])) or value != Decimal(str(matches.iloc[0].unit_nav)):
            raise ValueError('NAV witness value mismatch')
        published = pd.Timestamp(witness['publication_date'])
        valuation = pd.Timestamp(key[1])
        if 'valuation_date' in witness and pd.Timestamp(witness['valuation_date']) != valuation:
            raise ValueError('NAV witness valuation date mismatch')
        if published != published.normalize() or published <= valuation:
            raise ValueError('NAV publication chronology invalid')
        result[key] = dict(unit_nav=float(value), available_at=str(published + pd.Timedelta(days=1)),
                           source_url=witness['source_url'], witness_sha256=row['sha256'],
                           availability_evidence='independent_dated_public_value_conservative_next_day')
    return result


def apply_evidence(record, witness, asof):
    """Copy record only when exact value and prior-day disclosure are proven."""
    if Decimal(str(record['unit_nav'])) != Decimal(str(witness['unit_nav'])):
        raise ValueError('NAV publication value changed')
    available = pd.Timestamp(witness['available_at'])
    if available > pd.Timestamp(asof):
        return record
    current = record.get('available_at')
    if current is not None and pd.Timestamp(current) <= available:
        return record
    return dict(record, **witness, status='ok', reason='independently_published_before_decision_day')
