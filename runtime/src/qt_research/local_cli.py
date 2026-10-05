"""Mac local backtest CLI: S02 minute strategy, daily baseline, result inspect and doctor.

S02 reuses preserved source with explicit offline market and execution adapters.
"""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import sys
from decimal import Decimal

import pandas as pd

from .paths import ROOT
from .core_report import write_report
from .event_engine import FixedQuantityStrategy, run_events
from .execution import Bar, ExecutionConfig
from .orders import SecurityRule


def load_daily(snapshot):
    snapshot = Path(snapshot)
    report = json.loads((snapshot / "report.json").read_text())
    if report.get("provider") == "user-supplied HTTP GET gateway":
        raise ValueError("Gateway daily units must be normalized and audited before this baseline loader")
    parts = []
    for row in report["results"]:
        if row["api"] != "fund_daily" or row["state"] != "nonempty":
            continue
        file = (snapshot / row["file"]).resolve()
        if file.parent != snapshot.resolve():
            raise ValueError("Unsafe snapshot path")
        if hashlib.sha256(file.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("Snapshot checksum mismatch")
        parts.append(pd.read_parquet(file))
    if not parts:
        raise ValueError("Snapshot has no daily data")
    frame = pd.concat(parts, ignore_index=True)
    frame["trade_date"] = frame.trade_date.astype(str)
    if frame.duplicated(["ts_code", "trade_date"]).any():
        raise ValueError("Duplicate daily observation")
    for field in ("close", "vol"):
        frame[field] = pd.to_numeric(frame[field], errors="raise")
        if frame[field].isna().any():
            raise ValueError("Missing daily field")
    if (frame.close <= 0).any() or (frame.vol < 0).any():
        raise ValueError("Invalid close or volume")
    return frame, hashlib.sha256((snapshot / "report.json").read_bytes()).hexdigest()


def inspect_result(out):
    out = Path(out)
    checks = json.loads((out / "checksums.json").read_text())
    if not checks or any(Path(name).name != name for name in checks):
        raise ValueError("Invalid checksum manifest")
    for name, digest in checks.items():
        if hashlib.sha256((out / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Output checksum mismatch: " + name)
    return {"checksums_verified": len(checks), "summary": json.loads((out / "summary.json").read_text())}


def run(args):
    if Path(args.out).exists():
        raise FileExistsError("Output exists; choose a new directory")
    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)
    if start > end or args.shares < 0 or args.initial_cash <= 0:
        raise ValueError("Invalid date range, shares or initial cash")
    frame, digest = load_daily(args.snapshot)
    dates = pd.to_datetime(frame.trade_date, format="%Y%m%d")
    selected = frame.loc[(frame.ts_code == args.symbol) & (dates >= start) & (dates <= end)].sort_values("trade_date")
    if len(selected) < 2:
        raise ValueError("Need at least two daily observations")
    bars = []
    for row in selected.itertuples():
        volume = Decimal(str(row.vol)) * 100
        if volume != volume.to_integral_value():
            raise ValueError("Volume conversion must yield integer shares")
        time = pd.Timestamp(row.trade_date).strftime("%Y-%m-%d") + "T15:00:00+08:00"
        bars.append(Bar(time, args.symbol, row.close, int(volume)))
    benchmark = None
    if args.benchmark_symbol:
        bench = frame.loc[frame.ts_code == args.benchmark_symbol].set_index("trade_date").close
        before = bench.loc[bench.index < selected.trade_date.iloc[0]].sort_index()
        if before.empty:
            raise ValueError("Benchmark needs a previous-session base")
        base = float(before.iloc[-1])
        benchmark = {pd.Timestamp(day).strftime("%Y-%m-%d"): float(bench.loc[day]) / base
                     for day in selected.trade_date}
    config = ExecutionConfig(commission_rate=args.commission_rate, minimum_commission=args.minimum_commission,
                             participation=args.participation, slippage=args.slippage)
    strategy = FixedQuantityStrategy(args.symbol, args.shares)
    result = run_events(bars, strategy, {args.symbol: SecurityRule(same_day_sell=args.same_day_sell)},
                        args.initial_cash, config, day_orders=False)
    sources = ["ledger.py", "orders.py", "execution.py", "event_engine.py", "core_report.py", "local_cli.py"]
    hashes = {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest() for name in sources}
    manifest = {"schema": 1, "strategy": "fixed-quantity-baseline", "symbol": args.symbol, "shares": args.shares,
                "start": bars[0].time, "end": bars[-1].time, "initial_cash": args.initial_cash,
                "snapshot": str(Path(args.snapshot).resolve()), "snapshot_manifest_sha256": digest,
                "source_sha256": hashes, "benchmark": args.benchmark_symbol or "未设置基准",
                "frequency": "daily", "execution": {"model": "next daily close, shared participation cap",
                    "commission_rate": args.commission_rate, "minimum_commission": args.minimum_commission,
                    "participation": args.participation, "slippage": args.slippage,
                    "same_day_sell": args.same_day_sell, "limit_buffer": "0.01"},
                "order_validity": "research GTC across daily events; not exchange overnight orders",
                "description": "日线缓存数据的固定数量基线，用于验证数量账本与报告；S02分钟迁移另行验收。",
                "limitations": ["Historical dividends and splits not booked", "No exchange order queue or limit-up/down model",
                                "JQ metric parity not calibrated", "Daily events cannot reproduce 14:00 decisions"],
                "trading_days": 252, "risk_free": 0.0,
                "runtime": {"python": platform.python_version(), "platform": platform.system(),
                            "openpyxl": importlib.metadata.version("openpyxl"), "plotly": importlib.metadata.version("plotly")}}
    summary = write_report(args.out, result, manifest, benchmark)
    return {"out": str(Path(args.out).resolve()), "metrics": summary["metrics"],
            "orders": summary["orders"], "fills": summary["fills"], "S02_migration": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="Run a fixed-share daily baseline from SDK snapshot")
    for name in ("snapshot", "out", "start", "end", "symbol"):
        run_p.add_argument("--" + name, required=True)
    run_p.add_argument("--shares", type=int, default=5000)
    run_p.add_argument("--initial-cash", type=float, default=50000)
    run_p.add_argument("--benchmark-symbol")
    run_p.add_argument("--same-day-sell", action="store_true", help="Explicit security rule, verify for chosen security")
    for name, default in (("commission-rate", "0.0002"), ("minimum-commission", "5"),
                          ("participation", "0.25"), ("slippage", "0")):
        run_p.add_argument("--" + name, default=default)
    inspect = sub.add_parser("inspect", help="Verify result hashes and read metrics offline")
    inspect.add_argument("--out", required=True)
    sub.add_parser("doctor")
    s02 = sub.add_parser("s02", help="Offline minute replay of preserved S02 PTrade strategy")
    s02.add_argument("--minutes", required=True)
    s02.add_argument("--snapshot", required=True)
    s02.add_argument("--out", required=True)
    s02.add_argument("--start", default="2024-01-02")
    s02.add_argument("--end", default="2025-12-31")
    s02.add_argument("--initial-cash", type=float, default=100000)
    s02.add_argument("--participation", default="0.25")
    s02.add_argument("--nav-evidence", help="Immutable independent dated publication witnesses; original NAV metadata retained")
    s02.add_argument("--reference-mismatch", choices=['strict','warn'], default='strict', help="Strict rejects daily reference conflicts/gaps; warn preserves minute data and marks the diagnostic run invalid")
    s02.add_argument("--benchmark-folder", help="Verified benchmark cache with data.parquet and manifest.json")
    s02.add_argument("--dividend-root", help="Root containing s02-fund-div-CODE-v1 verified caches")
    prepare = sub.add_parser("prepare-s02", help="Download annual SDK inputs with warm-up into a new immutable S02 cache")
    prepare.add_argument("--start", required=True)
    prepare.add_argument("--end", required=True)
    prepare.add_argument("--out", required=True)
    prepare.add_argument("--resume", action="store_true", help="Resume a verified incomplete cache with exactly the same plan")
    prepare.add_argument("--reuse", action="append", help="Completed snapshot to reuse exactly matching verified queries; repeatable")
    importer = sub.add_parser('import-minutes', help='Import and audit eight immutable fund minute Parquet files')
    importer.add_argument('--source', required=True)
    importer.add_argument('--out', required=True)
    gaps = sub.add_parser('fill-minute-gaps', help='Query actual missing native minute intervals into a new verified cache')
    for name in ['minutes','usage-file','start','end','out']: gaps.add_argument('--'+name,required=True)
    gaps.add_argument('--patch-cache', help='Reuse prior actual interval Parquets after exact identity/time/quality checks')
    seal = sub.add_parser('seal-strategy', help='Seal a single strategy file with a credential-independent build ID; refuses overwrite')
    seal.add_argument('--file',required=True)
    seal.add_argument('--out',required=True)
    compare = sub.add_parser("compare-s02", help="Offline JQ/local target, NAV, trade and common-metric comparison")
    for name in ("local-out", "jq-dir", "jq-nav-log", "snapshot", "out"):
        compare.add_argument("--"+name, required=True)
    compare.add_argument("--jq-market-log", help="Complete jqcli minute-probe log JSON")
    compare.add_argument("--minutes", help="Immutable local minute cache for quote comparison")
    args = parser.parse_args()
    try:
        if args.command == "run":
            from .research_catalog import tracked_run
            output = tracked_run(args,run)
        elif args.command == "s02":
            from .s02_local import run as run_s02
            from .research_catalog import tracked_run
            output = tracked_run(args,run_s02)
        elif args.command == "prepare-s02":
            from .s02_cache import prepare
            output = prepare(args)
        elif args.command == 'import-minutes':
            from .minute_import import run as import_minutes
            output = import_minutes(args)
        elif args.command == 'fill-minute-gaps':
            from .minute_gaps import run as fill_gaps
            output = fill_gaps(args)
        elif args.command == 'seal-strategy':
            from .strategy_source import seal
            output = seal(args)
        elif args.command == "compare-s02":
            from .s02_compare import compare
            output = compare(args)
            from .research_catalog import Catalog
            output['catalog_comparison_id'] = Catalog().import_comparison(args.out)
        elif args.command == "inspect":
            output = inspect_result(args.out)
        else:
            output = {"python": platform.python_version(), "entry": sys.executable,
                      "dependencies": {name: importlib.metadata.version(name) for name in ("pandas", "openpyxl", "plotly")},
                      "commands": ["run", "s02", "prepare-s02", "import-minutes", "fill-minute-gaps", "seal-strategy", "compare-s02", "inspect", "doctor"], "strategies": ["fixed-quantity-baseline", "S02-local-v1"]}
        print(json.dumps({"ok": True, "result": output}, ensure_ascii=False, allow_nan=False))
    except Exception as error:
        print(json.dumps({"ok": False, "error": {"code": type(error).__name__, "message": str(error)}}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
