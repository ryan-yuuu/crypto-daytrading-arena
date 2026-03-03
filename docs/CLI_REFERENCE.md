# CLI Reference

## Configuration File

The trading arena supports a JSON configuration file (`config.json` by default) for managing:
- Multiple LLM providers (OpenAI, OpenRouter)
- Multiple ChatNodes with different providers/models
- Exchange selection and trading pairs for Binance and Coinbase

To get started, copy the example and fill in your API keys:
```bash
cp config.example.json config.json
```

> **Note:** `config.json` is gitignored to prevent accidental secret commits.

For the full config schema with all fields, types, and defaults, see [`config.schema.json`](../config.schema.json). IDEs that support JSON Schema (VS Code, JetBrains) will provide autocompletion and validation automatically via the `$schema` reference in the config file.

**Example config.json:**
```json
{
  "$schema": "./config.schema.json",
  "llm_providers": {
    "openai": {
      "api_key": "${OPENAI_API_KEY}",
      "base_url": "https://api.openai.com/v1",
      "default_model": "gpt-5-nano"
    },
    "openrouter": {
      "api_key": "${OPENROUTER_API_KEY}",
      "base_url": "https://openrouter.ai/api/v1",
      "default_model": "anthropic/claude-sonnet-4"
    }
  },
  "chat_nodes": [
    {
      "name": "gpt-5-nano",
      "provider": "openai",
      "model": "gpt-5-nano",
      "max_workers": 1
    },
    {
      "name": "claude",
      "provider": "openrouter",
      "model": "anthropic/claude-sonnet-4",
      "max_workers": 1
    }
  ],
  "trading": {
    "exchange": "coinbase",
    "binance_symbols": ["BTCUSDT", "SOLUSDT", "FARTCOINUSDT"],
    "coinbase_products": ["BTC-USD", "SOL-USD", "FARTCOIN-USD"]
  }
}
```

> **Note:** The Anthropic API is not OpenAI-compatible. To use Claude models, configure them via the `openrouter` provider or another OpenAI-compatible proxy.

**API Key Formats:**
- Environment variable: `"${OPENAI_API_KEY}"` - Reads from env var at runtime
- Embedded key: `"sk-..."` - Key embedded directly in config (less secure)

---

## deploy/chat_node.py

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
uv run python deploy/chat_node.py \
    --name gpt-5-nano --model-id gpt-5-nano \
    --bootstrap-servers localhost:9092 \
    --api-key $OPENAI_API_KEY

# Load from config file
uv run python deploy/chat_node.py \
    --from-config gpt-5-nano \
    --bootstrap-servers localhost:9092

# Using OpenRouter (for Claude and other non-OpenAI models)
uv run python deploy/chat_node.py \
    --name claude --model-id anthropic/claude-sonnet-4 \
    --base-url https://openrouter.ai/api/v1 \
    --api-key $OPENROUTER_API_KEY \
    --bootstrap-servers localhost:9092
```

## deploy/router_node.py

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--name` | Yes | — | Agent name (consumer group + identity) |
| `--chat-node-name` | Yes | — | Name of the deployed ChatNode to target |
| `--strategy` | Yes | — | Trading strategy: `default`, `momentum`, `brainrot`, or `scalper` |
| `--bootstrap-servers` | Yes | — | Kafka broker address |

---

## exchanges/binance.py

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
uv run python exchanges/binance.py --bootstrap-servers localhost:9092

# Override with specific symbols
uv run python exchanges/binance.py \
    --bootstrap-servers localhost:9092 \
    --symbols BTCUSDT ETHUSDT SOLUSDT
```

---

## exchanges/coinbase.py

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
uv run python exchanges/coinbase.py --bootstrap-servers localhost:9092

# Override with specific products
uv run python exchanges/coinbase.py \
    --bootstrap-servers localhost:9092 \
    --products BTC-USD ETH-USD SOL-USD
```
