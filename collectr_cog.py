"""
Collectr Tracker Cog
=====================
A discord.py (2.x) cog that periodically checks each tracked user's Collectr
collection for newly added cards and market-value movement, and posts staggered
updates to configured channels at scheduled local times.

Dependencies:
    pip install discord.py aiosqlite aiohttp

This assumes a Collectr REST API optimistically (see CollectrClient below).
Swap the base URL / auth / response parsing in CollectrClient to match the
real API once you have docs for it -- everything else (DB, scheduling,
diffing, UI) is written to be drop-in usable as-is.

Usage:
    # in your bot's main file
    await bot.load_extension("collectr_cog")
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger("collectr_cog")

# =============================================================================
# CONFIG / HYPERPARAMETERS
# =============================================================================

DB_PATH = "collectr.db"

COLLECTR_API_BASE_URL = "https://api.collectr.com/v1"  # TODO: confirm real base URL
COLLECTR_API_KEY = "REPLACE_ME"                          # TODO: load from env/secret store

INTERNAL_CHECK_FREQUENCY_HOURS = 2      # how often the background loop polls Collectr at all
MAX_CARDS_PER_MESSAGE = 25              # cards listed before "...and N more"
STAGGER_SECONDS_BETWEEN_USER_DMS = 2    # delay between each user's posted update

API_REQUEST_STAGGER_SECONDS = 1         # minimum gap enforced between successive Collectr API
                                         # calls, process-wide -- see CollectrClient's semaphore

COMMON_TIMEZONES = [
    "US/Eastern", "US/Central", "US/Mountain", "US/Pacific", "US/Alaska", "US/Hawaii",
    "UTC", "Europe/London", "Europe/Paris", "Europe/Berlin",
    "Asia/Tokyo", "Asia/Shanghai", "Asia/Kolkata", "Australia/Sydney",
]


# =============================================================================
# COLLECTR API CLIENT
# (Written optimistically -- assumes a REST endpoint shaped like this exists.
#  Adjust base_url / auth / JSON shape once real docs are available; nothing
#  else in this file needs to change as long as get_collection() still
#  returns a list[CollectrCard].)
# =============================================================================

@dataclass
class CollectrCard:
    card_id: str
    title: str
    market_value: float
    market_movement: float


class CollectrClient:
    """
    All outbound requests funnel through a single-slot (width-1) semaphore,
    so at most one Collectr API call is ever in flight process-wide -- this
    protects against multiple servers/channels becoming "due" at the same
    instant and firing concurrent requests. The stagger sleep is inside a
    `finally`, so it runs even when the request raises; that's what stops a
    caller's retry/for-loop from immediately re-hitting the API after an
    error with no delay.
    """

    def __init__(self, api_key: str = COLLECTR_API_KEY, base_url: str = COLLECTR_API_BASE_URL):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
        self._api_semaphore = asyncio.Semaphore(1)

    async def start(self):
        self._session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.api_key}"}
        )

    async def close(self):
        if self._session:
            await self._session.close()

    async def get_collection(self, collectr_id: str) -> list[CollectrCard]:
        async with self._api_semaphore:
            try:
                return await self._fetch_collection(collectr_id)
            finally:
                await asyncio.sleep(API_REQUEST_STAGGER_SECONDS)

    async def _fetch_collection(self, collectr_id: str) -> list[CollectrCard]:
        """
        GET /users/{collectr_id}/collection

        Assumed response shape:
        {
          "cards": [
            {"id": "abc123", "title": "Charizard VMAX", "market_value": 120.50, "market_movement": 4.25},
            ...
          ]
        }
        """
        url = f"{self.base_url}/users/{collectr_id}/collection"
        async with self._session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()

        return [
            CollectrCard(
                card_id=str(c["id"]),
                title=c.get("title", "Unknown Card"),
                market_value=float(c.get("market_value", 0.0)),
                market_movement=float(c.get("market_movement", 0.0)),
            )
            for c in data.get("cards", [])
        ]


# =============================================================================
# DATABASE LAYER
# =============================================================================

@dataclass
class UserRow:
    discord_id: int
    discord_name: str
    collectr_id: str
    server_id: int
    threshold: float
    mentions: bool


async def init_db(db: aiosqlite.Connection):
    await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            discord_id   INTEGER NOT NULL,
            discord_name TEXT NOT NULL,
            collectr_id  TEXT NOT NULL,
            server_id    INTEGER NOT NULL,
            threshold    REAL NOT NULL DEFAULT 0.0,
            mentions     INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (discord_id, server_id)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS collection (
            collectr_id     TEXT NOT NULL,
            card_id         TEXT NOT NULL,
            card_title      TEXT,
            market_movement REAL NOT NULL DEFAULT 0.0,
            PRIMARY KEY (collectr_id, card_id)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS update_time (
            server_id  INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            timezone   TEXT NOT NULL,
            hour       INTEGER NOT NULL,
            minute     INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (server_id, channel_id, timezone, hour, minute)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS update_log (
            server_id       INTEGER NOT NULL,
            channel_id      INTEGER NOT NULL,
            last_checked_at TEXT,
            PRIMARY KEY (server_id, channel_id)
        )
    """)
    await db.commit()


