"""Rich Live dashboard for the trading arena."""

from __future__ import annotations

import typing
from collections import deque
from datetime import datetime

import plotext as plt
from rich.ansi import AnsiDecoder
from rich.columns import Columns
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from arena.account_store import AccountStore
from arena.models import INITIAL_CASH

MAX_BALANCE_HISTORY = 300  # ~25 min at 5s intervals

AGENT_COLORS: dict[str, str] = {
    "momentum": "cyan",
    "brainrot-daytrader": "magenta",
    "scalper": "yellow",
}
_FALLBACK_COLORS = ["green", "red", "blue", "orange", "white"]


class PlotextChart:
    """Rich-compatible renderable that draws a plotext line chart."""

    def __init__(
        self,
        balance_history: dict[str, deque[tuple[str, float]]],
        chart_height: int = 12,
    ) -> None:
        self._balance_history = balance_history
        self._chart_height = chart_height

    def __rich_console__(
        self, console: object, options: object
    ) -> typing.Generator[Text, None, None]:
        width = getattr(options, "max_width", 80)

        plt.clf()
        plt.plotsize(width, self._chart_height)
        plt.theme("dark")
        plt.title("Portfolio Value Over Time")
        plt.ylabel("USD")

        has_data = any(len(d) > 0 for d in self._balance_history.values())

        if not has_data:
            plt.plot([0, 1], [INITIAL_CASH, INITIAL_CASH], label="waiting...", color="gray")
        else:
            # Right-align all series so the latest snapshot is always at
            # the right edge, regardless of when each agent started.
            max_len = max(len(h) for h in self._balance_history.values())

            fallback_idx = 0
            for agent_id, history in self._balance_history.items():
                if not history:
                    continue
                timestamps, values = zip(*history)
                n = len(values)
                offset = max_len - n
                x_indices = list(range(offset, offset + n))
                color = AGENT_COLORS.get(agent_id)
                if color is None:
                    color = _FALLBACK_COLORS[fallback_idx % len(_FALLBACK_COLORS)]
                    fallback_idx += 1
                plt.plot(x_indices, list(values), label=agent_id, color=color, marker="braille")

            # Build evenly-spaced time tick labels from the longest series
            longest = max(self._balance_history.values(), key=len)
            n = len(longest)
            num_ticks = min(7, n)
            if num_ticks > 1:
                step = (n - 1) / (num_ticks - 1)
                positions = [int(round(i * step)) for i in range(num_ticks)]
            else:
                positions = [0]
            labels = [longest[p][0] for p in positions]
            plt.xticks(positions, labels)

        canvas = plt.build()
        decoder = AnsiDecoder()
        yield from decoder.decode(canvas)


