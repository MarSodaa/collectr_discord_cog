# Collectr Tracker Discord Cog

A proof-of-concept `discord.py 2.x` cog for tracking users' Collectr collections and posting scheduled Discord updates when new cards are detected or market values move.

> **Proof of Concept — Not Currently Functional**
>
> This project is currently a **non-functional proof of concept**. The Discord-side database, scheduling, diffing, permissions, and UI logic have been implemented, but the Collectr API integration is based on an **assumed/optimistic REST API structure**.
>
> **Collectr approval and official API documentation are required before this can become a functional production integration.**
>
> Do not deploy this as a working Collectr integration until Collectr has approved the integration and provided the necessary API access/documentation.

---

## What This Cog Is Supposed to Do

The Collectr Tracker Cog is designed to connect a Discord bot to a user's Collectr collection.

Once the real Collectr API integration is available, the cog is intended to:

1. Track Collectr accounts associated with Discord users.
2. Periodically check their Collectr collections.
3. Detect newly added cards.
4. Detect changes in card market movement.
5. Apply a configurable market-movement threshold.
6. Post updates into configured Discord channels.
7. Allow servers to schedule updates using their preferred time zone.
8. Allow users to control whether their Discord account is mentioned in updates.
9. Store tracking information and previous collection data in SQLite.
10. Provide Discord slash commands and interactive menus for configuration.

The goal is to make Collectr collection updates largely automatic once the underlying Collectr API access is available.

---

# Current Status

## Non-Functional Proof of Concept

This repository should currently be treated as a **design/implementation prototype**, not a finished integration.

The Discord bot functionality has been built around the assumption that Collectr provides an API similar to:

```text
GET /v1/users/{collectr_id}/collection
```

with a response resembling:

```json
{
  "cards": [
    {
      "id": "abc123",
      "title": "Charizard VMAX",
      "market_value": 120.50,
      "market_movement": 4.25
    }
  ]
}
```

This API structure is **not being represented as an official Collectr API specification**.

The following values in the Python file are placeholders and must be confirmed/replaced when official API documentation is available:

```python
COLLECTR_API_BASE_URL = "https://api.collectr.com/v1"
COLLECTR_API_KEY = "REPLACE_ME"
```

The `CollectrClient` is intentionally isolated so that the API implementation can be replaced without having to rewrite the Discord database, scheduling, UI, and notification systems.

---

# Features

### Collection Tracking

Each Discord server can associate Discord users with their Collectr IDs.

A tracked user stores:

* Discord user ID
* Discord display name
* Collectr ID
* Discord server ID
* Market movement threshold
* Mention preference

---

### New Card Detection

The cog maintains a local copy of the previously observed collection.

When the Collectr API eventually provides current collection data, the cog compares it with the locally stored collection.

Cards that exist in the current Collectr collection but not in the stored collection are treated as new cards.

Example notification:

```text
@User, here's what's new in your Collectr collection:

New cards:
• Charizard VMAX
• Pikachu V
• Umbreon VMAX
```

---

### Market Movement Detection

The cog also stores the previous market movement value for each tracked card.

When a card's market movement changes, the cog can report the change.

Users can configure a threshold so that small movements can be ignored.

For example, a `$5` threshold means a card must have an absolute market movement of at least `$5` before it is included in the update.

Example:

```text
Market value movement:
• Charizard VMAX: +12.5
• Umbreon VMAX: -8
```

---

### Scheduled Updates

Servers can configure channels where Collectr updates should be posted.

Schedules are stored with:

* Time zone
* Hour
* Minute
* Discord channel

The cog periodically checks whether a configured update time has been reached.

The internal polling interval is currently:

```python
INTERNAL_CHECK_FREQUENCY_HOURS = 2
```

This means the bot checks whether scheduled updates are due every two hours.

The scheduled posting system and the Collectr API polling system are intentionally separate concepts.

---

### Time Zone Support

The cog uses Python's `zoneinfo` system and includes several common time zones such as:

