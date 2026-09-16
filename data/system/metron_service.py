"""Metron/Mokkari access, bounded concurrency, detail caching, and prefetching."""

import asyncio
import time
import traceback

import mokkari

from .database import (
    load_cached_character,
    load_cached_issue,
    load_cached_series,
    store_cached_character,
    store_cached_issue,
    store_cached_series,
)
from .settings import METRON_MAX_CONCURRENCY, METRON_PASSWORD, METRON_USERNAME
from .utils import _format_log_context, db_call, display_name, logger, run_blocking


metron = mokkari.api(METRON_USERNAME, METRON_PASSWORD)

_metron_semaphore = asyncio.Semaphore(METRON_MAX_CONCURRENCY)
_background_tasks: set[asyncio.Task] = set()
_issue_fetch_tasks: dict[int, asyncio.Task] = {}
_series_fetch_tasks: dict[int, asyncio.Task] = {}
_character_fetch_tasks: dict[int, asyncio.Task] = {}


async def metron_call(
    operation: str,
    func,
    *args,
    context: dict | None = None,
    **kwargs,
):
    """Run one Metron call with shared timing and bounded concurrency."""
    queued_at = time.perf_counter()

    async with _metron_semaphore:
        queue_wait = time.perf_counter() - queued_at
        if queue_wait >= 0.010:
            logger.info(
                "[METRON] %s waited %.3fs for a request slot%s",
                operation,
                queue_wait,
                _format_log_context(context),
            )

        return await run_blocking(
            "METRON",
            operation,
            func,
            *args,
            context=context,
            **kwargs,
        )


def normalize_issue_details(issue) -> dict:
    """Convert a Mokkari issue object into JSON-safe data used by embeds."""
    series = getattr(issue, "series", None)
    story_titles = getattr(issue, "story_titles", None) or []
    credits = getattr(issue, "credits", None) or []

    normalized_credits = []
    for credit in credits:
        creator_obj = getattr(credit, "creator", None)
        creator = display_name(creator_obj)
        creator_id = getattr(creator_obj, "id", None)
        roles = getattr(credit, "role", None) or getattr(credit, "roles", None) or []

        if not isinstance(roles, (list, tuple, set)):
            roles = [roles]

        role_names = [display_name(role, "") for role in roles]
        role_names = [role for role in role_names if role]
        normalized_credits.append(
            {
                "creator_id": int(creator_id) if creator_id is not None else None,
                "creator": creator,
                "roles": role_names,
            }
        )

    resource_url = getattr(issue, "resource_url", None)
    image = getattr(issue, "image", None)
    cover_date = getattr(issue, "cover_date", None)
    store_date = getattr(issue, "store_date", None)
    page_count = getattr(issue, "page_count", None)
    price = getattr(issue, "price", None)
    volume = getattr(series, "volume", None)

    return {
        "id": int(issue.id),
        "number": str(getattr(issue, "number", "?")),
        "description": str(
            getattr(issue, "desc", None) or "No description available."
        ).strip(),
        "resource_url": str(resource_url) if resource_url else None,
        "image_url": str(image) if image else None,
        "publisher": display_name(getattr(issue, "publisher", None)),
        "cover_date": str(cover_date) if cover_date else None,
        "store_date": str(store_date) if store_date else None,
        "volume": str(volume) if volume is not None else None,
        "page_count": str(page_count) if page_count is not None else None,
        "price": str(price) if price is not None else None,
        "story_titles": [str(story) for story in story_titles],
        "credits": normalized_credits,
    }


async def get_issue_details(issue_id: int) -> dict:
    """Return cached issue details, fetching Metron only on a cache miss."""
    cached = await db_call(
        "issue cache read",
        load_cached_issue,
        issue_id,
        context={"issue_id": issue_id},
    )
    if cached is not None:
        logger.info("[CACHE] issue detail hit | issue_id=%s", issue_id)
        return cached

    logger.info("[CACHE] issue detail miss | issue_id=%s", issue_id)

    existing_task = _issue_fetch_tasks.get(issue_id)
    if existing_task is not None:
        logger.info("[CACHE] awaiting in-flight issue fetch | issue_id=%s", issue_id)
        return await existing_task

    async def fetch_and_cache() -> dict:
        issue = await metron_call(
            "issue details",
            metron.issue,
            issue_id,
            context={"issue_id": issue_id},
        )
        details = normalize_issue_details(issue)
        await db_call(
            "issue cache write",
            store_cached_issue,
            issue_id,
            details,
            context={"issue_id": issue_id},
        )
        return details

    task = asyncio.create_task(fetch_and_cache())
    _issue_fetch_tasks[issue_id] = task

    try:
        return await task
    finally:
        if _issue_fetch_tasks.get(issue_id) is task:
            del _issue_fetch_tasks[issue_id]


def normalize_character_details(character) -> dict:
    """Convert a Mokkari character-detail object into dropdown-friendly data."""
    universes = getattr(character, "universes", None) or []
    return {
        "id": int(character.id),
        "name": str(getattr(character, "name", None) or display_name(character)),
        "universes": [display_name(universe) for universe in universes],
    }


