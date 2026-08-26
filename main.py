"""Carmine Discord bot.

Provides comic lookup commands backed by the Metron API, including persistent
issue-result pagination using SQLite and Discord dynamic components.
"""

import asyncio
import json
import secrets
import sqlite3
import traceback
from datetime import date
from pathlib import Path

import discord
import mokkari
from discord import Interaction, SelectOption, app_commands
from discord.ext import commands
from discord.ui import Select, View

import config


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "carmine.db"
SUPERMAN_IMAGE_PATH = BASE_DIR / "superman.jpg"

EMBED_COLOR = discord.Color.green()
ISSUE_LIST_PAGE_LIMIT = 4_000
EMBED_DESCRIPTION_LIMIT = 1_500
EMBED_FIELD_LIMIT = 1_024

# Only active background fetches live in memory. Completed tasks remove themselves.
_background_tasks: set[asyncio.Task] = set()
_issue_fetch_tasks: dict[int, asyncio.Task] = {}

metron = mokkari.api(config.username, config.password)

intents = discord.Intents.default()
intents.message_content = True


class CarmineBot(commands.Bot):
    """Discord bot with one-time startup setup."""

    async def setup_hook(self) -> None:
        """Initialize persistent state and sync slash commands once per startup."""
        init_lookup_db()
        self.add_dynamic_items(IssuePageButton)
        self.add_dynamic_items(IssueListPageButton)
        await self.tree.sync()


bot = CarmineBot(command_prefix="*", intents=intents, case_insensitive=True)
bot.remove_command("help")


@bot.event
async def on_ready() -> None:
    """Log when Carmine has connected to Discord."""
    print("-----------------------------")
    print("the bot is now ready for use!")
    print("-----------------------------")


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Series lookup UI
# ---------------------------------------------------------------------------