* `US/Eastern`
* `US/Central`
* `US/Mountain`
* `US/Pacific`
* `UTC`
* `Europe/London`
* `Europe/Paris`
* `Europe/Berlin`
* `Asia/Tokyo`
* `Asia/Shanghai`
* `Asia/Kolkata`
* `Australia/Sydney`

---

### Permission Controls

The cog distinguishes between normal users and moderators.

A moderator is currently defined as a Discord member with:

```text
Manage Server
```

permissions.

Moderators can manage other users' tracking entries and configure server-wide Collectr channels and schedules.

Normal users can manage their own tracking entry.

---

### Mention Controls

Users can toggle whether update messages mention them.

With mentions enabled:

```text
@Username, here's what's new in your Collectr collection:
```

With mentions disabled:

```text
**Username**, here's what's new in your Collectr collection:
```

---

### Persistent Storage

The cog uses SQLite through `aiosqlite`.

The default database file is:

```text
collectr.db
```

The database stores:

* Tracked users
* Previously observed cards
* Scheduled update times
* Last update/check timestamps

This allows the bot to retain its tracking information across restarts.

---

# Requirements

The cog is written for:

* Python 3.9+
* `discord.py 2.x`
* `aiohttp`
* `aiosqlite`

Install the Python dependencies with:

```bash
pip install discord.py aiosqlite aiohttp
```

If your bot already has these dependencies installed, no additional installation is required.

---

# Adding the Cog to Your Discord Bot

Place:

```text
collectr_cog.py
```

in the same directory as your bot's other extensions/cogs.

For example:

```text
my-discord-bot/
├── bot.py
├── collectr_cog.py
├── other_cog.py
├── requirements.txt
└── ...
```

The cog exposes the standard `discord.py` extension setup function:

```python
async def setup(bot: commands.Bot):
    await bot.add_cog(CollectrCog(bot))
```

---

# Loading the Cog

If your bot uses `load_extension`, add the cog to your extension loading code.

For example:

```python
await bot.load_extension("collectr_cog")
```

A typical bot startup might look like:

```python
async def setup_hook(self):
    await self.load_extension("collectr_cog")
```

The exact location depends on how the existing bot is structured.

If the file is inside a package/subdirectory, use the appropriate Python module path, for example:

```python
await bot.load_extension("cogs.collectr_cog")
```

---

# Collectr API Configuration

Before this can actually communicate with Collectr, the `CollectrClient` needs to be connected to the **official Collectr API**.

Currently the file contains:

```python
COLLECTR_API_BASE_URL = "https://api.collectr.com/v1"
COLLECTR_API_KEY = "REPLACE_ME"
```

These values are placeholders.

## Do Not Treat These as Official API Details

The current implementation assumes:

```text
GET /users/{collectr_id}/collection
```

and expects card information in this general format:

```json
{
  "id": "card-id",
  "title": "Card Name",
  "market_value": 100.00,
  "market_movement": 5.00
}
```

The actual Collectr API may use completely different:

* URLs
* Authentication
* Headers
* User identifiers
* JSON structures
* Card identifiers
* Market-value fields
* Rate limits
* Permissions
* API access requirements

The `CollectrClient` should therefore be updated once Collectr provides official API documentation/access.

---

# API Key

The current proof of concept contains:

```python
COLLECTR_API_KEY = "REPLACE_ME"
```

For a real deployment, the API key should **not** be committed to GitHub.

A production implementation should load secrets from an environment variable or another secure secret-management system.

For example:

```python
import os

COLLECTR_API_KEY = os.getenv("COLLECTR_API_KEY")
```

Then configure:

```text
COLLECTR_API_KEY=your_actual_api_key
```

in the bot's environment.

The exact authentication mechanism should ultimately follow Collectr's official API requirements.

---

# Using the Cog in Discord

Once the cog is loaded into the bot, it provides two slash commands.

## `/collectr-users`