def _row_to_user(row) -> UserRow:
    return UserRow(
        discord_id=row[0], discord_name=row[1], collectr_id=row[2],
        server_id=row[3], threshold=row[4], mentions=bool(row[5]),
    )


async def get_users(db, server_id: int) -> list[UserRow]:
    async with db.execute(
        "SELECT * FROM users WHERE server_id=? ORDER BY discord_name", (server_id,)
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_user(r) for r in rows]


async def get_user(db, server_id: int, discord_id: int) -> Optional[UserRow]:
    async with db.execute(
        "SELECT * FROM users WHERE server_id=? AND discord_id=?", (server_id, discord_id)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_user(row) if row else None


async def add_user(db, discord_id, discord_name, collectr_id, server_id, threshold=0.0, mentions=True):
    await db.execute(
        "INSERT INTO users (discord_id, discord_name, collectr_id, server_id, threshold, mentions) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(discord_id, server_id) DO UPDATE SET "
        "discord_name=excluded.discord_name, collectr_id=excluded.collectr_id, threshold=excluded.threshold",
        (discord_id, discord_name, collectr_id, server_id, threshold, int(mentions)),
    )
    await db.commit()


async def remove_user(db, server_id, discord_id):
    user = await get_user(db, server_id, discord_id)
    await db.execute("DELETE FROM users WHERE server_id=? AND discord_id=?", (server_id, discord_id))
    await db.commit()
    if user:
        # only drop cached collection rows if no other tracked entry (any
        # server) still references this collectr_id
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE collectr_id=?", (user.collectr_id,)
        ) as cur:
            (count,) = await cur.fetchone()
        if count == 0:
            await db.execute("DELETE FROM collection WHERE collectr_id=?", (user.collectr_id,))
            await db.commit()


async def set_threshold(db, server_id, discord_id, threshold):
    await db.execute(
        "UPDATE users SET threshold=? WHERE server_id=? AND discord_id=?",
        (threshold, server_id, discord_id),
    )
    await db.commit()


async def toggle_mentions(db, server_id, discord_id):
    await db.execute(
        "UPDATE users SET mentions = NOT mentions WHERE server_id=? AND discord_id=?",
        (server_id, discord_id),
    )
    await db.commit()


async def get_tracked_channels(db, server_id: int) -> list[int]:
    async with db.execute(
        "SELECT DISTINCT channel_id FROM update_time WHERE server_id=?", (server_id,)
    ) as cur:
        rows = await cur.fetchall()
    return [r[0] for r in rows]


async def get_update_times(db, server_id: int, channel_id: int):
    async with db.execute(
        "SELECT timezone, hour, minute FROM update_time WHERE server_id=? AND channel_id=? "
        "ORDER BY timezone, hour, minute",
        (server_id, channel_id),
    ) as cur:
        return await cur.fetchall()


async def add_update_time(db, server_id, channel_id, tz_name, hour, minute=0):
    await db.execute(
        "INSERT OR IGNORE INTO update_time (server_id, channel_id, timezone, hour, minute) "
        "VALUES (?, ?, ?, ?, ?)",
        (server_id, channel_id, tz_name, hour, minute),
    )
    await db.commit()


