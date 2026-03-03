import argparse
import asyncio
import logging

from dotenv import load_dotenv
from rich.live import Live

from calfkit.broker.broker import BrokerClient
from calfkit.runners.service import NodesService
from exchanges import (
    PRICE_TOPIC,
    TickerMessage,
)
from arena.tools import (
    calculator,
    execute_trade,
    get_portfolio,
    price_book,
    store,
    view,
)

# Tools & Price Feed — Deploys trading tool workers and subscribes
# to the Kafka price topic published by the connector.
#
# The price subscriber hydrates the shared price book that the trading
# tools read from when executing trades.
#
# Usage:
#     uv run python deploy/tools_and_dashboard.py
#
# Prerequisites:
#     - Kafka broker running at localhost:9092

load_dotenv()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Deploy trading tools, price feed, and dashboard.",
    )
    parser.add_argument(
        "--bootstrap-servers",
        required=True,
        help="Kafka bootstrap servers address",
    )
    parser.add_argument(
        "--snapshot-interval",
        type=float,
        default=600.0,
        help="Seconds between portfolio snapshots (0 disables recording)",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default="./data",
        help="Output directory for CSV data files",
    )
    return parser.parse_args()


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    args = parse_args()

    print("=" * 50)
    print("Tools & Price Feed Deployment")
    print("=" * 50)

    print(f"\nConnecting to Kafka broker at {args.bootstrap_servers}...")
    broker = BrokerClient(bootstrap_servers=args.bootstrap_servers)
    service = NodesService(broker)

    # ── Tool nodes ───────────────────────────────────────────────
    print("\nRegistering trading tool nodes...")
    for tool in (execute_trade, get_portfolio, calculator):
        service.register_node(tool)
        print(f"  - {tool.tool_schema.name} (topic: {tool.subscribed_topic})")

    # ── Price subscriber ─────────────────────────────────────────
    @broker.subscriber(PRICE_TOPIC, group_id="tools-dashboard")
    async def handle_price_update(ticker: TickerMessage) -> None:
        price_book.update(ticker.model_dump())
        view.rerender()

    # ── Data recorder (optional) ────────────────────────────────
    recorder = None
    if args.snapshot_interval > 0:
        from arena.recorder import DataRecorder

        recorder = DataRecorder(data_dir=args.data_dir)
        store.attach_recorder(recorder)
        print(f"\nCSV recording enabled → {args.data_dir}/ (snapshots every {args.snapshot_interval}s)")

    print("\nStarting portfolio dashboard (prices via Kafka)...")

    try:
        with Live(view._build_layout(), auto_refresh=False, screen=True) as live:
            view.attach_live(live)
            if recorder is not None:
                recorder.start_snapshot_loop(store, interval=args.snapshot_interval)
            await service.run()
    finally:
        if recorder is not None:
            await recorder.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nTools and price feed stopped.")
