# Carmine
Discord Bot to access and retrieve information from the Metron Comic Book Database using the Metron API and Discord API and is built using Python and the discord.py library.

## Instructions

Requires Python 3.12

You must create a 'config.env' file in the root directory of the project and add your Discord bot token, Metron username, and Metron password to it. The format should be as follows:
```
DISCORD_TOKEN=your_discord_bot_token_here
METRON_USERNAME=your_metron_username_here
METRON_PASSWORD=your_metron_password_here
```

### Windows First-time Setup
```
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python main.py
```

### Linux First-time Setup
```
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python main.py
```

After first-time setup, you can run the bot anytime with `.\.venv\Scripts\Activate.bat` or `source .venv/bin/activate`, and then `python main.py`.
To add the bot to a Discord server, you will need to create an application on the Discord Developer Portal and generate an OAuth2 invite link with the `applications.commands` and `bot` scopes. Under the `bot` scope, select the `View Channels`, `Send Messages`, `Embed Links`, `Attach Files`, and `Use Slash Commands` permissions. Use this link to invite the bot to your server.