async def remove_update_time(db, server_id, channel_id, tz_name, hour, minute=0):
    await db.execute(
        "DELETE FROM update_time WHERE server_id=? AND channel_id=? AND timezone=? AND hour=? AND minute=?",
        (server_id, channel_id, tz_name, hour, minute),
    )
    await db.commit()


async def remove_channel(db, server_id, channel_id):
    await db.execute("DELETE FROM update_time WHERE server_id=? AND channel_id=?", (server_id, channel_id))
    await db.execute("DELETE FROM update_log WHERE server_id=? AND channel_id=?", (server_id, channel_id))
    await db.commit()


async def get_last_checked(db, server_id, channel_id) -> datetime:
    async with db.execute(
        "SELECT last_checked_at FROM update_log WHERE server_id=? AND channel_id=?",
        (server_id, channel_id),
    ) as cur:
        row = await cur.fetchone()
    if row and row[0]:
        return datetime.fromisoformat(row[0])
    return datetime.fromtimestamp(0, tz=dt_timezone.utc)


async def set_last_checked(db, server_id, channel_id, when: datetime):
    await db.execute(
        "INSERT INTO update_log (server_id, channel_id, last_checked_at) VALUES (?, ?, ?) "
        "ON CONFLICT(server_id, channel_id) DO UPDATE SET last_checked_at=excluded.last_checked_at",
        (server_id, channel_id, when.isoformat()),
    )
    await db.commit()


# =============================================================================
# PERMISSIONS
# =============================================================================

def is_mod(member: discord.Member) -> bool:
    return member.guild_permissions.manage_guild


def can_edit_user_entry(actor: discord.Member, target_discord_id: int) -> bool:
    return is_mod(actor) or actor.id == target_discord_id


# =============================================================================
# CORE DIFF / NOTIFY LOGIC
# =============================================================================

async def process_user_update(client: CollectrClient, db, user: UserRow) -> dict:
    live_cards = await client.get_collection(user.collectr_id)
    live_by_id = {c.card_id: c for c in live_cards}

    async with db.execute(
        "SELECT card_id, card_title, market_movement FROM collection WHERE collectr_id=?",
        (user.collectr_id,),
    ) as cur:
        stored_rows = await cur.fetchall()
    stored_by_id = {r[0]: {"title": r[1], "market_movement": r[2]} for r in stored_rows}

    new_cards = []
    moved_cards = []

    # 1. New cards: present live, not in stored
    for card_id, card in live_by_id.items():
        if card_id not in stored_by_id:
            new_cards.append({"card_id": card_id, "title": card.title})
            await db.execute(
                "INSERT INTO collection (collectr_id, card_id, card_title, market_movement) "
                "VALUES (?, ?, ?, ?)",
                (user.collectr_id, card_id, card.title, card.market_movement),
            )

    # 2. Missing cards: present in stored, gone from live -> delete
    for card_id in stored_by_id:
        if card_id not in live_by_id:
            await db.execute(
                "DELETE FROM collection WHERE collectr_id=? AND card_id=?",
                (user.collectr_id, card_id),
            )

    # 3. Market movement: changed since last check AND >= user's threshold
    for card_id, card in live_by_id.items():
        if card_id not in stored_by_id:
            continue  # already reported as new, don't double-notify
        old_movement = stored_by_id[card_id]["market_movement"]
        if card.market_movement != old_movement and abs(card.market_movement) >= user.threshold:
            moved_cards.append({
                "card_id": card_id,
                "title": card.title,
                "market_movement": card.market_movement,
            })
            await db.execute(
                "UPDATE collection SET market_movement=?, card_title=? WHERE collectr_id=? AND card_id=?",
                (card.market_movement, card.title, user.collectr_id, card_id),
            )

    await db.commit()
    return {"new_cards": new_cards, "moved_cards": moved_cards}