This command opens the tracked-user management interface.

It allows users to:

* Add themselves for tracking
* View tracked users
* Set a market movement threshold
* Toggle mentions
* Remove a tracked user

Moderators can additionally select another server member when adding a tracked user and manage other users' entries.

### Adding a User

Select:

```text
Add User
```

The bot will ask for:

```text
Collectr ID
```

and:

```text
Market movement threshold ($)
```

For example:

```text
Collectr ID: 8f3ac21
Threshold: 5
```

The user is then associated with that Collectr ID.

---

# `/collectr-config`

This command is restricted to members with **Manage Server** permission.

It controls where and when Collectr updates are posted.

The configuration interface allows moderators to:

* Add a Discord channel
* Remove a tracked channel
* View configured channels
* Manage scheduled update times

---

## Configuring a Channel

Run:

```text
/collectr-config
```

Then select a channel from the channel selector.

The channel becomes available for Collectr update scheduling.

---

## Configuring an Update Time

After selecting a channel, choose:

```text
Manage Update Times
```

Then select:

1. A time zone
2. An hour

The standard interface schedules times on the hour.

For example:

```text
US/Central — 18:00
```

Moderators can also use:

```text
Custom Time (mods)
```

to specify a time such as:

```text
18:30
```

using 24-hour notation.

---

# How Updates Work

The intended process is:

```text
Discord Bot
    │
    ▼
Background Scheduler
    │
    ▼
Is a configured update time due?
    │
    ├── No ──► Wait for next check
    │
    └── Yes
         │
         ▼
   Collect tracked users
         │
         ▼
   Request collection data (at times set by mods)
         │
         ▼
   Compare with stored data
         │
         ├── New cards
         │
         └── Market movement
                 │
                 ▼
          Apply threshold
                 │
                 ▼
        Build Discord message
                 │
                 ▼
        Post to configured channel
```

The cog also limits API requests so that only one Collectr request is in flight at a time.

There is currently a one-second stagger between API calls:

```python
API_REQUEST_STAGGER_SECONDS = 1
```

This is intended to reduce the likelihood of unnecessarily hammering the eventual API.

Actual rate limiting should ultimately follow Collectr's official API requirements.

---

# Database

The default database is:

```text
collectr.db
```

The cog automatically creates its tables when it loads.

The database contains four primary tables.

### `users`

Stores Discord-to-Collectr tracking relationships.

### `collection`

Stores the locally observed card collection and previous market movement values.

### `update_time`

Stores scheduled update times for each Discord channel.

### `update_log`

Stores when a channel was last processed.

The database is intended to allow the cog to recover its tracking state after the bot restarts.

---

# Important Scheduling Behavior

The cog internally checks for due schedules every two hours by default.

```python
INTERNAL_CHECK_FREQUENCY_HOURS = 2
```

This means a scheduled time is **not necessarily a precise-to-the-minute trigger**.

For example, if the bot checks at:

```text
12:00
14:00
16:00
```

and an update is scheduled for:

```text
13:00
```

the next internal check may not occur until approximately:

```text
14:00
```

The code intentionally treats the schedule as a due-check rather than implementing a separate timer for every scheduled event.

The configuration interface also warns when scheduled times are closer together than the internal polling frequency because a fresh Collectr API fetch may not occur between those times.

---

# Message Limits

The cog limits the number of cards displayed in a single update to:

```python
MAX_CARDS_PER_MESSAGE = 25
```

If more than 25 cards are detected, the message displays the first 25 and reports the remaining count.

For example:

```text
...and 14 more.
```

---

# User Update Staggering

When multiple users are being processed for a channel, the cog waits between messages:

```python
STAGGER_SECONDS_BETWEEN_USER_DMS = 2
```

This is intended to avoid sending a large number of messages simultaneously.

---

# Recommended Production Changes

Before this is considered production-ready, the following should be addressed.

## 1. Obtain Collectr Approval

The most important requirement is obtaining approval from Collectr for the intended integration.

