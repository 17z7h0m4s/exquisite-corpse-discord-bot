"""
Exquisite Corpse Discord Bot

A two-player collaborative poetry game based on the Surrealist parlor game.

Game flow:
1. Player A starts with N words (default 6) in a channel
2. Anyone can claim the game by responding with their N words
3. Players alternate via DMs, each seeing only the last word from the previous turn
4. Two contributions form one line; default is 4 lines (8 turns total)
5. If a player times out (2h), anyone can take over their slot
6. Completed poem posts to the original channel with all contributor credits
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, field
import sqlite3
import json
from pathlib import Path


# === Database ===

DB_PATH = Path(__file__).parent / "exquisite_corpse.db"


def init_db():
    """Initialize the database schema."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("""
        CREATE TABLE IF NOT EXISTS games (
            channel_id INTEGER PRIMARY KEY,
            starter_id INTEGER NOT NULL,
            first_words TEXT NOT NULL,
            words_per_turn INTEGER NOT NULL DEFAULT 6,
            total_lines INTEGER NOT NULL DEFAULT 4,
            contributions TEXT NOT NULL DEFAULT '[]',
            contributors TEXT NOT NULL DEFAULT '[]',
            player_a INTEGER,
            player_b INTEGER,
            status TEXT NOT NULL DEFAULT 'open',
            last_activity TEXT NOT NULL,
            current_turn INTEGER NOT NULL DEFAULT 1
        )
    """)
    
    conn.commit()
    conn.close()


def save_game(game: "Game"):
    """Save a game to the database."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("""
        INSERT OR REPLACE INTO games 
        (channel_id, starter_id, first_words, words_per_turn, total_lines,
         contributions, contributors, player_a, player_b, status, 
         last_activity, current_turn)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        game.channel_id,
        game.starter_id,
        game.first_words,
        game.words_per_turn,
        game.total_lines,
        json.dumps(game.contributions),
        json.dumps(game.contributors),
        game.player_a,
        game.player_b,
        game.status,
        game.last_activity.isoformat(),
        game.current_turn
    ))
    
    conn.commit()
    conn.close()


def load_all_games() -> dict[int, "Game"]:
    """Load all non-complete games from the database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    c.execute("SELECT * FROM games WHERE status != 'complete'")
    rows = c.fetchall()
    conn.close()
    
    games = {}
    for row in rows:
        game = Game(
            channel_id=row["channel_id"],
            starter_id=row["starter_id"],
            first_words=row["first_words"],
            words_per_turn=row["words_per_turn"],
            total_lines=row["total_lines"],
            _skip_init=True
        )
        game.contributions = json.loads(row["contributions"])
        game.contributors = json.loads(row["contributors"])
        game.player_a = row["player_a"]
        game.player_b = row["player_b"]
        game.status = row["status"]
        game.last_activity = datetime.fromisoformat(row["last_activity"])
        game.current_turn = row["current_turn"]
        games[game.channel_id] = game
    
    return games


def delete_game(channel_id: int):
    """Remove a completed game from the database."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM games WHERE channel_id = ?", (channel_id,))
    conn.commit()
    conn.close()


# === Game State ===

