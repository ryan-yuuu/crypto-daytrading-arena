"""Integration tests for the crypto daytrading arena.

Uses FastStream's TestKafkaBroker for in-memory Kafka simulation
(no real broker required). Requires an OpenAI API key for LLM inference.
"""

import asyncio
import os

import pytest
from dotenv import load_dotenv
from faststream.kafka import TestKafkaBroker

from calfkit._vendor.pydantic_ai import ModelResponse
from calfkit.broker.broker import BrokerClient
from calfkit.nodes.agent_router_node import AgentRouterNode
from calfkit.nodes.chat_node import ChatNode
from calfkit.providers.pydantic_ai.openai import OpenAIModelClient
from calfkit.runners.service import NodesService
from calfkit.runners.service_client import RouterServiceClient
from calfkit.stores.in_memory import InMemoryMessageHistoryStore

from arena.models import INITIAL_CASH
from arena.strategies import STRATEGIES
from arena.tools import calculator, execute_trade, get_portfolio, price_book, store

load_dotenv()

# The deployed router processes all requests; trades are recorded under its name.
ROUTER_NAME = "arena_router"

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
def deploy_broker() -> BrokerClient:
    """Wire up all arena worker nodes on a BrokerClient.

    Registers: ChatNode (LLM), tool nodes, and a router node.
    The router processes all incoming requests. Test-created routers
    serve as client references that define system_prompt and tool selection
    (packaged into the EventEnvelope), but the deployed router executes them.
    """
    broker = BrokerClient()
    service = NodesService(broker)

    # ChatNode worker (LLM inference)
    model_client = OpenAIModelClient("gpt-5-nano", reasoning_effort="low")
    chat_node = ChatNode(model_client)
    service.register_node(chat_node)

    # Tool node workers
    service.register_node(execute_trade)
    service.register_node(get_portfolio)
    service.register_node(calculator)

    # Router node (subscriber for agent_router.input)
    all_tools = [execute_trade, get_portfolio, calculator]
    router_node = AgentRouterNode(
        chat_node=ChatNode(),
        tool_nodes=all_tools,
        message_history_store=InMemoryMessageHistoryStore(),
        name=ROUTER_NAME,
    )
    service.register_node(router_node)

    return broker


def _account():
    """Get the arena router's account from the shared store."""
    return store.get_or_create(ROUTER_NAME)


# ── Tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@skip_if_no_openai_key
async def test_agent_executes_trade(deploy_broker):
    """Agent receives a prompt and executes a BTC buy via the execute_trade tool."""
    broker = deploy_broker
    router = AgentRouterNode(
        chat_node=ChatNode(),
        tool_nodes=[execute_trade, get_portfolio, calculator],
        name="trade_tester",
        system_prompt=(
            "You are a test trading agent. When the user asks you to buy, "
            "use the execute_trade tool immediately. Do not ask for confirmation."
        ),
    )

    async with TestKafkaBroker(broker):
        client = RouterServiceClient(broker, router)
        response = await client.request(user_prompt="Buy 0.1 BTC-USD right now.")
        final_msg = await asyncio.wait_for(response.get_final_response(), timeout=30.0)
        assert isinstance(final_msg, ModelResponse)

        account = _account()
        assert account.positions.get("BTC-USD", 0) > 0, "Agent should have bought BTC"
        assert account.cash < INITIAL_CASH, "Cash should have decreased after buying"
        assert account.trade_count > 0


@pytest.mark.asyncio
@skip_if_no_openai_key
async def test_agent_checks_portfolio(deploy_broker):
    """Agent uses get_portfolio tool and reports back."""
    broker = deploy_broker
    router = AgentRouterNode(
        chat_node=ChatNode(),
        tool_nodes=[get_portfolio],
        name="portfolio_viewer",
        system_prompt=(
            "You are a helpful trading assistant. When asked about the portfolio, "
            "always use the get_portfolio tool and relay the results."
        ),
    )

    async with TestKafkaBroker(broker):
        client = RouterServiceClient(broker, router)
        response = await client.request(user_prompt="What does my portfolio look like?")
        final_msg = await asyncio.wait_for(response.get_final_response(), timeout=30.0)
        assert isinstance(final_msg, ModelResponse)
        assert final_msg.text is not None
        assert "100,000" in final_msg.text or "100000" in final_msg.text


@pytest.mark.asyncio
@skip_if_no_openai_key
async def test_multi_turn_trading(deploy_broker):
    """Multi-turn conversation: buy, then check portfolio across turns."""
    broker = deploy_broker
    router = AgentRouterNode(
        chat_node=ChatNode(),
        tool_nodes=[execute_trade, get_portfolio],
        name="multi_turn_trader",
        system_prompt=(
            "You are a trading assistant. Execute trades when asked. "
            "Check portfolio when asked. Be concise."
        ),
    )
    thread_id = "test-multi-turn"

    async with TestKafkaBroker(broker):
        client = RouterServiceClient(broker, router)

        # Turn 1: Buy SOL
        response = await client.request(user_prompt="Buy 5 SOL-USD", thread_id=thread_id)
        final_msg = await asyncio.wait_for(response.get_final_response(), timeout=30.0)
        assert isinstance(final_msg, ModelResponse)

        account = _account()
        assert account.positions.get("SOL-USD", 0) > 0, "Should have bought SOL"

        # Turn 2: Check portfolio (should show SOL position)
        response = await client.request(
            user_prompt="Show me my current portfolio", thread_id=thread_id
        )
        final_msg = await asyncio.wait_for(response.get_final_response(), timeout=30.0)
        assert isinstance(final_msg, ModelResponse)
        assert final_msg.text is not None
        assert "sol" in final_msg.text.lower(), "Portfolio should mention SOL position"


