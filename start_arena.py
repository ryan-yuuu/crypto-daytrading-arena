#!/usr/bin/env python3
"""
Trading Arena Startup Script

Automatically starts all components of the Agents Trading Arena in the correct order:
1. Prerequisites check (Docker, Python, uv)
2. Kafka broker (auto-clones calfkit-broker if needed)
3. Dependencies installation (uv sync)
4. Coinbase connector (market data)
5. Tools & dashboard (trading engine + UI)
6. ChatNode (LLM inference)
7. Agent routers (3 default strategies: momentum, brainrot, scalper)
8. Optional: Response viewer

Usage:
    uv run python start_arena.py
    uv run python start_arena.py --broker-url localhost:9092
    uv run python start_arena.py --cloud-broker <cloud-url>
    uv run python start_arena.py --with-viewer

Environment Variables:
    OPENAI_API_KEY - Required for ChatNode (or use --api-key)
    KAFKA_BOOTSTRAP_SERVERS - Broker address (default: localhost:9092)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from config import load_config, ArenaConfig, ChatNodeConfig

# ANSI colors for terminal output
COLORS = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "red": "\033[91m",
    "green": "\033[92m",
    "yellow": "\033[93m",
    "blue": "\033[94m",
    "magenta": "\033[95m",
    "cyan": "\033[96m",
}


def log(message: str, level: str = "info") -> None:
    """Print colored log messages."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    color_map = {
        "info": COLORS["cyan"],
        "success": COLORS["green"],
        "warning": COLORS["yellow"],
        "error": COLORS["red"],
        "header": COLORS["magenta"] + COLORS["bold"],
        "debug": COLORS["blue"],
    }
    color = color_map.get(level, COLORS["reset"])
    prefix = f"[{timestamp}]"
    if level == "header":
        print(f"\n{color}{'='*60}{COLORS['reset']}")
        print(f"{color}{message}{COLORS['reset']}")
        print(f"{color}{'='*60}{COLORS['reset']}")
    else:
        print(f"{COLORS['dim']}{prefix}{COLORS['reset']} {color}{message}{COLORS['reset']}")


# Add dim to colors
def _init():
    COLORS["dim"] = "\033[90m"
_init()


@dataclass
class ComponentStatus:
    """Track status of a running component."""
    name: str
    process: Optional[subprocess.Popen] = None
    pid: Optional[int] = None
    log_file: Optional[Path] = None
    status: str = "pending"  # pending, starting, running, failed, stopped
    start_time: Optional[float] = None
    error_message: Optional[str] = None


