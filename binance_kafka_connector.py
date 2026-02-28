"""
Binance-to-Kafka connector that streams real-time market data from
the Binance Exchange WebSocket and invokes an AgentRouterNode
for each price update (fire-and-forget).

Uses the 24hr ticker stream for ~1-second price updates.

Usage:
    uv run python binance_kafka_connector.py
    KAFKA_BOOTSTRAP_SERVERS=broker:9092 \
        uv run python binance_kafka_connector.py
    uv run python binance_kafka_connector.py \
        --symbols BTCUSDT ETHUSDT SOLUSDT
    uv run python binance_kafka_connector.py \
        --min-interval 30

Prerequisites:
    - Kafka broker running (set KAFKA_BOOTSTRAP_SERVERS env var, default: localhost:9092)
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from typing import Optional

import websockets
from pydantic import BaseModel

from calfkit.broker.broker import BrokerClient
from calfkit.nodes.agent_router_node import AgentRouterNode
from calfkit.runners.service_client import RouterServiceClient
from binance_consumer import CandleBook, poll_rest
from config import get_default_symbols

logger = logging.getLogger(__name__)

BINANCE_WS_URL = "wss://stream.binance.com:9443"
BINANCE_WS_URL_FALLBACK = "wss://stream.binance.com:443"

# Load defaults from config if available
try:
    DEFAULT_SYMBOLS = get_default_symbols("binance")
except Exception:
    DEFAULT_SYMBOLS = [
        "BTCUSDT",
        "FARTCOINUSDT",
        "SOLUSDT",
    ]

RECONNECT_DELAY_SECONDS = 3
PING_INTERVAL_SECONDS = 30  # Must send ping within 60 seconds per Binance docs
MAX_CONNECTION_LIFETIME_SECONDS = 82800  # 23 hours (Binance limit is 24h)

PRICE_TOPIC = "market_data.prices"


class TickerMessage(BaseModel):
    """Binance ticker message published to Kafka."""

    product_id: str
    price: str
    best_bid: str
    best_bid_size: str
    best_ask: str
    best_ask_size: str
    side: str
    last_size: str
    open_24h: str
    high_24h: str
    low_24h: str
    volume_24h: str
    volume_30d: str
    trade_id: int
    sequence: int
    time: str


class BinanceKafkaConnector:
    """Streams Binance ticker data to an AgentRouterNode.

    Connects to the Binance Exchange WebSocket ticker stream
    and invokes the configured AgentRouterNode with each price update
    using fire-and-forget publishes via RouterServiceClient.

    When min_publish_interval is set, incoming tickers are buffered per
    symbol. Only the latest data for each symbol is kept. A symbol's
    buffer is flushed once at least min_publish_interval seconds have
    elapsed since that symbol's last invocation.
    """

    def __init__(
        self,
        broker: BrokerClient,
        router_node: AgentRouterNode,
        symbols: list[str],
        min_publish_interval: float = 0.0,
        candle_book: Optional[CandleBook] = None,
    ) -> None:
        if not symbols:
            raise ValueError("At least one symbol must be specified")
        self._broker = broker
        self._client = RouterServiceClient(broker, router_node)
        self._symbols = symbols
        self._min_interval = min_publish_interval
        self._running = True
        self._candle_book = candle_book

        # Latest ticker per symbol — patched on every incoming message
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

    def _parse_binance_ticker(self, data: dict) -> Optional[TickerMessage]:
        """Parse Binance 24hr ticker data into TickerMessage."""
        try:
            return TickerMessage(
                product_id=data["s"],  # Symbol
                price=data["c"],  # Last price (close)
                best_bid=data["b"],  # Best bid
                best_bid_size=data["B"],  # Best bid qty
                best_ask=data["a"],  # Best ask
                best_ask_size=data["A"],  # Best ask qty
                side="",  # Not provided by Binance ticker
                last_size=data.get("Q", "0"),  # Last quantity
                open_24h=data["o"],  # Open price
                high_24h=data["h"],  # High price
                low_24h=data["l"],  # Low price
                volume_24h=data["v"],  # Base volume
                volume_30d="0",  # Not provided by Binance
                trade_id=data.get("n", 0),  # Number of trades
                sequence=0,  # Not provided by Binance
                time=__import__("datetime").datetime.fromtimestamp(
                    data["E"] / 1000, tz=__import__("datetime").timezone.utc
                ).isoformat(),
            )
        except Exception as e:
            logger.error("Failed to parse ticker data: %s (data: %s)", e, data)
            return None

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
                f"{self._candle_book.format_prompt(self._symbols)}"
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

    async def _periodic_publish(self) -> None:
        """Publish the latest snapshot on a fixed interval."""
        interval = max(self._min_interval, 1.0)
        while self._running:
            await asyncio.sleep(interval)
            await self._publish_latest()

    async def _ping_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Send ping frames periodically to keep connection alive."""
        while self._running:
            try:
                await asyncio.sleep(PING_INTERVAL_SECONDS)
                if ws.open:
                    await ws.ping()
                    logger.debug("Ping sent")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Ping failed: %s", e)

    async def _lifetime_manager(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Force reconnection before 24-hour limit."""
        await asyncio.sleep(MAX_CONNECTION_LIFETIME_SECONDS)
        logger.info("Connection approaching 24h limit, forcing reconnection")
        await ws.close()

    async def _connect_with_fallback(self, streams: str) -> websockets.WebSocketClientProtocol:
        """Connect to Binance WebSocket with fallback endpoints.

        Tries primary endpoint first, then falls back to alternative ports/URLs
        if connection is refused or times out.
        """
        urls_to_try = [
            f"{BINANCE_WS_URL}/stream?streams={streams}",
            f"{BINANCE_WS_URL_FALLBACK}/stream?streams={streams}",
        ]

        last_error = None
        for url in urls_to_try:
            try:
                logger.info("Connecting to Binance WebSocket: %s", url.replace(streams, "..."))
                ws = await websockets.connect(url, open_timeout=10)
                logger.info("Successfully connected to %s", url.split("/")[2])
                return ws
            except (ConnectionRefusedError, TimeoutError, OSError) as e:
                logger.warning("Failed to connect to %s: %s", url.split("/")[2], e)
                last_error = e
                continue

        raise last_error if last_error else ConnectionError("All Binance endpoints failed")

    async def _consume_and_publish(self) -> None:
        """Connect to Binance WebSocket and buffer tickers for periodic publish."""
        self._latest.clear()

        # Build combined stream URL: /stream?streams=btcusdt@ticker/ethusdt@ticker/...
        # Note: Binance combined streams use /stream?streams= format, not /ws/
        streams = "/".join(f"{s.lower()}@ticker" for s in self._symbols)

        ws = await self._connect_with_fallback(streams)
        async with ws:
            logger.info(
                "Subscribed to %d symbols on 24hrTicker: %s",
                len(self._symbols),
                ", ".join(self._symbols),
            )

            flush_task = asyncio.create_task(self._periodic_publish())
            ping_task = asyncio.create_task(self._ping_loop(ws))
            lifetime_task = asyncio.create_task(self._lifetime_manager(ws))

            candle_task: Optional[asyncio.Task] = None
            if self._candle_book is not None:
                from binance_consumer import PriceBook

                candle_task = asyncio.create_task(
                    poll_rest(
                        symbols=self._symbols,
                        price_book=PriceBook(),
                        candle_book=self._candle_book,
                        interval=60.0,
                    )
                )

            try:
                async for raw in ws:
                    if not self._running:
                        break

                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("Failed to decode message: %s", raw[:200])
                        continue

                    # Handle ping/pong (Binance-specific)
                    if "ping" in data:
                        await ws.send(json.dumps({"pong": data["ping"]}))
                        continue

                    # Handle combined stream wrapper
                    if "stream" in data and "data" in data:
                        ticker_data = data["data"]
                    else:
                        ticker_data = data

                    # Only process 24hrTicker events
                    if ticker_data.get("e") != "24hrTicker":
                        continue

                    ticker = self._parse_binance_ticker(ticker_data)
                    if ticker is None:
                        continue

                    self._latest[ticker.product_id] = ticker
                    await self._broker.publish(ticker, PRICE_TOPIC)
            finally:
                flush_task.cancel()
                ping_task.cancel()
                lifetime_task.cancel()
                try:
                    await flush_task
                except asyncio.CancelledError:
                    pass
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass
                try:
                    await lifetime_task
                except asyncio.CancelledError:
                    pass
                if candle_task is not None:
                    candle_task.cancel()
                    try:
                        await candle_task
                    except asyncio.CancelledError:
                        pass


def parse_args(argv: Optional[list[str]] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream Binance market data to a Kafka topic.",
    )
    parser.add_argument(
        "--bootstrap-servers",
        default=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
        help="Kafka bootstrap servers (default: $KAFKA_BOOTSTRAP_SERVERS or localhost:9092).",
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to config file for symbols (default: config.json).",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="Binance symbols to subscribe to (overrides config).",
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=0.0,
        help=(
            "Minimum seconds between publishes per symbol. "
            "Incoming data is buffered and only the latest per symbol is published "
            "when the interval elapses. 0 = publish immediately (default: 0)."
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
    # Load symbols from config if not provided via CLI
    symbols = args.symbols
    if symbols is None:
        from config import load_config
        try:
            config = load_config(args.config)
            symbols = config.trading.binance_symbols
        except Exception:
            symbols = DEFAULT_SYMBOLS

    broker = BrokerClient(bootstrap_servers=args.bootstrap_servers)
    connector = BinanceKafkaConnector(
        broker=broker,
        router_node=router_node,
        symbols=symbols,
        min_publish_interval=args.min_interval,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, connector.stop)

    logger.info("Starting Binance -> Kafka connector")
    logger.info("  Router topic:  %s", router_node.subscribed_topic)
    logger.info("  Broker:        %s", args.bootstrap_servers)
    logger.info("  Symbols:       %s", ", ".join(symbols))
    logger.info("  Min interval:  %ss", args.min_interval)

    await connector.start()


def main() -> None:
    from calfkit.nodes.chat_node import ChatNode
    from calfkit.stores.in_memory import InMemoryMessageHistoryStore

    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    router_node = AgentRouterNode(
        chat_node=ChatNode(),
        tool_nodes=[],
        message_history_store=InMemoryMessageHistoryStore(),
        system_prompt="You are a market data consumer.",
    )
    asyncio.run(run(args, router_node))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
