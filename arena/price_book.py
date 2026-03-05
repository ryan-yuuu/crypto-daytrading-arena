"""Deduplicated PriceBook and CandleBook used by all exchange consumers."""

from __future__ import annotations

import io
import logging
from collections.abc import Callable
from datetime import datetime, timezone

from arena.models import Candle, Timeframe, TIMEFRAMES

log = logging.getLogger(__name__)


class PriceBook:
    """Maintains the latest price snapshot for each subscribed product."""

    def __init__(self) -> None:
        self._book: dict[str, dict] = {}

    def update(self, data: dict) -> None:
        product_id = data["product_id"]
        self._book[product_id] = {
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
        log.debug(
            "price_book.update: %s price=%s bid=%s ask=%s",
            product_id, data["price"], data["best_bid"], data["best_ask"],
        )

    def get(self, product_id: str) -> dict | None:
        entry = self._book.get(product_id)
        if entry is None:
            log.debug("price_book.get: %s -> NOT FOUND (available: %s)",
                      product_id, list(self._book.keys()))
        return entry

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


def _default_parse_row(row: list) -> Candle:
    """Parse a Coinbase-format candle row: [ts, low, high, open, close, vol]."""
    return Candle(
        time=datetime.fromtimestamp(row[0], tz=timezone.utc),
        open=float(row[3]),
        high=float(row[2]),
        low=float(row[1]),
        close=float(row[4]),
        volume=float(row[5]),
    )


class CandleBook:
    """Maintains multi-timeframe OHLCV candles per product from a REST API."""

    def __init__(self, parse_row: Callable[[list], Candle] | None = None) -> None:
        self._candles: dict[tuple[str, int], list[Candle]] = {}
        self._parse_row = parse_row or _default_parse_row

    def update_from_api(self, product_id: str, granularity: int, raw_candles: list[list]) -> None:
        """Parse REST candle response and replace stored candles."""
        candles = [self._parse_row(row) for row in raw_candles]
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
