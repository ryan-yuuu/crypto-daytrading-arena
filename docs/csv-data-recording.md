# CSV Data Recording

The data recorder writes two CSV files per session into the `data/` directory (configurable via `--data-dir`). Files are named with the session start timestamp:

```
data/
  trades_20260226_143052.csv
  snapshots_20260226_143052.csv
```

## Enabling / Disabling

Recording is enabled by default with a 10-minute snapshot interval. Configure via CLI flags:

```bash
# Custom snapshot interval (seconds) and output directory
uv run python -m deploy.tools_and_dashboard --bootstrap-servers localhost:9092 \
  --snapshot-interval 60 --data-dir ./data

# Disable recording entirely
uv run python -m deploy.tools_and_dashboard --bootstrap-servers localhost:9092 \
  --snapshot-interval 0
```

When disabled (`--snapshot-interval 0`), no files or directories are created.

---

## Trades CSV

One row is written per executed trade, immediately on execution.

| Column | Type | Description |
|--------|------|-------------|
| `timestamp` | ISO 8601 string | When the trade executed (e.g. `2026-02-26T14:30:52.123456`) |
| `agent_id` | string | Name of the agent that placed the trade |
| `action` | string | `buy` or `sell` |
| `product_id` | string | Trading pair (e.g. `BTC-USD`, `SOL-USD`) |
| `quantity` | float | Number of units traded |
| `price` | float | Execution price per unit (best ask for buys, best bid for sells) |
| `total_value` | float | `price * quantity` — total dollar value of the trade |
| `cash_after` | float | Agent's cash balance after the trade settled |
| `latency` | float or empty | Seconds between tool invocation and trade execution. Empty if not measured |

### Example

```csv
timestamp,agent_id,action,product_id,quantity,price,total_value,cash_after,latency
2026-02-26T14:30:52.123456,momentum,buy,BTC-USD,0.5,64200.00,32100.00,67900.00,1.2
2026-02-26T14:31:10.654321,scalper,sell,SOL-USD,10.0,142.50,1425.00,101425.00,0.8
```

---

## Snapshots CSV

Periodic portfolio snapshots taken at the configured interval. The data is **denormalized** — one row per agent-holding pair per snapshot.

| Column | Type | Description |
|--------|------|-------------|
| `timestamp` | ISO 8601 string | When the snapshot was taken |
| `agent_id` | string | Name of the agent |
| `cash` | float | Agent's current cash balance |
| `product_id` | string | Held asset (e.g. `BTC-USD`). Empty string if the agent has no positions |
| `quantity` | float | Units held of this asset. `0.0` if no positions |
| `cost_basis` | float | Total cost paid for this position (average cost method) |
| `market_price` | float | Current market price per unit from the live price book |
| `market_value` | float | `market_price * quantity` — current dollar value of the position |
| `unrealized_pnl` | float | `market_value - cost_basis` — unrealized profit/loss on this position |
| `portfolio_value` | float | Agent's total portfolio value (cash + all positions at market). Repeated on every row for the agent |
| `trade_count` | int | Total number of trades the agent has executed to date |

### Denormalization

An agent holding 3 assets produces 3 rows per snapshot (one per asset), all sharing the same `timestamp`, `agent_id`, `cash`, `portfolio_value`, and `trade_count`. An agent with no positions produces 1 row with `product_id=""` and zero-valued position fields — this preserves the cash and portfolio value in the record.

### Example

```csv
timestamp,agent_id,cash,product_id,quantity,cost_basis,market_price,market_value,unrealized_pnl,portfolio_value,trade_count
2026-02-26T14:35:00.000000,momentum,67900.00,BTC-USD,0.5,32100.00,64500.00,32250.00,150.00,100150.00,1
2026-02-26T14:35:00.000000,scalper,101425.00,,0.0,0.0,0.0,0.0,0.0,101425.00,1
```

---

## Durability

- Files are opened line-buffered and explicitly flushed after each trade write and each snapshot batch
- On graceful shutdown (Ctrl+C), a final snapshot is written before files are closed
- At most one incomplete CSV line can be lost in a hard crash
