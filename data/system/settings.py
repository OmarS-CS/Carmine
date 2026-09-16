"""Shared configuration, paths, and environment settings for Carmine."""

import os
from pathlib import Path

import discord


SYSTEM_DIR = Path(__file__).resolve().parent
DATA_DIR = SYSTEM_DIR.parent
PROJECT_ROOT = DATA_DIR.parent
MEDIA_DIR = DATA_DIR / "media"

CONFIG_ENV_PATH = PROJECT_ROOT / "config.env"
DB_PATH = DATA_DIR / "carmine.db"
SUPERMAN_IMAGE_PATH = MEDIA_DIR / "superman.jpg"

EMBED_COLOR = discord.Color.green()
ISSUE_LIST_PAGE_LIMIT = 2_000
ISSUE_SEARCH_FILTER_PAGE_SIZE = 22
EMBED_DESCRIPTION_LIMIT = 1_500
EMBED_FIELD_LIMIT = 1_024
METRON_MAX_CONCURRENCY = 3
CHARACTER_SELECT_LIMIT = 10


def _read_config_env(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE pairs from config.env without extra dependencies."""
    if not path.exists():
        return {}

    values: dict[str, str] = {}

    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()

        if not line or line.startswith("#"):
            continue

        if "=" not in line:
            raise RuntimeError(
                f"Invalid config.env entry on line {line_number}: expected KEY=VALUE."
            )

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if value.startswith(('"', "'")) and value.endswith(value[0]) and len(value) >= 2:
            value = value[1:-1]

        if not key:
            raise RuntimeError(
                f"Invalid config.env entry on line {line_number}: missing key."
            )

        values[key] = value

    return values


def _required_setting(name: str, file_values: dict[str, str]) -> str:
    """Return a required secret from the process environment or config.env."""
    value = os.getenv(name) or file_values.get(name)

    if value:
        return value

    raise RuntimeError(
        f"Missing required setting {name}. Add it to {CONFIG_ENV_PATH.name} "
        "or define it as an environment variable."
    )


_config_values = _read_config_env(CONFIG_ENV_PATH)

DISCORD_TOKEN = _required_setting("DISCORD_TOKEN", _config_values)
METRON_USERNAME = _required_setting("METRON_USERNAME", _config_values)
METRON_PASSWORD = _required_setting("METRON_PASSWORD", _config_values)