class SeriesSelect(Select):
    """Dropdown containing up to Discord's 25 allowed series choices."""

    def __init__(self, series_results):
        options = [
            SelectOption(
                label=series.name[:100],
                description=f"ID: {series.id}",
                value=str(series.id),
            )
            for series in series_results[:25]
        ]

        super().__init__(
            placeholder="Select a comic series...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: Interaction) -> None:
        selected_series_id = self.values[0]
        await interaction.response.send_message(
            f"You selected series ID `{selected_series_id}`.\n"
            "Please now use `/issue_lookup` to look up an issue.",
            ephemeral=True,
        )


class SeriesView(View):
    """Temporary view used by /series_lookup."""

    def __init__(self, series_results):
        super().__init__(timeout=60)
        self.add_item(SeriesSelect(series_results))


# ---------------------------------------------------------------------------
# Persistent issue lookup storage
# ---------------------------------------------------------------------------


def init_lookup_db() -> None:
    """Create the persistent issue-lookup table if it does not already exist."""
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
            CREATE TABLE IF NOT EXISTS issue_searches (
                search_id TEXT PRIMARY KEY,
                date_label TEXT NOT NULL,
                pages_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def create_lookup_record(user_id: int, issue_results):
    """Store lightweight Metron search results and return their lookup ID."""
    matches = []

    for result in issue_results:
        series = getattr(result, "series", None)
        year_began = getattr(series, "year_began", None)

        matches.append(
            {
                "id": int(result.id),
                "series_name": str(getattr(series, "name", "Unknown series")),
                "year_began": str(year_began) if year_began is not None else None,
            }
        )

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
    cached = await asyncio.to_thread(load_cached_issue, issue_id)
    if cached is not None:
        return cached

    # If a prefetch for this issue is already running, share that request.
    existing_task = _issue_fetch_tasks.get(issue_id)
    if existing_task is not None:
        return await existing_task

    async def fetch_and_cache() -> dict:
        issue = await asyncio.to_thread(metron.issue, issue_id)
        details = normalize_issue_details(issue)
        await asyncio.to_thread(store_cached_issue, issue_id, details)
        return details

    task = asyncio.create_task(fetch_and_cache())
    _issue_fetch_tasks[issue_id] = task

    try:
        return await task
    finally:
        if _issue_fetch_tasks.get(issue_id) is task:
            del _issue_fetch_tasks[issue_id]


def schedule_issue_prefetch(matches: list[dict], current_page: int, direction: str = "next") -> None:
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
            # Prefetch failures should never break the paginator itself.
            traceback.print_exc()

    task = asyncio.create_task(prefetch())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# ---------------------------------------------------------------------------
# Issue lookup embed and persistent pagination
# ---------------------------------------------------------------------------


async def build_issue_embed(
    match: dict,
    current_page: int,
    total_pages: int,
) -> discord.Embed:
    """Build an issue embed from SQLite cache, fetching Metron only if needed."""
    details = await get_issue_details(int(match["id"]))

    series_name = match.get("series_name") or "Unknown series"
    year_began = match.get("year_began")
    issue_number = details.get("number", "?")

    if year_began:
        title = f"{series_name} ({year_began}) #{issue_number}"
    else:
        title = f"{series_name} #{issue_number}"

    description = truncate(
        details.get("description") or "No description available.",
        EMBED_DESCRIPTION_LIMIT,
    )

    embed = discord.Embed(
        title=title,
        url=details.get("resource_url"),
        description=description,
        color=EMBED_COLOR,
    )

    if details.get("image_url"):
        embed.set_image(url=details["image_url"])

    embed.add_field(
        name="Publisher",
        value=details.get("publisher") or "Unknown",
        inline=True,
    )
    embed.add_field(
        name="Cover Date",
        value=details.get("cover_date") or "Unknown",
        inline=True,
    )
    embed.add_field(
        name="Store Date",
        value=details.get("store_date") or "Unknown",
        inline=True,
    )
    embed.add_field(
        name="Volume",
        value=details.get("volume") or "Unknown",
        inline=True,
    )
    embed.add_field(
        name="Pages",
        value=details.get("page_count") or "Unknown",
        inline=True,
    )

    price = details.get("price")
    embed.add_field(
        name="Price",
        value=f"${price}" if price is not None else "Unknown",
        inline=True,
    )

    story_titles = details.get("story_titles") or []
    if story_titles:
        embed.add_field(
            name="Stories",
            value=truncate("\n".join(story_titles), EMBED_FIELD_LIMIT),
            inline=False,
        )

    credits = details.get("credits") or []
    if credits:
        credit_lines = []

        for credit in credits:
            creator = credit.get("creator") or "Unknown"
            roles = credit.get("roles") or []

            if roles:
                credit_lines.append(f"**{creator}** — {', '.join(roles)}")
            else:
                credit_lines.append(creator)

        embed.add_field(
            name="Credits",
            value=truncate("\n".join(credit_lines), EMBED_FIELD_LIMIT),
            inline=False,
        )

    embed.set_footer(
        text=f"Match {current_page + 1}/{total_pages} • Metron ID: {match['id']}"
    )
    return embed


class IssuePageButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=(
        r"carmine:issue:"
        r"(?P<lookup_id>[0-9a-f]{16}):"
        r"(?P<page>[0-9]+):"
        r"(?P<direction>prev|next):"
        r"(?P<user_id>[0-9]+)"
    ),
):
    """Persistent Previous/Next button reconstructed from its custom ID."""

    def __init__(
        self,
        lookup_id: str,
        page: int,
        direction: str,
        user_id: int,
        *,
        disabled: bool = False,
    ):
        if direction == "prev":
            label = "Previous"
            emoji = "◀️"
        else:
            label = "Next"
            emoji = "▶️"

        custom_id = f"carmine:issue:{lookup_id}:{page}:{direction}:{user_id}"

        super().__init__(
            discord.ui.Button(
                label=label,
                emoji=emoji,
                style=discord.ButtonStyle.secondary,
                custom_id=custom_id,
                disabled=disabled,
            )
        )

        self.lookup_id = lookup_id
        self.page = page
        self.direction = direction
        self.user_id = user_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        """Reconstruct a dynamic button from the state encoded in its custom ID."""
        return cls(
            lookup_id=match["lookup_id"],
            page=int(match["page"]),
            direction=match["direction"],
            user_id=int(match["user_id"]),
            disabled=item.disabled,
        )

    async def interaction_check(self, interaction: Interaction) -> bool:
        """Allow only the user who created the lookup to control its paginator."""
        if interaction.user.id == self.user_id:
            return True

        await interaction.response.send_message(
            "Only the person who ran this lookup can use these buttons.",
            ephemeral=True,
        )
        return False

    async def callback(self, interaction: Interaction) -> None:
        """Load the requested page from SQLite/Metron and update the message."""
        await interaction.response.defer()

        try:
            record = await asyncio.to_thread(load_lookup_record, self.lookup_id)

            if record is None:
                await interaction.followup.send(
                    "This lookup is no longer stored by Carmine.",
                    ephemeral=True,
                )
                return

            stored_user_id, matches = record

            if interaction.user.id != stored_user_id:
                await interaction.followup.send(
                    "Only the person who ran this lookup can use these buttons.",
                    ephemeral=True,
                )
                return

            if not 0 <= self.page < len(matches):
                await interaction.followup.send(
                    "That issue page no longer exists.",
                    ephemeral=True,
                )
                return

            embed = await build_issue_embed(
                matches[self.page],
                self.page,
                len(matches),
            )
            view = build_issue_lookup_view(
                self.lookup_id,
                self.page,
                len(matches),
                stored_user_id,
            )

            await interaction.edit_original_response(embed=embed, view=view)

            # Follow the user's browsing direction so the next likely page is ready.
            schedule_issue_prefetch(matches, self.page, self.direction)

        except Exception as exc:
            traceback.print_exc()
            await interaction.followup.send(
                f"An error occurred while changing pages: {exc}",
                ephemeral=True,
            )


def build_issue_lookup_view(
    lookup_id: str,
    current_page: int,
    total_pages: int,
    user_id: int,
):
    """Build the persistent Previous/Next controls for an issue lookup."""
    view = View(timeout=None)

    if total_pages <= 1:
        return view
    previous_page = max(0, current_page - 1)
    next_page = min(total_pages - 1, current_page + 1)

    view.add_item(
        IssuePageButton(
            lookup_id,
            previous_page,
            "prev",
            user_id,
            disabled=current_page == 0,
        )
    )
    view.add_item(
        IssuePageButton(
            lookup_id,
            next_page,
            "next",
            user_id,
            disabled=current_page == total_pages - 1,
        )
    )

    return view


# ---------------------------------------------------------------------------
# Persistent /issues pagination
# ---------------------------------------------------------------------------


class IssueListPageButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=(
        r"carmine:issues:"
        r"(?P<search_id>[0-9a-f]{16}):"
        r"(?P<page>[0-9]+):"
        r"(?P<direction>prev|next)"
    ),
):
    """Persistent Previous/Next button for /issues result lists."""

    def __init__(
        self,
        search_id: str,
        page: int,
        direction: str,
        *,
        disabled: bool = False,
    ):
        if direction == "prev":
            label = "Previous"
            emoji = "◀️"
        else:
            label = "Next"
            emoji = "▶️"

        custom_id = f"carmine:issues:{search_id}:{page}:{direction}"

        super().__init__(
            discord.ui.Button(
                label=label,
                emoji=emoji,
                style=discord.ButtonStyle.secondary,
                custom_id=custom_id,
                disabled=disabled,
            )
        )

        self.search_id = search_id
        self.page = page
        self.direction = direction

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        """Reconstruct a /issues button from the state in its custom ID."""
        return cls(
            search_id=match["search_id"],
            page=int(match["page"]),
            direction=match["direction"],
            disabled=item.disabled,
        )

    async def callback(self, interaction: Interaction) -> None:
        """Load a stored result page and update the original Discord message."""
        await interaction.response.defer()

        try:
            record = await asyncio.to_thread(
                load_issue_search_record,
                self.search_id,
            )

            if record is None:
                await interaction.followup.send(
                    "This issue search is no longer stored by Carmine.",
                    ephemeral=True,
                )
                return

            date_label, pages = record

            if not 0 <= self.page < len(pages):
                await interaction.followup.send(
                    "That results page no longer exists.",
                    ephemeral=True,
                )
                return

            embed = build_issue_list_embed(
                pages[self.page],
                self.page,
                len(pages),
                date_label,
            )
            view = build_issue_list_view(
                self.search_id,
                self.page,
                len(pages),
            )

            await interaction.edit_original_response(embed=embed, view=view)

        except Exception as exc:
            traceback.print_exc()
            await interaction.followup.send(
                f"An error occurred while changing pages: {exc}",
                ephemeral=True,
            )


