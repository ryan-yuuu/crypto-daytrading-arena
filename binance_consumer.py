"""
Binance Exchange WebSocket consumer that maintains an up-to-date price book.

Subscribes to 24hr ticker streams for a set of symbols and keeps
a local dictionary of the latest bid/ask/price for each.

Also provides a REST-polling alternative (``poll_rest``) that fetches
1-minute OHLCV candles + current prices from the Binance REST API.

Usage:
    uv run python binance_consumer.py
"""

import asyncio
import io
import json
import logging
import signal
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
import websockets

logger = logging.getLogger(__name__)

BINANCE_WS_URL = "wss://stream.binance.com:9443"
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


class PriceBook:
    """Maintains the latest price snapshot for each subscribed product."""

    def __init__(self):
        self._book: dict[str, dict] = {}

    def update(self, data: dict) -> None:
        self._book[data["product_id"]] = {
            "price": data["price"],
            "best_bid": data["best_bid"],
            "best_bid_size": data.get("best_bid_size", "0"),
            "best_ask": data["best_ask"],
            "best_ask_size": data.get("best_ask_size", "0"),
            "side": data.get("side", ""),
            "last_size": data.get("last_size", "0"),
            "volume_24h": data.get("volume_24h", "0"),
            "time": data.get("time", ""),
        }

    def get(self, product_id: str) -> Optional[dict]:
        return self._book.get(product_id)

    def snapshot(self) -> dict[str, dict]:
        return dict(self._book)

    def display(self, product_ids: list[str]) -> None:
        if not self._book:
            return

        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"\n{'=' * 78}")
        print(f"  Price Book @ {now} UTC")
        print(f"{'=' * 78}")
        print(
            f"  {'Product':<14} {'Price':>12} {'Bid':>12} {'Ask':>12}"
            f" {'Spread':>10} {'Vol 24h':>14}"
        )
        print(f"  {'-' * 74}")

        for product_id in product_ids:
            entry = self._book.get(product_id)
            if entry is None:
                print(f"  {product_id:<14} {'--':>12}")
                continue

            bid = float(entry["best_bid"])
            ask = float(entry["best_ask"])
            spread = ask - bid

            print(
                f"  {product_id:<14}"
                f" {entry['price']:>12}"
                f" {entry['best_bid']:>12}"
                f" {entry['best_ask']:>12}"
                f" {spread:>10.6f}"
                f" {float(entry['volume_24h']):>14,.2f}"
            )

        print(f"{'=' * 78}")


@dataclass
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Timeframe:
    """Defines a candle granularity and the time window it covers."""

    granularity: int  # seconds (Binance API param: 60, 300, 900)
    start_minutes_ago: int  # beginning of window (farther from now)
    end_minutes_ago: int  # end of window (closer to now)
    label: str  # human-readable label for the agent prompt


TIMEFRAMES = [
    Timeframe(900, 180, 90, "15-min candles (3h ago -> 90min ago)"),
    Timeframe(300, 90, 20, "5-min candles (90min ago -> 20min ago)"),
    Timeframe(60, 20, 0, "1-min candles (last 20 minutes)"),
]


