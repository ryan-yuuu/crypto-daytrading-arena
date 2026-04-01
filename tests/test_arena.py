"""Integration tests for the crypto daytrading arena.

Uses FastStream's TestKafkaBroker for in-memory Kafka simulation
(no real broker required). Requires an OpenAI API key for LLM inference.
"""

import asyncio
import os

import pytest
from dotenv import load_dotenv
from faststream.kafka import TestKafkaBroker

from calfkit import Agent, Client, OpenAIModelClient, Worker

from arena.models import INITIAL_CASH
from arena.tools import calculator, execute_trade, get_portfolio, price_book, store

load_dotenv()

# The deployed agent processes all requests; trades are recorded under its name.
AGENT_NAME = "arena_agent"
AGENT_INPUT_TOPIC = "agent_router.input"

skip_if_no_openai_key = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="Skipping: OPENAI_API_KEY not set",
)

# ── Test market data ────────────────────────────────────────────

TEST_PRICES = {
    "BTC-USD": {
        "product_id": "BTC-USD",
        "price": "50000.00",
        "best_bid": "49990.00",
        "best_bid_size": "1.5",
        "best_ask": "50010.00",
        "best_ask_size": "2.0",
        "side": "buy",
        "last_size": "0.1",
        "volume_24h": "15000.0",
        "time": "2024-01-01T00:00:00Z",
    },
    "SOL-USD": {
        "product_id": "SOL-USD",
        "price": "100.00",
        "best_bid": "99.90",
        "best_bid_size": "100",
        "best_ask": "100.10",
        "best_ask_size": "150",
        "side": "buy",
        "last_size": "5.0",
        "volume_24h": "500000.0",
        "time": "2024-01-01T00:00:00Z",
    },
}


# ── Fixtures ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def seed_price_book():
    """Seed shared PriceBook with test data and reset accounts between tests."""
    for data in TEST_PRICES.values():
        price_book.update(data)
    store._accounts.clear()
    store._trade_log.clear()
    yield
    store._accounts.clear()
    store._trade_log.clear()


@pytest.fixture(scope="session")
def deploy_client() -> Client:
    """Wire up all arena worker nodes on a Client.

    Registers: Agent (with embedded model client) and tool nodes.
    The agent processes all incoming requests via the Worker.

    NOTE: temp_instructions on execute_node() are silently dropped by
    pydantic-ai's UserPromptNode — only the Agent's system_prompt reaches
    the model. This system_prompt must work for all tests.
    """
    client = Client.connect()

    model_client = OpenAIModelClient("gpt-5-nano", reasoning_effort="high")

    # Agent node (subscriber for agent_router.input)
    agent = Agent(
        AGENT_NAME,
        system_prompt=(
            "You are an obedient trading bot. Execute trades immediately when asked. "
            "Use tools to check your portfolio and make trades. Never ask for confirmation. "
            "When you see large unrealized gains, sell to lock in profits. Always act decisively."
        ),
        subscribe_topics=AGENT_INPUT_TOPIC,
        model_client=model_client,
        tools=[execute_trade, get_portfolio, calculator],
    )

    worker = Worker(client, nodes=[agent, execute_trade, get_portfolio, calculator])
    worker.register_handlers()

    return client


def _account():
    """Get the arena agent's account from the shared store."""
    return store.get_or_create(AGENT_NAME)


# ── Tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@skip_if_no_openai_key
async def test_agent_executes_trade(deploy_client):
    """Agent receives a prompt and executes a BTC buy via the execute_trade tool."""
    client = deploy_client

    async with TestKafkaBroker(client.broker):
        await client.execute_node(
            user_prompt="Buy 0.1 BTC-USD right now.",
            topic=AGENT_INPUT_TOPIC,
            timeout=30.0,
        )

        account = _account()
        assert account.positions.get("BTC-USD", 0) > 0, "Agent should have bought BTC"
        assert account.cash < INITIAL_CASH, "Cash should have decreased after buying"
        assert account.trade_count > 0


@pytest.mark.asyncio
@skip_if_no_openai_key
async def test_agent_checks_portfolio(deploy_client):
    """Agent uses get_portfolio tool and reports back."""
    client = deploy_client

    async with TestKafkaBroker(client.broker):
        result = await client.execute_node(
            user_prompt="What does my portfolio look like?",
            topic=AGENT_INPUT_TOPIC,
            timeout=30.0,
        )

        assert result.output is not None
        assert "100,000" in str(result.output) or "100000" in str(result.output)


