"""Carmine slash-command registration and command-specific helpers."""

import time
import traceback
from datetime import date

import discord
from discord import Interaction, app_commands
from discord.ext import commands

from database import (
    create_issue_search_record,
    create_lookup_record_from_matches,
    issue_lookup_search_key,
    load_cached_issue_lookup_search,
    load_cached_issue_year,
    load_cached_series_search,
    series_search_key,
    store_cached_issue_lookup_search,
    store_cached_issue_year,
    store_cached_series_search,
)
from metron_service import metron, metron_call, schedule_issue_prefetch
from settings import EMBED_COLOR, ISSUE_LIST_PAGE_LIMIT, SUPERMAN_IMAGE_PATH
from status import CommandStatus
from ui import (
    SeriesView,
    build_issue_embed,
    build_issue_list_embed,
    build_issue_list_view,
    build_issue_lookup_view,
)
from utils import db_call, log_elapsed, logger


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


async def fetch_issues_by_cover_date(
    start: date,
    end: date,
    publisher: str,
    progress_callback=None,
):
    """Fetch cached cover-year results, reporting progress before Metron misses."""
    matching_issues = []
    seen_issue_ids = set()
    cache_hits = 0
    cache_misses = 0

    years = list(range(start.year, end.year + 1))
    total_years = len(years)

    for year_index, year in enumerate(years, start=1):
        context = {"publisher": publisher, "cover_year": year}
        year_results = await db_call(
            "issue year cache read",
            load_cached_issue_year,
            publisher,
            year,
            context=context,
        )

        if year_results is None:
            cache_misses += 1
            logger.info(
                "[CACHE] issue year miss | publisher=%s, cover_year=%s",
                publisher,
                year,
            )

            if progress_callback is not None:
                await progress_callback(year, year_index, total_years)

            metron_results = await metron_call(
                "issues list by cover year",
                metron.issues_list,
                {
                    "cover_year": year,
                    "publisher_name": publisher,
                },
                context=context,
            )

            year_results = await db_call(
                "issue year cache write",
                store_cached_issue_year,
                publisher,
                year,
                metron_results,
                context={**context, "issues": len(metron_results)},
            )
        else:
            cache_hits += 1
            logger.info(
                "[CACHE] issue year hit | publisher=%s, cover_year=%s, issues=%s",
                publisher,
                year,
                len(year_results),
            )

        for issue in year_results:
            cover_date = issue.cover_date
            issue_id = issue.id

            if (
                cover_date is not None
                and start <= cover_date <= end
                and issue_id not in seen_issue_ids
            ):
                matching_issues.append(issue)
                seen_issue_ids.add(issue_id)

    matching_issues.sort(
        key=lambda issue: (
            issue.cover_date or date.max,
            issue.issue_name.casefold(),
        )
    )

    logger.info(
        "[CACHE] issue year summary | publisher=%s, hits=%s, misses=%s",
        publisher,
        cache_hits,
        cache_misses,
    )

    return matching_issues


