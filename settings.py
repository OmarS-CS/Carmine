"""Shared configuration constants for Carmine."""

from pathlib import Path

import discord


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "carmine.db"
SUPERMAN_IMAGE_PATH = BASE_DIR / "superman.jpg"

EMBED_COLOR = discord.Color.green()
ISSUE_LIST_PAGE_LIMIT = 4_000
EMBED_DESCRIPTION_LIMIT = 1_500
EMBED_FIELD_LIMIT = 1_024
METRON_MAX_CONCURRENCY = 3
