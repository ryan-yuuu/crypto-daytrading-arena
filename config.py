"""Configuration management for the Trading Arena.

Supports loading from config.json with fallback to defaults.
API keys can be embedded directly or referenced via env vars using ${VAR_NAME} syntax.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

DEFAULT_CONFIG_PATH = Path("config.json")

# Default coin pairs
DEFAULT_BINANCE_SYMBOLS = ["BTCUSDT", "FARTCOINUSDT", "SOLUSDT"]
DEFAULT_COINBASE_PRODUCTS = ["BTC-USD", "FARTCOIN-USD", "SOL-USD"]

# Provider default endpoints
PROVIDER_DEFAULTS = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-3-sonnet-20240229",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "anthropic/claude-3.5-sonnet",
    },
}


class LLMProviderConfig(BaseModel):
    """Configuration for an LLM provider."""

    api_key: str = ""
    base_url: str = ""
    default_model: str = ""

    @field_validator("api_key")
    @classmethod
    def resolve_api_key(cls, v: str) -> str:
        """Resolve API key from env var if it uses ${VAR_NAME} syntax."""
        if not v:
            return v
        match = re.match(r'^\$\{([^}]+)\}$', v)
        if match:
            env_var = match.group(1)
            env_value = os.getenv(env_var, "")
            if not env_value:
                raise ValueError(f"Environment variable {env_var} is not set")
            return env_value
        return v


class ChatNodeConfig(BaseModel):
    """Configuration for a ChatNode deployment."""

    name: str
    provider: str
    model: str
    max_workers: int = 1
    reasoning_effort: str | None = None


class TradingConfig(BaseModel):
    """Trading-related configuration."""

    binance_symbols: list[str] = Field(default_factory=lambda: DEFAULT_BINANCE_SYMBOLS.copy())
    coinbase_products: list[str] = Field(default_factory=lambda: DEFAULT_COINBASE_PRODUCTS.copy())


class ArenaConfig(BaseModel):
    """Root configuration for the Trading Arena."""

    llm_providers: dict[str, LLMProviderConfig] = Field(default_factory=dict)
    chat_nodes: list[ChatNodeConfig] = Field(default_factory=list)
    trading: TradingConfig = Field(default_factory=TradingConfig)

    def get_provider_config(self, provider_name: str) -> LLMProviderConfig | None:
        """Get configuration for a specific provider."""
        return self.llm_providers.get(provider_name)

    def get_chat_node_config(self, name: str) -> ChatNodeConfig | None:
        """Get configuration for a specific chat node."""
        for node in self.chat_nodes:
            if node.name == name:
                return node
        return None


def resolve_env_vars(value: Any, path: str = "root") -> Any:
    """Recursively resolve environment variables in a value.

    Args:
        value: The value to resolve env vars in.
        path: Current path for error reporting (e.g., "llm_providers.openai.api_key").

    Returns:
        The value with env vars resolved.

    Raises:
        ValueError: If a referenced environment variable is not set.
    """
    if isinstance(value, str):
        match = re.match(r'^\$\{([^}]+)\}$', value)
        if match:
            env_var = match.group(1)
            env_value = os.getenv(env_var)
            if env_value is None:
                raise ValueError(
                    f"Environment variable '{env_var}' is not set (referenced at {path})"
                )
            return env_value
        return value
    elif isinstance(value, dict):
        return {k: resolve_env_vars(v, f"{path}.{k}") for k, v in value.items()}
    elif isinstance(value, list):
        return [resolve_env_vars(item, f"{path}[{i}]") for i, item in enumerate(value)]
    return value

def load_config(config_path: Path | str | None = None) -> ArenaConfig:
    """Load configuration from a file.

    Args:
        config_path: Path to config file. Defaults to config.json in project root.

    Returns:
        ArenaConfig with loaded values or defaults if file doesn't exist.
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    else:
        config_path = Path(config_path)

    if not config_path.exists():
        # Return default configuration
        return ArenaConfig(
            trading=TradingConfig(
                binance_symbols=DEFAULT_BINANCE_SYMBOLS.copy(),
                coinbase_products=DEFAULT_COINBASE_PRODUCTS.copy(),
            )
        )

    with open(config_path, "r") as f:
        data = json.load(f)

    # Resolve environment variables before validation
    data = resolve_env_vars(data)

    return ArenaConfig.model_validate(data)


def get_default_symbols(exchange: str = "binance") -> list[str]:
    """Get default symbols for an exchange.

    Args:
        exchange: Either 'binance' or 'coinbase'.

    Returns:
        List of default symbols/products for the exchange.
    """
    config = load_config()
    if exchange.lower() == "binance":
        return config.trading.binance_symbols
    elif exchange.lower() == "coinbase":
        return config.trading.coinbase_products
    else:
        raise ValueError(f"Unknown exchange: {exchange}")
