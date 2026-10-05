---
name: tushare
description: Write local quantitative strategies with shared PTrade-compatible logic, acquire and validate Tushare data, run supported local backtests, archive results in SQLite and review the research dashboard. Use for local strategy research, ETF backtesting, data gaps, NAV validation and local/PTrade comparisons.
---

# Local strategy research

## Input → strategy → output
Accept a strategy goal or complete source, dates, capital, universe and execution assumptions. Produce versioned complete source, verified immutable data/report files, catalog run identity, validity/errors and a dashboard view. The Agent writes and analyzes code; the CLI performs data queries, checks, execution and registration. Ordinary research does not submit real orders.

## Discovery and configuration
Run `tushare-env version`, `capabilities --json`, `doctor --json` and `skill source`. Use `setup` to install the bundled Python runtime. Select the host adapter under `adapters/`; Node 22+ and Python 3.11+ are required. Discover the data root from doctor; it can be configured with TUSHARE_ENV_ROOT. Settings/credentials, caches, strategy working files, database and reports are external to the npm package. Load [runtime.md](references/runtime.md) only when configuring or diagnosing service access. Installation does not grant data permissions.

## Main line
1. **Select and write.** Inspect `research strategies`, versions and relevant runs. Match the intended strategy/version. For a new or modified strategy read [shared-source.md](references/shared-source.md), then deliver complete code, parameters and data/execution dependencies. Core ranking, filtering, risk and target decisions are shared with PTrade; platform data/order services are explicit adapters. Current executable runners are S02 and the fixed-quantity baseline. A new source registered in the catalog needs an implemented, tested runner before it can execute; report RUNNER_UNAVAILABLE rather than pretending arbitrary code is supported.
2. **Validate inputs.** Load [research.md](references/research.md) for a hypothesis, data gap or platform comparison. Establish schema, units, coverage, history visibility, costs and matching. Use the configured SDK/read-only CLI to acquire immutable caches; permission errors stop the affected API. Tool discovery is not evidence of access. NAV absence or failed fallback is an explicit error, retained in reports and validity.
3. **Run and archive.** Read [backtest-catalog.md](references/backtest-catalog.md). Use `backtest s02` or `backtest run` with an unused output directory. The runner snapshots source/engine, registers running, verifies files and records completed/warning/failed with parameters and attachments. Reports include logs, transaction Excel, equity, metrics, HTML and checksums. Never reuse an older run as the requested current execution.
4. **Verify and review.** Run `backtest inspect --out DIR` and `research show RUN_ID`; report source identity, actual dates/capital, run status, data validity and artifacts. Use `dashboard start/status` to display the read-only Web dashboard. A completed run with data errors remains an invalid/warning research result.
5. **Iterate or hand off.** Classify discrepancies as data, signal, execution or accounting. Repair implementation errors within the agreed intent; a changed trading hypothesis becomes a new recorded version. Return to step 1. For requested broker verification resolve the exact installed `ptradecli` Skill, read its complete current instructions, hand over the same complete source and dates/capital/build ID, execute its manual-client/cache contract, then return here for comparison. If unavailable report DEPENDENCY_MISSING; do not recreate a private fallback.

## Boundaries and QA
Default shared source is PTrade mode; local host reads STRATEGY_RUNTIME=local and injects services without importing os/sys in broker code. Preserve immutable versions and original error evidence. Source identity does not establish equal market data, NAV visibility or broker fills. Keep provider tokens outside packages/Skill/report metadata. Historical metrics require actual files, not inference. Current S02 candidate is backtest-only pending broker validation. NAS simulation and live trading are separate capabilities.

Knowledge mode is hybrid: accepted lifecycle and shared-source contracts plus compiled research methods, with observed runtime limits. Read the schema/index/recent log and only the current route at knowledge-dependent nodes. Development evaluations remain outside runtime; clean-context regression and non-macOS Agent discovery are untested. Recurring mechanical issues belong in CLI tests and the appropriate reference, then return to the main line.

Knowledge is loaded on-demand only at the current node; later-node pages remain deferred.
