# CLI Reference

## Configuration File

The trading arena supports a JSON configuration file (`config.json` by default) for managing:
- Multiple LLM providers (OpenAI, Anthropic, OpenRouter)
- Multiple ChatNodes with different providers/models
- Trading pairs for Binance and Coinbase

**Example config.json:**
```json
{
  "llm_providers": {
    "openai": {
      "api_key": "${OPENAI_API_KEY}",
      "base_url": "https://api.openai.com/v1",
      "default_model": "gpt-4o-mini"
    },
    "anthropic": {
      "api_key": "${ANTHROPIC_API_KEY}",
      "base_url": "https://api.anthropic.com/v1",
      "default_model": "claude-3-sonnet-20240229"
    }
  },
  "chat_nodes": [
    {
      "name": "gpt4o",
      "provider": "openai",
      "model": "gpt-4o",
      "max_workers": 1
    },
    {
      "name": "claude",
      "provider": "anthropic",
      "model": "claude-3-opus-20240229",
      "max_workers": 1
    }
  ],
  "trading": {
    "binance_symbols": ["BTCUSDT", "SOLUSDT", "FARTCOINUSDT"],
    "coinbase_products": ["BTC-USD", "SOL-USD", "FARTCOIN-USD"]
  }
}
```

**API Key Formats:**
- Environment variable: `"${OPENAI_API_KEY}"` - Reads from env var at runtime
- Embedded key: `"sk-..."` - Key embedded directly in config (less secure)

---

## start_arena.py / start_arena.sh

Automated startup scripts that launch all components in the correct order.

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--broker-url` | No | `localhost:9092` | Kafka broker address |
| `--cloud-broker` | No | — | Use cloud broker URL (skips local Docker broker) |
| `--config` | No | `config.json` | Path to configuration file |
| `--api-key` | No | `$OPENAI_API_KEY` | API key for LLM provider (legacy mode) |
| `--model-id` | No | `gpt-4o-mini` | Model ID for ChatNode (legacy mode) |
| `--reasoning-effort` | No | — | Reasoning level: `low`, `medium`, `high` |
| `--interval` | No | `60` | Market data update interval in seconds |
| `--with-viewer` | No | — | Also start the response viewer |
| `--skip-checks` | No | — | Skip prerequisite checks |
| `--skip-deps` | No | — | Skip dependency installation |
| `--continue-on-error` | No | — | Continue even if components fail |

**Examples:**
```bash
# Basic startup with config file
uv run python start_arena.py

# Use custom config file
uv run python start_arena.py --config my-config.json

# Use cloud broker with custom model
uv run python start_arena.py --cloud-broker broker.example.com:9092

# Fast updates with all features
uv run python start_arena.py --interval 30 --with-viewer

# Using bash version
./start_arena.sh --interval 30 --with-viewer
```

---

## deploy_chat_node.py

Deploy a ChatNode for LLM inference. Can use explicit CLI args or load from config.

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--name` | Yes* | — | ChatNode name (becomes topic `ai_prompted.<name>`) |
| `--model-id` | Yes* | — | Model ID (e.g. `gpt-5-nano`, `deepseek-chat`) |
| `--bootstrap-servers` | Yes | — | Kafka broker address |
| `--base-url` | No | OpenAI | Base URL for OpenAI-compatible providers |
| `--api-key` | No | `$OPENAI_API_KEY` | API key for the provider |
| `--max-workers` | No | `1` | Concurrent inference workers |
| `--reasoning-effort` | No | `None` | For reasoning models (e.g. `"low"`) |
| `--from-config` | No | — | Load ChatNode config by name from config file |
| `--config-path` | No | `config.json` | Path to config file |

\* Required unless using `--from-config`

**Examples:**
```bash
# Explicit configuration
uv run python deploy_chat_node.py \
    --name gpt4o --model-id gpt-4o \
    --bootstrap-servers localhost:9092 \
    --api-key $OPENAI_API_KEY

# Load from config file
uv run python deploy_chat_node.py \
    --from-config gpt4o \
    --bootstrap-servers localhost:9092

# Using OpenRouter
uv run python deploy_chat_node.py \
    --name claude --model-id anthropic/claude-3.5-sonnet \
    --base-url https://openrouter.ai/api/v1 \
    --api-key $OPENROUTER_API_KEY \
    --bootstrap-servers localhost:9092
```

## deploy_router_node.py

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--name` | Yes | — | Agent name (consumer group + identity) |
| `--chat-node-name` | Yes | — | Name of the deployed ChatNode to target |
| `--strategy` | Yes | — | Trading strategy: `default`, `momentum`, `brainrot`, or `scalper` |
| `--bootstrap-servers` | Yes | — | Kafka broker address |

---

## binance_kafka_connector.py

Stream real-time market data from Binance to Kafka.

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--bootstrap-servers` | No | `localhost:9092` | Kafka broker address |
| `--config` | No | `config.json` | Path to config file for symbols |
| `--symbols` | No | From config | Binance symbols to subscribe (overrides config) |
| `--min-interval` | No | `0` | Minimum seconds between publishes |
| `--log-level` | No | `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

**Examples:**
```bash
# Use symbols from config file
uv run python binance_kafka_connector.py --bootstrap-servers localhost:9092

# Override with specific symbols
uv run python binance_kafka_connector.py \
    --bootstrap-servers localhost:9092 \
    --symbols BTCUSDT ETHUSDT SOLUSDT
```

---

## coinbase_kafka_connector.py

Stream real-time market data from Coinbase to Kafka.

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--bootstrap-servers` | No | `localhost:9092` | Kafka broker address |
| `--config` | No | `config.json` | Path to config file for products |
| `--products` | No | From config | Coinbase products to subscribe (overrides config) |
| `--min-interval` | No | `0` | Minimum seconds between publishes |
| `--log-level` | No | `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

**Examples:**
```bash
# Use products from config file
uv run python coinbase_kafka_connector.py --bootstrap-servers localhost:9092

# Override with specific products
uv run python coinbase_kafka_connector.py \
    --bootstrap-servers localhost:9092 \
    --products BTC-USD ETH-USD SOL-USD
```
