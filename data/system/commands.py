"""Carmine slash-command registration and command-specific helpers."""

import asyncio
import time
import traceback
from datetime import date

import discord
from discord import Interaction, app_commands
from discord.ext import commands

from .database import (
    create_issue_search_record,
    create_lookup_record_from_matches,
    create_series_lookup_record,
    issue_lookup_search_key,
    issue_year_search_key,
    load_cached_issue_lookup_search,
    load_cached_issue_year,
    load_cached_issue_year_search,
    load_cached_resource_search,
    load_cached_series_search,
    resource_search_key,
    series_search_key,
    store_cached_issue_lookup_search,
    store_cached_issue_year,
    store_cached_issue_year_search,
    store_cached_resource_search,
    store_cached_series_search,
)
from .metron_service import (
    metron,
    metron_call,
    schedule_issue_prefetch,
    schedule_series_prefetch,
)
from .settings import (
    EMBED_COLOR,
    ISSUE_LIST_PAGE_LIMIT,
    ISSUE_SEARCH_FILTER_PAGE_SIZE,
    SUPERMAN_IMAGE_PATH,
)
from .status import CommandStatus
from .ui import (
    build_issue_embed,
    build_issue_list_embed,
    build_issue_list_view,
    build_issue_search_filter_view,
    build_issue_lookup_view,
    build_series_embed,
    build_series_lookup_view,
)
from .utils import db_call, log_elapsed, logger


def create_issue_list_pages(issues) -> tuple[list[str], list[list[dict]]]:
    """Split /issue_search results into display pages and structured page entries."""
    pages: list[str] = []
    entry_pages: list[list[dict]] = []
    page = "Here are the issues for your query:\n\n"
    entries: list[dict] = []

    for issue in issues:
        issue_name = str(issue.issue_name)
        line = f"{issue_name}\n"

        if entries and len(page) + len(line) > ISSUE_LIST_PAGE_LIMIT:
            pages.append(page)
            entry_pages.append(entries)
            page = ""
            entries = []

        page += line
        entries.append({"id": int(issue.id), "issue_name": issue_name})

    if entries:
        pages.append(page)
        entry_pages.append(entries)

    return pages, entry_pages


async def _fetch_issue_filter_combinations(
    *,
    operation: str,
    base_filters: dict,
    creator_ids: list[int],
    character_ids: list[int],
    context: dict,
):
    """Fetch creator/character matches without a Cartesian-product explosion.

    One creator or character filter is queried directly. When "show all" resolves
    to several creators and several characters, Carmine unions each side and then
    intersects the resulting issue IDs locally. That changes C×K Metron queries
    into at most C+K queries while preserving AND semantics between the two fields.
    """
    creator_ids = sorted(set(creator_ids))
    character_ids = sorted(set(character_ids))

    if not creator_ids and not character_ids:
        return await metron_call(
            operation,
            metron.issues_list,
            base_filters,
            context=context,
        )

    if len(creator_ids) == 1 and len(character_ids) == 1:
        filters = {
            **base_filters,
            "creator_id": creator_ids[0],
            "character_id": character_ids[0],
        }
        return await metron_call(
            operation,
            metron.issues_list,
            filters,
            context={
                **context,
                "creator_id": creator_ids[0],
                "character_id": character_ids[0],
            },
        )

    async def fetch_union(filter_name: str, ids: list[int]):
        merged = {}
        for resource_id in ids:
            filters = {**base_filters, filter_name: resource_id}
            results = await metron_call(
                operation,
                metron.issues_list,
                filters,
                context={**context, filter_name: resource_id},
            )
            for issue in results:
                merged[int(issue.id)] = issue
        return merged

    if creator_ids and not character_ids:
        return list((await fetch_union("creator_id", creator_ids)).values())

    if character_ids and not creator_ids:
        return list((await fetch_union("character_id", character_ids)).values())

    creator_matches = await fetch_union("creator_id", creator_ids)
    character_matches = await fetch_union("character_id", character_ids)
    shared_ids = creator_matches.keys() & character_matches.keys()
    return [creator_matches[issue_id] for issue_id in shared_ids]


