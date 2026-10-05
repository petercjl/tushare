from pathlib import Path
import json
import re
from urllib.parse import urlsplit, parse_qs, urlencode, urlunsplit
from qt_research.public_tushare import credential

from qt_research.paths import ROOT, ENGINE

def settings():
    p = ROOT / 'config/settings.json'
    if not p.is_file(): raise ValueError('CONFIG_REQUIRED: create private config/settings.json with provider, token_file and sdk_url')
    return json.loads(p.read_text())

def token():
    return credential(settings()['token_file'])

def api():
    import tushare as ts
    config = settings()
    obj = ts.pro_api(token(), timeout=15)
    obj._DataApi__http_url = config['sdk_url']
    return obj

def mcp_url():
    config = settings()
    usage = Path(config['usage_file']).read_text()
    match = re.search(r'"url"\s*:\s*"(https://[^"\s]+)"', usage)
    if not match:
        raise ValueError('MCP URL absent in usage file')
    url = urlsplit(match.group(1))
    if url.scheme != 'https' or url.username or url.password:
        raise ValueError('Invalid MCP endpoint')
    query = parse_qs(url.query)
    if 'token' in query:
        query['token'] = [token()]
        return urlunsplit((url.scheme, url.netloc, url.path, urlencode(query, doseq=True), ''))
    raise ValueError('Expected token query in user-provided MCP URL')

def safe_error(exc):
    text = str(exc)
    try: text = text.replace(token(), '[REDACTED]')
    except Exception: pass
    text = re.sub(r'tsr_[A-Za-z0-9_-]+', '[REDACTED]', text)
    return re.sub(r'[0-9a-fA-F]{40,100}', '[REDACTED]', text)[:1000]
