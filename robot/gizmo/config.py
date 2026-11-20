"""Your setup, as opposed to how Gizmo behaves: AWS, the AgentCore brain, and the optional add-ins.

~/.config/gizmo/config.toml on the robot (every key optional), each overridable by an environment
variable GIZMO_<KEY> (lists comma-separated):

    aws_profile = "gizmo-robot"          # credentials for Bedrock and AgentCore; empty: the default chain
    aws_region = "us-east-1"
    agentcore_runtime_arn = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/gizmo_demo_brain-AbCdEf1234"
    # Add-ins (addons/ in the repo), both optional:
    voice_servers = ["http://192.168.1.50:8770"]  # the voice add-in: cloned and built-in voices
    box_hosts = ["192.168.1.50"]         # the home box add-in: ears 8771, LM Studio 1234 (and voices 8770)
    local_model = "qwen/qwen3.8-27b"     # the LM Studio model the home box brain uses

Settings the console changes (the personality in use, the model, listening...) live next to it in settings.json.
"""

from __future__ import annotations

import json
import logging
import os
import tomllib
from pathlib import Path

logger = logging.getLogger("gizmo.config")

CONFIG_FILE = Path.home() / ".config" / "gizmo" / "config.toml"
SETTINGS_FILE = Path.home() / ".config" / "gizmo" / "settings.json"
DEFAULTS: dict = {
    "aws_profile": "",
    "aws_region": "us-east-1",
    "agentcore_runtime_arn": "",
    "voice_servers": [],
    "box_hosts": [],
    "local_model": "qwen/qwen3.8-27b",
}


def load(path: Path = CONFIG_FILE) -> dict:
    config = dict(DEFAULTS)
    try:
        config.update({k: v for k, v in tomllib.loads(path.read_text()).items() if k in DEFAULTS})
    except FileNotFoundError:
        pass
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning(f"Ignoring {path}: {e}")
    for key, default in DEFAULTS.items():
        value = os.environ.get(f"GIZMO_{key.upper()}")
        if value is not None:
            config[key] = [v.strip() for v in value.split(",") if v.strip()] if isinstance(default, list) else value
    return config


CONFIG = load()


def load_settings() -> dict:
    """What the console has saved (an OpenAI-compatible endpoint's API key can be in here: the file is 0600)."""
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
    SETTINGS_FILE.chmod(0o600)


def box_urls(port: int, scheme: str = "http", path: str = "") -> list[str]:
    """The home box's address for one of its services, for each configured host (none: no box)."""
    return [f"{scheme}://{host}:{port}{path}" for host in CONFIG["box_hosts"]]