async def get_character_details(character_id: int) -> dict:
    """Return cached character details, fetching Metron only on a cache miss."""
    cached = await db_call(
        "character cache read",
        load_cached_character,
        character_id,
        context={"character_id": character_id},
    )
    if cached is not None:
        logger.info("[CACHE] character detail hit | character_id=%s", character_id)
        return cached

    logger.info("[CACHE] character detail miss | character_id=%s", character_id)

    existing_task = _character_fetch_tasks.get(character_id)
    if existing_task is not None:
        return await existing_task

    async def fetch_and_cache() -> dict:
        character = await metron_call(
            "character details",
            metron.character,
            character_id,
            context={"character_id": character_id},
        )
        details = normalize_character_details(character)
        await db_call(
            "character cache write",
            store_cached_character,
            character_id,
            details,
            context={"character_id": character_id},
        )
        return details

    task = asyncio.create_task(fetch_and_cache())
    _character_fetch_tasks[character_id] = task
    try:
        return await task
    finally:
        if _character_fetch_tasks.get(character_id) is task:
            del _character_fetch_tasks[character_id]


def normalize_series_details(series) -> dict:
    """Convert a Mokkari series-detail object into JSON-safe embed data."""
    genres = getattr(series, "genres", None) or []
    associated = getattr(series, "associated", None) or []
    alt_names = getattr(series, "alt_names", None) or []

    normalized_associated = []
    for item in associated:
        item_id = getattr(item, "id", None)
        item_name = display_name(item)
        normalized_associated.append(
            {
                "id": int(item_id) if item_id is not None else None,
                "name": item_name,
            }
        )

    resource_url = getattr(series, "resource_url", None)
    publisher = getattr(series, "publisher", None)
    imprint = getattr(series, "imprint", None)
    series_type = getattr(series, "series_type", None)

    return {
        "id": int(series.id),
        "name": str(getattr(series, "name", None) or display_name(series)),
        "sort_name": str(getattr(series, "sort_name", None) or ""),
        "publisher": display_name(publisher),
        "imprint": display_name(imprint, "") if imprint is not None else None,
        "series_type": display_name(series_type),
        "status": str(getattr(series, "status", None) or "Unknown"),
        "year_began": getattr(series, "year_began", None),
        "year_end": getattr(series, "year_end", None),
        "volume": getattr(series, "volume", None),
        "issue_count": getattr(series, "issue_count", None),
        "description": str(
            getattr(series, "desc", None) or "No description available."
        ).strip(),
        "genres": [display_name(genre) for genre in genres],
        "associated": normalized_associated,
        "alt_names": [str(name) for name in alt_names],
        "language": getattr(series, "language", None),
        "cv_id": getattr(series, "cv_id", None),
        "gcd_id": getattr(series, "gcd_id", None),
        "resource_url": str(resource_url) if resource_url else None,
    }


async def get_series_details(series_id: int) -> dict:
    """Return cached series details, fetching Metron only on a cache miss."""
    cached = await db_call(
        "series cache read",
        load_cached_series,
        series_id,
        context={"series_id": series_id},
    )
    if cached is not None:
        logger.info("[CACHE] series detail hit | series_id=%s", series_id)
        return cached

    logger.info("[CACHE] series detail miss | series_id=%s", series_id)

    existing_task = _series_fetch_tasks.get(series_id)
    if existing_task is not None:
        logger.info(
            "[CACHE] awaiting in-flight series fetch | series_id=%s",
            series_id,
        )
        return await existing_task

    async def fetch_and_cache() -> dict:
        series = await metron_call(
            "series details",
            metron.series,
            series_id,
            context={"series_id": series_id},
        )
        details = normalize_series_details(series)
        await db_call(
            "series cache write",
            store_cached_series,
            series_id,
            details,
            context={"series_id": series_id},
        )
        return details

    task = asyncio.create_task(fetch_and_cache())
    _series_fetch_tasks[series_id] = task

    try:
        return await task
    finally:
        if _series_fetch_tasks.get(series_id) is task:
            del _series_fetch_tasks[series_id]


def schedule_series_prefetch(
    matches: list[dict],
    current_page: int,
    direction: str = "next",
) -> None:
    """Prefetch one neighboring series-detail page in the background."""
    offset = -1 if direction == "prev" else 1
    target_page = current_page + offset

    if not 0 <= target_page < len(matches):
        return

    series_id = int(matches[target_page]["id"])

    async def prefetch() -> None:
        try:
            await get_series_details(series_id)
        except Exception:
            traceback.print_exc()

    task = asyncio.create_task(prefetch())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def schedule_issue_prefetch(
    matches: list[dict],
    current_page: int,
    direction: str = "next",
) -> None:
    """Fetch one neighboring page in the background without retaining lookup views."""
    offset = -1 if direction == "prev" else 1
    target_page = current_page + offset

    if not 0 <= target_page < len(matches):
        return

    issue_id = int(matches[target_page]["id"])

    async def prefetch() -> None:
        try:
            await get_issue_details(issue_id)
        except Exception:
            traceback.print_exc()

    task = asyncio.create_task(prefetch())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
