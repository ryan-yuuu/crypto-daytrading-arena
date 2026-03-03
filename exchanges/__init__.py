"""Shared types for exchange connectors."""

from pydantic import BaseModel

PRICE_TOPIC = "market_data.prices"


class TickerMessage(BaseModel):
    """Ticker message published to Kafka (common schema for all exchanges)."""

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