def build_issue_list_view(
    search_id: str,
    current_page: int,
    total_pages: int,
) -> View:
    """Build persistent Previous/Next controls for an /issues result list."""
    view = View(timeout=None)

    if total_pages <= 1:
        return view

    previous_page = max(0, current_page - 1)
    next_page = min(total_pages - 1, current_page + 1)

    view.add_item(
        IssueListPageButton(
            search_id,
            previous_page,
            "prev",
            disabled=current_page == 0,
        )
    )
    view.add_item(
        IssueListPageButton(
            search_id,
            next_page,
            "next",
            disabled=current_page == total_pages - 1,
        )
    )

    return view


def build_issue_list_embed(
    page_text: str,
    current_page: int,
    total_pages: int,
    date_label: str,
) -> discord.Embed:
    """Build one /issues result embed from a stored page string."""
    return discord.Embed(
        title=(
            f"Issues by {date_label} "
            f"(Page {current_page + 1}/{total_pages})"
        ),
        description=page_text,
        color=EMBED_COLOR,
    )


# ---------------------------------------------------------------------------
# Command helpers
# ---------------------------------------------------------------------------


def create_issue_list_pages(issues) -> list[str]:
    """Split /issues results into pages that fit comfortably in an embed."""
    pages = []
    page = "Here are the issues for your query: (Comic ID on the left)\n\n"

    for issue in issues:
        line = f"**{issue.id}**: {issue.issue_name}\n"

        if len(page) + len(line) > ISSUE_LIST_PAGE_LIMIT:
            pages.append(page)
            page = ""

        page += line

    if page:
        pages.append(page)

    return pages


