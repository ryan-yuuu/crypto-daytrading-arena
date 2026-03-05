"""Coinbase exchange connector — WebSocket consumer, REST poller,
and Kafka connector in a single module.

Usage:
    uv run python -m exchanges.coinbase
    uv run python -m exchanges.coinbase --products BTC-USD ETH-USD SOL-USD
    uv run python -m exchanges.coinbase --min-interval 30
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

import httpx
import websockets

from calfkit.broker.broker import BrokerClient
from calfkit.nodes.agent_router_node import AgentRouterNode
from calfkit.runners.service_client import RouterServiceClient

from arena.models import Candle, TIMEFRAMES
from arena.price_book import CandleBook
from exchanges import PRICE_TOPIC, TickerMessage

logger = logging.getLogger(__name__)

COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"
COINBASE_REST_BASE = "https://api.exchange.coinbase.com"

DEFAULT_PRODUCTS = [
    "BTC-USD",
    "FARTCOIN-USD",
    "SOL-USD",
]

RECONNECT_DELAY_SECONDS = 3


def parse_coinbase_candle(row: list) -> Candle:
    """Parse a Coinbase candle row: [ts, low, high, open, close, vol]."""
    return Candle(
        time=datetime.fromtimestamp(row[0], tz=timezone.utc),
        open=float(row[3]),
        high=float(row[2]),
        low=float(row[1]),
        close=float(row[4]),
        volume=float(row[5]),
    )


async def poll_rest(
    products: list[str],
    candle_book: CandleBook,
    interval: float = 60.0,
) -> None:
    """Poll Coinbase REST API for multi-timeframe candles and current prices."""
    async with httpx.AsyncClient(base_url=COINBASE_REST_BASE, timeout=15.0) as client:
        while True:
            now = int(datetime.now(timezone.utc).timestamp())

            for product_id in products:
                try:
                    for tf in TIMEFRAMES:
                        start = now - tf.start_minutes_ago * 60
                        end = now - tf.end_minutes_ago * 60
                        resp = await client.get(
                            f"/products/{product_id}/candles",
                            params={
                                "granularity": tf.granularity,
                                "start": start,
                                "end": end,
                            },
                        )
                        resp.raise_for_status()
                        candle_book.update_from_api(product_id, tf.granularity, resp.json())

                    resp = await client.get(f"/products/{product_id}/ticker")
                    resp.raise_for_status()
                except Exception:
                    logger.exception("REST poll failed for %s", product_id)

            await asyncio.sleep(interval)


class CoinbaseKafkaConnector:
    """Streams Coinbase ticker data to an AgentRouterNode.

    Connects to the Coinbase Exchange WebSocket ticker_batch channel
    and invokes the configured AgentRouterNode with each price update
    using fire-and-forget publishes via RouterServiceClient.

    When min_publish_interval is set, incoming tickers are buffered per
    product ID. Only the latest data for each product is kept. A product's
    buffer is flushed once at least min_publish_interval seconds have
    elapsed since that product's last invocation.
    """

    def __init__(
        self,
        broker: BrokerClient,
        router_node: AgentRouterNode,
        products: list[str],
        min_publish_interval: float = 0.0,
        candle_book: CandleBook | None = None,
    ) -> None:
        if not products:
            raise ValueError("At least one product must be specified")
        self._broker = broker
        self._client = RouterServiceClient(broker, router_node, deps_type=dict)
        self._products = products
        self._min_interval = min_publish_interval
        self._running = True
        self._candle_book = candle_book

        # Latest ticker per product — patched on every incoming message
        self._latest: dict[str, TickerMessage] = {}

    async def start(self) -> None:
        """Start the connector. Blocks until shutdown is triggered."""
        await self._broker.start()
        logger.info("Kafka broker connected")

        try:
            while self._running:
                try:
                    await self._consume_and_publish()
                except websockets.ConnectionClosed:
                    if not self._running:
                        break
                    logger.warning(
                        "WebSocket connection lost. Reconnecting in %ds...",
                        RECONNECT_DELAY_SECONDS,
                    )
                    await asyncio.sleep(RECONNECT_DELAY_SECONDS)
                except Exception:
                    if not self._running:
                        break
                    logger.exception(
                        "Unexpected error. Reconnecting in %ds...",
                        RECONNECT_DELAY_SECONDS,
                    )
                    await asyncio.sleep(RECONNECT_DELAY_SECONDS)
        finally:
            await self._broker.close()
            logger.info("Kafka broker closed")

    def stop(self) -> None:
        """Signal the connector to shut down gracefully."""
        self._running = False

    async def _publish_latest(self) -> None:
        """Snapshot and publish the current latest tickers as a single batch."""
        if not self._latest:
            return

        batch = list(self._latest.values())
        _exclude = {
            "best_bid_size",
            "best_ask_size",
            "last_size",
            "side",
            "trade_id",
            "sequence",
            "open_24h",
            "high_24h",
            "low_24h",
            "volume_24h",
            "volume_30d",
            "time",
        }
        batch_json = json.dumps([t.model_dump(exclude=_exclude) for t in batch])

        prompt_parts = [
            "Here is the latest ticker information. You should view your "
            "portfolio first before making any decisions to trade.\n"
            "price = last traded price, best_bid = price you sell at, "
            "best_ask = price you buy at.\n\n"
            f"{batch_json}",
        ]

        if self._candle_book is not None and self._candle_book.has_data():
            prompt_parts.append(
                "\n## Price History (OHLCV candlesticks)\n"
                "Below are candlesticks at three granularities — coarser for "
                "broader trend context, finer for recent price action.\n\n"
                f"{self._candle_book.format_prompt(self._products)}"
            )

        await self._client.invoke(
            user_prompt="\n".join(prompt_parts),
            deps={"invoked_at": time.time()},
        )

        summary = ", ".join(f"{t.product_id} @ ${t.price}" for t in batch)
        logger.info(
            "Published batch of %d ticker(s) to router: %s",
            len(batch),
            summary,
        )

    async def _periodic_agent_invoke(self) -> None:
        """Publish the latest snapshot on a fixed interval."""
        interval = max(self._min_interval, 1.0)
        while self._running:
            await asyncio.sleep(interval)
            await self._publish_latest()

    async def _consume_and_publish(self) -> None:
        """Connect to Coinbase WebSocket and buffer tickers for periodic publish."""
        self._latest.clear()

        async with websockets.connect(COINBASE_WS_URL) as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "subscribe",
                        "product_ids": self._products,
                        "channels": ["ticker_batch"],
                    }
                )
            )
            logger.info(
                "Subscribed to %d products on ticker_batch: %s",
                len(self._products),
                ", ".join(self._products),
            )

            agent_invoke_task = asyncio.create_task(self._periodic_agent_invoke())

            candle_update_task: asyncio.Task | None = None
            if self._candle_book is not None:
                # CandleBook updates are independent; PriceBook updates come
                # from the WebSocket
                candle_update_task = asyncio.create_task(
                    poll_rest(
                        products=self._products,
                        candle_book=self._candle_book,
                        interval=60.0,
                    )
                )

            try:
                async for raw in ws:
                    if not self._running:
                        break

                    data = json.loads(raw)
                    if data.get("type") != "ticker":
                        continue

                    ticker = TickerMessage.model_validate(data)
                    self._latest[ticker.product_id] = ticker
                    await self._broker.publish(ticker, PRICE_TOPIC)
            finally:
                agent_invoke_task.cancel()
                try:
                    await agent_invoke_task
                except asyncio.CancelledError:
                    pass
                if candle_update_task is not None:
                    candle_update_task.cancel()
                    try:
                        await candle_update_task
                    except asyncio.CancelledError:
                        pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream Coinbase market data to a Kafka topic.",
    )
    parser.add_argument(
        "--bootstrap-servers",
        default=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
        help="Kafka bootstrap servers (default: $KAFKA_BOOTSTRAP_SERVERS or localhost:9092).",
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to config file for products (default: config.json).",
    )
    parser.add_argument(
        "--products",
        nargs="+",
        default=None,
        help="Coinbase product IDs to subscribe to (overrides config).",
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=60.0,
        help=(
            "Minimum seconds between publishes per product. "
            "Incoming data is buffered and only the latest per product is published "
            "when the interval elapses. 0 = publish immediately (default: 60)."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO).",
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace, router_node: AgentRouterNode) -> None:
    # Load products from config if not provided via CLI
    products = args.products
    if products is None:
        from config import load_config

        try:
            config = load_config(args.config)
            products = config.trading.coinbase_products
        except Exception as e:
            logger.debug("Config not loaded, using default products: %s", e)
            products = list(DEFAULT_PRODUCTS)

    broker = BrokerClient(bootstrap_servers=args.bootstrap_servers)
    connector = CoinbaseKafkaConnector(
        broker=broker,
        router_node=router_node,
        products=products,
        min_publish_interval=args.min_interval,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, connector.stop)

    logger.info("Starting Coinbase -> Kafka connector")
    logger.info("  Router topic:  %s", router_node.subscribed_topic)
    logger.info("  Broker:        %s", args.bootstrap_servers)
    logger.info("  Products:      %s", ", ".join(products))
    logger.info("  Min interval:  %ss", args.min_interval)

    await connector.start()


def main() -> None:

    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    asyncio.run(run(args, AgentRouterNode()))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