@pytest.mark.asyncio
@skip_if_no_openai_key
async def test_agent_uses_calculator(deploy_broker):
    """Agent uses the calculator tool for a math question."""
    broker = deploy_broker
    router = AgentRouterNode(
        chat_node=ChatNode(),
        tool_nodes=[calculator, get_portfolio],
        name="calc_tester",
        system_prompt=(
            "You are a trading assistant with a calculator. "
            "Always use the calculator tool for any math calculations. "
            "Report the exact result from the calculator."
        ),
    )

    async with TestKafkaBroker(broker):
        client = RouterServiceClient(broker, router)
        response = await client.request(user_prompt="Use the calculator to compute 50000 * 0.1")
        final_msg = await asyncio.wait_for(response.get_final_response(), timeout=30.0)
        assert isinstance(final_msg, ModelResponse)
        assert final_msg.text is not None
        assert "5000" in final_msg.text


@pytest.mark.asyncio
@skip_if_no_openai_key
async def test_full_trading_session(deploy_broker):
    """End-to-end session: buy, sell, check portfolio."""
    broker = deploy_broker
    router = AgentRouterNode(
        chat_node=ChatNode(),
        tool_nodes=[execute_trade, get_portfolio, calculator],
        name="full_session_trader",
        system_prompt=(
            "You are an obedient trading bot. Execute exactly what is asked. "
            "Do not ask for confirmation. Do not add extra trades."
        ),
    )
    thread_id = "test-full-session"

    async with TestKafkaBroker(broker):
        client = RouterServiceClient(broker, router)

        # Buy BTC
        response = await client.request(user_prompt="Buy 0.5 BTC-USD", thread_id=thread_id)
        await asyncio.wait_for(response.get_final_response(), timeout=30.0)

        account = _account()
        assert account.positions.get("BTC-USD", 0) == 0.5
        expected_cost = 50010.00 * 0.5  # best_ask * qty
        assert account.cash == pytest.approx(INITIAL_CASH - expected_cost, rel=1e-2)

        # Sell some
        response = await client.request(user_prompt="Sell 0.2 BTC-USD", thread_id=thread_id)
        await asyncio.wait_for(response.get_final_response(), timeout=30.0)

        account = _account()
        assert account.positions.get("BTC-USD", 0) == 0.3
        assert account.trade_count == 2

        # Check portfolio mentions BTC
        response = await client.request(user_prompt="Show my portfolio", thread_id=thread_id)
        final_msg = await asyncio.wait_for(response.get_final_response(), timeout=30.0)
        assert isinstance(final_msg, ModelResponse)
        assert final_msg.text is not None
        assert "btc" in final_msg.text.lower()


@pytest.mark.asyncio
@skip_if_no_openai_key
async def test_autonomous_portfolio_check_and_trade(deploy_broker):
    """Default-strategy agent checks portfolio and sells into a price spike in one turn.

    Setup:
    - Account pre-seeded with 1.0 BTC-USD at $50,010 cost basis
    - BTC-USD live price spiked to $500,000 (10x unrealized gain)

    The agent should autonomously:
    1. Call get_portfolio — discover BTC position with massive unrealized P&L
    2. Call execute_trade — sell some/all BTC to lock in profits

    Both tool calls occur within a single client.request() invocation, proving the
    agent makes multiple autonomous tool calls in one turn.
    """
    broker = deploy_broker
    router = AgentRouterNode(
        chat_node=ChatNode(),
        tool_nodes=[execute_trade, get_portfolio, calculator],
        name="autonomous_trader",
        system_prompt=STRATEGIES["default"],
    )

    # Pre-seed the arena_router account with a BTC position
    account = store.get_or_create(ROUTER_NAME)
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

    async with TestKafkaBroker(broker):
        client = RouterServiceClient(broker, router)
        ticker_json = (
            '[{"product_id": "BTC-USD", "price": "500000.00", '
            '"best_bid": "499500.00", "best_ask": "500500.00"}, '
            '{"product_id": "SOL-USD", "price": "100.00", '
            '"best_bid": "99.90", "best_ask": "100.10"}]'
        )
        response = await client.request(
            user_prompt=(
                "Here is the latest ticker information. You should view your "
                "portfolio first before making any decisions to trade.\n"
                "price = last traded price, best_bid = price you sell at, "
                "best_ask = price you buy at.\n\n"
                f"{ticker_json}"
            ),
        )
        final_msg = await asyncio.wait_for(response.get_final_response(), timeout=45.0)
        assert isinstance(final_msg, ModelResponse)

    account = _account()
    assert account.trade_count > 0, "Agent should have executed at least one trade"
    pre_trade_cash = INITIAL_CASH - 50_010.0
    assert account.cash > pre_trade_cash, "Cash should have increased from selling BTC"
    assert account.positions.get("BTC-USD", 0) < 1.0, "Agent should have sold some/all BTC"