@dataclass
class Game:
    channel_id: int
    starter_id: int
    first_words: str
    words_per_turn: int = 6
    total_lines: int = 4
    _skip_init: bool = field(default=False, repr=False)
    
    contributions: list[str] = field(default_factory=list)
    contributors: list[int] = field(default_factory=list)
    player_a: Optional[int] = None
    player_b: Optional[int] = None
    status: str = "open"  # pending, open, active, complete
    last_activity: datetime = field(default_factory=datetime.utcnow)
    current_turn: int = 1
    
    def __post_init__(self):
        if self._skip_init:
            return
        self.contributions = [self.first_words]
        self.contributors = [self.starter_id]
        self.player_a = self.starter_id
    
    @property
    def total_turns(self) -> int:
        return self.total_lines * 2
    
    @property
    def current_player(self) -> Optional[int]:
        if self.status == "open":
            return None
        return self.player_a if self.current_turn % 2 == 0 else self.player_b
    
    @property
    def last_word(self) -> Optional[str]:
        if not self.contributions:
            return None
        return self.contributions[-1].split()[-1]
    
    @property
    def lines_complete(self) -> int:
        return len(self.contributions) // 2
    
    def add_contribution(self, user_id: int, words: str):
        self.contributions.append(words)
        self.contributors.append(user_id)
        self.current_turn += 1
        self.last_activity = datetime.utcnow()
        
        if self.current_turn >= self.total_turns:
            self.status = "complete"
    
    def get_poem(self) -> str:
        lines = []
        for i in range(0, len(self.contributions), 2):
            if i + 1 < len(self.contributions):
                lines.append(f"{self.contributions[i]} / {self.contributions[i + 1]}")
            else:
                lines.append(self.contributions[i])
        return "\n".join(lines)
    
    def get_unique_contributors(self) -> list[int]:
        seen = []
        for uid in self.contributors:
            if uid not in seen:
                seen.append(uid)
        return seen
    
    def timeout_current_player(self):
        if self.current_turn % 2 == 0:
            self.player_a = None
        else:
            self.player_b = None
    
    def slot_is_open(self) -> bool:
        if self.status == "pending":
            return False  # Can't join until starter submits words
        if self.status == "open":
            return True
        if self.status == "active":
            return (self.current_turn % 2 == 0 and self.player_a is None) or \
                   (self.current_turn % 2 == 1 and self.player_b is None)
        return False


# === Bot ===

class ExquisiteCorpseBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        super().__init__(command_prefix="!", intents=intents)
        
        init_db()
        self.games: dict[int, Game] = load_all_games()
        self.player_games: dict[int, int] = {}  # user_id -> channel_id
        self.pending_responses: dict[int, int] = {}  # user_id -> channel_id
        
        # Rebuild player tracking from loaded games
        for channel_id, game in self.games.items():
            if game.player_a:
                self.player_games[game.player_a] = channel_id
            if game.player_b:
                self.player_games[game.player_b] = channel_id
            
            # Rebuild pending responses
            if game.status == "pending" and game.player_a:
                self.pending_responses[game.player_a] = channel_id
            elif game.status == "active" and game.current_player:
                self.pending_responses[game.current_player] = channel_id
    
    async def setup_hook(self):
        self.timeout_checker.start()
        await self.tree.sync()
        print(f"Bot synced and ready. Loaded {len(self.games)} active game(s).")
    
    @tasks.loop(minutes=5)
    async def timeout_checker(self):
        now = datetime.utcnow()
        timeout = timedelta(hours=2)
        
        for channel_id, game in list(self.games.items()):
            if game.status != "active":
                continue
            if now - game.last_activity <= timeout:
                continue
            
            timed_out_player = game.current_player
            
            # Skip if no current player (already timed out, waiting for someone to join)
            if not timed_out_player:
                continue
            
            game.timeout_current_player()
            save_game(game)
            
            # Clean up player tracking
            self.player_games.pop(timed_out_player, None)
            self.pending_responses.pop(timed_out_player, None)
            
            # Notify channel
            channel = self.get_channel(channel_id)
            if channel:
                await channel.send(
                    f"<@{timed_out_player}> timed out.\n\n"
                    f"Last word: **{game.last_word}**\n"
                    f"`/corpse join` to continue."
                )


bot = ExquisiteCorpseBot()


# === Helpers ===

def count_words(text: str) -> int:
    return len(text.split())


async def prompt_next_player(game: Game):
    player_id = game.current_player
    if not player_id:
        return
    
    try:
        user = bot.get_user(player_id) or await bot.fetch_user(player_id)
    except discord.NotFound:
        return
    
    bot.pending_responses[player_id] = game.channel_id
    
    try:
        await user.send(
            f"**Exquisite Corpse** — Your turn!\n"
            f"Lines: {game.lines_complete}/{game.total_lines}\n\n"
            f"Last word: **{game.last_word}**\n\n"
            f"Reply with exactly {game.words_per_turn} words:"
        )
    except discord.Forbidden:
        channel = bot.get_channel(game.channel_id)
        if channel:
            await channel.send(
                f"<@{player_id}> — I can't DM you. Enable DMs from server members.\n"
                f"Last word: **{game.last_word}**"
            )