async def fetch_issues_by_release_date(
    start: date,
    end: date,
    publisher: str,
):
    """Fetch issues using Metron's store-date range filters."""
    issues = await metron_call(
        "issues list by release date",
        metron.issues_list,
        {
            "store_date_range_after": start.isoformat(),
            "store_date_range_before": end.isoformat(),
            "publisher_name": publisher,
        },
        context={
            "publisher": publisher,
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
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
    command_started = time.perf_counter()
    command_context = {
        "publisher": publisher,
        "start": start_date,
        "end": end_date,
        "date_type": date_type,
    }
    status = CommandStatus(interaction)

    try:
        try:
            start = date.fromisoformat(start_date)
            end = date.fromisoformat(end_date)
        except ValueError:
            await interaction.response.send_message(
                "Dates must use the YYYY-MM-DD format.",
                ephemeral=True,
            )
            return

        if end < start:
            await interaction.response.send_message(
                "The end date must be the same as or later than the start date.",
                ephemeral=True,
            )
            return

        date_label = "Release Date" if date_type == "release" else "Cover Date"
        await status.start(
            "🔎 **Searching comic issues...**\n"
            f"**Publisher:** {publisher}\n"
            f"**Date type:** {date_label}\n"
            f"**Range:** {start_date} → {end_date}"
        )

        if date_type == "release":
            await status.update(
                "⏳ **Searching Metron...**\n"
                f"Looking for {publisher} issues by release date from "
                f"{start_date} → {end_date}."
            )
            issues_list = await fetch_issues_by_release_date(start, end, publisher)
        else:
            total_years = end.year - start.year + 1

            if total_years > 1:
                await status.update(
                    "🗂️ **Checking Carmine's cover-date cache...**\n"
                    f"{publisher} • {start.year}–{end.year} • {total_years} years"
                )

            async def report_cover_year_progress(
                year: int,
                year_index: int,
                year_count: int,
            ) -> None:
                if year_count == 1:
                    progress = f"Fetching **{year}** from Metron..."
                else:
                    progress = (
                        f"Fetching **{year}** from Metron "
                        f"(year {year_index}/{year_count})..."
                    )

                await status.update(
                    "⏳ **Searching cover-date records...**\n"
                    f"{progress}"
                )

            issues_list = await fetch_issues_by_cover_date(
                start,
                end,
                publisher,
                progress_callback=report_cover_year_progress,
            )

        processing_started = time.perf_counter()
        pages = create_issue_list_pages(issues_list)
        log_elapsed(
            "LOCAL",
            "build /issues pages",
            processing_started,
            context={"issues": len(issues_list)},
        )
        if not pages:
            pages = ["No issues were found for the given query."]

        embed = build_issue_list_embed(pages[0], 0, len(pages), date_label)

        if len(pages) <= 1:
            await status.finish(embed=embed)
            return

        search_id = await db_call(
            "issue search write",
            create_issue_search_record,
            date_label,
            pages,
            context={"pages": len(pages)},
        )
        view = build_issue_list_view(search_id, 0, len(pages))

        await status.finish(embed=embed, view=view)

    except Exception as exc:
        traceback.print_exc()
        await status.error(f"An error occurred: {exc}")
    finally:
        log_elapsed(
            "COMMAND",
            "/issues",
            command_started,
            context=command_context,
        )


async def handle_kryptonian(interaction: Interaction) -> None:
    """Send the local Superman image."""
    command_started = time.perf_counter()

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
    finally:
        log_elapsed("COMMAND", "/kryptonian", command_started)


def register_commands(bot: commands.Bot) -> None:
    """Register Carmine's slash commands on the supplied bot instance."""

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
        await handle_issues(interaction, start_date, end_date, publisher, date_type)

    @bot.tree.command(
        name="series_lookup",
        description="Search for a comic series title.",
    )
    @app_commands.describe(name="The name of the comic series")
    async def series_lookup(interaction: Interaction, name: str) -> None:
        command_started = time.perf_counter()
        command_context = {"name": name}
        status = CommandStatus(interaction, ephemeral=True)

        await status.start(
            "🔎 **Searching for comic series...**\n"
            f"Looking for titles matching **{name}**."
        )

        try:
            search_key = series_search_key(name)
            series_results = await db_call(
                "series search cache read",
                load_cached_series_search,
                search_key,
                context=command_context,
            )

            if series_results is None:
                logger.info("[CACHE] series search miss | name=%s", name)
                await status.update(
                    "⏳ **Searching Metron for comic series...**\n"
                    f"Looking for titles matching **{name}**."
                )

                metron_results = await metron_call(
                    "series list",
                    metron.series_list,
                    {"name": name},
                    context=command_context,
                )
                series_results = await db_call(
                    "series search cache write",
                    store_cached_series_search,
                    search_key,
                    metron_results,
                    context={**command_context, "matches": len(metron_results)},
                )
            else:
                logger.info(
                    "[CACHE] series search hit | name=%s, matches=%s",
                    name,
                    len(series_results),
                )

            if not series_results:
                await status.finish(content="No matching comic series found.")
                return

            view = SeriesView(series_results)
            await status.finish(
                content=(
                    f"Found **{len(series_results)}** matching series. "
                    "Select the one you're looking for:"
                ),
                view=view,
            )

        except Exception as exc:
            traceback.print_exc()
            await status.error(f"An error occurred: {exc}")
        finally:
            log_elapsed(
                "COMMAND",
                "/series_lookup",
                command_started,
                context=command_context,
            )

    @bot.tree.command(
        name="kryptonian",
        description="It's the Man of Tomorrow himself...",
    )
    async def kryptonian_slash(interaction: Interaction) -> None:
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
        command_started = time.perf_counter()
        command_context = {
            "series": series,
            "issue_number": issue_number,
            "year": year,
            "publisher": publisher,
        }
        status = CommandStatus(interaction)

        filter_details = []
        if year is not None:
            filter_details.append(f"year {year}")
        if publisher:
            filter_details.append(publisher)

        filter_text = (
            f" • {' • '.join(filter_details)}"
            if filter_details
            else ""
        )

        await status.start(
            "🔎 **Searching for an issue...**\n"
            f"**{series} #{issue_number}**{filter_text}"
        )

        try:
            search_key = issue_lookup_search_key(
                series,
                issue_number,
                year,
                publisher,
            )
            matches = await db_call(
                "issue lookup search cache read",
                load_cached_issue_lookup_search,
                search_key,
                context=command_context,
            )

            if matches is None:
                logger.info(
                    "[CACHE] issue lookup search miss | series=%s, issue_number=%s, "
                    "year=%s, publisher=%s",
                    series,
                    issue_number,
                    year,
                    publisher,
                )
                await status.update(
                    "⏳ **Searching Metron for an issue...**\n"
                    f"**{series} #{issue_number}**{filter_text}"
                )

                filters = {
                    "series_name": series,
                    "number": issue_number,
                }

                if year is not None:
                    filters["series_year_began"] = year

                if publisher:
                    filters["publisher_name"] = publisher

                issue_results = list(
                    await metron_call(
                        "issue lookup search",
                        metron.issues_list,
                        filters,
                        context=command_context,
                    )
                )
                matches = await db_call(
                    "issue lookup search cache write",
                    store_cached_issue_lookup_search,
                    search_key,
                    issue_results,
                    context={**command_context, "matches": len(issue_results)},
                )
            else:
                logger.info(
                    "[CACHE] issue lookup search hit | series=%s, issue_number=%s, "
                    "year=%s, publisher=%s, matches=%s",
                    series,
                    issue_number,
                    year,
                    publisher,
                    len(matches),
                )

            if not matches:
                await status.fail_ephemeral("No matching issues were found.")
                return

            await status.update(
                f"📚 **Found {len(matches)} matching issue"
                f"{'s' if len(matches) != 1 else ''}.**\n"
                "Loading issue details..."
            )

            lookup_id, matches = await db_call(
                "issue lookup write",
                create_lookup_record_from_matches,
                interaction.user.id,
                matches,
                context={"matches": len(matches)},
            )

            embed = await build_issue_embed(matches[0], 0, len(matches))
            view = build_issue_lookup_view(
                lookup_id,
                0,
                len(matches),
                interaction.user.id,
            )

            await status.finish(embed=embed, view=view)
            schedule_issue_prefetch(matches, 0, "next")

        except Exception as exc:
            traceback.print_exc()
            await status.fail_ephemeral(f"An error occurred: {exc}")
        finally:
            log_elapsed(
                "COMMAND",
                "/issue_lookup",
                command_started,
                context=command_context,
            )

    @bot.tree.command(
        name="help",
        description="List Carmine's commands and what they do.",
    )
    async def help_command(interaction: Interaction) -> None:
        command_started = time.perf_counter()
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
        log_elapsed("COMMAND", "/help", command_started)