def fetch_issues_by_cover_date(
    start: date,
    end: date,
    publisher: str,
):
    """Fetch issues by cover year, then apply the exact cover-date range locally."""
    matching_issues = []
    seen_issue_ids = set()

    for year in range(start.year, end.year + 1):
        year_results = metron.issues_list(
            {
                "cover_year": year,
                "publisher_name": publisher,
            }
        )

        for issue in year_results:
            cover_date = getattr(issue, "cover_date", None)
            issue_id = getattr(issue, "id", None)

            if (
                cover_date is not None
                and start <= cover_date <= end
                and issue_id not in seen_issue_ids
            ):
                matching_issues.append(issue)
                seen_issue_ids.add(issue_id)

    matching_issues.sort(
        key=lambda issue: (
            getattr(issue, "cover_date", date.max),
            str(getattr(issue, "issue_name", "")).casefold(),
        )
    )

    return matching_issues


def fetch_issues_by_release_date(
    start: date,
    end: date,
    publisher: str,
):
    """Fetch issues using Metron's store-date range filters."""
    issues = metron.issues_list(
        {
            "store_date_range_after": start.isoformat(),
            "store_date_range_before": end.isoformat(),
            "publisher_name": publisher,
        }
    )

    return sorted(
        issues,
        key=lambda issue: (
            getattr(issue, "store_date", date.max) or date.max,
            str(getattr(issue, "issue_name", "")).casefold(),
        ),
    )


async def handle_issues(
    interaction: Interaction,
    start_date: str,
    end_date: str,
    publisher: str,
    date_type: str = "cover",
) -> None:
    """Fetch a publisher/date range and paginate results with persistent buttons."""
    await interaction.response.defer()

    try:
        try:
            start = date.fromisoformat(start_date)
            end = date.fromisoformat(end_date)
        except ValueError:
            await interaction.followup.send(
                "Dates must use the YYYY-MM-DD format.",
                ephemeral=True,
            )
            return

        if end < start:
            await interaction.followup.send(
                "The end date must be the same as or later than the start date.",
                ephemeral=True,
            )
            return

        if date_type == "release":
            issues_list = await asyncio.to_thread(
                fetch_issues_by_release_date,
                start,
                end,
                publisher,
            )
            date_label = "Release Date"
        else:
            issues_list = await asyncio.to_thread(
                fetch_issues_by_cover_date,
                start,
                end,
                publisher,
            )
            date_label = "Cover Date"

        pages = create_issue_list_pages(issues_list)
        if not pages:
            pages = ["No issues were found for the given query."]

        embed = build_issue_list_embed(
            pages[0],
            0,
            len(pages),
            date_label,
        )

        if len(pages) <= 1:
            await interaction.followup.send(embed=embed)
            return

        search_id = await asyncio.to_thread(
            create_issue_search_record,
            date_label,
            pages,
        )
        view = build_issue_list_view(search_id, 0, len(pages))

        await interaction.followup.send(embed=embed, view=view)

    except Exception as exc:
        traceback.print_exc()
        await interaction.followup.send(f"An error occurred: {exc}")


async def handle_kryptonian(interaction: Interaction) -> None:
    """Send the local Superman image."""
    try:
        image = discord.File(SUPERMAN_IMAGE_PATH)
        await interaction.response.send_message(
            "The Man of Steel himself...",
            file=image,
        )
    except Exception as exc:
        error_message = f"An error occurred: {exc}"

        if interaction.response.is_done():
            await interaction.followup.send(error_message)
        else:
            await interaction.response.send_message(error_message)


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


