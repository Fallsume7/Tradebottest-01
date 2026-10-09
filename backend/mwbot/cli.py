"""Command line: serve the dashboard, run demo days / backtests, manage datasets."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from mwbot.config import EnvSettings
from mwbot.data.base import MarketDataProvider
from mwbot.logging_setup import configure_logging


def _provider(env: EnvSettings, dataset: str | None) -> MarketDataProvider:
    from mwbot.data.csv_provider import CsvHistoricalProvider
    from mwbot.data.synthetic import SyntheticProvider

    if dataset in (None, "demo"):
        return SyntheticProvider(42)
    return CsvHistoricalProvider(Path(env.historical_dir) / dataset)


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    env = EnvSettings()
    configure_logging(env.log_level, env.log_json)
    uvicorn.run(
        "mwbot.api.app:app_factory",
        factory=True,
        host=args.host or env.host,
        port=args.port or env.port,
        log_level=env.log_level.lower(),
    )
    return 0


def cmd_demo_day(args: argparse.Namespace) -> int:
    """Run one full synthetic trading day headlessly and print the result."""
    from mwbot.bot.engine import TradingBot
    from mwbot.bot.report import report_markdown
    from mwbot.bot.runner import SessionRunner
    from mwbot.config import AppSettings
    from mwbot.data.synthetic import SyntheticProvider
    from mwbot.trading.broker import PaperBroker

    settings = AppSettings()
    broker = PaperBroker(settings.risk.starting_capital, settings.costs)
    bot = TradingBot(settings, broker)
    bot.start()
    d = date.fromisoformat(args.date)
    source = SyntheticProvider(args.seed).open_session(d)
    report = SessionRunner.for_date(bot, source, d).run()
    assert report is not None
    print("*** SYNTHETIC DATA - fabricated prices, not a real market ***")
    print(report_markdown(report))
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    from mwbot.backtest.engine import BacktestConfig, run_backtest, summary_to_csv, trades_to_csv

    env = EnvSettings()
    provider = _provider(env, args.dataset)
    cfg = BacktestConfig(name=args.name, strategies=args.strategies, oos_fraction=args.oos)
    res = run_backtest(provider, cfg)
    print(
        json.dumps({k: v for k, v in res.summary.items() if not isinstance(v, dict)}, indent=2, default=str)
    )
    for w in res.warnings:
        print("WARNING:", w, file=sys.stderr)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{res.id}_trades.csv").write_text(trades_to_csv(res.trades), encoding="utf-8")
        (out / f"{res.id}_summary.csv").write_text(summary_to_csv(res), encoding="utf-8")
        print(f"exported to {out}/ (id {res.id}, hash {res.result_hash[:16]})")
    return 0


def cmd_make_sample(args: argparse.Namespace) -> int:
    from mwbot.data.synthetic import write_sample_dataset

    out = write_sample_dataset(
        Path(args.out),
        date.fromisoformat(args.start),
        date.fromisoformat(args.end),
        seed=args.seed,
        n_symbols=args.symbols,
    )
    print(f"wrote SYNTHETIC dataset to {out}")
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    from mwbot.data.massive import MassiveClient, download_dataset

    env = EnvSettings()
    client = MassiveClient(env.massive_api_key, env.massive_base_url)
    out = download_dataset(
        client, args.symbols, date.fromisoformat(args.start), date.fromisoformat(args.end), Path(args.out)
    )
    print(f"wrote dataset to {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mwbot", description="MarketWatchBot (paper trading only)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="start API + dashboard")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_serve)

    d = sub.add_parser("demo-day", help="run one synthetic trading day headlessly")
    d.add_argument("--date", default="2026-10-08")
    d.add_argument("--seed", type=int, default=42)
    d.set_defaults(fn=cmd_demo_day)

    b = sub.add_parser("backtest", help="run a backtest on a dataset (or 'demo')")
    b.add_argument("--dataset", default="synthetic_sample")
    b.add_argument(
        "--strategies",
        nargs="+",
        default=["opening_momentum", "gap_continuation", "momentum_breakout", "volume_surge"],
    )
    b.add_argument("--oos", type=float, default=0.3)
    b.add_argument("--name", default="cli-backtest")
    b.add_argument("--out")
    b.set_defaults(fn=cmd_backtest)

    m = sub.add_parser("make-sample-data", help="generate a labelled SYNTHETIC dataset")
    m.add_argument("--out", default="backend/sample_data/synthetic_sample")
    m.add_argument("--start", default="2026-09-14")
    m.add_argument("--end", default="2026-09-25")
    m.add_argument("--seed", type=int, default=7)
    m.add_argument("--symbols", type=int, default=30)
    m.set_defaults(fn=cmd_make_sample)

    dl = sub.add_parser("download-dataset", help="download real 1-min bars from Massive with YOUR key")
    dl.add_argument("--symbols", nargs="+", required=True)
    dl.add_argument("--start", required=True)
    dl.add_argument("--end", required=True)
    dl.add_argument("--out", required=True)
    dl.set_defaults(fn=cmd_download)

    args = ap.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
