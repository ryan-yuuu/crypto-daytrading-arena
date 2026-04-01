"""Deploy a single named Agent for the daytrading arena.

Each agent subscribes to the shared ``agent_router.input`` topic with its
own consumer group, so every agent receives every market tick independently.
The model client is embedded directly in the Agent (no separate ChatNode).

Example:
    uv run python -m deploy.router_node \
        --name momentum --strategy momentum \
        --model-id gpt-5-nano --bootstrap-servers <broker-url>

    uv run python -m deploy.router_node \
        --name brainrot-daytrader --strategy brainrot \
        --model-id deepseek-chat --base-url https://api.deepseek.com/v1 \
        --api-key $DEEPSEEK_API_KEY --bootstrap-servers <broker-url>

    uv run python -m deploy.router_node \
        --from-config gpt-5-nano --strategy default \
        --bootstrap-servers <broker-url>
"""

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv

from calfkit import Agent, Client, OpenAIModelClient, Worker
from arena.tools import calculator, execute_trade, get_portfolio
from arena.strategies import STRATEGIES
from config import load_config, PROVIDER_DEFAULTS

load_dotenv()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deploy a named Agent for the daytrading arena.",
    )
    parser.add_argument(
        "--name",
        help="Agent name (used as consumer group + identity)",
    )
    parser.add_argument(
        "--strategy",
        required=True,
        choices=list(STRATEGIES.keys()),
        help="Trading strategy (selects system prompt)",
    )
    parser.add_argument(
        "--model-id",
        help="Model ID passed to OpenAIModelClient (e.g. gpt-5-nano, deepseek-chat)",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Base URL for OpenAI-compatible providers (default: OpenAI)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key for the provider (default: $OPENAI_API_KEY)",
    )
    parser.add_argument(
        "--reasoning-effort",
        default=None,
        help='Reasoning effort for reasoning models (e.g. "low")',
    )
    parser.add_argument(
        "--bootstrap-servers",
        required=True,
        help="Kafka bootstrap servers address",
    )
    parser.add_argument(
        "--from-config",
        metavar="AGENT_NAME",
        dest="from_config",
        help="Load agent configuration from config.json by name",
    )
    parser.add_argument(
        "--config-path",
        default="config.json",
        help="Path to config file (default: config.json)",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    # Handle config-based deployment
    if args.from_config:
        config = load_config(args.config_path)
        agent_config = config.get_agent_config(args.from_config)
        if not agent_config:
            print(f"ERROR: Agent '{args.from_config}' not found in config.")
            print(f"Available agents: {[a.name for a in config.agents]}")
            sys.exit(1)

        # Set values from config
        args.name = agent_config.name
        args.model_id = agent_config.model
        args.reasoning_effort = agent_config.reasoning_effort

        # Get provider config
        provider_config = config.get_provider_config(agent_config.provider)
        if provider_config:
            args.api_key = provider_config.api_key or args.api_key
            args.base_url = provider_config.base_url or args.base_url
            if not args.model_id and provider_config.default_model:
                args.model_id = provider_config.default_model
        else:
            print(f"WARNING: Provider '{agent_config.provider}' not configured. Using CLI/env values.")

    # Validate required args
    if not args.name:
        print("ERROR: --name is required (or use --from-config)")
        sys.exit(1)
    if not args.model_id:
        print("ERROR: --model-id is required (or use --from-config with configured model)")
        sys.exit(1)

    system_prompt = STRATEGIES.get(args.strategy)
    if system_prompt is None:
        print(f"ERROR: Unknown strategy '{args.strategy}'")
        print(f"Available: {', '.join(STRATEGIES.keys())}")
        sys.exit(1)

    # Resolve API key: explicit flag > config > env var
    api_key = args.api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: No API key provided.")
        print("Pass --api-key, set in config, or set OPENAI_API_KEY.")
        sys.exit(1)

    print("=" * 50)
    print(f"Agent Deployment: {args.name}")
    print("=" * 50)

    print(f"\nConnecting to Kafka broker at {args.bootstrap_servers}...")
    client = Client.connect(args.bootstrap_servers)

    tools = [execute_trade, get_portfolio, calculator]
    agent = Agent(
        args.name,
        system_prompt=system_prompt,
        subscribe_topics="agent_router.input",
        model_client=OpenAIModelClient(
            model_name=args.model_id,
            base_url=args.base_url,
            api_key=api_key,
            reasoning_effort=args.reasoning_effort,
        ),
        tools=tools,
    )
    worker = Worker(client, nodes=[agent], group_id=args.name)

    tool_names = ", ".join(t.tool_schema.name for t in tools)
    print(f"  - Agent:    {args.name}")
    print(f"  - Strategy: {args.strategy}")
    print(f"  - Model:    {args.model_id}")
    print(f"  - Input:    agent_router.input")
    print(f"  - Tools:    {tool_names}")
    if args.base_url:
        print(f"  - Base URL: {args.base_url}")
    if args.reasoning_effort:
        print(f"  - Reasoning effort: {args.reasoning_effort}")

    print("\nAgent ready. Waiting for requests...")
    await worker.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nAgent stopped.")
