import argparse
import asyncio
import logging

from dotenv import load_dotenv

from calfkit.broker.broker import BrokerClient
from calfkit.nodes.agent_router_node import AgentRouterNode
from binance_consumer import CandleBook
from binance_kafka_connector import (
    DEFAULT_SYMBOLS,
    BinanceKafkaConnector,
)

# Binance Connector — Streams live market data from the Binance
# Exchange WebSocket and invokes the deployed agent routers via
# RouterServiceClient on each price tick.
#
# Usage:
#     uv run python binance_connector.py --bootstrap-servers <broker-url>
#
# Prerequisites:
#     - Kafka broker running (set KAFKA_BOOTSTRAP_SERVERS env var, default: localhost:9092)
#     - Router nodes deployed (deploy_router_node.py)
#     - Chat node deployed (deploy_chat_node.py)
#     - Tools deployed (tools_and_dashboard.py)

load_dotenv()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Stream live Binance market data to deployed agents.",
    )
    parser.add_argument(
        "--bootstrap-servers",
        required=True,
        help="Kafka bootstrap servers address",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="Minimum publish interval in seconds between market data updates to agents (default: 60)",
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
    print("Binance Connector Deployment")
    print("=" * 50)

    print(f"\nConnecting to Kafka broker at {args.bootstrap_servers}...")
    broker = BrokerClient(bootstrap_servers=args.bootstrap_servers)

    # Reference router node for topic routing.
    # tool_nodes=None so the deployed routers use their own tools.
    router_node = AgentRouterNode()

    print(f"  Router topic: {router_node.subscribed_topic}")
    print(f"  Symbols: {', '.join(DEFAULT_SYMBOLS)}")
    print(f"  Min publish interval: {args.interval}s")

    candle_book = CandleBook()

    connector = BinanceKafkaConnector(
        broker=broker,
        router_node=router_node,
        symbols=DEFAULT_SYMBOLS,
        min_publish_interval=args.interval,
        candle_book=candle_book,
    )

    print("\nStarting Binance connector...")
    await connector.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBinance connector stopped.")
