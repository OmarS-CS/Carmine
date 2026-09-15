"""Carmine Discord bot entrypoint."""

import discord
from discord.ext import commands

import config
from commands import register_commands
from database import init_lookup_db
from ui import IssueListPageButton, IssuePageButton, SeriesPageButton
from utils import logger


intents = discord.Intents.default()
intents.message_content = True


class CarmineBot(commands.Bot):
    """Discord bot with one-time startup setup."""

    async def setup_hook(self) -> None:
        """Initialize persistent state and sync slash commands once per startup."""
        init_lookup_db()
        self.add_dynamic_items(IssuePageButton)
        self.add_dynamic_items(IssueListPageButton)
        self.add_dynamic_items(SeriesPageButton)
        await self.tree.sync()
        logger.info(
            "Startup setup complete | dynamic items registered | slash commands synced"
        )


bot = CarmineBot(command_prefix="*", intents=intents, case_insensitive=True)
bot.remove_command("help")
register_commands(bot)


@bot.event
async def on_ready() -> None:
    """Log when Carmine has connected to Discord."""
    logger.info("Carmine is ready as %s", bot.user)


if __name__ == "__main__":
    bot.run(config.token)
