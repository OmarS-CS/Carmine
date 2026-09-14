"""Metron/Mokkari access, bounded concurrency, detail caching, and prefetching."""

import asyncio
import time
import traceback

import mokkari

import config
from database import load_cached_issue, store_cached_issue
from settings import METRON_MAX_CONCURRENCY
from utils import _format_log_context, db_call, display_name, logger, run_blocking


metron = mokkari.api(config.username, config.password)

_metron_semaphore = asyncio.Semaphore(METRON_MAX_CONCURRENCY)
_background_tasks: set[asyncio.Task] = set()
_issue_fetch_tasks: dict[int, asyncio.Task] = {}


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
        creator = display_name(getattr(credit, "creator", None))
        roles = getattr(credit, "role", None) or getattr(credit, "roles", None) or []

        if not isinstance(roles, (list, tuple, set)):
            roles = [roles]

        role_names = [display_name(role, "") for role in roles]
        role_names = [role for role in role_names if role]
        normalized_credits.append({"creator": creator, "roles": role_names})

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
