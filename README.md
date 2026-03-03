# The Agents Trading Arena 🤖 🤺

A multi-agent crypto trading arena where AI agents compete against each other, trading with live crypto market data from Coinbase or Binance. Each agent consumes a livestream of ticker data and standard candlestick charts, has access to its portfolio and calculator, and executes trades autonomously. This is all built with [Calfkit](https://github.com/calf-ai/calfkit-sdk) agents, namely for their multi-agent orchestration and realtime data streaming functionality.

<br>

<p align="center">
  <img src="assets/demo.gif" alt="Arena Demo">
</p>

<br>

## Architecture

```
                         ┌──────────────────┐
                         │ Agent Router(s)  │
                         └──────────────────┘
                                  ▲
                                  │
                                  ▼
Live Market          ┌────────────────┐      ┌──────────────────┐
Data Stream  ──▶     │  Kafka Broker  │◀────▶│  ChatNode(s)     │
                     └────────────────┘      │  (LLM Inference) │
                                  ▲          └──────────────────┘
                                  │
                                  ▼
                       ┌────────────────────────┐
                       │ Tools & Dashboard      │
                       │ (Trading Tools + UI)   │
                       └────────────────────────┘
```

Each box (or node) is an independent process communicating with eachother. Each node can run on the same machine, on separate servers, or across different cloud regions.

Key design points:
- **Per-agent model selection**: Each agent targets a named stateless ChatNode, so different agents can use different LLMs or share LLMs.
- **Fan-out via consumer groups**: Every agent independently receives every market data update, with no replicated work.
- **Shared tools via ToolContext**: A single deployed set of trading tools serves all agents — each tool resolves the calling agent's identity at runtime.
- **Dynamic agent accounts**: Agents appear on the dashboard automatically on their first trade — no pre-registration needed.

<br>

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) — fast Python package manager
- Docker installed and running (in order to run a kafka broker)
- An API key (and optionally base url) for your LLM provider

<br>

### 1. Install uv

If you don't have `uv` installed:

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# Or via Homebrew
brew install uv
```

After installation, restart your terminal.

<br>

### 2. Install the Calfkit SDK

```bash
uv add calfkit@latest
# Or, python -m venv .venv && source .venv/bin/activate && pip install --upgrade calfkit
```

[Calfkit](https://github.com/calf-ai/calfkit-sdk) is the event-stream SDK that powers this project. It handles the agent realtime stream consumption and orechestration.

<br>

### 3. Start the Broker

The broker orchestrates all nodes and enables realtime data streaming between all components.

<details>
<summary><strong>Option A: Local broker setup (Docker required)</strong></summary>

Run the following to clone the [calfkit-broker](https://github.com/calf-ai/calfkit-broker) repo and start a local Kafka broker container:

```bash
git clone https://github.com/calf-ai/calfkit-broker && cd calfkit-broker && make dev-up
```

Once the broker is ready, open a new terminal tab to continue with the quickstart. The default broker address is `localhost:9092`.

</details>

<details>
<summary><strong>Option B: Calfkit cloud broker</strong></summary>

There's also a [cloud broker](https://github.com/calf-ai/calfkit-sdk?tab=readme-ov-file#2-start-a-calfkit-broker) version so you can simply use the cloud broker URL (which would be provided to you) to deploy your agents instead of setting up and maintaining a broker locally.

</details>

<br>

## Quickstart (Automated)

The easiest way to start is using the provided startup script:

```bash
uv run python start_arena.py
```

This will:
1. Check prerequisites (Docker, Python, uv)
2. Start the Kafka broker (auto-clones if needed)
3. Install dependencies
4. Launch all components in order
5. Start 3 default trading agents (momentum, brainrot, scalper)

**Options:**
```bash
# Use Binance instead of Coinbase
uv run python start_arena.py --exchange binance

# Use cloud broker instead of local
uv run python start_arena.py --cloud-broker <broker-url>

# Faster market data updates (30 seconds)
uv run python start_arena.py --interval 30

# Include response viewer
uv run python start_arena.py --with-viewer

# Specify API key and model
uv run python start_arena.py --api-key sk-... --model-id gpt-4o
```

Press `Ctrl+C` to gracefully stop all components.

<br>

---

## Quickstart (Manual)

If you prefer to start components manually:

Install dependencies:

```bash
uv sync
# Or, pip install -r requirements.txt
```

Then launch each component in its own terminal. All components will access the same broker.

<br>

### 1. Start the exchange connector

Start either the Coinbase or Binance connector to stream live market data:

```bash
# Coinbase (default)
uv run python coinbase_kafka_connector.py --bootstrap-servers <broker-url>

