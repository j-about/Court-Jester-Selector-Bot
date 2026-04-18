# Court Jester Selector Bot

🃏 A fun Telegram bot that crowns a random friend as the day's entertainer based on weighted randomization. All hail the royal jester! 🎪

[![Python](https://img.shields.io/badge/python-3.13.13%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](./LICENSE)
[![Code style: Ruff](https://img.shields.io/badge/code%20style-ruff-D7FF64.svg)](https://docs.astral.sh/ruff/)
[![Type checker: ty](https://img.shields.io/badge/types-ty-5d21d2.svg)](https://github.com/astral-sh/ty)

## Overview

Court Jester Selector Bot is a Telegram group bot that picks a "jester of the day" from the group's members using a per-player weighted random draw. It is aimed at friend-group chats that want a playful daily ritual without anyone picking by hand.

Each Telegram group the bot joins becomes a [`Group`](./models.py) row; every user the bot observes in that group is tracked as a [`Player`](./models.py) with an integer `weight`; and every `/crown_the_jester` invocation produces one [`Draw`](./models.py) row. A unique constraint on `(group_id, draw_date)` guarantees at most one jester per group per calendar day, where the day rolls over at midnight in the IANA timezone set by `DRAW_TIMEZONE` (default `UTC`). Groups must be approved (by a configurable set of admin Telegram user IDs) before any interactive command is honored.

The bot is async-first (`python-telegram-bot` 22 + `SQLModel` over `asyncpg`), observes itself via Sentry, and is shipped as a Docker image that runs Alembic migrations on every start.

## Key Features

- Weighted random daily draw, idempotent per group per day via a `UniqueConstraint("group_id", "draw_date")` on the `draw` table.
- Group approval workflow driven by `TG_BOT_ADMIN_RIGHTS_CHAT_MEMBER_STATUS` and `TG_BOT_ADMIN_RIGHTS_USER_IDS`, with per-admin approval prompts persisted on the `group` row.
- Per-player weight tuning by group admins, clamped at startup and write time into `[MIN_WEIGHT, MAX_WEIGHT]`.
- Default commands `/crown_the_jester`, `/court_leaderboard`, and `/my_jester_stats` — all renamable via environment variables.
- Every bot-visible message is an env-overridable template with validated `{username}` / `{rank}` / `{draw_count}` / `{min_players}` placeholders.
- Sentry integration with bot-token scrubbing and structured audit logging in [`observability.py`](./observability.py).
- Schema managed by Alembic; migrations are applied on container start by [`docker-entrypoint.sh`](./docker-entrypoint.sh).
- Fully containerized deployment (Dockerfile + `docker-compose.yml`) with an Ansible skeleton under [`playbooks/`](./playbooks/) and [`roles/`](./roles/).

## Requirements

- Python **3.13.13** or newer (`requires-python = ">=3.13.13"` in [pyproject.toml](./pyproject.toml)).
- PostgreSQL 18 (the reference stack pins `postgres:18.3-trixie` in [docker-compose.yml](./docker-compose.yml)).
- Docker and Docker Compose for the supported deployment workflow.
- [`uv`](https://docs.astral.sh/uv/) for local development (the project is locked with [`uv.lock`](./uv.lock)).
- A Telegram bot token from [@BotFather](https://t.me/BotFather).

## Installation

This package is not published to PyPI. Use one of the two supported workflows below.

### Docker Compose (recommended)

```bash
git clone git@github.com:j-about/Court-Jester-Selector-Bot.git
cd Court-Jester-Selector-Bot
cp .env.example .env
# edit .env and fill in the Required section (token + Postgres credentials)
docker compose up -d --build
```

`docker compose up` builds the bot image from the [Dockerfile](./Dockerfile) (nonroot `uid:gid 999:999`, `uv sync --locked`), starts a healthchecked Postgres, and on every bot start runs `alembic upgrade head` before `python main.py` begins long-polling.

### From source with `uv`

```bash
git clone git@github.com:j-about/Court-Jester-Selector-Bot.git
cd Court-Jester-Selector-Bot
uv sync                 # installs runtime deps and the `dev` group
cp .env.example .env    # fill in the Required section; point POSTGRES_HOST at your DB
uv run alembic upgrade head
uv run court-jester-bot # equivalent to: uv run python main.py
```

The `court-jester-bot` console script is declared in [pyproject.toml](./pyproject.toml) and maps to [`main:main`](./main.py).

## Quick Start

1. **Create and configure the bot with @BotFather.** Message [@BotFather](https://t.me/BotFather) and run `/newbot` to obtain a token for `TG_BOT_TOKEN`. Then, on the same bot, run `/setprivacy` and choose **Disable** so the bot receives every message posted in the groups it joins. Privacy mode must be off for the auto-enrollment model described in the [Overview](#overview) to work — with privacy on, the bot would only see commands and @mentions and could never register silent members as `Player` rows.
2. **Configure the environment.** Copy `.env.example` to `.env` and fill in at minimum `TG_BOT_TOKEN`, `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD`. To bypass approval for yourself, put your numeric Telegram user id into `TG_BOT_ADMIN_RIGHTS_USER_IDS`. Leaving `TG_BOT_ADMIN_RIGHTS_USER_IDS` empty disables the approval workflow entirely and auto-approves every group the bot is added to — convenient for single-operator or private deployments, but unsafe on a bot that can be added to arbitrary groups. Every Telegram user ID you put into `TG_BOT_ADMIN_RIGHTS_USER_IDS` must have privately messaged the bot at least once (open the bot's chat and tap **Start**) **before** the bot is added to its first group; Telegram refuses direct messages from a bot to a user who has never initiated the conversation, and the group-approval prompt would be silently dropped for that admin (see the swallowed `TelegramError` in [`handlers/lifecycle.py`](./handlers/lifecycle.py)).
3. **Start the stack.**

   ```bash
   docker compose up -d --build
   ```

4. **Add the bot to a Telegram group.** On join, the bot posts an approval request to each configured admin. Once any admin approves, the group becomes interactive.
5. **Crown a jester.** In the approved group, any member sends:

   ```text
   /crown_the_jester
   ```

   Expected reply (default template):

   ```text
   🎪 By royal decree, @alice is hereby appointed as today's Royal Entertainer! The throne awaits your foolery! 🎭
   ```

Sending `/crown_the_jester` a second time on the same calendar day returns the already-crowned player rather than re-rolling.

## Usage

The three default commands below are registered for approved groups only. Every name and message below is configurable; the values shown are the shipping defaults.

### Crown the daily jester

```text
/crown_the_jester
```

Picks one `Player` at random, weighted by each player's `weight` field (default `3`, range `[MIN_WEIGHT, MAX_WEIGHT]`). The group must have at least `MIN_PLAYERS` (default `10`) registered members; otherwise the bot replies with `NOT_ENOUGH_PLAYERS_MESSAGE`. The result is written to the `draw` table; re-issuing the command the same day is a no-op that echoes the existing winner.

### View rankings and personal stats

```text
/court_leaderboard
/my_jester_stats
```

`/court_leaderboard` prints a paginated list of players ordered by their all-time draw count, using `LEADERBOARD_INTRO_MESSAGE`, `LEADERBOARD_RANK_MESSAGE`, and `LEADERBOARD_OUTRO_MESSAGE`. `/my_jester_stats` shows the caller's own draw count and rank, falling back to `PERSONAL_STATS_NO_PICKED_PLAYER_MESSAGE` when they have never been crowned.

### Admin weight tuning

Group admins (as defined by `TG_BOT_ADMIN_RIGHTS_CHAT_MEMBER_STATUS`, and anyone listed in `TG_BOT_ADMIN_RIGHTS_USER_IDS`) interact with the admin handler registered by [`handlers/admin.py`](./handlers/admin.py) to adjust individual players' `weight` values. The bot validates every new weight against `[MIN_WEIGHT, MAX_WEIGHT]` and also clamps all stored weights back into that range at startup (see `clamp_player_weights` in [`queries.py`](./queries.py)).

## Configuration

All configuration flows through the `Settings` class in [`config.py`](./config.py), which is a [`pydantic-settings`](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) model that loads variables from the process environment and from a `.env` file at the working directory. Unknown variables are ignored. A complete, commented template lives in [`.env.example`](./.env.example).

### Required

| Name | Type | Default | Description |
|---|---|---|---|
| `TG_BOT_TOKEN` | string | — | Telegram bot API token from @BotFather. |
| `POSTGRES_DB` | string | — | Database name. |
| `POSTGRES_USER` | string | — | Database user. |
| `POSTGRES_PASSWORD` | secret string | — | Database password. |

### Database

| Name | Type | Default | Description |
|---|---|---|---|
| `POSTGRES_HOST` | string | `db` | Hostname of the Postgres server (defaults to the compose service name). |
| `POSTGRES_PORT` | int | `5432` | Postgres port. |

### Admin rights

| Name | Type | Default | Description |
|---|---|---|---|
| `TG_BOT_ADMIN_RIGHTS_CHAT_MEMBER_STATUS` | CSV of `creator\|administrator` | `creator,administrator` | Which Telegram chat-member statuses confer admin powers inside a group. |
| `TG_BOT_ADMIN_RIGHTS_USER_IDS` | CSV of positive ints | empty | Telegram user IDs that are always treated as bot admins and receive per-group approval prompts. If left empty, every newly-joined group is auto-approved (no approval workflow runs); set this to at least one trusted Telegram user ID to keep human gating in place. **Each listed admin must have opened a private chat with the bot and pressed `/start` before the bot is added to any group** — otherwise Telegram will refuse the DM and that admin will not receive the approval prompt. |

### Draw weights and pagination

| Name | Type | Default | Description |
|---|---|---|---|
| `MIN_WEIGHT` | int (≥ 0) | `1` | Lower bound for a player's draw weight. |
| `MAX_WEIGHT` | int (≥ `MIN_WEIGHT`) | `5` | Upper bound for a player's draw weight. |
| `DEFAULT_WEIGHT` | int (within `[MIN_WEIGHT, MAX_WEIGHT]`) | `3` | Weight assigned to newly observed players. |
| `GROUPS_PER_PAGE` | int (≥ 1) | `5` | Page size for the admin group listing. |
| `PLAYERS_PER_PAGE` | int (≥ 1) | `5` | Page size for the admin player listing. |
| `MIN_PLAYERS` | int (≥ 2) | `10` | Minimum player count before draws/leaderboard/stats are allowed. |

### Command names and descriptions

Each command name must match `^[a-z][a-z0-9_]{0,31}$`.

| Name | Type | Default | Description |
|---|---|---|---|
| `PICK_PLAYER_COMMAND` | string | `crown_the_jester` | Slash command that performs the daily draw. |
| `PICK_PLAYER_COMMAND_DESCRIPTION` | string | `Crown today's jester.` | Help text shown by Telegram. |
| `SHOW_LEADERBOARD_COMMAND` | string | `court_leaderboard` | Slash command that prints the leaderboard. |
| `SHOW_LEADERBOARD_COMMAND_DESCRIPTION` | string | `View the court rankings.` | Help text shown by Telegram. |
| `SHOW_PERSONAL_STATS_COMMAND` | string | `my_jester_stats` | Slash command that prints the caller's stats. |
| `SHOW_PERSONAL_STATS_COMMAND_DESCRIPTION` | string | `Check your jester stats.` | Help text shown by Telegram. |

### Message templates

All templates default to the values in [`config.py`](./config.py) and support the placeholders shown below. Placeholders marked *required* are validated at load time; omitting them raises a `ValueError` before the bot starts.

| Name | Required placeholders | Optional placeholders |
|---|---|---|
| `NON_APPROVED_GROUP_MESSAGE` | — | — |
| `NOT_ENOUGH_PLAYERS_MESSAGE` | — | `{min_players}` |
| `PICK_PLAYER_PICKED_PLAYER_MESSAGE` | `{username}` | — |
| `LEADERBOARD_INTRO_MESSAGE` | — | — |
| `LEADERBOARD_RANK_MESSAGE` | `{rank}`, `{username}` | `{draw_count}` |
| `LEADERBOARD_OUTRO_MESSAGE` | — | — |
| `LEADERBOARD_NOT_ENOUGH_PICKED_PLAYERS_MESSAGE` | — | — |
| `PERSONAL_STATS_MESSAGE` | `{draw_count}` | `{username}`, `{rank}` |
| `PERSONAL_STATS_NO_PICKED_PLAYER_MESSAGE` | — | `{username}` |

### Draw timing

| Name | Type | Default | Description |
|---|---|---|---|
| `DRAW_TIMEZONE` | IANA tz name | `UTC` | Timezone whose midnight defines the rollover between one `draw_date` and the next. Any name accepted by [`zoneinfo.ZoneInfo`](https://docs.python.org/3/library/zoneinfo.html) works (e.g. `Europe/Paris`, `America/New_York`); invalid names fail fast at startup. The configured zone is also emitted on every `draw.execution` audit record alongside the UTC instant of the decision. |

### Observability

| Name | Type | Default | Description |
|---|---|---|---|
| `SENTRY_DSN` | string | *(unset)* | If set, errors and audit events are reported via the Sentry SDK. The bot token is scrubbed from outgoing events by [`observability.py`](./observability.py). |

## Development

Local development uses [`uv`](https://docs.astral.sh/uv/) end-to-end. `uv sync` installs both the runtime dependencies and the `dev` [dependency group](./pyproject.toml) (`pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`, `ty`).

```bash
uv sync                                                   # install runtime + dev deps
uv run ruff check .                                       # lint
uv run ruff format .                                      # format
uv run ty check                                           # type-check (see scope in pyproject.toml)
uv run pytest                                             # run the suite (coverage is on by default)
uv run alembic revision --autogenerate -m "describe it"   # create a new migration
uv run alembic upgrade head                               # apply migrations to the configured DB
```

Ruff is configured with `line-length = 120`, `target-version = "py313"`, and rule set `E, F, W, I, UP, B, SIM, RUF` (see [pyproject.toml](./pyproject.toml)). `ty` is Astral's type checker and is currently in alpha (`0.0.31`); its configured scope is the top-level modules plus the [`handlers/`](./handlers/) package.

## Testing

The suite lives under [`tests/`](./tests/) and is discovered by pytest via `testpaths = ["tests"]`. `asyncio_mode = "auto"` is set globally, so `async def test_*` functions need no decorator. Coverage is wired into pytest's default `addopts`, so `uv run pytest` already enforces the **85 %** floor over `config`, `models`, `database`, `queries`, `observability`, `handlers`, and `utils`:

```bash
uv run pytest                                      # terminal coverage + 85% gate
uv run pytest --cov-report=html -- tests/some_file # HTML report at htmlcov/index.html
```

The suite covers the settings model, handlers, queries, and the observability module. `main.py` and the Alembic migration folder are explicitly omitted from coverage — see `[tool.coverage.run]` in [pyproject.toml](./pyproject.toml).

## Deployment

The shipping deployment path is Docker Compose. The [Dockerfile](./Dockerfile) uses the `ghcr.io/astral-sh/uv:python3.13-trixie-slim` base image, runs as a dedicated nonroot user, and installs a locked, pre-compiled bytecode environment via `uv sync --locked`. On container start, [`docker-entrypoint.sh`](./docker-entrypoint.sh) runs `alembic upgrade head` before execing the configured `CMD` (`python main.py`).

An Ansible skeleton is present for server provisioning — see [`inventory.yaml`](./inventory.yaml), [`playbooks/cjsb_deployment.yaml`](./playbooks/cjsb_deployment.yaml), and the role under [`roles/cjsb_deployment/`](./roles/cjsb_deployment/). The inventory reads `HOST_IP`, `HOST_USER`, and `HOST_SSH_PRIVATE_KEY_FILE` from the environment, mirroring the Deployment section of [`.env.example`](./.env.example).

## License

Released under the [MIT License](./LICENSE), © 2026 Jonathan About.