class PortfolioView:
    """Builds and rerenders a Rich Live dashboard from AccountStore state."""

    def __init__(self, store: AccountStore) -> None:
        self._store = store
        self._live: Live | None = None
        self._balance_history: dict[str, deque[tuple[str, float]]] = {}

    def attach_live(self, live: Live) -> None:
        self._live = live

    def rerender(self) -> None:
        if self._live is not None:
            self._capture_balance_snapshot()
            self._live.update(self._build_layout(), refresh=True)

    def _capture_balance_snapshot(self) -> None:
        price_book = self._store.price_book
        ts = datetime.now().strftime("%H:%M:%S")
        for agent_id, account in self._store.accounts.items():
            if agent_id not in self._balance_history:
                self._balance_history[agent_id] = deque(maxlen=MAX_BALANCE_HISTORY)
            value = account.portfolio_value(price_book)
            self._balance_history[agent_id].append((ts, value))

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="summary_header", size=1),
            Layout(name="summary", size=7),
            Layout(name="body", ratio=2),
            Layout(name="chart", size=15),
        )
        layout["header"].update(self._build_header())
        layout["summary_header"].update(
            Text.from_markup("[bold]Agent Account Summaries[/]", justify="center")
        )
        layout["summary"].update(self._build_summary_cards())
        layout["body"].split_row(
            Layout(name="positions", ratio=3),
            Layout(name="log", ratio=2),
        )
        layout["positions"].update(self._build_positions_table())
        layout["log"].update(self._build_trade_log())
        layout["chart"].update(self._build_chart())
        return layout

    def _build_chart(self) -> Panel:
        chart = PlotextChart(self._balance_history, chart_height=12)
        return Panel(chart, border_style="blue")

    def _build_header(self) -> Panel:
        now = datetime.now().strftime("%H:%M:%S")
        return Panel(
            Text.from_markup(
                "[bold cyan]Portfolio Dashboard[/]  [bold red]●[/] "
                f"[bold green]LIVE[/]  [dim]|  {now}[/]"
            ),
            style="cyan",
            height=3,
        )

    def _build_summary_cards(self) -> Columns:
        accounts = self._store.accounts
        price_book = self._store.price_book

        cards = []
        sorted_accounts = sorted(
            accounts.items(),
            key=lambda item: item[1].portfolio_value(price_book),
            reverse=True,
        )
        for rank, (agent_id, account) in enumerate(sorted_accounts, start=1):
            value = account.portfolio_value(price_book)
            card = Panel(
                Text.from_markup(
                    f"[magenta]Total Value:[/] ${value:,.2f}\n"
                    f"[yellow]Positions:[/] {len(account.positions)}  "
                    f"[cyan]Trades:[/] {account.trade_count}"
                ),
                title=f"[bold]#{rank} {agent_id}[/]",
                border_style="cyan",
            )
            cards.append(card)

        if not cards:
            cards.append(Panel("[dim]No accounts yet[/]", border_style="dim"))

        return Columns(cards, expand=True, equal=True)

    def _build_positions_table(self) -> Panel:
        table = Table(expand=True, show_lines=False)
        table.add_column("Agent", style="bold cyan", ratio=2)
        table.add_column("Trades", justify="right", ratio=1)
        table.add_column("Cash", justify="right", ratio=2)
        table.add_column("Ticker", ratio=2)
        table.add_column("Qty", justify="right", ratio=1)
        table.add_column("Cost Basis", justify="right", ratio=2)
        table.add_column("Mkt Value", justify="right", ratio=2)
        table.add_column("P&L", justify="right", ratio=2)
        table.add_column("Total Value", justify="right", ratio=2)

        accounts = self._store.accounts
        price_book = self._store.price_book
        if not accounts:
            table.add_row("[dim]No accounts yet[/]", "", "", "", "", "", "", "", "")
        else:
            first = True
            for agent_id, account in accounts.items():
                if not first:
                    table.add_section()
                first = False
                total_value = account.portfolio_value(price_book)
                total_pnl = total_value - INITIAL_CASH
                # Agent header row with cash
                table.add_row(
                    agent_id,
                    str(account.trade_count),
                    f"[green]${account.cash:,.2f}[/]",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                )
                # Individual ticker rows
                if not account.positions:
                    table.add_row(
                        "",
                        "",
                        "",
                        "[dim]—[/]",
                        "[dim]—[/]",
                        "[dim]—[/]",
                        "[dim]—[/]",
                        "[dim]—[/]",
                        "",
                    )
                else:
                    for pid, qty in sorted(account.positions.items()):
                        entry = price_book.get(pid)
                        price = float(entry["price"]) if entry else 0.0
                        mkt_val = price * qty
                        cost_basis = account.cost_basis.get(pid, 0.0)
                        pnl = mkt_val - cost_basis
                        pnl_color = "green" if pnl >= 0 else "red"
                        pnl_sign = "+" if pnl >= 0 else ""
                        table.add_row(
                            "",
                            "",
                            "",
                            pid,
                            f"{qty:g}",
                            f"${cost_basis:,.2f}",
                            f"${mkt_val:,.2f}",
                            f"[{pnl_color}]{pnl_sign}${pnl:,.2f}[/]",
                            "",
                        )
                # Total value row
                total_pnl_color = "green" if total_pnl >= 0 else "red"
                total_pnl_sign = "+" if total_pnl >= 0 else ""
                table.add_row(
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "[bold]Total[/]",
                    f"[bold {total_pnl_color}]{total_pnl_sign}${total_pnl:,.2f}[/]",
                    f"[bold]${total_value:,.2f}[/]",
                )

        return Panel(table, title="[bold]Agent Portfolios[/]", border_style="green")

    def _build_trade_log(self) -> Panel:
        table = Table(expand=True, show_lines=False, show_header=True, box=None)
        table.add_column("Time", style="dim", ratio=1)
        table.add_column("Action", ratio=1)
        table.add_column("Qty", justify="right", ratio=1)
        table.add_column("Ticker", ratio=2)
        table.add_column("Unit Price", justify="right", ratio=2)
        table.add_column("Agent", style="dim", ratio=2)
        table.add_column("Latency", justify="right", style="dim", ratio=1)

        log = self._store.trade_log
        if not log:
            table.add_row("[dim italic]No trades yet...[/]", "", "", "", "", "", "")
        else:
            for ts, agent_id, action, product_id, qty, price, latency in reversed(log):
                action_style = "bold green" if action == "buy" else "bold red"
                latency_str = f"{latency:.1f}s" if latency is not None else ""
                table.add_row(
                    ts,
                    f"[{action_style}]{action.upper()}[/]",
                    f"{qty:g}",
                    product_id,
                    f"${price:,.2f}",
                    agent_id,
                    latency_str,
                )

        return Panel(table, title="[bold]Trade Log (most recent)[/]", border_style="yellow")