class CandleBook:
    """Maintains multi-timeframe OHLCV candles
    per product from the Binance REST API."""

    def __init__(self) -> None:
        # Keyed by (product_id, granularity_seconds)
        self._candles: dict[tuple[str, int], list[Candle]] = {}

    def update_from_api(self, product_id: str, granularity: int, raw_candles: list[list]) -> None:
        """Parse Binance REST candle response and replace stored candles.

        Binance returns arrays of
        ``[timestamp, open, high, low, close, volume, ...]``
        in *ascending* time order.
        """
        candles = [
            Candle(
                time=datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
            for row in raw_candles
        ]
        candles.sort(key=lambda c: c.time)
        self._candles[(product_id, granularity)] = candles

    def format_prompt(self, product_ids: list[str]) -> str:
        """Build a structured, multi-timeframe price history for the agent prompt."""
        buf = io.StringIO()
        for tf in TIMEFRAMES:
            buf.write(f"### {tf.label}\n")
            buf.write("product,time,open,high,low,close,volume\n")
            for pid in product_ids:
                for c in self._candles.get((pid, tf.granularity), []):
                    buf.write(
                        f"{pid},{c.time.strftime('%Y-%m-%dT%H:%M:%SZ')},"
                        f"{c.open:.2f},{c.high:.2f},{c.low:.2f},"
                        f"{c.close:.2f},{c.volume:.2f}\n"
                    )
            buf.write("\n")
        return buf.getvalue()

    def has_data(self) -> bool:
        return any(bool(v) for v in self._candles.values())


class BinanceRESTClient:
    """Binance REST API client with automatic failover."""

    def __init__(self):
        self._url_index = 0
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def _base_url(self) -> str:
        return BINANCE_REST_URLS[self._url_index]

    def _rotate_url(self) -> str:
        """Rotate to next fallback URL."""
        self._url_index = (self._url_index + 1) % len(BINANCE_REST_URLS)
        logger.info("Switching to fallback URL: %s", self._base_url)
        return self._base_url

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=15.0)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> dict:
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
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
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


async def consume(price_book: PriceBook, symbols: list[str]) -> None:
    """Consume ticker data from Binance WebSocket with ping/pong handling."""
    # Build combined stream URL: /ws/btcusdt@ticker/ethusdt@ticker/...
    streams = "/".join(f"{s.lower()}@ticker" for s in symbols)
    ws_url = f"{BINANCE_WS_URL}/ws/{streams}"

    while True:
        try:
            async with websockets.connect(ws_url) as ws:
                print(f"Connected. Subscribed to {len(symbols)} symbols.")
                print("Waiting for data (updates every ~1s)...\n")

                async for raw in ws:
                    data = json.loads(raw)

                    # Handle ping/pong (Binance-specific)
                    if "ping" in data:
                        await ws.send(json.dumps({"pong": data["ping"]}))
                        continue

                    # Handle ticker data (combined stream wrapper)
                    if "stream" in data and "data" in data:
                        ticker_data = data["data"]
                        if ticker_data.get("e") == "24hrTicker":
                            # Map Binance format to unified format
                            product_id = ticker_data["s"]
                            price_book.update({
                                "product_id": product_id,
                                "price": ticker_data["c"],
                                "best_bid": ticker_data["b"],
                                "best_bid_size": ticker_data["B"],
                                "best_ask": ticker_data["a"],
                                "best_ask_size": ticker_data["A"],
                                "side": "",  # Binance doesn't provide side in ticker
                                "last_size": ticker_data.get("Q", "0"),  # Last quantity
                                "volume_24h": ticker_data["v"],
                                "time": datetime.fromtimestamp(
                                    ticker_data["E"] / 1000, tz=timezone.utc
                                ).isoformat(),
                            })
                            price_book.display(symbols)

        except websockets.ConnectionClosed:
            print("\nConnection lost. Reconnecting in 3s...")
            await asyncio.sleep(3)
        except Exception as e:
            print(f"\nError: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)


async def poll_rest(
    symbols: list[str],
    price_book: PriceBook,
    candle_book: CandleBook,
    interval: float = 60.0,
) -> None:
    """Poll Binance REST API for multi-timeframe candles and current prices.

    Runs indefinitely, fetching candles at each configured timeframe and
    the latest ticker for each symbol every ``interval`` seconds.
    """
    async with BinanceRESTClient() as client:
        while True:
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

            for symbol in symbols:
                try:
                    # Fetch candles for each timeframe
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

                    # Fetch current ticker
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


def main() -> None:
    from binance_kafka_connector import DEFAULT_SYMBOLS

    price_book = PriceBook()

    loop = asyncio.new_event_loop()

    def shutdown(sig, frame):
        print("\nShutting down...")
        loop.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    loop.run_until_complete(consume(price_book, DEFAULT_SYMBOLS))


if __name__ == "__main__":
    main()
