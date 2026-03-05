"""In-memory trading account store, keyed by agent_id."""

from __future__ import annotations

import logging
from datetime import datetime

from arena.models import AgentAccount, TradeRecorder, TradeResult
from arena.price_book import PriceBook

log = logging.getLogger(__name__)


class AccountStore:
    """In-memory trading account store, keyed by agent_id."""

    def __init__(self, price_book: PriceBook) -> None:
        self._accounts: dict[str, AgentAccount] = {}
        self._trade_log: list[tuple[str, str, str, str, float, float, float | None]] = []
        self._price_book = price_book
        self._data_recorder: TradeRecorder | None = None

    def attach_recorder(self, recorder: TradeRecorder) -> None:
        self._data_recorder = recorder

    def get_or_create(self, agent_id: str) -> AgentAccount:
        if agent_id not in self._accounts:
            log.debug("account_store.get_or_create: new account for agent=%s", agent_id)
            self._accounts[agent_id] = AgentAccount()
        return self._accounts[agent_id]

    @property
    def accounts(self) -> dict[str, AgentAccount]:
        return self._accounts

    @property
    def price_book(self) -> PriceBook:
        return self._price_book

    @property
    def trade_log(self) -> list[tuple[str, str, str, str, float, float, float | None]]:
        return self._trade_log

    def execute_trade(
        self,
        agent_id: str,
        product_id: str,
        quantity: float,
        action: str,
        latency: float | None = None,
    ) -> TradeResult:
        product_id = product_id.upper().strip()
        action = action.lower().strip()
        log.debug(
            "account_store.execute_trade ENTER: agent=%s action=%s product=%s qty=%s",
            agent_id, action, product_id, quantity,
        )

        if action not in ("buy", "sell"):
            log.debug("account_store.execute_trade REJECT: invalid action=%s", action)
            return TradeResult(False, f"Invalid action '{action}'. Must be 'buy' or 'sell'.")

        entry = self._price_book.get(product_id)
        if entry is None:
            available = ", ".join(sorted(self._price_book.snapshot().keys()))
            log.debug(
                "account_store.execute_trade REJECT: no price for %s (available: %s)",
                product_id, available,
            )
            return TradeResult(
                False,
                f"No live price for '{product_id}'. "
                f"Available: {available or 'none (waiting for price data)'}",
            )

        if quantity <= 0:
            log.debug("account_store.execute_trade REJECT: non-positive qty=%s", quantity)
            return TradeResult(False, "Quantity must be positive.")

        rounded = round(quantity, 1)
        if abs(quantity - rounded) > 1e-9:
            log.debug("account_store.execute_trade REJECT: qty precision qty=%s", quantity)
            return TradeResult(
                False, "Quantity must have at most 1 decimal place (e.g., 0.5, 1.2)."
            )
        quantity = rounded

        account = self.get_or_create(agent_id)

        if action == "buy":
            price = float(entry["best_ask"])
            cost = price * quantity
            if cost > account.cash:
                log.debug(
                    "account_store.execute_trade REJECT: insufficient cash "
                    "need=%.2f have=%.2f agent=%s",
                    cost, account.cash, agent_id,
                )
                return TradeResult(
                    False,
                    f"Insufficient cash. Need ${cost:,.2f} but only have ${account.cash:,.2f}.",
                )
            account.cash -= cost
            existing_qty = account.positions.get(product_id, 0)
            now_ts = datetime.now().timestamp()
            existing_ts = account.avg_entry_ts.get(product_id, now_ts)
            account.avg_entry_ts[product_id] = (existing_qty * existing_ts + quantity * now_ts) / (
                existing_qty + quantity
            )
            account.positions[product_id] = existing_qty + quantity
            account.cost_basis[product_id] = account.cost_basis.get(product_id, 0.0) + cost
            account.trade_count += 1
            self._record_trade(agent_id, action, product_id, quantity, price, latency)
            log.debug(
                "account_store.execute_trade BUY OK: agent=%s %s %s @ $%.2f "
                "cost=$%.2f cash_after=$%.2f positions=%s trade_count=%d",
                agent_id, quantity, product_id, price,
                cost, account.cash, dict(account.positions), account.trade_count,
            )
            return TradeResult(
                True,
                f"Bought {quantity} {product_id} @ ${price:,.2f} for ${cost:,.2f}. "
                f"Cash remaining: ${account.cash:,.2f}.",
            )

        # sell
        price = float(entry["best_bid"])
        held = account.positions.get(product_id, 0)
        if quantity > held:
            log.debug(
                "account_store.execute_trade REJECT: insufficient holdings "
                "want=%s held=%s product=%s agent=%s",
                quantity, held, product_id, agent_id,
            )
            return TradeResult(
                False,
                f"Insufficient holdings. Want to sell {quantity} {product_id} "
                f"but only hold {held}.",
            )
        proceeds = price * quantity
        account.cash += proceeds
        # Reduce cost basis proportionally (average cost method)
        avg_cost = account.avg_cost_per_unit(product_id)
        account.cost_basis[product_id] = account.cost_basis.get(product_id, 0.0) - (
            avg_cost * quantity
        )
        new_qty = round(held - quantity, 1)
        if new_qty <= 0:
            del account.positions[product_id]
            del account.cost_basis[product_id]
            account.avg_entry_ts.pop(product_id, None)
        else:
            account.positions[product_id] = new_qty
        account.trade_count += 1
        self._record_trade(agent_id, action, product_id, quantity, price, latency)
        log.debug(
            "account_store.execute_trade SELL OK: agent=%s %s %s @ $%.2f "
            "proceeds=$%.2f cash_after=$%.2f positions=%s trade_count=%d",
            agent_id, quantity, product_id, price,
            proceeds, account.cash, dict(account.positions), account.trade_count,
        )
        return TradeResult(
            True,
            f"Sold {quantity} {product_id} @ ${price:,.2f} for ${proceeds:,.2f}. "
            f"Cash remaining: ${account.cash:,.2f}.",
        )

    def _record_trade(
        self,
        agent_id: str,
        action: str,
        product_id: str,
        quantity: int,
        price: float,
        latency: float | None = None,
    ) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._trade_log.append((ts, agent_id, action, product_id, quantity, price, latency))

        if self._data_recorder is not None:
            account = self._accounts.get(agent_id)
            self._data_recorder.record_trade(
                agent_id=agent_id,
                action=action,
                product_id=product_id,
                quantity=quantity,
                price=price,
                cash_after=account.cash if account else 0.0,
                latency=latency,
            )