# Binance
uv run python binance_kafka_connector.py --bootstrap-servers <broker-url>
```

Optional: You can use the `--min-interval <seconds>` flag which controls how often agents are fed market data (default: 60s). Note that candle data is only updated every 60 seconds due to Coinbase API restrictions, so intervals below a minute mean agents will receive updated live pricing (bid/ask spread, ~5s granularity) but the same candle data.

<br>

### 2. Deploy tools & dashboard

```bash
uv run python tools_and_dashboard.py --bootstrap-servers <broker-url>
# Or, source .venv/bin/activate && python tools_and_dashboard.py --bootstrap-servers <broker-url>
```

<br>

### 3. Deploy a ChatNode (LLM inference)

Deploy a ChatNode for each LLM model you'd like to run.
Note: ChatNodes are stateless so multiple agents can share the same ChatNode.

```bash
# OpenAI model
uv run python deploy_chat_node.py \
    --name <unique-name-of-chatnode> --model-id <openai-model-id> --bootstrap-servers <broker-url> \
    --reasoning-effort <optional-reasoning-level> --api-key <api-key>

# Or, OpenAI-compatible provider (e.g. DeepInfra, Gemini, etc.)
uv run python deploy_chat_node.py \
    --name <unique-name-of-chatnode> --model-id <model-id> --bootstrap-servers <broker-url> \
    --base-url <llm-provider-base-url> --reasoning-effort <optional-reasoning-level> --api-key <api-key>

# Or, source .venv/bin/activate && python deploy_chat_node.py \
#     --name <unique-name-of-chatnode> --model-id <model-id> --bootstrap-servers <broker-url> \
#     --api-key <api-key>
```

<br>

### 4. Deploy agent routers

Deploy one router per agent. Each targets a ChatNode you define by name and uses a trading strategy you can edit in `deploy_router_node.py`. See `deploy_router_node.py` for the full system prompts.

```bash
uv run python deploy_router_node.py \
    --name <unique-agent-name> --chat-node-name <name-of-chatnode> \
    --strategy <strategy> --bootstrap-servers <broker-url>

# Or, source .venv/bin/activate && python deploy_router_node.py \
#     --name <unique-agent-name> --chat-node-name <name-of-chatnode> \
#     --strategy <strategy> --bootstrap-servers <broker-url>
```

Once agent routers are deployed, market data flows to the agents and trades should hydrate the dashboard soon.

<br>

### 5. (Optional) Start the response viewer

A live dashboard that shows all agent activity, such as tool calls, text responses (agent reasoning), and tool results, as they happen.

```bash
uv run python response_viewer.py --bootstrap-servers <broker-url>
# Or, source .venv/bin/activate && python response_viewer.py --bootstrap-servers <broker-url>
```

<br>

## Data Recording

All trades and periodic portfolio snapshots are automatically saved to CSV files in the `data/` directory. Each session produces two files:

- **`trades_<timestamp>.csv`** — every executed trade with price, quantity, and agent cash after settlement
- **`snapshots_<timestamp>.csv`** — periodic portfolio state per agent, including positions, market values, and unrealized P&L

You can configure the snapshot interval and output directory:

```bash
uv run python tools_and_dashboard.py \
    --bootstrap-servers <broker-url> \
    --snapshot-interval <default-600-seconds> \
    --data-dir ./data
```

To disable recording entirely, pass `--snapshot-interval 0`.

For full column descriptions and examples, see [docs/csv-data-recording.md](docs/csv-data-recording.md).

<br>

## CLI Reference

For full CLI flags and options, see [CLI_REFERENCE.md](docs/CLI_REFERENCE.md).

<br>

## Available Agent Tools

| Tool | Description |
|------|-------------|
| `execute_trade` | Buy or sell a crypto product at the current market price |
| `get_portfolio` | View cash, open positions, cost basis, P&L, and average time held |
| `calculator` | Evaluate math expressions for position sizing, P&L calculations, etc. |

<br>

## Deployment Configurations

| File | Constant | Default | Description |
|------|----------|---------|-------------|
| `trading_tools.py` | `INITIAL_CASH` | `100_000.0` | Starting cash balance per agent |
| `coinbase_kafka_connector.py` | `DEFAULT_PRODUCTS` | 3 products | Coinbase products tracked by the price feed |
| `binance_kafka_connector.py` | `DEFAULT_SYMBOLS` | 3 symbols | Binance symbols tracked by the price feed |
