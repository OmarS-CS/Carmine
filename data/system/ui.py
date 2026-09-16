"""Discord embeds, dropdowns, and persistent pagination components."""

import time
import traceback
from collections.abc import Awaitable, Callable

import discord
from discord import Interaction
from discord.ui import View

from .database import (
    load_issue_search_record,
    load_lookup_record,
    load_series_lookup_record,
)
from .metron_service import (
    get_issue_details,
    get_series_details,
    schedule_issue_prefetch,
    schedule_series_prefetch,
)
from .settings import (
    EMBED_COLOR,
    EMBED_DESCRIPTION_LIMIT,
    EMBED_FIELD_LIMIT,
    ISSUE_SEARCH_FILTER_PAGE_SIZE,
)
from .utils import db_call, log_elapsed, truncate


async def build_series_embed(
    match: dict,
    current_page: int,
    total_pages: int,
) -> discord.Embed:
    """Build one detailed series embed, using the persistent series cache."""
    details = await get_series_details(int(match["id"]))

    title = details.get("name") or match.get("display_name") or "Unknown series"
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

    embed.add_field(
        name="Publisher",
        value=details.get("publisher") or "Unknown",
        inline=True,
    )
    embed.add_field(
        name="Imprint",
        value=details.get("imprint") or "None",
        inline=True,
    )
    embed.add_field(
        name="Series Type",
        value=details.get("series_type") or "Unknown",
        inline=True,
    )

    status = details.get("status") or "Unknown"
    if isinstance(status, str):
        status = status.replace("_", " ").title()

    embed.add_field(name="Status", value=status, inline=True)

    year_began = details.get("year_began")
    year_end = details.get("year_end")
    if year_began is None:
        years = "Unknown"
    elif year_end is not None:
        years = f"{year_began}–{year_end}"
    else:
        years = str(year_began)

    embed.add_field(name="Years", value=years, inline=True)
    embed.add_field(
        name="Volume",
        value=(
            str(details.get("volume"))
            if details.get("volume") is not None
            else "Unknown"
        ),
        inline=True,
    )
    embed.add_field(
        name="Issues",
        value=(
            str(details.get("issue_count"))
            if details.get("issue_count") is not None
            else "Unknown"
        ),
        inline=True,
    )

    language = details.get("language")
    if language:
        embed.add_field(name="Language", value=str(language).upper(), inline=True)

    genres = details.get("genres") or []
    if genres:
        embed.add_field(
            name="Genres",
            value=truncate(", ".join(genres), EMBED_FIELD_LIMIT),
            inline=False,
        )

    alt_names = details.get("alt_names") or []
    if alt_names:
        embed.add_field(
            name="Alternate Names",
            value=truncate("\n".join(alt_names), EMBED_FIELD_LIMIT),
            inline=False,
        )

    associated = details.get("associated") or []
    if associated:
        associated_names = [
            item.get("name") or f"Series {item.get('id')}"
            for item in associated
        ]
        embed.add_field(
            name="Associated Series",
            value=truncate("\n".join(associated_names), EMBED_FIELD_LIMIT),
            inline=False,
        )

    external_ids = []
    if details.get("cv_id") is not None:
        external_ids.append(f"Comic Vine: {details['cv_id']}")
    if details.get("gcd_id") is not None:
        external_ids.append(f"GCD: {details['gcd_id']}")
    if external_ids:
        embed.add_field(
            name="External IDs",
            value=" • ".join(external_ids),
            inline=False,
        )

    embed.set_footer(
        text=f"Match {current_page + 1}/{total_pages} • Metron ID: {match['id']}"
    )
    return embed


class SeriesPageButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=(
        r"carmine:series:"
        r"(?P<lookup_id>[0-9a-f]{16}):"
        r"(?P<page>[0-9]+):"
        r"(?P<direction>prev|next):"
        r"(?P<user_id>[0-9]+)"
    ),
):
    """Persistent Previous/Next button for detailed /series_lookup results."""

    def __init__(
        self,
        lookup_id: str,
        page: int,
        direction: str,
        user_id: int,
        *,
        disabled: bool = False,
    ):
        label = "Previous" if direction == "prev" else "Next"
        emoji = "◀️" if direction == "prev" else "▶️"

        custom_id = f"carmine:series:{lookup_id}:{page}:{direction}:{user_id}"
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
        """Reconstruct a series paginator button from its custom ID."""
        return cls(
            lookup_id=match["lookup_id"],
            page=int(match["page"]),
            direction=match["direction"],
            user_id=int(match["user_id"]),
            disabled=item.disabled,
        )

    async def interaction_check(self, interaction: Interaction) -> bool:
        """Allow only the user who created the lookup to control it."""
        if interaction.user.id == self.user_id:
            return True

        await interaction.response.send_message(
            "Only the person who ran this lookup can use these buttons.",
            ephemeral=True,
        )
        return False

    async def callback(self, interaction: Interaction) -> None:
        """Load the requested series page and update the original message."""
        page_started = time.perf_counter()
        page_context = {
            "lookup_id": self.lookup_id,
            "page": self.page + 1,
            "direction": self.direction,
        }
        await interaction.response.defer()

        try:
            record = await db_call(
                "series lookup read",
                load_series_lookup_record,
                self.lookup_id,
                context={"lookup_id": self.lookup_id},
            )

            if record is None:
                await interaction.followup.send(
                    "This series lookup is no longer stored by Carmine.",
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
                    "That series page no longer exists.",
                    ephemeral=True,
                )
                return

            embed = await build_series_embed(
                matches[self.page],
                self.page,
                len(matches),
            )
            view = build_series_lookup_view(
                self.lookup_id,
                self.page,
                len(matches),
                stored_user_id,
            )

            await interaction.edit_original_response(embed=embed, view=view)
            schedule_series_prefetch(matches, self.page, self.direction)

        except Exception as exc:
            traceback.print_exc()
            await interaction.followup.send(
                f"An error occurred while changing series pages: {exc}",
                ephemeral=True,
            )
        finally:
            log_elapsed(
                "UI",
                "/series_lookup page change",
                page_started,
                context=page_context,
            )


def build_series_lookup_view(
    lookup_id: str,
    current_page: int,
    total_pages: int,
    user_id: int,
) -> View:
    """Build persistent Previous/Next controls for a series lookup."""
    view = View(timeout=None)

    if total_pages <= 1:
        return view

    previous_page = max(0, current_page - 1)
    next_page = min(total_pages - 1, current_page + 1)

    view.add_item(
        SeriesPageButton(
            lookup_id,
            previous_page,
            "prev",
            user_id,
            disabled=current_page == 0,
        )
    )
    view.add_item(
        SeriesPageButton(
            lookup_id,
            next_page,
            "next",
            user_id,
            disabled=current_page == total_pages - 1,
        )
    )

    return view


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
        label = "Previous" if direction == "prev" else "Next"
        emoji = "◀️" if direction == "prev" else "▶️"

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
        page_started = time.perf_counter()
        page_context = {
            "lookup_id": self.lookup_id,
            "page": self.page + 1,
            "direction": self.direction,
        }
        await interaction.response.defer()

        try:
            record = await db_call(
                "issue lookup read",
                load_lookup_record,
                self.lookup_id,
                context={"lookup_id": self.lookup_id},
            )

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
            schedule_issue_prefetch(matches, self.page, self.direction)

        except Exception as exc:
            traceback.print_exc()
            await interaction.followup.send(
                f"An error occurred while changing pages: {exc}",
                ephemeral=True,
            )
        finally:
            log_elapsed(
                "UI",
                "/issue_lookup page change",
                page_started,
                context=page_context,
            )


def build_issue_lookup_view(
    lookup_id: str,
    current_page: int,
    total_pages: int,
    user_id: int,
) -> View:
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


class IssueListPageButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=(
        r"carmine:issues:"
        r"(?P<search_id>[0-9a-f]{16}):"
        r"(?P<page>[0-9]+):"
        r"(?P<direction>prev|next)"
    ),
):
    """Persistent Previous/Next button for stored issue-list results."""

    def __init__(
        self,
        search_id: str,
        page: int,
        direction: str,
        *,
        disabled: bool = False,
    ):
        label = "Previous" if direction == "prev" else "Next"
        emoji = "◀️" if direction == "prev" else "▶️"

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
        """Reconstruct an issue-list button from the state in its custom ID."""
        return cls(
            search_id=match["search_id"],
            page=int(match["page"]),
            direction=match["direction"],
            disabled=item.disabled,
        )

    async def callback(self, interaction: Interaction) -> None:
        """Load a stored result page and update the original Discord message."""
        page_started = time.perf_counter()
        page_context = {
            "search_id": self.search_id,
            "page": self.page + 1,
            "direction": self.direction,
        }
        await interaction.response.defer()

        try:
            record = await db_call(
                "issue search read",
                load_issue_search_record,
                self.search_id,
                context={"search_id": self.search_id},
            )

            if record is None:
                await interaction.followup.send(
                    "This issue search is no longer stored by Carmine.",
                    ephemeral=True,
                )
                return

            (
                date_label,
                query_text,
                pages,
                _page_entries,
                _creator_ids,
                _creator_names,
            ) = record

            if not 0 <= self.page < len(pages):
                await interaction.followup.send(
                    "That results page no longer exists.",
                    ephemeral=True,
                )
                return

            embed = await build_issue_list_embed(
                pages[self.page],
                self.page,
                len(pages),
                date_label,
                query_text,
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
        finally:
            log_elapsed(
                "UI",
                "issue list page change",
                page_started,
                context=page_context,
            )


def build_issue_list_view(
    search_id: str,
    current_page: int,
    total_pages: int,
) -> View:
    """Build persistent Previous/Next controls for an /issue_search result list."""
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


async def build_issue_list_embed(
    page_text: str,
    current_page: int,
    total_pages: int,
    date_label: str,
    query_text: str = "",
) -> discord.Embed:
    """Build one stored issue-list result embed with its resolved search query."""
    rendered_page = page_text.replace("**", "")
    description = rendered_page
    if query_text:
        description = f"**Search Query**\n{query_text}\n\n{rendered_page}"

    return discord.Embed(
        title=f"Issues by {date_label} (Page {current_page + 1}/{total_pages})",
        description=description,
        color=EMBED_COLOR,
    )


class IssueSearchFilterSelect(discord.ui.Select):
    """One temporary selector used to disambiguate an /issue_search filter."""

    PAGE_SIZE = ISSUE_SEARCH_FILTER_PAGE_SIZE

    def __init__(
        self,
        *,
        filter_key: str,
        filter_label: str,
        query: str,
        candidates: list[dict],
        row: int,
        page: int = 0,
        selected_value: str | int = "all",
    ) -> None:
        self.filter_key = filter_key
        self.page = page
        self.candidates = candidates

        page_count = max(1, (len(candidates) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        page = max(0, min(page, page_count - 1))
        self.page = page
        start = page * self.PAGE_SIZE
        end = min(len(candidates), start + self.PAGE_SIZE)
        page_candidates = candidates[start:end]

        options = [
            discord.SelectOption(
                label="Show all matching results",
                value="all",
                description=f'Use every match for "{query}"'[:100],
                default=(selected_value == "all"),
            )
        ]

        for candidate in page_candidates:
            candidate_value = str(candidate["id"])
            options.append(
                discord.SelectOption(
                    label=str(candidate["label"])[:100],
                    value=candidate_value,
                    description=(candidate.get("description") or "")[:100] or None,
                    default=(str(selected_value) == candidate_value),
                )
            )

        if page > 0:
            options.append(
                discord.SelectOption(
                    label="Previous matches",
                    value="__previous_page__",
                    description=f"Show match page {page} of {page_count}",
                )
            )
        if page < page_count - 1:
            options.append(
                discord.SelectOption(
                    label="Next matches",
                    value="__next_page__",
                    description=f"Show match page {page + 2} of {page_count}",
                )
            )

        placeholder = f"{filter_label}: {query}"
        if page_count > 1:
            placeholder += f" (matches {page + 1}/{page_count})"

        super().__init__(
            placeholder=placeholder[:150],
            min_values=1,
            max_values=1,
            options=options,
            row=row,
        )

    async def callback(self, interaction: Interaction) -> None:
        """Remember a choice or move this dropdown to another match page."""
        view = self.view
        if not isinstance(view, IssueSearchFilterView):
            await interaction.response.defer()
            return

        value = self.values[0]
        if value == "__previous_page__":
            await view.change_filter_page(interaction, self.filter_key, -1)
            return
        if value == "__next_page__":
            await view.change_filter_page(interaction, self.filter_key, 1)
            return

        view.selections[self.filter_key] = (
            "all" if value == "all" else int(value)
        )
        await interaction.response.defer()


class IssueSearchFilterView(View):
    """Temporary disambiguation controls shown before an /issue_search runs."""

    def __init__(
        self,
        *,
        user_id: int,
        filters: list[dict],
        on_submit: Callable[[Interaction, dict[str, str | int]], Awaitable[None]],
        on_page_change: Callable[[str, int, int], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(timeout=900)
        self.user_id = user_id
        self.on_submit = on_submit
        self.on_page_change = on_page_change
        self.filters = filters
        self.selections: dict[str, str | int] = {
            item["key"]: "all" for item in filters
        }
        self.filter_pages: dict[str, int] = {
            item["key"]: 0 for item in filters
        }
        self.message: discord.Message | None = None
        self._rebuild_selects()

    def _rebuild_selects(self) -> None:
        """Rebuild dropdowns while preserving the Search Issues button."""
        for child in list(self.children):
            if isinstance(child, IssueSearchFilterSelect):
                self.remove_item(child)

        for row, item in enumerate(self.filters):
            key = item["key"]
            self.add_item(
                IssueSearchFilterSelect(
                    filter_key=key,
                    filter_label=item["label"],
                    query=item["query"],
                    candidates=item["candidates"],
                    row=row,
                    page=self.filter_pages.get(key, 0),
                    selected_value=self.selections.get(key, "all"),
                )
            )

    async def change_filter_page(
        self,
        interaction: Interaction,
        filter_key: str,
        delta: int,
    ) -> None:
        """Move one dropdown between pages without losing other selections."""
        spec = next((item for item in self.filters if item["key"] == filter_key), None)
        if spec is None:
            await interaction.response.defer()
            return

        page_count = max(
            1,
            (len(spec["candidates"]) + IssueSearchFilterSelect.PAGE_SIZE - 1)
            // IssueSearchFilterSelect.PAGE_SIZE,
        )
        new_page = max(
            0,
            min(page_count - 1, self.filter_pages.get(filter_key, 0) + delta),
        )

        await interaction.response.defer()
        if self.on_page_change is not None:
            start = new_page * IssueSearchFilterSelect.PAGE_SIZE
            end = min(
                len(spec["candidates"]),
                start + IssueSearchFilterSelect.PAGE_SIZE,
            )
            await self.on_page_change(filter_key, start, end)

        self.filter_pages[filter_key] = new_page
        self._rebuild_selects()
        await interaction.edit_original_response(view=self)

    async def interaction_check(self, interaction: Interaction) -> bool:
        """Only the user who started the search may change its filters."""
        if interaction.user.id == self.user_id:
            return True

        await interaction.response.send_message(
            "Only the person who started this search can use these controls.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(
        label="Search Issues",
        style=discord.ButtonStyle.primary,
        row=4,
    )
    async def submit(
        self,
        interaction: Interaction,
        button: discord.ui.Button,
    ) -> None:
        """Run the final issue query with the user's resolved filter choices."""
        button.disabled = True
        await interaction.response.edit_message(
            content="**Searching comic issues...**",
            embed=None,
            view=None,
        )
        self.stop()
        await self.on_submit(interaction, dict(self.selections))

    async def on_timeout(self) -> None:
        """Disable stale temporary filter controls after 15 minutes."""
        for item in self.children:
            item.disabled = True

        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


def build_issue_search_filter_view(
    *,
    user_id: int,
    filters: list[dict],
    on_submit: Callable[[Interaction, dict[str, str | int]], Awaitable[None]],
    on_page_change: Callable[[str, int, int], Awaitable[None]] | None = None,
) -> IssueSearchFilterView:
    """Build the temporary disambiguation view for /issue_search."""
    return IssueSearchFilterView(
        user_id=user_id,
        filters=filters,
        on_submit=on_submit,
        on_page_change=on_page_change,
    )