@pytest.mark.asyncio
@skip_if_no_openai_key
async def test_multi_turn_trading(deploy_client):
    """Multi-turn conversation: buy, then check portfolio across turns."""
    client = deploy_client

    async with TestKafkaBroker(client.broker):
        # Turn 1: Buy SOL
        result = await client.execute_node(
            user_prompt="Buy 5 SOL-USD",
            topic=AGENT_INPUT_TOPIC,
            timeout=30.0,
        )

        account = _account()
        assert account.positions.get("SOL-USD", 0) > 0, "Should have bought SOL"

        # Turn 2: Check portfolio (pass message_history for multi-turn)
        result = await client.execute_node(
            user_prompt="Show me my current portfolio",
            topic=AGENT_INPUT_TOPIC,
            message_history=result.message_history,
            timeout=30.0,
        )

        assert result.output is not None
        assert "sol" in str(result.output).lower(), "Portfolio should mention SOL position"


@pytest.mark.asyncio
@skip_if_no_openai_key
async def test_agent_uses_calculator(deploy_client):
    """Agent uses the calculator tool for a math question."""
    client = deploy_client

    async with TestKafkaBroker(client.broker):
        result = await client.execute_node(
            user_prompt="Use the calculator to compute 50000 * 0.1",
            topic=AGENT_INPUT_TOPIC,
            timeout=30.0,
        )

        assert result.output is not None
        assert "5000" in str(result.output)


@pytest.mark.asyncio
@skip_if_no_openai_key
async def test_full_trading_session(deploy_client):
    """End-to-end session: buy, sell, check portfolio."""
    client = deploy_client

    async with TestKafkaBroker(client.broker):
        # Buy BTC
        result = await client.execute_node(
            user_prompt="Buy 0.5 BTC-USD",
            topic=AGENT_INPUT_TOPIC,
            timeout=30.0,
        )

        account = _account()
        assert account.positions.get("BTC-USD", 0) == 0.5
        expected_cost = 50010.00 * 0.5  # best_ask * qty
        assert account.cash == pytest.approx(INITIAL_CASH - expected_cost, rel=1e-2)

        # Sell some
        result = await client.execute_node(
            user_prompt="Sell 0.2 BTC-USD",
            topic=AGENT_INPUT_TOPIC,
            message_history=result.message_history,
            timeout=30.0,
        )

        account = _account()
        assert account.positions.get("BTC-USD", 0) == 0.3
        assert account.trade_count == 2

        # Check portfolio mentions BTC
        result = await client.execute_node(
            user_prompt="Show my portfolio",
            topic=AGENT_INPUT_TOPIC,
            message_history=result.message_history,
            timeout=30.0,
        )

        assert result.output is not None
        assert "btc" in str(result.output).lower()


@pytest.mark.asyncio
@skip_if_no_openai_key
async def test_autonomous_portfolio_check_and_trade(deploy_client):
    """Default-strategy agent checks portfolio and sells into a price spike in one turn.

    Setup:
    - Account pre-seeded with 1.0 BTC-USD at $50,010 cost basis
    - BTC-USD live price spiked to $500,000 (10x unrealized gain)

    The agent should autonomously:
    1. Call get_portfolio — discover BTC position with massive unrealized P&L
    2. Call execute_trade — sell some/all BTC to lock in profits

    Both tool calls occur within a single execute_node() invocation, proving the
    agent makes multiple autonomous tool calls in one turn.
    """
    client = deploy_client

    # Pre-seed the arena_agent account with a BTC position
    account = store.get_or_create(AGENT_NAME)
    account.positions["BTC-USD"] = 1.0
    account.cost_basis["BTC-USD"] = 50_010.0  # avg cost $50,010 (original best_ask)
    account.cash = INITIAL_CASH - 50_010.0  # $49,990 remaining

    # Spike BTC price to $500,000 — a 10x move over cost basis
    price_book.update({
        "product_id": "BTC-USD",
        "price": "500000.00",
        "best_bid": "499500.00",
        "best_bid_size": "5.0",
        "best_ask": "500500.00",
        "best_ask_size": "3.0",
        "side": "buy",
        "last_size": "0.5",
        "volume_24h": "25000.0",
        "time": "2024-01-01T12:00:00Z",
    })

    ticker_json = (
        '[{"product_id": "BTC-USD", "price": "500000.00", '
        '"best_bid": "499500.00", "best_ask": "500500.00"}, '
        '{"product_id": "SOL-USD", "price": "100.00", '
        '"best_bid": "99.90", "best_ask": "100.10"}]'
    )

    async with TestKafkaBroker(client.broker):
        await client.execute_node(
            user_prompt=(
                "Here is the latest ticker information. You should view your "
                "portfolio first before making any decisions to trade.\n"
                "price = last traded price, best_bid = price you sell at, "
                "best_ask = price you buy at.\n\n"
                f"{ticker_json}"
            ),
            topic=AGENT_INPUT_TOPIC,
            timeout=45.0,
        )

    account = _account()
    assert account.trade_count > 0, "Agent should have executed at least one trade"
    pre_trade_cash = INITIAL_CASH - 50_010.0
    assert account.cash > pre_trade_cash, "Cash should have increased from selling BTC"
    assert account.positions.get("BTC-USD", 0) < 1.0, "Agent should have sold some/all BTC"
