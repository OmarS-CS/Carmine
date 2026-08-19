import mokkari
import config
import discord
from discord import app_commands, Interaction, SelectOption
from discord.ext import commands
from discord.ui import View, Select
import asyncio

m = mokkari.api(config.username, config.password)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='*', intents=intents, case_insensitive=True)

@bot.event
async def on_ready():
    print("-----------------------------")
    print("the bot is now ready for use!")
    print("-----------------------------")
    #syncs slash commands
    await bot.tree.sync()

# code for classes:

class SeriesSelect(Select):
    def __init__(self, series_results):
        options = [
            SelectOption(
                label=s.name[:100],  # Discord max label length = 100
                description=f"ID: {s.id}",
                value=str(s.id)
            )
            for s in series_results[:25]  # Discord max: 25 options
        ]

        super().__init__(placeholder="Select a comic series...", min_values=1, max_values=1, options=options)
        self.selected_series_id = None
    
    async def callback(self, interaction: Interaction):
        self.selected_series_id = self.values[0]
        await interaction.response.send_message(f"You selected series ID `{self.selected_series_id}`.\nPlease now use `/issue_lookup` to look up an issue.", ephemeral=True)

class SeriesView(View):
    def __init__(self, series_results):
        super().__init__(timeout=60)
        self.select = SeriesSelect(series_results)
        self.add_item(self.select)

# Now here's the code for the commands:
async def handle_issues(interaction, start_date: str, end_date: str, publisher: str):
    try:
        await interaction.response.defer() #gives time for processing

        # Fetch issues from Mokkari
        issues_list = m.issues_list({
            "store_date_range_after": start_date,
            "store_date_range_before": end_date,
            "publisher_name": publisher
        })
        
        # Function to paginate the issues
        def create_pages(issues):
            pages = []
            message = "Here are the issues for your query: (Comic ID on the left) \n\n"
            for issue in issues:
                if len(message) + len(f"**{issue.id}**: {issue.issue_name}\n") > 2000:
                    pages.append(message)
                    message = ""
                message += f"**{issue.id}**: {issue.issue_name}\n"
            if message:
                pages.append(message)
            return pages
        
        pages = create_pages(issues_list)
        if not pages:
            pages = ["No issues were found for the given query."] #ensure there's at least one page
        
        # Send the initial page in an embed
        pembed = discord.Embed(title=f"Issues (Page 1/{len(pages)})", description= pages[0], color=discord.Color.green())
        await interaction.followup.send(embed = pembed, wait = True)
        message = await interaction.original_response()
        if len(pages) > 1:
            await message.add_reaction("◀️")
            await message.add_reaction("▶️")
        
        # Handles reactions (returns in boolean)
        def check(reaction, user):
            return user != bot.user and str(reaction.emoji) in ["◀️", "▶️"]
        
        # Page traversal
        current_page = 0
        while True:
            try:
                reaction, user = await bot.wait_for('reaction_add', timeout=120.0, check=check)
                if reaction.emoji == "◀️":
                    current_page = (current_page - 1) % len(pages)
                elif reaction.emoji == "▶️":
                    current_page = (current_page + 1) % len(pages)
                #updates the page (embed) and removes the user's reaction, to keep the reactions clean
                new_embed = discord.Embed(title=f"Issues (Page {current_page + 1}/{len(pages)})", description=pages[current_page], color=discord.Color.green())
                await message.edit(embed=new_embed)
                await message.remove_reaction(reaction, user)
            #times-out if user doesnt react within a minute
            except asyncio.TimeoutError:
                await message.clear_reactions()
                break
    except Exception as e:
        error_message = f"An error has occured: {e}"
        await interaction.followup.send(error_message)


async def handle_kryptonian(interaction):
    try:
        await interaction.response.defer() #gives time for processing
        
    
    except Exception as e:
        error_message = f"An error has occured: {e}"
        await interaction.followup.send(error_message)    

# Slash commands
@bot.tree.command(name="issues", description="Fetches comic issues given publisher and date range. Use the YYYY-MM-DD date format.")
async def issues_slash(interaction: discord.Interaction, start_date: str, end_date: str, publisher: str):
    await handle_issues(interaction, start_date, end_date, publisher)

@bot.tree.command(name="series_lookup", description="Search for a comic series title.")
@app_commands.describe(name="The name of the comic series")
async def series_lookup(interaction: Interaction, name: str):
    await interaction.response.defer(thinking=True)
    try:
        # Search Metron for matching series
        series_results = await asyncio.to_thread(m.series_list, {"series_name": name})
        if not series_results:
            await interaction.followup.send("No matching comic series found.", ephemeral=True)
            return

        view = SeriesView(series_results)
        await interaction.followup.send("Select the comic series you're looking for:", view=view, ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"An error occurred: {e}", ephemeral=True)

@bot.tree.command(name="kryptonian", description="It's the Man of Tomorrow himself..")
async def kryptonian_slash(interaction: discord.Interaction):
    await handle_kryptonian(interaction) 

# removes the default help command
bot.remove_command('help')

#the custom help command
@bot.tree.command(name="help", description="Lists the bots commands and what they do")
async def help(interaction: discord.Interaction):
    hembed = discord.Embed(
        title="Bot Commands",
        description="Here are the commands you can use:",
        color=discord.Color.green()
    )

    hembed.add_field(
        name="issues",
        value="Fetches a list of comic issues from the specified publisher within the specified date range. Date format is YYYY-MM-DD",
        inline=False
    )

    hembed.add_field(
        name="help",
        value="Displays this list of the bot's commands!",
        inline=False
    )

    await interaction.response.send_message(embed=hembed)

#ensures the bot only goes online when directly run
if __name__ == '__main__':
    bot.run(config.token)