The bot should not be represented as an officially supported Collectr integration until that approval exists.

---

## 2. Obtain Official API Documentation

The current `CollectrClient` is based on an assumed API.

The real implementation should use Collectr's documented:

* Base URL
* Authentication
* Endpoints
* Request parameters
* Response format
* Card identifiers
* Market-value fields
* Error responses
* Rate limits

---

## 3. Replace the Placeholder API Client

The intended architecture makes this relatively straightforward.

The rest of the cog expects:

```python
await client.get_collection(collectr_id)
```

to return:

```python
list[CollectrCard]
```

Therefore, the main API-specific work should remain inside `CollectrClient`.

As long as:

```python
get_collection()
```

continues returning the expected `CollectrCard` objects, the database, comparison, scheduling, UI, and notification systems can remain largely independent of the actual Collectr API implementation.

---

## 4. Secure API Credentials

Do not commit real API credentials to the repository.

Use environment variables or a secure secret store.

---

## 5. Verify API Rate Limits

The current one-request-at-a-time system is a conservative starting point, but the final implementation should follow whatever rate limits Collectr establishes.

---

## 6. Verify Collection Semantics

The proof of concept assumes that a missing card means it has been removed from the user's collection.

The production implementation should verify that this interpretation matches Collectr's actual collection API behavior.

---

# Project Architecture

The cog is intentionally divided into several logical layers.

```text
collectr_cog.py
│
├── Configuration
│
├── CollectrClient
│   └── Collectr API communication
│
├── Database Layer
│   ├── Users
│   ├── Collections
│   ├── Update Times
│   └── Update Logs
│
├── Permissions
│
├── Collection Diff / Notification Logic
│
├── Discord UI
│   ├── User Management
│   ├── Server Configuration
│   └── Update Scheduling
│
└── CollectrCog
    ├── Background Scheduler
    └── Slash Commands
```

This separation is intentional.

The API integration can be replaced without having to redesign the entire Discord-side system.

---

# Example User Flow

A typical intended setup would look like this:

### 1. Load the cog

The bot administrator loads:

```python
await bot.load_extension("collectr_cog")
```

### 2. Add tracked users

A user runs:

```text
/collectr-users
```

and adds their Collectr ID.

### 3. Configure the threshold

For example:

```text
$5
```

This means market movements smaller than `$5` are ignored.

### 4. Configure a channel

A server moderator runs:

```text
/collectr-config
```

and selects:

```text
#collectr-updates
```

### 5. Configure a schedule

For example:

```text
US/Central — 18:00
```

### 6. Background processing

The bot periodically checks whether an update is due.

Once the real Collectr API is connected, it retrieves the tracked user's collection.

### 7. Compare data

The cog compares the live collection against the locally stored state.

### 8. Post changes

If new cards or qualifying market movements are found, the bot posts an update in the configured channel.

---

# Current Limitations

This proof of concept currently has several important limitations.

* The Collectr API endpoint is assumed, not verified.
* The API authentication mechanism is assumed.
* The API response format is assumed.
* The API base URL is a placeholder.
* `REPLACE_ME` is not a valid production API credential.
* Actual Collectr API availability/access has not been established.
* API rate limits have not been confirmed.
* Collectr's terms and integration requirements have not been confirmed.

---

# Collectr Approval Disclaimer

This project is intended as a **technical proof of concept for a potential Discord/Collectr integration**.

It is **not an official Collectr integration** and should not be presented as one.

The API implementation is speculative and exists solely to demonstrate how the Discord bot integration could work if Collectr provides an appropriate API.

**Actual functionality is pending Collectr approval, API access, and official API documentation.**

Once Collectr provides the necessary approval and technical information, the `CollectrClient` can be updated to use the real API while preserving the majority of the Discord-side architecture.

---

# License / Usage

Until Collectr approval and API access are established, this repository should be treated as a **proof-of-concept implementation** rather than a production-ready Collectr integration.