def format_update_message(user: UserRow, diff: dict) -> Optional[str]:
    if not diff["new_cards"] and not diff["moved_cards"]:
        return None

    who = f"<@{user.discord_id}>" if user.mentions else f"**{user.discord_name}**"
    lines = [f"{who}, here's what's new in your Collectr collection:"]

    if diff["new_cards"]:
        lines.append("")
        lines.append("**New cards:**")
        shown = diff["new_cards"][:MAX_CARDS_PER_MESSAGE]
        lines.extend(f"• {c['title']}" for c in shown)
        remainder = len(diff["new_cards"]) - len(shown)
        if remainder > 0:
            lines.append(f"...and {remainder} more.")

    if diff["moved_cards"]:
        lines.append("")
        lines.append("**Market value movement:**")
        shown = diff["moved_cards"][:MAX_CARDS_PER_MESSAGE]
        for c in shown:
            sign = "+" if c["market_movement"] >= 0 else ""
            lines.append(f"• {c['title']}: {sign}{c['market_movement']:g}")
        remainder = len(diff["moved_cards"]) - len(shown)
        if remainder > 0:
            lines.append(f"...and {remainder} more.")

    return "\n".join(lines)


def build_min_gap_warning(times, internal_freq_hours: int) -> Optional[str]:
    """
    Non-blocking, informational only if the update time interval < api call interval
    """
    if len(times) < 2:
        return None

    today_utc = datetime.now(dt_timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    utc_minutes = []
    for tz_name, hour, minute in times:
        tz = ZoneInfo(tz_name)
        local_dt = today_utc.astimezone(tz).replace(hour=hour, minute=minute, second=0, microsecond=0)
        utc_dt = local_dt.astimezone(dt_timezone.utc)
        utc_minutes.append(utc_dt.hour * 60 + utc_dt.minute)

    utc_minutes.sort()
    wrapped = utc_minutes + [utc_minutes[0] + 24 * 60]

    for a, b in zip(wrapped, wrapped[1:]):
        gap_hours = (b - a) / 60
        if gap_hours < internal_freq_hours:
            return (
                f"Some scheduled times are less than {internal_freq_hours}h apart. "
                f"New Collectr data is unlikely to have been fetched again in that window."
            )
    return None


# =============================================================================
# VIEWS: USER MANAGEMENT MENU
# =============================================================================

class TrackedUserSelect(discord.ui.Select):
    def __init__(self, users: list[UserRow]):
        options = [
            discord.SelectOption(
                label=u.discord_name[:100],
                description=f"threshold=${u.threshold:g}  mentions={'on' if u.mentions else 'off'}",
                value=str(u.discord_id),
            )
            for u in users
        ] or [discord.SelectOption(label="No users tracked yet", value="__none__")]
        super().__init__(placeholder="Select a tracked user...", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        view: UserManagementView = self.view
        if self.values[0] == "__none__":
            await interaction.response.defer()
            return
        view.selected_discord_id = int(self.values[0])
        can_edit = can_edit_user_entry(interaction.user, view.selected_discord_id)
        view.remove_button.disabled = not can_edit
        view.edit_threshold_button.disabled = not can_edit
        view.toggle_mentions_button.disabled = not can_edit
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class UserPickerSelect(discord.ui.UserSelect):
    """Mod-only picker for choosing *which* member to add tracking for."""

    def __init__(self, db, server_id: int, management_view: "UserManagementView"):
        super().__init__(placeholder="Choose a member to track...")
        self.db = db
        self.server_id = server_id
        self.management_view = management_view

    async def callback(self, interaction: discord.Interaction):
        target = self.values[0]
        await interaction.response.send_modal(
            AddUserModal(self.db, self.server_id, target.id, target.display_name, self.management_view)
        )


class AddUserPickerView(discord.ui.View):
    def __init__(self, db, server_id: int, management_view: "UserManagementView"):
        super().__init__(timeout=120)
        self.add_item(UserPickerSelect(db, server_id, management_view))


class AddUserModal(discord.ui.Modal, title="Add Collectr User"):
    collectr_id = discord.ui.TextInput(label="Collectr ID", placeholder="e.g. 8f3ac21", required=True)
    threshold = discord.ui.TextInput(
        label="Market movement threshold ($)", placeholder="0", required=False, default="0"
    )

    def __init__(self, db, server_id, target_discord_id, target_discord_name, management_view):
        super().__init__()
        self.db = db
        self.server_id = server_id
        self.target_discord_id = target_discord_id
        self.target_discord_name = target_discord_name
        self.management_view = management_view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            threshold_val = float(self.threshold.value) if self.threshold.value else 0.0
        except ValueError:
            threshold_val = 0.0

        await add_user(
            self.db, self.target_discord_id, self.target_discord_name,
            self.collectr_id.value.strip(), self.server_id, threshold_val,
        )
        await interaction.response.send_message(
            f"Added **{self.target_discord_name}** (collectr id `{self.collectr_id.value.strip()}`).",
            ephemeral=True,
        )


class EditThresholdModal(discord.ui.Modal, title="Edit Threshold"):
    threshold = discord.ui.TextInput(label="New threshold ($)", placeholder="e.g. 5.00", required=True)

    def __init__(self, db, server_id, target_discord_id, management_view):
        super().__init__()
        self.db = db
        self.server_id = server_id
        self.target_discord_id = target_discord_id
        self.management_view = management_view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = float(self.threshold.value)
        except ValueError:
            await interaction.response.send_message("Please enter a valid number.", ephemeral=True)
            return
        await set_threshold(self.db, self.server_id, self.target_discord_id, val)
        await interaction.response.send_message(f"Threshold updated to ${val:g}.", ephemeral=True)


class UserManagementView(discord.ui.View):
    def __init__(self, db, server_id: int, actor: discord.Member, users: list[UserRow]):
        super().__init__(timeout=180)
        self.db = db
        self.server_id = server_id
        self.actor = actor
        self.users = users
        self.selected_discord_id: Optional[int] = None

        self.add_item(TrackedUserSelect(users))

        self.add_button = discord.ui.Button(label="Add User", style=discord.ButtonStyle.success, row=1)
        self.add_button.callback = self.on_add
        self.add_item(self.add_button)

        self.remove_button = discord.ui.Button(label="Remove", style=discord.ButtonStyle.danger, disabled=True, row=1)
        self.remove_button.callback = self.on_remove
        self.add_item(self.remove_button)

        self.edit_threshold_button = discord.ui.Button(
            label="Edit Threshold", style=discord.ButtonStyle.primary, disabled=True, row=1
        )
        self.edit_threshold_button.callback = self.on_edit_threshold
        self.add_item(self.edit_threshold_button)

        self.toggle_mentions_button = discord.ui.Button(
            label="Toggle Mentions", style=discord.ButtonStyle.secondary, disabled=True, row=1
        )
        self.toggle_mentions_button.callback = self.on_toggle_mentions
        self.add_item(self.toggle_mentions_button)

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="Tracked Collectr Users", color=discord.Color.blurple())
        if not self.users:
            embed.description = "No users are being tracked yet. Click **Add User** to get started."
        else:
            lines = []
            for u in self.users:
                state = "mentions on" if u.mentions else "mentions off"
                lines.append(f"**{u.discord_name}** — threshold ${u.threshold:g}, {state}")
            embed.description = "\n".join(lines)
        return embed

    async def refresh(self, interaction: discord.Interaction):
        users = await get_users(self.db, self.server_id)
        new_view = UserManagementView(self.db, self.server_id, self.actor, users)
        await interaction.response.edit_message(embed=new_view.build_embed(), view=new_view)

    async def on_add(self, interaction: discord.Interaction):
        if is_mod(interaction.user):
            await interaction.response.send_message(
                "Pick a member to add:",
                view=AddUserPickerView(self.db, self.server_id, self),
                ephemeral=True,
            )
        else:
            await interaction.response.send_modal(
                AddUserModal(self.db, self.server_id, interaction.user.id, interaction.user.display_name, self)
            )

    async def on_remove(self, interaction: discord.Interaction):
        if not self.selected_discord_id or not can_edit_user_entry(interaction.user, self.selected_discord_id):
            await interaction.response.send_message("You don't have permission to remove this user.", ephemeral=True)
            return
        await remove_user(self.db, self.server_id, self.selected_discord_id)
        await self.refresh(interaction)

    async def on_edit_threshold(self, interaction: discord.Interaction):
        if not self.selected_discord_id or not can_edit_user_entry(interaction.user, self.selected_discord_id):
            await interaction.response.send_message("You don't have permission to edit this user.", ephemeral=True)
            return
        await interaction.response.send_modal(
            EditThresholdModal(self.db, self.server_id, self.selected_discord_id, self)
        )

    async def on_toggle_mentions(self, interaction: discord.Interaction):
        if not self.selected_discord_id or not can_edit_user_entry(interaction.user, self.selected_discord_id):
            await interaction.response.send_message("You don't have permission to edit this user.", ephemeral=True)
            return
        await toggle_mentions(self.db, self.server_id, self.selected_discord_id)
        await self.refresh(interaction)


# =============================================================================
# VIEWS: SERVER CONFIG MENU (channels + update times)
# =============================================================================

class TrackedChannelSelect(discord.ui.Select):
    def __init__(self, channel_ids: list[int]):
        options = [
            discord.SelectOption(label=f"#{cid}", value=str(cid)) for cid in channel_ids
        ] or [discord.SelectOption(label="No channels configured", value="__none__")]
        super().__init__(placeholder="Select a tracked channel...", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        view: ServerConfigView = self.view
        if self.values[0] == "__none__":
            await interaction.response.defer()
            return
        view.selected_channel_id = int(self.values[0])
        view.manage_times_button.disabled = False
        view.remove_channel_button.disabled = False
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class AddChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, parent_view: "ServerConfigView"):
        super().__init__(
            placeholder="Pick a channel to start tracking...",
            channel_types=[discord.ChannelType.text],
            row=1,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        time_view = UpdateTimeView(self.parent_view.db, self.parent_view.server_id, channel.id, [])
        await interaction.response.edit_message(embed=time_view.build_embed(), view=time_view)


class ServerConfigView(discord.ui.View):
    def __init__(self, db, server_id: int, channel_ids: list[int]):
        super().__init__(timeout=180)
        self.db = db
        self.server_id = server_id
        self.channel_ids = channel_ids
        self.selected_channel_id: Optional[int] = None

        self.add_item(TrackedChannelSelect(channel_ids))
        self.add_item(AddChannelSelect(self))

        self.manage_times_button = discord.ui.Button(
            label="Manage Update Times", style=discord.ButtonStyle.primary, disabled=True, row=2
        )
        self.manage_times_button.callback = self.on_manage_times
        self.add_item(self.manage_times_button)

        self.remove_channel_button = discord.ui.Button(
            label="Remove Channel", style=discord.ButtonStyle.danger, disabled=True, row=2
        )
        self.remove_channel_button.callback = self.on_remove_channel
        self.add_item(self.remove_channel_button)

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="Collectr Server Config", color=discord.Color.gold())
        if not self.channel_ids:
            embed.description = "No channels configured yet. Use the channel dropdown to add one."
        else:
            embed.description = "Channels currently receiving Collectr updates:\n" + "\n".join(
                f"<#{cid}>" for cid in self.channel_ids
            )
        embed.set_footer(text=f"Internal poll frequency: every {INTERNAL_CHECK_FREQUENCY_HOURS}h")
        return embed

    async def on_manage_times(self, interaction: discord.Interaction):
        times = await get_update_times(self.db, self.server_id, self.selected_channel_id)
        time_view = UpdateTimeView(self.db, self.server_id, self.selected_channel_id, times)
        await interaction.response.edit_message(embed=time_view.build_embed(), view=time_view)

    async def on_remove_channel(self, interaction: discord.Interaction):
        await remove_channel(self.db, self.server_id, self.selected_channel_id)
        channel_ids = await get_tracked_channels(self.db, self.server_id)
        new_view = ServerConfigView(self.db, self.server_id, channel_ids)
        await interaction.response.edit_message(embed=new_view.build_embed(), view=new_view)


# =============================================================================
# VIEWS: UPDATE TIME MENU (per channel)
# =============================================================================

class TimezoneSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=tz, value=tz) for tz in COMMON_TIMEZONES]
        super().__init__(placeholder="Select timezone...", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        view: UpdateTimeView = self.view
        view.selected_timezone = self.values[0]
        view.save_button.disabled = view.selected_hour is None
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class HourSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=f"{h:02d}:00", value=str(h)) for h in range(24)]
        super().__init__(placeholder="Select hour...", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        view: UpdateTimeView = self.view
        view.selected_hour = int(self.values[0])
        view.save_button.disabled = view.selected_timezone is None
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class ExistingTimeSelect(discord.ui.Select):
    def __init__(self, times):
        options = [
            discord.SelectOption(label=f"{tz}  {h:02d}:{m:02d}", value=f"{tz}|{h}|{m}")
            for tz, h, m in times
        ] or [discord.SelectOption(label="No scheduled times yet", value="__none__")]
        super().__init__(placeholder="Existing scheduled times (for removal)...", options=options, row=2)

    async def callback(self, interaction: discord.Interaction):
        view: UpdateTimeView = self.view
        if self.values[0] == "__none__":
            await interaction.response.defer()
            return
        view.selected_existing_time = self.values[0]
        view.remove_time_button.disabled = False
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class CustomTimeModal(discord.ui.Modal, title="Custom Update Time"):
    time_str = discord.ui.TextInput(label="Time (HH:MM, 24h)", placeholder="e.g. 14:30", required=True)

    def __init__(self, db, server_id, channel_id, tz_name, parent_view: "UpdateTimeView"):
        super().__init__()
        self.db = db
        self.server_id = server_id
        self.channel_id = channel_id
        self.tz_name = tz_name
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            hour_str, minute_str = self.time_str.value.strip().split(":")
            hour, minute = int(hour_str), int(minute_str)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "Please use HH:MM in 24-hour format, e.g. 14:30.", ephemeral=True
            )
            return

        await add_update_time(self.db, self.server_id, self.channel_id, self.tz_name, hour, minute)
        times = await get_update_times(self.db, self.server_id, self.channel_id)
        new_view = UpdateTimeView(self.db, self.server_id, self.channel_id, times)
        await interaction.response.edit_message(embed=new_view.build_embed(), view=new_view)


