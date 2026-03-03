"""Binance exchange connector — WebSocket consumer, REST poller,
and Kafka connector in a single module.

Usage:
    uv run python exchanges/binance.py
    uv run python exchanges/binance.py --symbols BTCUSDT ETHUSDT SOLUSDT
    uv run python exchanges/binance.py --min-interval 30
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import websockets

from calfkit.broker.broker import BrokerClient
from calfkit.nodes.agent_router_node import AgentRouterNode
from calfkit.runners.service_client import RouterServiceClient

from arena.models import Candle, TIMEFRAMES
from arena.price_book import CandleBook, PriceBook
from exchanges import PRICE_TOPIC, TickerMessage

logger = logging.getLogger(__name__)

BINANCE_WS_URL = "wss://stream.binance.com:9443"
BINANCE_WS_URL_FALLBACK = "wss://stream.binance.com:443"
BINANCE_REST_URLS = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]

# Binance interval mapping (seconds -> Binance format)
BINANCE_INTERVAL_MAP = {
    60: "1m",
    300: "5m",
    900: "15m",
}

DEFAULT_SYMBOLS = [
    "BTCUSDT",
    "FARTCOINUSDT",
    "SOLUSDT",
]

RECONNECT_DELAY_SECONDS = 3
PING_INTERVAL_SECONDS = 30  # Must send ping within 60 seconds per Binance docs
MAX_CONNECTION_LIFETIME_SECONDS = 82800  # 23 hours (Binance limit is 24h)


def parse_binance_candle(row: list) -> Candle:
    """Parse a Binance candle row: [ts_ms, open, high, low, close, vol, ...]."""
    return Candle(
        time=datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
    )


class BinanceRESTClient:
    """Binance REST API client with automatic failover."""

    def __init__(self) -> None:
        self._url_index = 0
        self._client = httpx.AsyncClient(timeout=15.0)

    @property
    def _base_url(self) -> str:
        return BINANCE_REST_URLS[self._url_index]

    def _rotate_url(self) -> str:
        """Rotate to next fallback URL."""
        self._url_index = (self._url_index + 1) % len(BINANCE_REST_URLS)
        logger.info("Switching to fallback URL: %s", self._base_url)
        return self._base_url

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        """Make request with automatic retry on different base URLs."""
        last_error = None

        for _ in range(len(BINANCE_REST_URLS)):
            url = f"{self._base_url}{path}"
            try:
                response = await self._client.request(method, url, **kwargs)
                response.raise_for_status()
                return response.json()
            except (httpx.NetworkError, httpx.TimeoutException) as e:
                last_error = e
                logger.warning("Request failed to %s: %s", url, e)
                self._rotate_url()
            except httpx.HTTPStatusError as e:
                # Don't retry on 4xx errors
                if e.response.status_code < 500:
                    raise
                last_error = e
                logger.warning("Server error from %s: %s", url, e)
                self._rotate_url()

        raise last_error or Exception("All Binance API endpoints failed")

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 1000,
    ) -> list[list]:
        """Fetch klines (candlestick) data."""
        params = {
            "symbol": symbol.upper(),
            "interval": interval,
            "limit": limit,
        }
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time

        return await self._request("GET", "/api/v3/klines", params=params)

    async def get_24h_ticker(self, symbol: str) -> dict:
        """Fetch 24h rolling window ticker statistics."""
        params = {"symbol": symbol.upper()}
        return await self._request("GET", "/api/v3/ticker/24hr", params=params)

async def poll_rest(
    symbols: list[str],
    price_book: PriceBook,
    candle_book: CandleBook,
    interval: float = 60.0,
) -> None:
    """Poll Binance REST API for multi-timeframe candles and current prices."""
    async with BinanceRESTClient() as client:
        while True:
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

            for symbol in symbols:
                try:
                    for tf in TIMEFRAMES:
                        start_ms = now_ms - tf.start_minutes_ago * 60 * 1000
                        end_ms = now_ms - tf.end_minutes_ago * 60 * 1000
                        interval_str = BINANCE_INTERVAL_MAP.get(tf.granularity, "1m")

                        resp = await client.get_klines(
                            symbol=symbol,
                            interval=interval_str,
                            start_time=start_ms,
                            end_time=end_ms,
                        )
                        candle_book.update_from_api(symbol, tf.granularity, resp)

                    resp = await client.get_24h_ticker(symbol)
                    price_book.update({
                        "product_id": symbol,
                        "price": resp["lastPrice"],
                        "best_bid": resp["bidPrice"],
                        "best_bid_size": resp["bidQty"],
                        "best_ask": resp["askPrice"],
                        "best_ask_size": resp["askQty"],
                        "side": "",
                        "last_size": resp["lastQty"],
                        "volume_24h": resp["volume"],
                        "time": datetime.fromtimestamp(
                            resp["closeTime"] / 1000, tz=timezone.utc
                        ).isoformat(),
                    })
                except Exception:
                    logger.exception("REST poll failed for %s", symbol)

            await asyncio.sleep(interval)


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
        candle_book: CandleBook | None = None,
    ) -> None:
        if not symbols:
            raise ValueError("At least one symbol must be specified")
        self._broker = broker
        self._client = RouterServiceClient(broker, router_node, deps_type=dict)
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

    def _parse_binance_ticker(self, data: dict) -> TickerMessage | None:
        """Parse Binance 24hr ticker data into TickerMessage."""
        try:
            return TickerMessage(
                product_id=data["s"],
                price=data["c"],
                best_bid=data["b"],
                best_bid_size=data["B"],
                best_ask=data["a"],
                best_ask_size=data["A"],
                side="",
                last_size=data.get("Q", "0"),
                open_24h=data["o"],
                high_24h=data["h"],
                low_24h=data["l"],
                volume_24h=data["v"],
                volume_30d="0",
                trade_id=data.get("n", 0),
                sequence=0,
                time=datetime.fromtimestamp(
                    data["E"] / 1000, tz=timezone.utc
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

    async def _ping_loop(self, ws: websockets.ClientConnection) -> None:
        """Send ping frames periodically to keep connection alive."""
        while self._running:
            try:
                await asyncio.sleep(PING_INTERVAL_SECONDS)
                if ws.state.name == "OPEN":
                    await ws.ping()
                    logger.debug("Ping sent")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Ping failed: %s", e)

    async def _lifetime_manager(self, ws: websockets.ClientConnection) -> None:
        """Force reconnection before 24-hour limit."""
        await asyncio.sleep(MAX_CONNECTION_LIFETIME_SECONDS)
        logger.info("Connection approaching 24h limit, forcing reconnection")
        await ws.close()

    async def _connect_with_fallback(self, streams: str) -> websockets.ClientConnection:
        """Connect to Binance WebSocket with fallback endpoints."""
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

            candle_task: asyncio.Task | None = None
            if self._candle_book is not None:
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
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
        except Exception as e:
            logger.debug("Config not loaded, using default symbols: %s", e)
            symbols = list(DEFAULT_SYMBOLS)

    candle_book = CandleBook(parse_row=parse_binance_candle)
    broker = BrokerClient(bootstrap_servers=args.bootstrap_servers)
    connector = BinanceKafkaConnector(
        broker=broker,
        router_node=router_node,
        symbols=symbols,
        min_publish_interval=args.min_interval,
        candle_book=candle_book,
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
