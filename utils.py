"""General helpers, logging, and timed blocking-work wrappers."""

import asyncio
import logging
import time


logger = logging.getLogger("carmine")
logger.setLevel(logging.INFO)
logger.propagate = False

if not logger.handlers:
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logger.addHandler(console_handler)


def truncate(text: str, limit: int) -> str:
    """Truncate text to a Discord field limit while preserving an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def display_name(value, default: str = "Unknown") -> str:
    """Return an object's name attribute when available, otherwise its string value."""
    if value is None:
        return default

    name = getattr(value, "name", None)
    if name:
        return str(name)

    text = str(value)
    return text if text else default


def series_display_name(series) -> str:
    """Return a readable title for a Metron series result.

    Mokkari's BaseSeries model exposes the API's ``series`` field as
    ``display_name``. The fallbacks keep this helper compatible with other
    series models returned by Metron/Mokkari.
    """
    for attribute in (
        "display_name",
        "name",
        "series_name",
        "title",
        "sort_name",
    ):
        value = getattr(series, attribute, None)
        if value:
            return str(value)

    if hasattr(series, "model_dump"):
        data = series.model_dump(by_alias=True)
        for key in ("series", "name", "series_name", "title", "sort_name"):
            value = data.get(key)
            if value:
                return str(value)

    return f"Series {getattr(series, 'id', '?')}"


def _format_log_context(context: dict | None) -> str:
    """Format optional operation metadata for concise console timing logs."""
    if not context:
        return ""

    values = ", ".join(f"{key}={value}" for key, value in context.items())
    return f" | {values}"


def log_elapsed(
    category: str,
    operation: str,
    started_at: float,
    *,
    context: dict | None = None,
) -> None:
    """Log elapsed wall-clock time for a completed operation."""
    elapsed = time.perf_counter() - started_at
    logger.info(
        "[%s] %s completed in %.3fs%s",
        category,
        operation,
        elapsed,
        _format_log_context(context),
    )


async def run_blocking(
    category: str,
    operation: str,
    func,
    *args,
    context: dict | None = None,
    **kwargs,
):
    """Run blocking work off the event loop and record its duration."""
    started_at = time.perf_counter()

    try:
        result = await asyncio.to_thread(func, *args, **kwargs)
    except Exception:
        elapsed = time.perf_counter() - started_at
        logger.exception(
            "[%s] %s failed after %.3fs%s",
            category,
            operation,
            elapsed,
            _format_log_context(context),
        )
        raise

    log_elapsed(category, operation, started_at, context=context)
    return result


async def db_call(
    operation: str,
    func,
    *args,
    context: dict | None = None,
    **kwargs,
):
    """Run and time one SQLite operation without blocking Discord's event loop."""
    return await run_blocking(
        "SQLITE",
        operation,
        func,
        *args,
        context=context,
        **kwargs,
    )