class UpdateTimeView(discord.ui.View):
    def __init__(self, db, server_id, channel_id, times):
        super().__init__(timeout=180)
        self.db = db
        self.server_id = server_id
        self.channel_id = channel_id
        self.times = times
        self.selected_timezone: Optional[str] = None
        self.selected_hour: Optional[int] = None
        self.selected_existing_time: Optional[str] = None

        self.add_item(TimezoneSelect())
        self.add_item(HourSelect())
        self.add_item(ExistingTimeSelect(times))

        self.save_button = discord.ui.Button(
            label="Save Time", style=discord.ButtonStyle.success, disabled=True, row=3
        )
        self.save_button.callback = self.on_save
        self.add_item(self.save_button)

        self.custom_time_button = discord.ui.Button(
            label="Custom Time (mods)", style=discord.ButtonStyle.secondary, row=3
        )
        self.custom_time_button.callback = self.on_custom_time
        self.add_item(self.custom_time_button)

        self.remove_time_button = discord.ui.Button(
            label="Remove Selected Time", style=discord.ButtonStyle.danger, disabled=True, row=3
        )
        self.remove_time_button.callback = self.on_remove_time
        self.add_item(self.remove_time_button)

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title=f"Update Times — <#{self.channel_id}>", color=discord.Color.teal())
        embed.add_field(
            name="Poll frequency",
            value=f"The bot checks Collectr for new data every **{INTERNAL_CHECK_FREQUENCY_HOURS} hour(s)**.",
            inline=False,
        )
        if self.times:
            lines = [f"• {tz} — {h:02d}:{m:02d}" for tz, h, m in self.times]
            embed.add_field(name="Scheduled post times", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Scheduled post times", value="None yet.", inline=False)

        warning = build_min_gap_warning(self.times, INTERNAL_CHECK_FREQUENCY_HOURS)
        if warning:
            embed.add_field(name="⚠️ Heads up", value=warning, inline=False)

        return embed

    async def refresh(self, interaction: discord.Interaction):
        times = await get_update_times(self.db, self.server_id, self.channel_id)
        new_view = UpdateTimeView(self.db, self.server_id, self.channel_id, times)
        await interaction.response.edit_message(embed=new_view.build_embed(), view=new_view)

    async def on_save(self, interaction: discord.Interaction):
        await add_update_time(self.db, self.server_id, self.channel_id, self.selected_timezone, self.selected_hour, 0)
        await self.refresh(interaction)

    async def on_custom_time(self, interaction: discord.Interaction):
        if not is_mod(interaction.user):
            await interaction.response.send_message("Only mods can set a custom time.", ephemeral=True)
            return
        await interaction.response.send_modal(
            CustomTimeModal(self.db, self.server_id, self.channel_id, self.selected_timezone or "UTC", self)
        )

    async def on_remove_time(self, interaction: discord.Interaction):
        tz, h, m = self.selected_existing_time.split("|")
        await remove_update_time(self.db, self.server_id, self.channel_id, tz, int(h), int(m))
        await self.refresh(interaction)


# =============================================================================
# THE COG
# =============================================================================

class CollectrCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db: Optional[aiosqlite.Connection] = None
        self.client = CollectrClient()

    async def cog_load(self):
        self.db = await aiosqlite.connect(DB_PATH)
        await init_db(self.db)
        await self.client.start()
        self.check_loop.change_interval(hours=INTERNAL_CHECK_FREQUENCY_HOURS)
        self.check_loop.start()

    async def cog_unload(self):
        self.check_loop.cancel()
        await self.client.close()
        if self.db:
            await self.db.close()

    # -------------------------------------------------------------------
    # BACKGROUND LOOP
    # -------------------------------------------------------------------
    @tasks.loop(hours=INTERNAL_CHECK_FREQUENCY_HOURS)
    async def check_loop(self):
        now_utc = datetime.now(dt_timezone.utc)

        async with self.db.execute("SELECT DISTINCT server_id, channel_id FROM update_time") as cur:
            channels = await cur.fetchall()

        for server_id, channel_id in channels:
            try:
                await self._maybe_run_channel_check(server_id, channel_id, now_utc)
            except Exception:
                log.exception("Failed processing channel %s in server %s", channel_id, server_id)

    @check_loop.before_loop
    async def before_check_loop(self):
        await self.bot.wait_until_ready()

    async def _maybe_run_channel_check(self, server_id: int, channel_id: int, now_utc: datetime):
        times = await get_update_times(self.db, server_id, channel_id)
        last_checked_at = await get_last_checked(self.db, server_id, channel_id)

        is_due = False
        for tz_name, hour, minute in times:
            tz = ZoneInfo(tz_name)
            local_now = now_utc.astimezone(tz)
            scheduled_local = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            scheduled_utc = scheduled_local.astimezone(dt_timezone.utc)
            if last_checked_at < scheduled_utc <= now_utc:
                is_due = True
                break

        if not is_due:
            return

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            log.warning("Channel %s not cached/found, skipping this cycle", channel_id)
            return

        users = await get_users(self.db, server_id)

        for user in users:
            try:
                diff = await process_user_update(self.client, self.db, user)
            except Exception:
                log.exception("Failed fetching collection for collectr_id=%s", user.collectr_id)
                continue

            message = format_update_message(user, diff)
            if message:
                try:
                    await channel.send(message)
                except discord.HTTPException:
                    log.exception("Failed sending update message for discord_id=%s", user.discord_id)
                await asyncio.sleep(STAGGER_SECONDS_BETWEEN_USER_DMS)

        await set_last_checked(self.db, server_id, channel_id, now_utc)

    # -------------------------------------------------------------------
    # SLASH COMMANDS
    # -------------------------------------------------------------------
    @app_commands.command(name="collectr-users", description="View and manage tracked Collectr users")
    @app_commands.guild_only()
    async def collectr_users_cmd(self, interaction: discord.Interaction):
        users = await get_users(self.db, interaction.guild_id)
        view = UserManagementView(self.db, interaction.guild_id, interaction.user, users)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)

    @app_commands.command(name="collectr-config", description="Configure Collectr update channels and times")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def collectr_config_cmd(self, interaction: discord.Interaction):
        channel_ids = await get_tracked_channels(self.db, interaction.guild_id)
        view = ServerConfigView(self.db, interaction.guild_id, channel_ids)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)

    @collectr_config_cmd.error
    async def collectr_config_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You need **Manage Server** permission to use this command.", ephemeral=True
            )
        else:
            log.exception("Unhandled error in collectr-config", exc_info=error)
            await interaction.response.send_message("Something went wrong.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(CollectrCog(bot))