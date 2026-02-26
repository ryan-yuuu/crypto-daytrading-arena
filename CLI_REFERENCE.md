# CLI Reference

## start_arena.py / start_arena.sh

Automated startup scripts that launch all components in the correct order.

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--broker-url` | No | `localhost:9092` | Kafka broker address |
| `--cloud-broker` | No | — | Use cloud broker URL (skips local Docker broker) |
| `--api-key` | No | `$OPENAI_API_KEY` | API key for LLM provider |
| `--model-id` | No | `gpt-4o-mini` | Model ID for ChatNode |
| `--reasoning-effort` | No | — | Reasoning level: `low`, `medium`, `high` |
| `--interval` | No | `60` | Market data update interval in seconds |
| `--with-viewer` | No | — | Also start the response viewer |
| `--skip-checks` | No | — | Skip prerequisite checks |
| `--skip-deps` | No | — | Skip dependency installation |
| `--continue-on-error` | No | — | Continue even if components fail |

**Examples:**
```bash
# Basic startup
uv run python start_arena.py

# Use cloud broker with custom model
uv run python start_arena.py --cloud-broker broker.example.com:9092 --model-id gpt-4o

# Fast updates with all features
uv run python start_arena.py --interval 30 --with-viewer --api-key sk-...

# Using bash version
./start_arena.sh --interval 30 --with-viewer
```

---

## deploy_chat_node.py

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--name` | Yes | — | ChatNode name (becomes topic `ai_prompted.<name>`) |
| `--model-id` | Yes | — | Model ID (e.g. `gpt-5-nano`, `deepseek-chat`) |
| `--bootstrap-servers` | Yes | — | Kafka broker address |
| `--base-url` | No | OpenAI | Base URL for OpenAI-compatible providers |
| `--api-key` | No | `$OPENAI_API_KEY` | API key for the provider |
| `--max-workers` | No | `1` | Concurrent inference workers |
| `--reasoning-effort` | No | `None` | For reasoning models (e.g. `"low"`) |

## deploy_router_node.py

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--name` | Yes | — | Agent name (consumer group + identity) |
| `--chat-node-name` | Yes | — | Name of the deployed ChatNode to target |
| `--strategy` | Yes | — | Trading strategy: `default`, `momentum`, `brainrot`, or `scalper` |
| `--bootstrap-servers` | Yes | — | Kafka broker address |