async def fetch_issues_by_cover_date(
    start: date,
    end: date,
    publisher: str | None,
    title: str | None,
    creator_ids: list[int] | None = None,
    character_ids: list[int] | None = None,
    *,
    publisher_id: int | None = None,
    series_id: int | None = None,
    progress_callback=None,
):
    """Fetch cover-year issue results with persistent caching."""
    creator_ids = creator_ids or []
    character_ids = character_ids or []
    matching_issues = []
    seen_issue_ids = set()
    cache_hits = 0
    cache_misses = 0

    years = list(range(start.year, end.year + 1))
    total_years = len(years)
    use_publisher_cache = (
        title is None
        and not creator_ids
        and not character_ids
        and publisher is not None
        and publisher_id is None
        and series_id is None
    )
    flexible_search_key = None
    if not use_publisher_cache:
        flexible_search_key = issue_year_search_key(
            publisher,
            title,
            creator_ids=creator_ids,
            character_ids=character_ids,
            publisher_id=publisher_id,
            series_id=series_id,
        )

    for year_index, year in enumerate(years, start=1):
        context = {
            "publisher": publisher,
            "title": title,
            "publisher_id": publisher_id,
            "series_id": series_id,
            "creator_ids": creator_ids,
            "character_ids": character_ids,
            "cover_year": year,
        }

        if use_publisher_cache:
            year_results = await db_call(
                "issue year cache read",
                load_cached_issue_year,
                publisher,
                year,
                context=context,
            )
        else:
            year_results = await db_call(
                "issue year search cache read",
                load_cached_issue_year_search,
                flexible_search_key,
                year,
                context=context,
            )

        if year_results is None:
            cache_misses += 1
            logger.info(
                "[CACHE] issue year miss | publisher=%s, title=%s, "
                "publisher_id=%s, series_id=%s, creator_ids=%s, "
                "character_ids=%s, cover_year=%s",
                publisher,
                title,
                publisher_id,
                series_id,
                creator_ids,
                character_ids,
                year,
            )

            if progress_callback is not None:
                await progress_callback(year, year_index, total_years)

            base_filters = {"cover_year": year}
            if publisher_id is not None:
                base_filters["publisher_id"] = publisher_id
            elif publisher:
                base_filters["publisher_name"] = publisher

            if series_id is not None:
                base_filters["series_id"] = series_id
            elif title:
                base_filters["series_name"] = title

            metron_results = await _fetch_issue_filter_combinations(
                operation="issues list by cover year",
                base_filters=base_filters,
                creator_ids=creator_ids,
                character_ids=character_ids,
                context=context,
            )

            if use_publisher_cache:
                year_results = await db_call(
                    "issue year cache write",
                    store_cached_issue_year,
                    publisher,
                    year,
                    metron_results,
                    context={**context, "issues": len(metron_results)},
                )
            else:
                year_results = await db_call(
                    "issue year search cache write",
                    store_cached_issue_year_search,
                    flexible_search_key,
                    year,
                    metron_results,
                    context={**context, "issues": len(metron_results)},
                )
        else:
            cache_hits += 1
            logger.info(
                "[CACHE] issue year hit | publisher=%s, title=%s, "
                "publisher_id=%s, series_id=%s, creator_ids=%s, "
                "character_ids=%s, cover_year=%s, issues=%s",
                publisher,
                title,
                publisher_id,
                series_id,
                creator_ids,
                character_ids,
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
        "[CACHE] issue year summary | publisher=%s, title=%s, "
        "publisher_id=%s, series_id=%s, creator_ids=%s, "
        "character_ids=%s, hits=%s, misses=%s",
        publisher,
        title,
        publisher_id,
        series_id,
        creator_ids,
        character_ids,
        cache_hits,
        cache_misses,
    )

    return matching_issues