class ArenaStartupManager:
    """Manages the startup and lifecycle of all trading arena components."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.broker_url = args.broker_url or os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
        self.api_key = args.api_key or os.getenv("OPENAI_API_KEY", "")
        self.components: Dict[str, ComponentStatus] = {}
        self.logs_dir = Path("logs")
        self.broker_dir = Path("../calfkit-broker")
        self.running = True
        self.config: Optional[ArenaConfig] = None
        self._load_config()
        self._setup_signal_handlers()

    def _load_config(self) -> None:
        """Load configuration from file."""
        try:
            self.config = load_config(self.args.config)
            log(f"Loaded config from {self.args.config}", "success")
        except Exception as e:
            log(f"Warning: Could not load config: {e}", "warning")
            self.config = None

    def _setup_signal_handlers(self) -> None:
        """Setup handlers for graceful shutdown."""
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame) -> None:
        """Handle shutdown signals gracefully."""
        signal_name = "SIGINT" if signum == signal.SIGINT else "SIGTERM"
        log(f"\nReceived {signal_name}, shutting down gracefully...", "warning")
        self.running = False
        self.shutdown()
        sys.exit(0)

    def _check_command(self, cmd: List[str]) -> bool:
        """Check if a command exists and is executable."""
        try:
            subprocess.run(
                cmd,
                capture_output=True,
                check=True,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def check_prerequisites(self) -> bool:
        """Check all prerequisites are met."""
        log("Checking Prerequisites", "header")

        all_good = True

        # Check Python version
        py_version = sys.version_info
        if py_version < (3, 10):
            log(f"Python 3.10+ required, found {py_version.major}.{py_version.minor}", "error")
            all_good = False
        else:
            log(f"Python {py_version.major}.{py_version.minor}.{py_version.micro} ", "success")

        # Check uv
        if self._check_command(["uv", "--version"]):
            result = subprocess.run(["uv", "--version"], capture_output=True, text=True)
            log(f"uv found: {result.stdout.strip()}", "success")
        else:
            log("uv not found. Install from https://docs.astral.sh/uv/", "error")
            log("  curl -LsSf https://astral.sh/uv/install.sh | sh", "debug")
            all_good = False

        # Check Docker
        if self._check_command(["docker", "--version"]):
            result = subprocess.run(["docker", "--version"], capture_output=True, text=True)
            log(f"Docker found: {result.stdout.strip()}", "success")

            # Check Docker is running
            if self._check_command(["docker", "info"]):
                log("Docker daemon is running", "success")
            else:
                log("Docker daemon is not running. Please start Docker.", "error")
                all_good = False
        else:
            log("Docker not found. Install from https://docs.docker.com/", "error")
            all_good = False

        # Check API key
        if self.api_key:
            masked_key = self.api_key[:8] + "..." + self.api_key[-4:] if len(self.api_key) > 12 else "***"
            log(f"API key found: {masked_key}", "success")
        else:
            log("No API key found. Set OPENAI_API_KEY or use --api-key", "warning")
            log("ChatNode startup will fail without an API key", "warning")

        return all_good

    def setup_logs_directory(self) -> None:
        """Create logs directory for component outputs."""
        self.logs_dir.mkdir(exist_ok=True)
        log(f"Logs directory: {self.logs_dir.absolute()}", "debug")

    def clone_broker_if_needed(self) -> bool:
        """Clone calfkit-broker repo if not present."""
        if self.args.cloud_broker:
            log("Using cloud broker, skipping local broker setup", "info")
            return True

        if self.broker_dir.exists():
            log(f"calfkit-broker found at {self.broker_dir}", "success")
            return True

        log("calfkit-broker not found, cloning...", "warning")
        parent_dir = Path("..").absolute()

        try:
            result = subprocess.run(
                ["git", "clone", "https://github.com/calf-ai/calfkit-broker"],
                cwd=parent_dir,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                log("Successfully cloned calfkit-broker", "success")
                return True
            else:
                log(f"Failed to clone broker: {result.stderr}", "error")
                return False
        except subprocess.TimeoutExpired:
            log("Clone operation timed out", "error")
            return False
        except Exception as e:
            log(f"Error cloning broker: {e}", "error")
            return False

    def start_broker(self) -> bool:
        """Start the Kafka broker using docker-compose."""
        if self.args.cloud_broker:
            log("Using cloud broker, skipping local broker startup", "info")
            return True

        log("Starting Kafka Broker", "header")

        if not self.broker_dir.exists():
            log("Broker directory not found", "error")
            return False

        # Check if broker already running
        try:
            result = subprocess.run(
                ["docker", "ps", "--filter", "name=kafka", "--format", "{{.Names}}"],
                capture_output=True,
                text=True,
            )
            if "kafka" in result.stdout.lower():
                log("Kafka broker already running", "success")
                return True
        except Exception:
            pass

        # Start the broker
        log("Starting Kafka broker with docker-compose...", "info")
        log_file = self.logs_dir / "broker.log"

        try:
            # Run make dev-up in the broker directory
            with open(log_file, "w") as f:
                process = subprocess.Popen(
                    ["make", "dev-up"],
                    cwd=self.broker_dir,
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    preexec_fn=os.setsid if sys.platform != "win32" else None,
                )

            self.components["broker"] = ComponentStatus(
                name="broker",
                process=process,
                pid=process.pid,
                log_file=log_file,
                status="starting",
                start_time=time.time(),
            )

            # Wait for broker to be ready
            log("Waiting for broker to be ready (this may take 30-60s)...", "info")
            time.sleep(10)  # Initial wait for containers to start

            # Poll for readiness
            for attempt in range(30):
                try:
                    result = subprocess.run(
                        ["docker", "ps", "--filter", "name=kafka", "--format", "{{.Status}}"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if "healthy" in result.stdout.lower() or "up" in result.stdout.lower():
                        self.components["broker"].status = "running"
                        log("Kafka broker is ready!", "success")
                        return True
                except Exception:
                    pass
                time.sleep(2)

            log("Broker may still be starting, continuing anyway...", "warning")
            self.components["broker"].status = "running"
            return True

        except Exception as e:
            log(f"Failed to start broker: {e}", "error")
            self.components["broker"].status = "failed"
            self.components["broker"].error_message = str(e)
            return False

    def install_dependencies(self) -> bool:
        """Install Python dependencies using uv."""
        log("Installing Dependencies", "header")

        try:
            result = subprocess.run(
                ["uv", "sync"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                log("Dependencies installed successfully", "success")
                return True
            else:
                log(f"Failed to install dependencies: {result.stderr}", "error")
                return False
        except subprocess.TimeoutExpired:
            log("Dependency installation timed out", "error")
            return False
        except Exception as e:
            log(f"Error installing dependencies: {e}", "error")
            return False

    def _start_component(
        self,
        name: str,
        cmd: List[str],
        env: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Start a component process."""
        log_file = self.logs_dir / f"{name}.log"

        try:
            # Merge environment variables
            process_env = os.environ.copy()
            if env:
                process_env.update(env)
            process_env["KAFKA_BOOTSTRAP_SERVERS"] = self.broker_url

            with open(log_file, "w") as f:
                process = subprocess.Popen(
                    cmd,
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    env=process_env,
                    preexec_fn=os.setsid if sys.platform != "win32" else None,
                )

            self.components[name] = ComponentStatus(
                name=name,
                process=process,
                pid=process.pid,
                log_file=log_file,
                status="running",
                start_time=time.time(),
            )

            log(f"Started {name} (PID: {process.pid})", "success")
            log(f"  Log: {log_file}", "debug")
            return True

        except Exception as e:
            log(f"Failed to start {name}: {e}", "error")
            self.components[name] = ComponentStatus(
                name=name,
                status="failed",
                error_message=str(e),
            )
            return False

    def start_coinbase_connector(self) -> bool:
        """Start the Coinbase market data connector."""
        log("Starting Coinbase Connector", "header")

        cmd = [
            "uv", "run", "python", "coinbase_kafka_connector.py",
            "--bootstrap-servers", self.broker_url,
            "--config", self.args.config,
        ]

        if self.args.interval:
            cmd.extend(["--min-interval", str(self.args.interval)])

        return self._start_component("coinbase-connector", cmd)

    def start_tools_dashboard(self) -> bool:
        """Start the tools and dashboard."""
        log("Starting Tools & Dashboard", "header")

        cmd = [
            "uv", "run", "python", "tools_and_dashboard.py",
            "--bootstrap-servers", self.broker_url,
        ]

        return self._start_component("tools-dashboard", cmd)

    def start_chat_nodes(self) -> list[str]:
        """Start ChatNode(s) for LLM inference.

        If config has chat_nodes defined, start all configured nodes.
        Otherwise, start a single default node.

        Returns:
            List of started chat node names.
        """
        log("Starting ChatNode(s) (LLM)", "header")

        started_nodes: list[str] = []

        # Check if we have config-based chat nodes
        if self.config and self.config.chat_nodes:
            log(f"Found {len(self.config.chat_nodes)} ChatNode(s) in config", "info")

            for node_config in self.config.chat_nodes:
                # Get provider config
                provider_config = self.config.get_provider_config(node_config.provider)
                if not provider_config:
                    log(f"Provider '{node_config.provider}' not configured for node '{node_config.name}'", "error")
                    continue

                if not provider_config.api_key:
                    log(f"No API key for provider '{node_config.provider}', skipping node '{node_config.name}'", "warning")
                    continue

                cmd = [
                    "uv", "run", "python", "deploy_chat_node.py",
                    "--from-config", node_config.name,
                    "--bootstrap-servers", self.broker_url,
                    "--config-path", self.args.config,
                ]

                component_name = f"chat-node-{node_config.name}"
                if self._start_component(component_name, cmd):
                    started_nodes.append(node_config.name)
                    time.sleep(1)  # Small delay between node starts

            if not started_nodes:
                log("No ChatNodes could be started from config", "error")
        else:
            # Fall back to legacy single ChatNode
            if not self.api_key:
                log("No API key provided, skipping ChatNode", "warning")
                log("Set OPENAI_API_KEY or use --api-key to enable", "debug")
                return []

            model_id = self.args.model_id or "gpt-4o-mini"
            chat_node_name = "default-chat"

            cmd = [
                "uv", "run", "python", "deploy_chat_node.py",
                "--name", chat_node_name,
                "--model-id", model_id,
                "--bootstrap-servers", self.broker_url,
                "--api-key", self.api_key,
                "--config-path", self.args.config,
            ]

            if self.args.reasoning_effort:
                cmd.extend(["--reasoning-effort", self.args.reasoning_effort])

            if self._start_component("chat-node", cmd):
                started_nodes.append(chat_node_name)

        return started_nodes

    def start_agent_routers(self, chat_node_names: list[str]) -> bool:
        """Start default agent routers with different strategies.

        Args:
            chat_node_names: List of available chat node names to distribute agents across.
        """
        log("Starting Agent Routers", "header")

        agents = [
            ("momentum-trader", "momentum"),
            ("brainrot-trader", "brainrot"),
            ("scalper-trader", "scalper"),
        ]

        # If no chat nodes available, can't start agents
        if not chat_node_names:
            log("No ChatNodes available, cannot start agents", "error")
            return False

        # Distribute agents across available chat nodes
        all_started = True
        for i, (agent_name, strategy) in enumerate(agents):
            # Round-robin assign to chat nodes
            chat_node_name = chat_node_names[i % len(chat_node_names)]

            cmd = [
                "uv", "run", "python", "deploy_router_node.py",
                "--name", agent_name,
                "--chat-node-name", chat_node_name,
                "--strategy", strategy,
                "--bootstrap-servers", self.broker_url,
            ]

            if not self._start_component(f"agent-{agent_name}", cmd):
                all_started = False

            time.sleep(1)  # Small delay between agent starts

        return all_started

    def start_response_viewer(self) -> bool:
        """Start the optional response viewer."""
        if not self.args.with_viewer:
            return True

        log("Starting Response Viewer", "header")

        cmd = [
            "uv", "run", "python", "response_viewer.py",
            "--bootstrap-servers", self.broker_url,
        ]

        return self._start_component("response-viewer", cmd)

    def print_status(self) -> None:
        """Print current status of all components."""
        log("Component Status", "header")

        for name, comp in self.components.items():
            if comp.status == "running":
                status_str = f"{COLORS['green']}RUNNING{COLORS['reset']}"
                pid_str = f"(PID: {comp.pid})"
            elif comp.status == "failed":
                status_str = f"{COLORS['red']}FAILED{COLORS['reset']}"
                pid_str = f"- {comp.error_message or ''}"
            elif comp.status == "starting":
                status_str = f"{COLORS['yellow']}STARTING{COLORS['reset']}"
                pid_str = ""
            else:
                status_str = f"{COLORS['dim']}{comp.status.upper()}{COLORS['reset']}"
                pid_str = ""

            print(f"  {name:<20} {status_str} {pid_str}")

        print(f"\n{COLORS['cyan']}Logs directory: {self.logs_dir.absolute()}{COLORS['reset']}")
        print(f"{COLORS['cyan']}Press Ctrl+C to stop all components{COLORS['reset']}\n")

    def monitor_loop(self) -> None:
        """Monitor running components and handle shutdown."""
        log("All components started! Monitoring...", "success")
        self.print_status()

        try:
            while self.running:
                # Check component health
                for name, comp in list(self.components.items()):
                    if comp.process and comp.status == "running":
                        ret_code = comp.process.poll()
                        if ret_code is not None:
                            comp.status = "failed"
                            comp.error_message = f"Exited with code {ret_code}"
                            log(f"{name} has stopped (exit code: {ret_code})", "warning")

                # Print status every 30 seconds
                time.sleep(5)

        except KeyboardInterrupt:
            pass

    def shutdown(self) -> None:
        """Gracefully shutdown all components."""
        log("Shutting down all components...", "header")

        # Stop in reverse order of dependency
        stop_order = [
            "agent-momentum-trader",
            "agent-brainrot-trader",
            "agent-scalper-trader",
            "response-viewer",
            "tools-dashboard",
            "coinbase-connector",
            "broker",
        ]

        # Add chat nodes to stop order (match any component starting with "chat-node")
        chat_node_components = [name for name in self.components if name.startswith("chat-")]
        stop_order = chat_node_components + stop_order

        for name in stop_order:
            if name not in self.components:
                continue

            comp = self.components[name]
            if comp.process and comp.pid:
                log(f"Stopping {name}...", "info")
                try:
                    if sys.platform != "win32":
                        os.killpg(os.getpgid(comp.pid), signal.SIGTERM)
                    else:
                        comp.process.terminate()

                    # Wait for graceful shutdown
                    comp.process.wait(timeout=5)
                    comp.status = "stopped"
                    log(f"{name} stopped gracefully", "success")

                except subprocess.TimeoutExpired:
                    log(f"{name} did not stop gracefully, forcing...", "warning")
                    try:
                        if sys.platform != "win32":
                            os.killpg(os.getpgid(comp.pid), signal.SIGKILL)
                        else:
                            comp.process.kill()
                        comp.status = "stopped"
                    except Exception as e:
                        log(f"Error killing {name}: {e}", "error")

                except Exception as e:
                    log(f"Error stopping {name}: {e}", "error")

        log("Shutdown complete", "success")
        self.print_final_summary()

    def print_final_summary(self) -> None:
        """Print final summary of the session."""
        print(f"\n{COLORS['magenta']}{'='*60}{COLORS['reset']}")
        print(f"{COLORS['bold']}Session Summary{COLORS['reset']}")
        print(f"{COLORS['magenta']}{'='*60}{COLORS['reset']}")

        running = sum(1 for c in self.components.values() if c.status == "running")
        failed = sum(1 for c in self.components.values() if c.status == "failed")
        stopped = sum(1 for c in self.components.values() if c.status == "stopped")

        print(f"  Components running: {running}")
        print(f"  Components failed:  {failed}")
        print(f"  Components stopped: {stopped}")
        print(f"\n  Logs saved to: {self.logs_dir.absolute()}")
        print(f"\n{COLORS['cyan']}Thank you for using Trading Arena!{COLORS['reset']}\n")

    def run(self) -> int:
        """Run the complete startup sequence."""
        log("Trading Arena Startup", "header")

        # Phase 1: Prerequisites
        if not self.check_prerequisites():
            if not self.args.skip_checks:
                log("Prerequisite checks failed. Use --skip-checks to bypass.", "error")
                return 1
            log("Skipping failed prerequisites due to --skip-checks", "warning")

        # Phase 2: Setup
        self.setup_logs_directory()

        # Phase 3: Broker setup
        if not self.args.cloud_broker:
            if not self.clone_broker_if_needed():
                log("Failed to setup broker. Use --cloud-broker if you have one.", "error")
                return 1

            if not self.start_broker():
                log("Failed to start broker. Check logs/broker.log", "error")
                if not self.args.continue_on_error:
                    return 1
                log("Continuing anyway due to --continue-on-error", "warning")

        # Phase 4: Dependencies
        if not self.args.skip_deps:
            if not self.install_dependencies():
                log("Failed to install dependencies", "error")
                if not self.args.continue_on_error:
                    return 1

        # Phase 5: Core Components
        time.sleep(2)  # Wait for broker to be fully ready

        if not self.start_coinbase_connector():
            log("Failed to start Coinbase connector", "error")
            if not self.args.continue_on_error:
                self.shutdown()
                return 1

        time.sleep(2)

        if not self.start_tools_dashboard():
            log("Failed to start tools/dashboard", "error")
            if not self.args.continue_on_error:
                self.shutdown()
                return 1

        # Phase 6: ChatNode(s) (LLM)
        time.sleep(2)

        chat_node_names = self.start_chat_nodes()
        if not chat_node_names:
            log("No ChatNodes started (check API keys in config or env)", "warning")
            log("Agents will not work without ChatNodes", "warning")
        else:
            # Phase 7: Agent Routers (only if ChatNodes started)
            time.sleep(3)  # Wait for ChatNodes to be ready

            if not self.start_agent_routers(chat_node_names):
                log("Some agents failed to start", "warning")

        # Phase 8: Optional components
        self.start_response_viewer()

        # Phase 9: Monitor
        self.monitor_loop()

        return 0


