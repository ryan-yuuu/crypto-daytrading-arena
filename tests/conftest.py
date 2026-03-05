import logging


def pytest_configure(config):
    """Show debug logs only from arena.* loggers; silence noisy third-party loggers."""
    for name in ("openai", "httpcore", "httpx", "asyncio", "aiokafka", "faststream"):
        logging.getLogger(name).setLevel(logging.WARNING)
