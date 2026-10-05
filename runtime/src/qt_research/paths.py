import os
from pathlib import Path
ROOT=Path(os.environ.get('TUSHARE_ENV_ROOT',Path.home()/'tushare-environment')).expanduser().resolve()
ENGINE=Path(__file__).resolve().parent