def create_argument_parser() -> argparse.ArgumentParser:
    """Create and configure the argument parser."""
    parser = argparse.ArgumentParser(
        description="Trading Arena Startup Script - Launch all components",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic startup with local broker
  uv run python start_arena.py

  # Use cloud broker
  uv run python start_arena.py --cloud-broker broker.example.com:9092

  # Fast market data updates (30 seconds)
  uv run python start_arena.py --interval 30

  # Include response viewer
  uv run python start_arena.py --with-viewer

  # Use specific model
  uv run python start_arena.py --model-id gpt-4o --api-key sk-...

Environment Variables:
  OPENAI_API_KEY          API key for LLM provider
  KAFKA_BOOTSTRAP_SERVERS Default broker URL
        """,
    )

    parser.add_argument(
        "--broker-url",
        default=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
        help="Kafka broker URL (default: localhost:9092 or $KAFKA_BOOTSTRAP_SERVERS)",
    )

    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to configuration file (default: config.json)",
    )

    parser.add_argument(
        "--cloud-broker",
        metavar="URL",
        help="Use cloud broker instead of local (skips Docker broker setup)",
    )

    parser.add_argument(
        "--api-key",
        default=os.getenv("OPENAI_API_KEY", ""),
        help="API key for LLM provider (default: $OPENAI_API_KEY)",
    )

    parser.add_argument(
        "--model-id",
        default="gpt-4o-mini",
        help="Model ID for ChatNode (default: gpt-4o-mini)",
    )

    parser.add_argument(
        "--reasoning-effort",
        choices=["low", "medium", "high"],
        help="Reasoning effort for supported models",
    )

    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Market data interval in seconds (default: 60)",
    )

    parser.add_argument(
        "--with-viewer",
        action="store_true",
        help="Also start the response viewer",
    )

    parser.add_argument(
        "--skip-checks",
        action="store_true",
        help="Skip prerequisite checks",
    )

    parser.add_argument(
        "--skip-deps",
        action="store_true",
        help="Skip dependency installation",
    )

    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue startup even if some components fail",
    )

    return parser


def main() -> int:
    """Main entry point."""
    parser = create_argument_parser()
    args = parser.parse_args()

    manager = ArenaStartupManager(args)
    return manager.run()


if __name__ == "__main__":
    sys.exit(main())
