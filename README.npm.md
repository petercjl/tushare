# @petercjl/tushare

CLI + bundled portable Skill for local strategy backtests, shared PTrade source and a SQLite/Web research catalog.

Node.js 22+ and Python 3.11+ required. macOS command tests are exercised; Windows/NAS and fresh-session Agent parity are unverified.

```sh
npm install -g @petercjl/tushare
tushare-env setup
tushare-env skill install --agent codex
tushare-env doctor --json
```

Existing unmanaged Skills require reviewed `--adopt`, which creates a recoverable backup. Credentials, data and results stay outside the package. Installation grants no service permissions. Use `update check --tag latest`, then `update install --version EXACT --agent codex` to update runtime and Skill. Run `skill source/status` to verify the canonical source and managed install.

Current runners are S02 and a fixed-quantity baseline. New strategies require tested adapters; shared logic does not promise identical PTrade fills. Configure the private data root using TUSHARE_ENV_ROOT and external config/settings.json. Use backtest and research commands to retain logs, transaction Excel, metrics, curves and immutable code/run snapshots. Dashboard is read-only/loopback.
