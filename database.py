"""SQLite persistence and cache helpers for Carmine."""

import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import date

from settings import DB_PATH


@dataclass(frozen=True)
class IssueListEntry:
    """Lightweight issue data used by /issues and its cover-year cache."""

    id: int
    issue_name: str
    cover_date: date | None


@dataclass(frozen=True)
class SeriesSearchEntry:
    """Lightweight series data used by the /series_lookup search cache."""

    id: int
    display_name: str
    year_began: int | None


def init_lookup_db() -> None:
    """Create Carmine's persistent tables if they do not already exist."""
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS issue_lookups (
                lookup_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                matches_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS issue_cache (
                issue_id INTEGER PRIMARY KEY,
                details_json TEXT NOT NULL,
                cached_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS issue_year_cache (
                publisher_key TEXT NOT NULL,
                cover_year INTEGER NOT NULL,
                issues_json TEXT NOT NULL,
                cached_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (publisher_key, cover_year)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS issue_searches (
                search_id TEXT PRIMARY KEY,
                date_label TEXT NOT NULL,
                pages_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS issue_lookup_search_cache (
                search_key TEXT PRIMARY KEY,
                matches_json TEXT NOT NULL,
                cached_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS series_search_cache (
                search_key TEXT PRIMARY KEY,
                results_json TEXT NOT NULL,
                cached_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS series_lookups (
                lookup_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                matches_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS series_cache (
                series_id INTEGER PRIMARY KEY,
                details_json TEXT NOT NULL,
                cached_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def _issue_series_name(series) -> str:
    """Extract a readable series title from Mokkari issue-list models."""
    if series is None:
        return "Unknown series"

    for attribute in ("name", "display_name", "series_name", "title", "sort_name"):
        value = getattr(series, attribute, None)
        if value:
            return str(value)

    if hasattr(series, "model_dump"):
        data = series.model_dump(by_alias=True)
        for key in ("series", "name", "series_name", "title", "sort_name"):
            value = data.get(key)
            if value:
                return str(value)

    return "Unknown series"


def serialize_issue_lookup_results(issue_results) -> list[dict]:
    """Normalize Mokkari issue search results into persistent lightweight matches."""
    matches = []

    for result in issue_results:
        series = getattr(result, "series", None)
        year_began = getattr(series, "year_began", None)

        matches.append(
            {
                "id": int(result.id),
                "series_name": _issue_series_name(series),
                "year_began": str(year_began) if year_began is not None else None,
            }
        )

    return matches


def create_lookup_record_from_matches(user_id: int, matches: list[dict]):
    """Store an already-normalized issue lookup for persistent pagination."""
    lookup_id = secrets.token_hex(8)

    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            INSERT INTO issue_lookups (lookup_id, user_id, matches_json)
            VALUES (?, ?, ?)
            """,
            (lookup_id, user_id, json.dumps(matches)),
        )

    return lookup_id, matches


def create_lookup_record(user_id: int, issue_results):
    """Store lightweight Metron search results and return their lookup ID."""
    return create_lookup_record_from_matches(
        user_id,
        serialize_issue_lookup_results(issue_results),
    )


def load_lookup_record(lookup_id: str):
    """Load a stored issue lookup by ID."""
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute(
            """
            SELECT user_id, matches_json
            FROM issue_lookups
            WHERE lookup_id = ?
            """,
            (lookup_id,),
        ).fetchone()

    if row is None:
        return None

    user_id, matches_json = row
    return int(user_id), json.loads(matches_json)


def _normalized_search_text(value: str | None) -> str:
    """Normalize optional user-entered search text for stable cache keys."""
    return value.strip().casefold() if value else ""


def issue_lookup_search_key(
    series: str,
    issue_number: str,
    year: int | None,
    publisher: str | None,
) -> str:
    """Build a deterministic key for one /issue_lookup search."""
    return json.dumps(
        {
            "series": _normalized_search_text(series),
            "issue_number": _normalized_search_text(issue_number),
            "year": year,
            "publisher": _normalized_search_text(publisher),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def load_cached_issue_lookup_search(search_key: str):
    """Load cached normalized matches for an /issue_lookup search, if present."""
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute(
            """
            SELECT matches_json
            FROM issue_lookup_search_cache
            WHERE search_key = ?
            """,
            (search_key,),
        ).fetchone()

    if row is None:
        return None

    return json.loads(row[0])


def store_cached_issue_lookup_search(search_key: str, issue_results) -> list[dict]:
    """Cache normalized /issue_lookup search matches indefinitely."""
    matches = serialize_issue_lookup_results(issue_results)

    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            INSERT INTO issue_lookup_search_cache (search_key, matches_json, cached_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(search_key) DO UPDATE SET
                matches_json = excluded.matches_json,
                cached_at = CURRENT_TIMESTAMP
            """,
            (search_key, json.dumps(matches)),
        )

    return matches


def series_search_key(name: str) -> str:
    """Build a case-insensitive cache key for /series_lookup."""
    return _normalized_search_text(name)


def _serialize_series_results(series_results) -> list[dict]:
    """Normalize Mokkari series search results for persistent caching."""
    records = []

    for series in series_results:
        display_name = None
        for attribute in ("display_name", "name", "series_name", "title", "sort_name"):
            value = getattr(series, attribute, None)
            if value:
                display_name = str(value)
                break

        if display_name is None and hasattr(series, "model_dump"):
            data = series.model_dump(by_alias=True)
            for key in ("series", "name", "series_name", "title", "sort_name"):
                value = data.get(key)
                if value:
                    display_name = str(value)
                    break

        records.append(
            {
                "id": int(series.id),
                "display_name": display_name or f"Series {series.id}",
                "year_began": getattr(series, "year_began", None),
            }
        )

    return records


def _deserialize_series_results(records: list[dict]) -> list[SeriesSearchEntry]:
    """Rebuild cached series entries for the Discord dropdown."""
    return [
        SeriesSearchEntry(
            id=int(record["id"]),
            display_name=str(record["display_name"]),
            year_began=(
                int(record["year_began"])
                if record.get("year_began") is not None
                else None
            ),
        )
        for record in records
    ]


def load_cached_series_search(search_key: str):
    """Load cached /series_lookup results, including cached empty searches."""
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute(
            """
            SELECT results_json
            FROM series_search_cache
            WHERE search_key = ?
            """,
            (search_key,),
        ).fetchone()

    if row is None:
        return None

    return _deserialize_series_results(json.loads(row[0]))


def store_cached_series_search(search_key: str, series_results):
    """Cache /series_lookup results indefinitely and return lightweight entries."""
    records = _serialize_series_results(series_results)

    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            INSERT INTO series_search_cache (search_key, results_json, cached_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(search_key) DO UPDATE SET
                results_json = excluded.results_json,
                cached_at = CURRENT_TIMESTAMP
            """,
            (search_key, json.dumps(records)),
        )

    return _deserialize_series_results(records)


def create_series_lookup_record(user_id: int, series_results):
    """Store series-search matches for persistent /series_lookup pagination."""
    lookup_id = secrets.token_hex(8)
    matches = _serialize_series_results(series_results)

    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            INSERT INTO series_lookups (lookup_id, user_id, matches_json)
            VALUES (?, ?, ?)
            """,
            (lookup_id, user_id, json.dumps(matches)),
        )

    return lookup_id, matches


def load_series_lookup_record(lookup_id: str):
    """Load one persistent /series_lookup paginator record."""
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute(
            """
            SELECT user_id, matches_json
            FROM series_lookups
            WHERE lookup_id = ?
            """,
            (lookup_id,),
        ).fetchone()

    if row is None:
        return None

    user_id, matches_json = row
    return int(user_id), json.loads(matches_json)


def load_cached_series(series_id: int):
    """Load normalized series details from SQLite, if cached."""
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute(
            """
            SELECT details_json
            FROM series_cache
            WHERE series_id = ?
            """,
            (series_id,),
        ).fetchone()

    if row is None:
        return None

    return json.loads(row[0])


def store_cached_series(series_id: int, details: dict) -> None:
    """Persist normalized series details for future /series_lookup pages."""
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            INSERT INTO series_cache (series_id, details_json, cached_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(series_id) DO UPDATE SET
                details_json = excluded.details_json,
                cached_at = CURRENT_TIMESTAMP
            """,
            (series_id, json.dumps(details)),
        )


def _publisher_cache_key(publisher: str) -> str:
    """Normalize publisher text so cache lookups are case-insensitive."""
    return publisher.strip().casefold()


def _serialize_issue_year_results(issue_results) -> list[dict]:
    """Convert one Metron cover-year response into compact JSON-safe records."""
    records = []

    for issue in issue_results:
        cover_date = getattr(issue, "cover_date", None)
        records.append(
            {
                "id": int(issue.id),
                "issue_name": str(getattr(issue, "issue_name", f"Issue {issue.id}")),
                "cover_date": cover_date.isoformat() if cover_date else None,
            }
        )

    return records


def _deserialize_issue_year_results(records: list[dict]) -> list[IssueListEntry]:
    """Rebuild lightweight /issues entries from cached JSON records."""
    return [
        IssueListEntry(
            id=int(record["id"]),
            issue_name=str(record["issue_name"]),
            cover_date=(
                date.fromisoformat(record["cover_date"])
                if record.get("cover_date")
                else None
            ),
        )
        for record in records
    ]


def load_cached_issue_year(publisher: str, cover_year: int):
    """Load a publisher/year issue list from SQLite, if it has been cached."""
    publisher_key = _publisher_cache_key(publisher)

    with sqlite3.connect(DB_PATH) as db:
        row = db.execute(
            """
            SELECT issues_json
            FROM issue_year_cache
            WHERE publisher_key = ? AND cover_year = ?
            """,
            (publisher_key, cover_year),
        ).fetchone()

    if row is None:
        return None

    return _deserialize_issue_year_results(json.loads(row[0]))


def store_cached_issue_year(
    publisher: str,
    cover_year: int,
    issue_results,
) -> list[IssueListEntry]:
    """Persist one complete publisher/year result set and return lightweight entries."""
    publisher_key = _publisher_cache_key(publisher)
    records = _serialize_issue_year_results(issue_results)

    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            INSERT INTO issue_year_cache (publisher_key, cover_year, issues_json, cached_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(publisher_key, cover_year) DO UPDATE SET
                issues_json = excluded.issues_json,
                cached_at = CURRENT_TIMESTAMP
            """,
            (publisher_key, cover_year, json.dumps(records)),
        )

    return _deserialize_issue_year_results(records)


def create_issue_search_record(date_label: str, pages: list[str]) -> str:
    """Store rendered /issues pages so persistent buttons can restore them."""
    search_id = secrets.token_hex(8)

    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            INSERT INTO issue_searches (search_id, date_label, pages_json)
            VALUES (?, ?, ?)
            """,
            (search_id, date_label, json.dumps(pages)),
        )

    return search_id


def load_issue_search_record(search_id: str):
    """Load a stored /issues paginator by its persistent search ID."""
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute(
            """
            SELECT date_label, pages_json
            FROM issue_searches
            WHERE search_id = ?
            """,
            (search_id,),
        ).fetchone()

    if row is None:
        return None

    date_label, pages_json = row
    return date_label, json.loads(pages_json)


def load_cached_issue(issue_id: int):
    """Load normalized issue details from SQLite, if they have been cached."""
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute(
            """
            SELECT details_json
            FROM issue_cache
            WHERE issue_id = ?
            """,
            (issue_id,),
        ).fetchone()

    if row is None:
        return None

    return json.loads(row[0])


def store_cached_issue(issue_id: int, details: dict) -> None:
    """Persist normalized issue details so future page loads avoid Metron."""
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            INSERT INTO issue_cache (issue_id, details_json, cached_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(issue_id) DO UPDATE SET
                details_json = excluded.details_json,
                cached_at = CURRENT_TIMESTAMP
            """,
            (issue_id, json.dumps(details)),
        )
