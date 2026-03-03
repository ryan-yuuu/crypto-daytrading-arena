"""Shared agent tools and module-level singletons for the trading arena."""

from __future__ import annotations

import os
import time
from datetime import datetime

import sympy

from calfkit.models.tool_context import ToolContext
from calfkit.nodes.base_tool_node import agent_tool

from arena.account_store import AccountStore
from arena.dashboard import PortfolioView
from arena.price_book import PriceBook

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")

# ── Module-level singletons ──────────────────────────────────────

price_book = PriceBook()
store = AccountStore(price_book)
view = PortfolioView(store)


# ── Shared tool logic ────────────────────────────────────────────


def _execute_trade(
    agent_id: str, product_id: str, quantity: float, action: str, latency: float | None = None
) -> str:
    result = store.execute_trade(agent_id, product_id, quantity, action, latency=latency)
    view.rerender()
    return result.message


def _format_hold_time(entry_ts: float | None) -> str:
    """Format elapsed time since entry as a human-readable string."""
    if entry_ts is None:
        return "N/A"
    seconds = datetime.now().timestamp() - entry_ts
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def _get_portfolio(agent_id: str) -> str:
    account = store.get_or_create(agent_id)
    pb = store.price_book

    lines = [f"Cash: ${account.cash:,.2f}"]

    if not account.positions:
        lines.append("Positions: none")
    else:
        lines.append(
            "| Ticker | Qty | Avg Cost | Total Cost "
            "| Current Price | Mkt Value | P&L | Avg Time Held |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for pid in sorted(account.positions):
            qty = account.positions[pid]
            avg_cost = account.avg_cost_per_unit(pid)
            total_cost = account.cost_basis.get(pid, 0.0)
            hold_str = _format_hold_time(account.avg_entry_ts.get(pid))

            entry = pb.get(pid)
            if entry is not None:
                current_price = float(entry["price"])
                mkt_value = current_price * qty
                pnl = mkt_value - total_cost
                pnl_sign = "+" if pnl >= 0 else ""
                lines.append(
                    f"| {pid} | {qty:g} | ${avg_cost:,.2f} | ${total_cost:,.2f} "
                    f"| ${current_price:,.2f} | ${mkt_value:,.2f} "
                    f"| {pnl_sign}${pnl:,.2f} | {hold_str} |"
                )
            else:
                lines.append(
                    f"| {pid} | {qty:g} | ${avg_cost:,.2f} | ${total_cost:,.2f} "
                    f"| N/A | N/A | N/A | {hold_str} |"
                )

    portfolio_val = account.portfolio_value(pb)
    lines.append(f"\nTotal portfolio value: ${portfolio_val:,.2f}")

    return "\n".join(lines)


# ── Shared agent tools (ToolContext injection) ───────────────────


@agent_tool
def execute_trade(ctx: ToolContext, product_id: str, quantity: float, action: str) -> str:
    """Execute a buy or sell trade (fill-or-cancel). The order fills immediately at the current
    market price if possible, or returns an error if it cannot be filled — it never waits or queues.
    Buys execute at the best ask price, sells at the best bid.
    Fractional share trading is allowed, but only to one decimal place (e.g., 0.5, 1.2).

    Args:
        product_id: Trading pair (e.g., BTC-USD, FARTCOIN-USD, SOL-USD)
        quantity: Number of units to trade (positive, up to 1 decimal place)
        action: 'buy' or 'sell'

    Returns:
        Trade confirmation with execution price and remaining cash, or an error message
    """
    latency: float | None = None
    if isinstance(ctx.deps, dict) and "invoked_at" in ctx.deps:
        latency = time.time() - ctx.deps["invoked_at"]
    return _execute_trade(ctx.agent_name, product_id, quantity, action, latency=latency)


@agent_tool
def get_portfolio(ctx: ToolContext) -> str:
    """View your portfolio: available cash, open positions, and total value.

    Returns:
        A table of positions with quantity, average cost basis, current market
        price, unrealized P&L, and average time held — plus cash and total value
    """
    return _get_portfolio(ctx.agent_name)


@agent_tool
def calculator(ctx: ToolContext, expression: str) -> str:
    """Evaluate a math expression. Use for financial calculations you can't do in your head,
    such as position sizing, P&L, percentage changes, or risk/reward ratios.

    Respects standard order of operations (PEMDAS).
    Supported operators: +, -, *, /, ** (power), % (modulo), parentheses for grouping.
    Functions: abs(), sqrt(), log(), floor(), ceil(), min(), max().

    Args:
        expression: A math expression (e.g., '100000 * 0.02', '64200 / 3',
            '(50000 - 32100) / 32100 * 100', 'max(10, 20)')

    Returns:
        The numeric result
    """

    try:
        result = sympy.sympify(expression)
        return str(result.evalf() if not result.is_number else result)
    except (sympy.SympifyError, TypeError) as e:
        return f"Invalid expression: {e}"
