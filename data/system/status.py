"""User-facing command status helpers for long-running Discord interactions."""

import discord
from discord import Interaction


class CommandStatus:
    """Manage one slash command's temporary status message and final response.

    Long-running commands acknowledge Discord immediately with a normal message,
    then edit that same message as work progresses. This avoids leaving users with
    Discord's generic "thinking" indicator for the entire operation.
    """

    def __init__(self, interaction: Interaction, *, ephemeral: bool = False):
        self.interaction = interaction
        self.ephemeral = ephemeral

    async def start(self, message: str) -> None:
        """Send the initial status message as the interaction response."""
        if self.interaction.response.is_done():
            await self.interaction.edit_original_response(
                content=message,
                embed=None,
                view=None,
            )
            return

        await self.interaction.response.send_message(
            message,
            ephemeral=self.ephemeral,
        )

    async def update(self, message: str) -> None:
        """Replace the current status text without creating another message."""
        await self.interaction.edit_original_response(
            content=message,
            embed=None,
            view=None,
        )

    async def finish(
        self,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
    ) -> None:
        """Replace the status message with the command's final response."""
        if self.interaction.response.is_done():
            await self.interaction.edit_original_response(
                content=content,
                embed=embed,
                view=view,
            )
            return

        await self.interaction.response.send_message(
            content=content,
            embed=embed,
            view=view,
            ephemeral=self.ephemeral,
        )

    async def error(self, message: str) -> None:
        """Replace the current status with an error message."""
        if self.interaction.response.is_done():
            await self.interaction.edit_original_response(
                content=message,
                embed=None,
                view=None,
            )
            return

        await self.interaction.response.send_message(
            message,
            ephemeral=self.ephemeral,
        )

    async def fail_ephemeral(self, message: str) -> None:
        """Remove a public status message and report the failure privately."""
        if self.ephemeral:
            await self.error(message)
            return

        if self.interaction.response.is_done():
            try:
                await self.interaction.delete_original_response()
            except discord.HTTPException:
                # If the status message cannot be removed, at least replace it with
                # a concise failure rather than leaving a stale "Searching..." state.
                await self.interaction.edit_original_response(
                    content="The lookup could not be completed.",
                    embed=None,
                    view=None,
                )

            await self.interaction.followup.send(message, ephemeral=True)
            return

        await self.interaction.response.send_message(message, ephemeral=True)
