"""CSV Data Recorder — writes periodic portfolio snapshots and every
trade execution to session-timestamped CSV files for post-session
analysis, backtesting research, and agent performance comparison.
"""

from __future__ import annotations

import asyncio
import csv
import logging
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from arena.account_store import AccountStore

logger = logging.getLogger(__name__)


# ── Pydantic row models ─────────────────────────────────────────


class TradeRow(BaseModel):
    timestamp: str
    agent_id: str
    action: str
    product_id: str
    quantity: float
    price: float
    total_value: float
    cash_after: float
    latency: float | None


class SnapshotRow(BaseModel):
    timestamp: str
    agent_id: str
    cash: float
    product_id: str
    quantity: float
    cost_basis: float
    market_price: float
    market_value: float
    unrealized_pnl: float
    portfolio_value: float
    trade_count: int


# ── DataRecorder ─────────────────────────────────────────────────


class DataRecorder:
    """Writes trade and snapshot CSV files for a single session."""

    def __init__(self, data_dir: str = "./data") -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)

        session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        trades_path = self._data_dir / f"trades_{session_ts}.csv"
        snapshots_path = self._data_dir / f"snapshots_{session_ts}.csv"

        self._trades_file = open(trades_path, "w", newline="", buffering=1)
        self._snapshots_file = open(snapshots_path, "w", newline="", buffering=1)

        self._trades_writer = csv.DictWriter(
            self._trades_file, fieldnames=list(TradeRow.model_fields.keys())
        )
        self._snapshots_writer = csv.DictWriter(
            self._snapshots_file, fieldnames=list(SnapshotRow.model_fields.keys())
        )

        self._trades_writer.writeheader()
        self._trades_file.flush()
        self._snapshots_writer.writeheader()
        self._snapshots_file.flush()

        self._snapshot_task: asyncio.Task[None] | None = None

        logger.info("DataRecorder writing to %s", self._data_dir)

    # ── Trade recording ──────────────────────────────────────────

    def record_trade(
        self,
        *,
        agent_id: str,
        action: str,
        product_id: str,
        quantity: float,
        price: float,
        cash_after: float,
        latency: float | None,
    ) -> None:
        row = TradeRow(
            timestamp=datetime.now().isoformat(),
            agent_id=agent_id,
            action=action,
            product_id=product_id,
            quantity=quantity,
            price=price,
            total_value=price * quantity,
            cash_after=cash_after,
            latency=latency,
        )
        self._trades_writer.writerow(row.model_dump())
        self._trades_file.flush()

    # ── Snapshot recording ───────────────────────────────────────

    def take_snapshot(self, store: AccountStore) -> None:
        accounts = store.accounts
        if not accounts:
            return

        ts = datetime.now().isoformat()
        price_book = store.price_book

        for agent_id, account in accounts.items():
            portfolio_val = account.portfolio_value(price_book)

            if not account.positions:
                row = SnapshotRow(
                    timestamp=ts,
                    agent_id=agent_id,
                    cash=account.cash,
                    product_id="",
                    quantity=0.0,
                    cost_basis=0.0,
                    market_price=0.0,
                    market_value=0.0,
                    unrealized_pnl=0.0,
                    portfolio_value=portfolio_val,
                    trade_count=account.trade_count,
                )
                self._snapshots_writer.writerow(row.model_dump())
            else:
                for pid, qty in sorted(account.positions.items()):
                    entry = price_book.get(pid)
                    market_price = float(entry["price"]) if entry else 0.0
                    market_value = market_price * qty
                    cost_basis = account.cost_basis.get(pid, 0.0)
                    unrealized_pnl = market_value - cost_basis

                    row = SnapshotRow(
                        timestamp=ts,
                        agent_id=agent_id,
                        cash=account.cash,
                        product_id=pid,
                        quantity=qty,
                        cost_basis=cost_basis,
                        market_price=market_price,
                        market_value=market_value,
                        unrealized_pnl=unrealized_pnl,
                        portfolio_value=portfolio_val,
                        trade_count=account.trade_count,
                    )
                    self._snapshots_writer.writerow(row.model_dump())

        self._snapshots_file.flush()

    # ── Snapshot loop ────────────────────────────────────────────

    def start_snapshot_loop(
        self, store: AccountStore, interval: float
    ) -> None:
        async def _loop() -> None:
            try:
                while True:
                    await asyncio.sleep(interval)
                    self.take_snapshot(store)
            except asyncio.CancelledError:
                self.take_snapshot(store)
                raise

        self._snapshot_task = asyncio.create_task(_loop())

    # ── Cleanup ──────────────────────────────────────────────────

    async def close(self) -> None:
        if self._snapshot_task is not None:
            self._snapshot_task.cancel()
            try:
                await self._snapshot_task
            except asyncio.CancelledError:
                pass
        self._trades_file.close()
        self._snapshots_file.close()
        logger.info("DataRecorder closed")