async def fetch_issues_by_release_date(
    start: date,
    end: date,
    publisher: str | None,
    title: str | None,
    creator_ids: list[int] | None = None,
    character_ids: list[int] | None = None,
    *,
    publisher_id: int | None = None,
    series_id: int | None = None,
):
    """Fetch issues using Metron's release-date range and optional filters."""
    creator_ids = creator_ids or []
    character_ids = character_ids or []
    base_filters = {
        "store_date_range_after": start.isoformat(),
        "store_date_range_before": end.isoformat(),
    }

    if publisher_id is not None:
        base_filters["publisher_id"] = publisher_id
    elif publisher:
        base_filters["publisher_name"] = publisher

    if series_id is not None:
        base_filters["series_id"] = series_id
    elif title:
        base_filters["series_name"] = title

    context = {
        "publisher": publisher,
        "title": title,
        "publisher_id": publisher_id,
        "series_id": series_id,
        "creator_ids": creator_ids,
        "character_ids": character_ids,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    issues = await _fetch_issue_filter_combinations(
        operation="issues list by release date",
        base_filters=base_filters,
        creator_ids=creator_ids,
        character_ids=character_ids,
        context=context,
    )

    return sorted(
        issues,
        key=lambda issue: (
            getattr(issue, "store_date", date.max) or date.max,
            str(getattr(issue, "issue_name", "")).casefold(),
        ),
    )


def _issues_search_description(
    publisher: str | None,
    title: str | None,
    creator: str | None,
    character: str | None,
) -> str:
    """Build concise user-facing text for active /issue_search filters."""
    filters = []
    if publisher:
        filters.append(f"**Publisher:** {publisher}")
    if title:
        filters.append(f"**Series:** {title}")
    if creator:
        filters.append(f"**Creator:** {creator}")
    if character:
        filters.append(f"**Character:** {character}")
    return "\n".join(filters)


async def _execute_issue_search(
    interaction: Interaction,
    *,
    start: date,
    end: date,
    start_date: str,
    end_date: str,
    publisher: str | None,
    title: str | None,
    creator: str | None,
    character: str | None,
    date_type: str,
    selections: dict[str, str | int],
    candidate_sets: dict[str, list],
) -> None:
    """Run /issue_search after the user has resolved ambiguous filters."""
    execution_started = time.perf_counter()
    status = CommandStatus(interaction)

    try:
        publisher_id: int | None = None
        series_id: int | None = None
        creator_ids: list[int] = []
        creator_names: list[str] = []
        character_ids: list[int] = []
        display_filters: list[str] = []
        query_lines: list[str] = []

        if publisher:
            selection = selections.get("publisher", "all")
            if selection == "all":
                display_filters.append(f'Publisher: all matches for "{publisher}"')
                query_lines.append(f"**Publisher:** {publisher}")
            else:
                publisher_id = int(selection)
                selected = next(
                    (item for item in candidate_sets["publisher"] if int(item.id) == publisher_id),
                    None,
                )
                selected_name = selected.name if selected is not None else publisher
                display_filters.append(f"Publisher: {selected_name}")
                query_lines.append(f"**Publisher:** {selected_name}")

        if title:
            selection = selections.get("title", "all")
            if selection == "all":
                display_filters.append(f'Series: all matches for "{title}"')
                query_lines.append(f"**Series:** {title}")
            else:
                series_id = int(selection)
                selected = next(
                    (item for item in candidate_sets["title"] if int(item.id) == series_id),
                    None,
                )
                selected_name = (
                    selected.display_name if selected is not None else title
                )
                display_filters.append(f"Series: {selected_name}")
                query_lines.append(f"**Series:** {selected_name}")

        if creator:
            selection = selections.get("creator", "all")
            if selection == "all":
                creator_ids = sorted(
                    {int(item.id) for item in candidate_sets["creator"]}
                )
                creator_names = sorted(
                    {str(item.name) for item in candidate_sets["creator"]}
                )
                display_filters.append(f'Creator: all matches for "{creator}"')
                query_lines.append(f"**Creator:** {creator}")
            else:
                creator_ids = [int(selection)]
                selected = next(
                    (item for item in candidate_sets["creator"] if int(item.id) == creator_ids[0]),
                    None,
                )
                selected_name = selected.name if selected is not None else creator
                creator_names = [str(selected_name)]
                display_filters.append(f"Creator: {selected_name}")
                query_lines.append(f"**Creator:** {selected_name}")

        if character:
            selection = selections.get("character", "all")
            if selection == "all":
                character_ids = sorted(
                    {int(item.id) for item in candidate_sets["character"]}
                )
                display_filters.append(f'Character: all matches for "{character}"')
                query_lines.append(f"**Character:** {character}")
            else:
                character_ids = [int(selection)]
                selected = next(
                    (item for item in candidate_sets["character"] if int(item.id) == character_ids[0]),
                    None,
                )
                selected_name = selected.name if selected is not None else character
                display_filters.append(f"Character: {selected_name}")
                query_lines.append(f"**Character:** {selected_name}")

        date_label = "Release Date" if date_type == "release" else "Cover Date"
        query_lines.append(
            f"**Date range:** {start_date} → {end_date} ({date_label})"
        )
        query_text = "\n".join(query_lines)
        resolved_text = "\n".join(f"**{item}**" for item in display_filters)
        await status.update(
            "**Searching comic issues...**\n"
            f"{resolved_text}\n"
            f"**Date type:** {date_label}\n"
            f"**Range:** {start_date} → {end_date}"
        )

        if date_type == "release":
            issues_list = await fetch_issues_by_release_date(
                start,
                end,
                publisher,
                title,
                creator_ids,
                character_ids,
                publisher_id=publisher_id,
                series_id=series_id,
            )
        else:
            total_years = end.year - start.year + 1

            if total_years > 1:
                await status.update(
                    "**Checking Carmine's cover-date cache...**\n"
                    f"{resolved_text}\n"
                    f"**Years:** {start.year}–{end.year} ({total_years})"
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
                    "**Searching cover-date records...**\n"
                    f"{progress}"
                )

            issues_list = await fetch_issues_by_cover_date(
                start,
                end,
                publisher,
                title,
                creator_ids,
                character_ids,
                publisher_id=publisher_id,
                series_id=series_id,
                progress_callback=report_cover_year_progress,
            )

        processing_started = time.perf_counter()
        pages, page_entries = create_issue_list_pages(issues_list)
        log_elapsed(
            "LOCAL",
            "build /issue_search pages",
            processing_started,
            context={"issues": len(issues_list)},
        )
        if not pages:
            pages = ["No issues were found for the given query."]
            page_entries = [[]]

        embed = await build_issue_list_embed(
            pages[0],
            0,
            len(pages),
            date_label,
            query_text,
        )

        if len(pages) <= 1:
            await status.finish(embed=embed)
            return

        search_id = await db_call(
            "issue search write",
            create_issue_search_record,
            date_label,
            query_text,
            pages,
            page_entries,
            creator_ids,
            creator_names,
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
            "/issue_search execution",
            execution_started,
            context={
                "publisher": publisher,
                "title": title,
                "creator": creator,
                "character": character,
                "start": start_date,
                "end": end_date,
                "date_type": date_type,
            },
        )


async def handle_issue_search(
    interaction: Interaction,
    start_date: str,
    end_date: str,
    publisher: str | None = None,
    title: str | None = None,
    creator: str | None = None,
    character: str | None = None,
    date_type: str = "cover",
) -> None:
    """Resolve user-entered filters, then let the user disambiguate them."""
    command_started = time.perf_counter()

    publisher = publisher.strip() if publisher else None
    title = title.strip() if title else None
    creator = creator.strip() if creator else None
    character = character.strip() if character else None
    command_context = {
        "publisher": publisher,
        "title": title,
        "creator": creator,
        "character": character,
        "start": start_date,
        "end": end_date,
        "date_type": date_type,
    }
    status = CommandStatus(interaction)

    try:
        if not any((publisher, title, creator, character)):
            await interaction.response.send_message(
                "Provide at least one of publisher, title, creator, or character.",
                ephemeral=True,
            )
            return

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
        search_description = _issues_search_description(
            publisher,
            title,
            creator,
            character,
        )
        await status.start(
            "**Finding matching filters...**\n"
            f"{search_description}\n"
            f"**Date type:** {date_label}\n"
            f"**Range:** {start_date} → {end_date}"
        )

        candidate_sets: dict[str, list] = {}
        filter_specs: list[dict] = []

        if publisher:
            results, _ = await _search_named_resources(
                resource_type="publisher",
                name=publisher,
                status=status,
            )
            if not results:
                await status.fail_ephemeral(
                    f"No publishers matching **{publisher}** were found."
                )
                return
            candidate_sets["publisher"] = results
            filter_specs.append(
                {
                    "key": "publisher",
                    "label": "Publisher",
                    "query": publisher,
                    "total_candidates": len(results),
                    "candidates": [
                        {
                            "id": int(item.id),
                            "label": item.name,
                            "description": f"Metron ID {item.id}",
                        }
                        for item in results
                    ],
                }
            )

        if title:
            results = await _search_series_filter_candidates(title, status=status)
            if not results:
                await status.fail_ephemeral(
                    f"No series matching **{title}** were found."
                )
                return
            candidate_sets["title"] = results
            filter_specs.append(
                {
                    "key": "title",
                    "label": "Series",
                    "query": title,
                    "total_candidates": len(results),
                    "candidates": [
                        {
                            "id": int(item.id),
                            "label": item.display_name,
                            "description": (
                                f"Started {item.year_began} • Metron ID {item.id}"
                                if item.year_began is not None
                                else f"Metron ID {item.id}"
                            ),
                        }
                        for item in results
                    ],
                }
            )

        if creator:
            results, _ = await _search_named_resources(
                resource_type="creator",
                name=creator,
                status=status,
            )
            if not results:
                await status.fail_ephemeral(
                    f"No creators matching **{creator}** were found."
                )
                return

            candidate_sets["creator"] = results
            filter_specs.append(
                {
                    "key": "creator",
                    "label": "Creator",
                    "query": creator,
                    "total_candidates": len(results),
                    "candidates": [
                        {
                            "id": int(item.id),
                            "label": item.name,
                            "description": f"Metron ID {item.id}",
                        }
                        for item in results
                    ],
                }
            )

        if character:
            results, _ = await _search_named_resources(
                resource_type="character",
                name=character,
                status=status,
            )
            if not results:
                await status.fail_ephemeral(
                    f"No characters matching **{character}** were found."
                )
                return

            candidate_sets["character"] = results

            normalized_query = " ".join(character.casefold().split())

            def character_sort_key(item):
                item_name = str(item.name)
                normalized_name = " ".join(item_name.casefold().split())
                return (
                    0 if normalized_name == normalized_query else 1,
                    normalized_name,
                    int(item.id),
                )

            ranked_characters = sorted(results, key=character_sort_key)
            character_candidates = [
                {
                    "id": int(item.id),
                    "label": str(item.name),
                    "description": f"Metron ID {item.id}",
                }
                for item in ranked_characters
            ]

            filter_specs.append(
                {
                    "key": "character",
                    "label": "Character",
                    "query": character,
                    "total_candidates": len(results),
                    "candidates": character_candidates,
                }
            )

        async def submit_search(
            component_interaction: Interaction,
            selections: dict[str, str | int],
        ) -> None:
            await _execute_issue_search(
                component_interaction,
                start=start,
                end=end,
                start_date=start_date,
                end_date=end_date,
                publisher=publisher,
                title=title,
                creator=creator,
                character=character,
                date_type=date_type,
                selections=selections,
                candidate_sets=candidate_sets,
            )

        view = build_issue_search_filter_view(
            user_id=interaction.user.id,
            filters=filter_specs,
            on_submit=submit_search,
        )

        paged_note = any(
            len(spec["candidates"]) > ISSUE_SEARCH_FILTER_PAGE_SIZE
            for spec in filter_specs
        )
        note = (
            "\nMenus with many matches include **Previous matches** and "
            "**Next matches** entries so every specific result can be reached."
            if paged_note
            else ""
        )
        await status.finish(
            content=(
                "**Choose how each filter should be applied.**\n"
                "Each menu defaults to **Show all matching results**. "
                "Choose a specific match only when you want to narrow that filter, "
                "then press **Search Issues**."
                f"{note}"
            ),
            view=view,
        )
        view.message = await interaction.original_response()

    except Exception as exc:
        traceback.print_exc()
        await status.error(f"An error occurred: {exc}")
    finally:
        log_elapsed(
            "COMMAND",
            "/issue_search filter setup",
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



async def _search_named_resources(
    *,
    resource_type: str,
    name: str,
    status: CommandStatus | None = None,
):
    """Search publisher/creator/character resources with a persistent cache."""
    search_key = resource_search_key(name)
    context = {"resource_type": resource_type, "name": name}
    cached = await db_call(
        f"{resource_type} search cache read",
        load_cached_resource_search,
        resource_type,
        search_key,
        context=context,
    )
    if cached is not None:
        logger.info(
            "[CACHE] %s search hit | name=%s, matches=%s",
            resource_type,
            name,
            len(cached),
        )
        return cached, True

    logger.info("[CACHE] %s search miss | name=%s", resource_type, name)
    nouns = {
        "publisher": "publishers",
        "creator": "creators",
        "character": "characters",
    }
    methods = {
        "publisher": metron.publishers_list,
        "creator": metron.creators_list,
        "character": metron.characters_list,
    }

    if resource_type not in methods:
        raise ValueError(f"Unsupported named resource type: {resource_type}")

    if status is not None:
        await status.update(
            f"**Searching Metron for {nouns[resource_type]}...**\n"
            f"Looking for **{name}**."
        )

    results = await metron_call(
        f"{resource_type} list",
        methods[resource_type],
        {"name": name},
        context=context,
    )
    cached = await db_call(
        f"{resource_type} search cache write",
        store_cached_resource_search,
        resource_type,
        search_key,
        results,
        context={**context, "matches": len(results)},
    )
    return cached, False


async def _search_series_filter_candidates(
    name: str,
    *,
    status: CommandStatus | None = None,
):
    """Search series candidates for /issue_search using the shared series cache."""
    search_key = series_search_key(name)
    context = {"name": name, "purpose": "issue_search_filter"}
    cached = await db_call(
        "series search cache read",
        load_cached_series_search,
        search_key,
        context=context,
    )
    if cached is not None:
        logger.info(
            "[CACHE] series filter search hit | name=%s, matches=%s",
            name,
            len(cached),
        )
        return cached

    logger.info("[CACHE] series filter search miss | name=%s", name)
    if status is not None:
        await status.update(
            "**Searching Metron for comic series...**\n"
            f"Looking for titles matching **{name}**."
        )

    results = await metron_call(
        "series list",
        metron.series_list,
        {"name": name},
        context=context,
    )
    return await db_call(
        "series search cache write",
        store_cached_series_search,
        search_key,
        results,
        context={**context, "matches": len(results)},
    )


def register_commands(bot: commands.Bot) -> None:
    """Register Carmine's slash commands on the supplied bot instance."""

    @bot.tree.command(
        name="issue_search",
        description=(
            "Search issues by publisher, title, creator, character, and date range."
        ),
    )
    @app_commands.describe(
        start_date="Start of the date range in YYYY-MM-DD format",
        end_date="End of the date range in YYYY-MM-DD format",
        publisher="Optional publisher name, such as Marvel or DC Comics",
        title="Optional series title, such as Amazing Spider-Man",
        creator="Optional creator name, such as Stan Lee or Jack Kirby",
        character="Optional character name, such as Spider-Man or Superman",
        date_type="Date field to search; defaults to cover date",
    )
    @app_commands.choices(
        date_type=[
            app_commands.Choice(name="Cover date", value="cover"),
            app_commands.Choice(name="Release date", value="release"),
        ]
    )
    async def issue_search_slash(
        interaction: Interaction,
        start_date: str,
        end_date: str,
        publisher: str | None = None,
        title: str | None = None,
        creator: str | None = None,
        character: str | None = None,
        date_type: str = "cover",
    ) -> None:
        await handle_issue_search(
            interaction,
            start_date,
            end_date,
            publisher,
            title,
            creator,
            character,
            date_type,
        )

    @bot.tree.command(
        name="series_lookup",
        description="Look up comic series information on Metron.",
    )
    @app_commands.describe(
        name="The name of the comic series",
        starting_year="Optional year the series began",
    )
    async def series_lookup(
        interaction: Interaction,
        name: str,
        starting_year: int | None = None,
    ) -> None:
        command_started = time.perf_counter()
        command_context = {"name": name, "starting_year": starting_year}
        status = CommandStatus(interaction)

        year_text = (
            f" starting in **{starting_year}**"
            if starting_year is not None
            else ""
        )
        await status.start(
            "**Searching for comic series...**\n"
            f"Looking for titles matching **{name}**{year_text}."
        )

        try:
            search_key = series_search_key(name, starting_year)
            series_results = await db_call(
                "series search cache read",
                load_cached_series_search,
                search_key,
                context=command_context,
            )

            if series_results is None:
                logger.info(
                    "[CACHE] series search miss | name=%s, starting_year=%s",
                    name,
                    starting_year,
                )
                await status.update(
                    "**Searching Metron for comic series...**\n"
                    f"Looking for titles matching **{name}**{year_text}."
                )

                filters = {"name": name}
                if starting_year is not None:
                    filters["year_began"] = starting_year

                metron_results = await metron_call(
                    "series list",
                    metron.series_list,
                    filters,
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
                    "[CACHE] series search hit | name=%s, starting_year=%s, matches=%s",
                    name,
                    starting_year,
                    len(series_results),
                )

            if not series_results:
                await status.fail_ephemeral("No matching comic series found.")
                return

            await status.update(
                f"**Found {len(series_results)} matching series.**\n"
                "Loading series details..."
            )

            lookup_id, matches = await db_call(
                "series lookup write",
                create_series_lookup_record,
                interaction.user.id,
                series_results,
                context={**command_context, "matches": len(series_results)},
            )

            embed = await build_series_embed(matches[0], 0, len(matches))
            view = build_series_lookup_view(
                lookup_id,
                0,
                len(matches),
                interaction.user.id,
            )

            await status.finish(embed=embed, view=view)
            schedule_series_prefetch(matches, 0, "next")

        except Exception as exc:
            traceback.print_exc()
            await status.fail_ephemeral(f"An error occurred: {exc}")
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
            "**Searching for an issue...**\n"
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
                    "**Searching Metron for an issue...**\n"
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
                f"**Found {len(matches)} matching issue"
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
            name="/issue_search",
            value=(
                "Search issues by publisher, series title, creator, character, or "
                "any combination. Carmine lets you choose a specific match or "
                "show all matching results before searching. Cover date is the "
                "default; release date is also available. Dates use YYYY-MM-DD."
            ),
            inline=False,
        )
        embed.add_field(
            name="/series_lookup",
            value=(
                "Look up a comic series, optionally filter by starting year, and "
                "browse detailed information for matching runs."
            ),
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