async def post_completed_poem(game: Game):
    channel = bot.get_channel(game.channel_id)
    if not channel:
        return
    
    poem = game.get_poem()
    credits = game.get_unique_contributors()
    credit_mentions = ", ".join(f"<@{uid}>" for uid in credits)
    
    await channel.send(
        f"**Exquisite Corpse — Complete**\n\n"
        f">>> {poem}\n\n"
        f"*Contributors: {credit_mentions}*"
    )
    
    # Clean up
    for uid in [game.player_a, game.player_b]:
        if uid:
            bot.player_games.pop(uid, None)
            bot.pending_responses.pop(uid, None)
    
    bot.games.pop(game.channel_id, None)
    delete_game(game.channel_id)


# === Commands ===

@bot.tree.command(name="corpse", description="Exquisite Corpse poetry game")
@app_commands.describe(
    action="start, join, status, or abandon",
    words="Your contribution (required for start/join)",
    lines="Number of lines (default 4, start only)",
    wordcount="Words per turn (default 6, start only)"
)
async def corpse(
    interaction: discord.Interaction,
    action: str,
    words: Optional[str] = None,
    lines: Optional[int] = 4,
    wordcount: Optional[int] = 6
):
    action = action.lower().strip()
    
    match action:
        case "start":
            await cmd_start(interaction, words, lines, wordcount)
        case "join":
            await cmd_join(interaction, words)
        case "status":
            await cmd_status(interaction)
        case "abandon":
            await cmd_abandon(interaction)
        case _:
            await interaction.response.send_message(
                "Unknown action. Use: `start`, `join`, `status`, or `abandon`",
                ephemeral=True
            )


async def cmd_start(
    interaction: discord.Interaction,
    words: Optional[str],
    lines: int,
    wordcount: int
):
    if interaction.user.id in bot.player_games:
        await interaction.response.send_message(
            "You're already in a game. `/corpse status` or `/corpse abandon`",
            ephemeral=True
        )
        return
    
    existing = bot.games.get(interaction.channel_id)
    if existing and existing.status != "complete":
        await interaction.response.send_message(
            "There's already an active game in this channel.",
            ephemeral=True
        )
        return
    
    # If words provided, validate and create game immediately
    if words:
        if count_words(words) != wordcount:
            await interaction.response.send_message(
                f"Need exactly {wordcount} words. You gave {count_words(words)}.",
                ephemeral=True
            )
            return
        
        game = Game(
            channel_id=interaction.channel_id,
            starter_id=interaction.user.id,
            first_words=words,
            words_per_turn=wordcount,
            total_lines=lines
        )
        
        bot.games[interaction.channel_id] = game
        bot.player_games[interaction.user.id] = interaction.channel_id
        save_game(game)
        
        await interaction.response.send_message(
            f"**Exquisite Corpse** — {lines} lines, {wordcount} words/turn\n\n"
            f"*{interaction.user.display_name}* started a poem.\n"
            f"`/corpse join` to play"
        )
        return
    
    # No words provided — create pending game and DM user for words
    game = Game(
        channel_id=interaction.channel_id,
        starter_id=interaction.user.id,
        first_words="",  # Will be filled via DM
        words_per_turn=wordcount,
        total_lines=lines,
        _skip_init=True
    )
    game.contributions = []
    game.contributors = []
    game.player_a = interaction.user.id
    game.status = "pending"  # New status: waiting for starter's words
    
    bot.games[interaction.channel_id] = game
    bot.player_games[interaction.user.id] = interaction.channel_id
    bot.pending_responses[interaction.user.id] = interaction.channel_id
    save_game(game)
    
    await interaction.response.send_message(
        f"**Exquisite Corpse** — {lines} lines, {wordcount} words/turn\n\n"
        f"*{interaction.user.display_name}* is starting a poem...\n"
        f"`/corpse join` to play (once ready)"
    )
    
    # DM the starter for their words
    try:
        user = interaction.user
        await user.send(
            f"**Exquisite Corpse** — You're starting a new poem!\n\n"
            f"Send your first {wordcount} words:"
        )
    except discord.Forbidden:
        await interaction.followup.send(
            f"<@{interaction.user.id}> — I can't DM you. Enable DMs from server members.",
            ephemeral=True
        )


