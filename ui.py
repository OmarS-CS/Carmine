"""Discord embeds, dropdowns, and persistent pagination components."""

import time
import traceback

import discord
from discord import Interaction, SelectOption
from discord.ui import Select, View

from database import load_issue_search_record, load_lookup_record
from metron_service import get_issue_details, schedule_issue_prefetch
from settings import EMBED_COLOR, EMBED_DESCRIPTION_LIMIT, EMBED_FIELD_LIMIT
from utils import db_call, log_elapsed, series_display_name, truncate


class SeriesSelect(Select):
    """Dropdown containing up to Discord's 25 allowed series choices."""

    def __init__(self, series_results):
        options = []

        for series in series_results[:25]:
            year_began = getattr(series, "year_began", None)
            description_parts = []

            if year_began is not None:
                description_parts.append(f"Started {year_began}")

            description_parts.append(f"Metron ID {series.id}")

            options.append(
                SelectOption(
                    label=series_display_name(series)[:100],
                    description=" • ".join(description_parts)[:100],
                    value=str(series.id),
                )
            )

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
        finally:
            log_elapsed(
                "UI",
                "/issues page change",
                page_started,
                context=page_context,
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
        title=f"Issues by {date_label} (Page {current_page + 1}/{total_pages})",
        description=page_text,
        color=EMBED_COLOR,
    )