@bot.tree.command(
    name="issues",
    description=(
        "Fetch comic issues given publisher and date range. "
        "Cover date is used by default."
    ),
)
@app_commands.describe(
    start_date="Start of the date range in YYYY-MM-DD format",
    end_date="End of the date range in YYYY-MM-DD format",
    publisher="Publisher name, such as Marvel or DC Comics",
    date_type="Date field to search; defaults to cover date",
)
@app_commands.choices(
    date_type=[
        app_commands.Choice(name="Cover date", value="cover"),
        app_commands.Choice(name="Release date", value="release"),
    ]
)
async def issues_slash(
    interaction: Interaction,
    start_date: str,
    end_date: str,
    publisher: str,
    date_type: str = "cover",
) -> None:
    """Slash-command entry point for date-range issue searches."""
    await handle_issues(
        interaction,
        start_date,
        end_date,
        publisher,
        date_type,
    )


@bot.tree.command(name="series_lookup", description="Search for a comic series title.")
@app_commands.describe(name="The name of the comic series")
async def series_lookup(interaction: Interaction, name: str) -> None:
    """Search Metron for series and present a selection menu."""
    await interaction.response.defer(thinking=True)

    try:
        series_results = await asyncio.to_thread(
            metron.series_list,
            {"series_name": name},
        )

        if not series_results:
            await interaction.followup.send(
                "No matching comic series found.",
                ephemeral=True,
            )
            return

        view = SeriesView(series_results)
        await interaction.followup.send(
            "Select the comic series you're looking for:",
            view=view,
            ephemeral=True,
        )

    except Exception as exc:
        traceback.print_exc()
        await interaction.followup.send(
            f"An error occurred: {exc}",
            ephemeral=True,
        )


@bot.tree.command(name="kryptonian", description="It's the Man of Tomorrow himself...")
async def kryptonian_slash(interaction: Interaction) -> None:
    """Send Superman."""
    await handle_kryptonian(interaction)


@bot.tree.command(
    name="issue_lookup",
    description="Look up a specific comic issue on Metron.",
)
@app_commands.describe(
    series="Name of the comic series",
    issue_number="Issue number, such as 1, 25, or 1.AU",
    year="Optional year the series began",
    publisher="Optional publisher name",
)
async def issue_lookup(
    interaction: Interaction,
    series: str,
    issue_number: str,
    year: int | None = None,
    publisher: str | None = None,
) -> None:
    """Find matching issues and display persistent paginated results."""
    await interaction.response.defer(thinking=True)

    try:
        filters = {
            "series_name": series,
            "number": issue_number,
        }

        if year is not None:
            filters["series_year_began"] = year

        if publisher:
            filters["publisher_name"] = publisher

        issue_results = list(
            await asyncio.to_thread(
                metron.issues_list,
                filters,
            )
        )

        if not issue_results:
            await interaction.followup.send(
                "No matching issues were found.",
                ephemeral=True,
            )
            return

        lookup_id, matches = await asyncio.to_thread(
            create_lookup_record,
            interaction.user.id,
            issue_results,
        )

        embed = await build_issue_embed(matches[0], 0, len(matches))
        view = build_issue_lookup_view(
            lookup_id,
            0,
            len(matches),
            interaction.user.id,
        )

        await interaction.followup.send(embed=embed, view=view)

        # Page 1 is visible now; prepare page 2 in the background.
        schedule_issue_prefetch(matches, 0, "next")

    except Exception as exc:
        traceback.print_exc()
        await interaction.followup.send(
            f"An error occurred: {exc}",
            ephemeral=True,
        )


@bot.tree.command(name="help", description="List Carmine's commands and what they do.")
async def help_command(interaction: Interaction) -> None:
    """Display Carmine's slash-command help."""
    embed = discord.Embed(
        title="Bot Commands",
        description="Here are the commands you can use:",
        color=EMBED_COLOR,
    )

    embed.add_field(
        name="/issues",
        value=(
            "Fetch issues from a publisher within a date range. "
            "Cover date is the default; release date is also available. "
            "Dates use YYYY-MM-DD."
        ),
        inline=False,
    )
    embed.add_field(
        name="/series_lookup",
        value="Search Metron for comic series by name.",
        inline=False,
    )
    embed.add_field(
        name="/issue_lookup",
        value="Look up a specific issue and browse matching results.",
        inline=False,
    )
    embed.add_field(
        name="/kryptonian",
        value="Summon the Man of Steel.",
        inline=False,
    )
    embed.add_field(
        name="/help",
        value="Display this command list.",
        inline=False,
    )

    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    bot.run(config.token)