async def cmd_join(interaction: discord.Interaction, words: Optional[str]):
    game = bot.games.get(interaction.channel_id)
    
    if not game or game.status == "complete":
        await interaction.response.send_message(
            "No active game here. Use `/corpse start` to begin.",
            ephemeral=True
        )
        return
    
    if game.status == "pending":
        await interaction.response.send_message(
            "Game is waiting for the starter to submit their words. Try again shortly.",
            ephemeral=True
        )
        return
    
    # Check if player is in another game
    if interaction.user.id in bot.player_games:
        other_channel = bot.player_games[interaction.user.id]
        if other_channel != interaction.channel_id:
            await interaction.response.send_message(
                f"You're in a game in <#{other_channel}>. Abandon it first.",
                ephemeral=True
            )
            return
    
    if not game.slot_is_open():
        await interaction.response.send_message(
            "This game already has two active players.",
            ephemeral=True
        )
        return
    
    # Prevent playing against yourself
    if game.status == "open" and interaction.user.id == game.player_a:
        await interaction.response.send_message(
            "You can't play against yourself.",
            ephemeral=True
        )
        return
    
    # If words provided directly, validate and use them
    if words:
        if count_words(words) != game.words_per_turn:
            await interaction.response.send_message(
                f"Need exactly {game.words_per_turn} words. You gave {count_words(words)}.",
                ephemeral=True
            )
            return
        
        # Handle open game (first join)
        if game.status == "open":
            game.player_b = interaction.user.id
            game.status = "active"
            game.add_contribution(interaction.user.id, words)
            bot.player_games[interaction.user.id] = interaction.channel_id
            save_game(game)
            
            await interaction.response.send_message(
                f"*{interaction.user.display_name}* joined the poem!\n"
                f"Lines: {game.lines_complete}/{game.total_lines}"
            )
            
            if game.status == "complete":
                await post_completed_poem(game)
            else:
                await prompt_next_player(game)
            return
        
        # Handle taking over a timed-out slot
        if game.current_turn % 2 == 0:
            game.player_a = interaction.user.id
        else:
            game.player_b = interaction.user.id
        
        game.add_contribution(interaction.user.id, words)
        bot.player_games[interaction.user.id] = interaction.channel_id
        save_game(game)
        
        await interaction.response.send_message(
            f"*{interaction.user.display_name}* takes over!\n"
            f"Lines: {game.lines_complete}/{game.total_lines}"
        )
        
        if game.status == "complete":
            await post_completed_poem(game)
        else:
            await prompt_next_player(game)
        return
    
    # No words provided — DM the player with the last word prompt
    if game.status == "open":
        game.player_b = interaction.user.id
        game.status = "active"
    elif game.current_turn % 2 == 0:
        game.player_a = interaction.user.id
    else:
        game.player_b = interaction.user.id
    
    bot.player_games[interaction.user.id] = interaction.channel_id
    bot.pending_responses[interaction.user.id] = interaction.channel_id
    save_game(game)
    
    await interaction.response.send_message(
        f"*{interaction.user.display_name}* joined! Check your DMs.",
        ephemeral=False
    )
    
    # DM them with the last word
    try:
        user = interaction.user
        await user.send(
            f"**Exquisite Corpse** — Your turn!\n"
            f"Lines: {game.lines_complete}/{game.total_lines}\n\n"
            f"Last word: **{game.last_word}**\n\n"
            f"Reply with exactly {game.words_per_turn} words:"
        )
    except discord.Forbidden:
        channel = bot.get_channel(interaction.channel_id)
        if channel:
            await channel.send(
                f"<@{interaction.user.id}> — I can't DM you. Enable DMs from server members."
            )


async def cmd_status(interaction: discord.Interaction):
    # Check user's active game
    if interaction.user.id in bot.player_games:
        channel_id = bot.player_games[interaction.user.id]
        game = bot.games.get(channel_id)
        
        if game:
            is_your_turn = game.current_player == interaction.user.id
            turn_status = "**your turn**" if is_your_turn else "waiting on other player"
            
            await interaction.response.send_message(
                f"**Your game:** <#{channel_id}>\n"
                f"Lines: {game.lines_complete}/{game.total_lines}\n"
                f"Status: {turn_status}\n"
                f"Last word: **{game.last_word}**",
                ephemeral=True
            )
            return
    
    # Check this channel
    game = bot.games.get(interaction.channel_id)
    if game and game.status != "complete":
        status = "waiting for second player" if game.status == "open" else "in progress"
        
        await interaction.response.send_message(
            f"**Game in this channel:**\n"
            f"Lines: {game.lines_complete}/{game.total_lines}\n"
            f"Status: {status}\n"
            f"Last word: **{game.last_word}**",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "No active game here or involving you.",
            ephemeral=True
        )


async def cmd_abandon(interaction: discord.Interaction):
    if interaction.user.id not in bot.player_games:
        await interaction.response.send_message(
            "You're not in a game.",
            ephemeral=True
        )
        return
    
    channel_id = bot.player_games.pop(interaction.user.id)
    bot.pending_responses.pop(interaction.user.id, None)
    
    game = bot.games.get(channel_id)
    if game:
        if game.player_a == interaction.user.id:
            game.player_a = None
        if game.player_b == interaction.user.id:
            game.player_b = None
        save_game(game)
        
        channel = bot.get_channel(channel_id)
        if channel:
            await channel.send(
                f"*{interaction.user.display_name}* left the poem.\n\n"
                f"Last word: **{game.last_word}**\n"
                f"`/corpse join words:<your {game.words_per_turn} words>`"
            )
    
    await interaction.response.send_message("You've left the game.", ephemeral=True)


# === DM Handler ===

@bot.event
async def on_message(message: discord.Message):
    # Ignore bots and non-DMs
    if message.author.bot:
        return
    if message.guild is not None:
        await bot.process_commands(message)
        return
    
    # Check if user has a pending response
    if message.author.id not in bot.pending_responses:
        await message.channel.send(
            "No pending turn. If you're in a game, wait for your turn."
        )
        return
    
    channel_id = bot.pending_responses[message.author.id]
    game = bot.games.get(channel_id)
    
    if not game:
        await message.channel.send("You don't have a pending turn right now.")
        bot.pending_responses.pop(message.author.id, None)
        return
    
    words = message.content.strip()
    
    if count_words(words) != game.words_per_turn:
        await message.channel.send(
            f"Need exactly {game.words_per_turn} words. You gave {count_words(words)}. Try again:"
        )
        return
    
    # Handle pending game (starter submitting first words)
    if game.status == "pending":
        bot.pending_responses.pop(message.author.id, None)
        game.first_words = words
        game.contributions = [words]
        game.contributors = [message.author.id]
        game.status = "open"
        game.last_activity = datetime.utcnow()
        save_game(game)
        
        await message.channel.send(
            f"✓ Your words are locked in.\n"
            f"Waiting for someone to `/corpse join` in the channel."
        )
        
        # Update channel message
        channel = bot.get_channel(channel_id)
        if channel:
            await channel.send(
                f"*{message.author.display_name}*'s poem is ready!\n"
                f"`/corpse join` to play"
            )
        return
    
    # Handle active game turns
    if game.current_player != message.author.id:
        await message.channel.send("You don't have a pending turn right now.")
        bot.pending_responses.pop(message.author.id, None)
        return
    
    # Accept contribution
    bot.pending_responses.pop(message.author.id, None)
    game.add_contribution(message.author.id, words)
    save_game(game)
    
    await message.channel.send(
        f"✓ Received.\n"
        f"Lines: {game.lines_complete}/{game.total_lines}"
    )
    
    if game.status == "complete":
        await post_completed_poem(game)
    else:
        await prompt_next_player(game)


# === Entry Point ===

if __name__ == "__main__":
    import os
    
    # Load from .env file if present
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass  # python-dotenv not installed, rely on system env vars
    
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("Set DISCORD_TOKEN in .env file or as environment variable.")
        print("Create a .env file with: DISCORD_TOKEN=your_token_here")
    else:
        bot.run(token)
