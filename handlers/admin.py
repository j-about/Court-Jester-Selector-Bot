"""Admin weight-change wizard.

Implements a private-chat, callback-driven flow that lets an authorized admin
browse the groups they can manage, pick a player, and change that player's
draw weight. Two admin tiers are supported: global User-ID admins listed in
``Settings`` and per-group status-tier admins resolved from the player's
current Telegram chat-member status.
"""
from __future__ import annotations

import logging

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

from config import Settings
from database import get_session
from models import Group, Player
from observability import log_weight_change
from queries import (
    get_admin_groups_by_status_page,
    get_approved_groups_page,
    get_players_page,
)
from utils.command_menu import CHANGE_WEIGHT_COMMAND, is_status_admin
from utils.players import player_display_name

logger = logging.getLogger(__name__)

__all__ = [
    "on_group_page",
    "on_player_page",
    "on_weight_back",
    "on_weight_confirm",
    "on_weight_select",
    "register_admin_handlers",
    "start_weight_change",
]


_NOT_AUTHORIZED = "Not authorized."
_NO_GROUPS = "No manageable groups available."
_NO_PLAYERS = "No players registered in this group yet."


async def _resolve_admin_tier(settings: Settings, telegram_id: int) -> str | None:
    """Resolve the admin tier granted to the given Telegram user.

    Returns:
        ``"user_id"`` if the user is listed in the global User-ID admin set,
        ``"status"`` if they hold an allowed chat-member status in at least
        one registered group, or ``None`` otherwise.
    """
    if telegram_id in settings.TG_BOT_ADMIN_RIGHTS_USER_IDS:
        return "user_id"
    async with get_session() as session:
        if await is_status_admin(session, telegram_id=telegram_id, settings=settings):
            return "status"
    return None


async def _fetch_group_page(settings: Settings, telegram_id: int, tier: str, page: int) -> tuple[list[Group], int]:
    async with get_session() as session:
        if tier == "user_id":
            return await get_approved_groups_page(session, page, settings.GROUPS_PER_PAGE)
        return await get_admin_groups_by_status_page(
            session,
            telegram_id=telegram_id,
            allowed_statuses=settings.TG_BOT_ADMIN_RIGHTS_CHAT_MEMBER_STATUS,
            page=page,
            per_page=settings.GROUPS_PER_PAGE,
        )


def _build_group_keyboard(groups: list[Group], page: int, total: int, per_page: int) -> InlineKeyboardMarkup:
    """Build a paginated inline keyboard listing the given groups.

    Each group occupies its own row; a trailing navigation row adds Previous
    and Next buttons when the corresponding neighbouring page exists.
    """
    buttons: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                g.telegram_title or str(g.telegram_id),
                callback_data=f"wp:{g.id}:0",
            )
        ]
        for g in groups
        if g.id is not None
    ]
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Previous", callback_data=f"wg:{page - 1}"))
    if (page + 1) * per_page < total:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"wg:{page + 1}"))
    if nav:
        buttons.append(nav)
    return InlineKeyboardMarkup(buttons)


def _build_player_keyboard(
    players: list[Player], group_id: int, page: int, total: int, per_page: int
) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                f"{player_display_name(p)} (w:{p.weight})",
                callback_data=f"ws:{p.id}",
            )
        ]
        for p in players
        if p.id is not None
    ]
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Previous", callback_data=f"wp:{group_id}:{page - 1}"))
    if (page + 1) * per_page < total:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"wp:{group_id}:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("↩ Back to groups", callback_data="wb:g")])
    return InlineKeyboardMarkup(buttons)


def _build_weight_keyboard(player_id: int, group_id: int, current: int, min_w: int, max_w: int) -> InlineKeyboardMarkup:
    row: list[InlineKeyboardButton] = []
    for w in range(min_w, max_w + 1):
        label = f"✓ {w}" if w == current else str(w)
        row.append(InlineKeyboardButton(label, callback_data=f"wc:{player_id}:{w}"))
    buttons = [row, [InlineKeyboardButton("↩ Back to players", callback_data=f"wb:p:{group_id}")]]
    return InlineKeyboardMarkup(buttons)


async def start_weight_change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start the private-chat weight-change wizard.

    Validates the caller's admin tier, caches it on ``context.user_data``,
    loads the first page of manageable groups, and replies with the group
    selection keyboard.
    """
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    settings: Settings = context.application.bot_data["settings"]
    tier = await _resolve_admin_tier(settings, user.id)
    if tier is None:
        await message.reply_text(_NOT_AUTHORIZED)
        return

    if context.user_data is not None:
        context.user_data["wc_admin_tier"] = tier

    groups, total = await _fetch_group_page(settings, user.id, tier, page=0)
    if not groups:
        await message.reply_text(_NO_GROUPS)
        return

    keyboard = _build_group_keyboard(groups, page=0, total=total, per_page=settings.GROUPS_PER_PAGE)
    await message.reply_text("Select a group:", reply_markup=keyboard)


async def _render_group_page(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    page: int,
    *,
    message: Message | None = None,
    query: CallbackQuery | None = None,
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    tier = (context.user_data or {}).get("wc_admin_tier") if context.user_data else None
    if tier is None:
        tier = await _resolve_admin_tier(settings, user_id)
        if tier is None:
            if message is not None:
                await message.reply_text(_NOT_AUTHORIZED)
            return
        if context.user_data is not None:
            context.user_data["wc_admin_tier"] = tier

    groups, total = await _fetch_group_page(settings, user_id, tier, page=page)
    if not groups:
        text = _NO_GROUPS
        keyboard = InlineKeyboardMarkup([])
    else:
        text = "Select a group:"
        keyboard = _build_group_keyboard(groups, page=page, total=total, per_page=settings.GROUPS_PER_PAGE)

    if query is not None:
        await query.edit_message_text(text, reply_markup=keyboard)
    elif message is not None:
        await message.reply_text(text, reply_markup=keyboard)


async def _ensure_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Re-verify the caller's admin rights on every callback query.

    Returns ``True`` iff the current user still qualifies as an admin of
    either tier. User-ID admins are checked against the static setting;
    status-tier admins are re-resolved from the database every time.
    """
    query = update.callback_query
    if query is None or query.from_user is None:
        return False
    settings: Settings = context.application.bot_data["settings"]
    cached = (context.user_data or {}).get("wc_admin_tier") if context.user_data else None
    if cached == "user_id":
        return query.from_user.id in settings.TG_BOT_ADMIN_RIGHTS_USER_IDS
    # A status-tier admin's Telegram chat-member status can change between the
    # wizard's first step and any later button press, so the tier must be
    # re-resolved on every callback rather than trusting the cached value.
    tier = await _resolve_admin_tier(settings, query.from_user.id)
    if tier is None:
        return False
    if context.user_data is not None:
        context.user_data["wc_admin_tier"] = tier
    return True


async def on_group_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-render the group list at the page encoded in the callback data."""
    query = update.callback_query
    if query is None or query.data is None or query.from_user is None:
        return
    await query.answer()
    if not await _ensure_admin(update, context):
        await query.answer(_NOT_AUTHORIZED, show_alert=True)
        return
    try:
        page = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        return
    await _render_group_page(context, query.from_user.id, page=page, query=query)


async def on_player_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Render the paginated player list for the group encoded in the callback."""
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()
    if not await _ensure_admin(update, context):
        await query.answer(_NOT_AUTHORIZED, show_alert=True)
        return
    try:
        _, group_id_str, page_str = query.data.split(":")
        group_id = int(group_id_str)
        page = int(page_str)
    except ValueError:
        return

    settings: Settings = context.application.bot_data["settings"]
    async with get_session() as session:
        players, total = await get_players_page(session, group_id, page, settings.PLAYERS_PER_PAGE)

    if context.user_data is not None:
        context.user_data["wc_group_id"] = group_id

    if not players:
        await query.edit_message_text(
            _NO_PLAYERS,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back to groups", callback_data="wb:g")]]),
        )
        return

    keyboard = _build_player_keyboard(
        players, group_id=group_id, page=page, total=total, per_page=settings.PLAYERS_PER_PAGE
    )
    await query.edit_message_text("Select a player:", reply_markup=keyboard)


async def on_weight_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Render the weight-picker keyboard for the player encoded in the callback."""
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()
    if not await _ensure_admin(update, context):
        await query.answer(_NOT_AUTHORIZED, show_alert=True)
        return
    try:
        player_id = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        return

    settings: Settings = context.application.bot_data["settings"]
    async with get_session() as session:
        player = await session.get(Player, player_id)

    if player is None or player.id is None:
        await query.edit_message_text("Player no longer exists.")
        return

    group_id = player.group_id
    if context.user_data is not None:
        context.user_data["wc_group_id"] = group_id

    keyboard = _build_weight_keyboard(
        player_id=player.id,
        group_id=group_id,
        current=player.weight,
        min_w=settings.MIN_WEIGHT,
        max_w=settings.MAX_WEIGHT,
    )
    await query.edit_message_text(
        f"Select new weight for {player_display_name(player)} (current: {player.weight}):",
        reply_markup=keyboard,
    )


async def on_weight_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Persist the selected weight and emit a weight-change audit event.

    Rejects values outside the configured ``MIN_WEIGHT`` / ``MAX_WEIGHT``
    range and no-ops when the new value equals the current one.
    """
    query = update.callback_query
    if query is None or query.data is None or query.from_user is None:
        return
    await query.answer()
    if not await _ensure_admin(update, context):
        await query.answer(_NOT_AUTHORIZED, show_alert=True)
        return
    try:
        _, player_id_str, weight_str = query.data.split(":")
        player_id = int(player_id_str)
        new_weight = int(weight_str)
    except ValueError:
        return

    settings: Settings = context.application.bot_data["settings"]
    if not (settings.MIN_WEIGHT <= new_weight <= settings.MAX_WEIGHT):
        await query.answer("Weight out of range.", show_alert=True)
        return

    try:
        async with get_session() as session:
            player = await session.get(Player, player_id)
            if player is None or player.id is None:
                await query.edit_message_text("Player no longer exists.")
                return
            old_weight = player.weight
            player_telegram_id = player.telegram_id
            name = player_display_name(player)
            if old_weight == new_weight:
                await query.edit_message_text(f"{name}'s weight is already {new_weight}. No change.")
                return
            player.weight = new_weight
    except Exception:
        logger.exception(
            "admin.weight_change failed",
            extra={
                "admin_id": query.from_user.id,
                "player_id": player_id,
                "new_weight": new_weight,
            },
        )
        await query.edit_message_text("Failed to update weight.")
        return

    log_weight_change(
        admin_id=query.from_user.id,
        player_id=player_telegram_id,
        old_weight=old_weight,
        new_weight=new_weight,
    )
    await query.edit_message_text(f"{name}'s weight changed from {old_weight} to {new_weight}.")


async def on_weight_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle back-navigation in the wizard.

    Callback data ``wb:g`` returns to group page 0; ``wb:p:{group_id}``
    returns to player page 0 of the given group.
    """
    query = update.callback_query
    if query is None or query.data is None or query.from_user is None:
        return
    await query.answer()
    if not await _ensure_admin(update, context):
        await query.answer(_NOT_AUTHORIZED, show_alert=True)
        return
    parts = query.data.split(":")
    if len(parts) == 2 and parts[1] == "g":
        await _render_group_page(context, query.from_user.id, page=0, query=query)
        return
    if len(parts) == 3 and parts[1] == "p":
        try:
            group_id = int(parts[2])
        except ValueError:
            return
        settings: Settings = context.application.bot_data["settings"]
        async with get_session() as session:
            players, total = await get_players_page(session, group_id, 0, settings.PLAYERS_PER_PAGE)
        if not players:
            await query.edit_message_text(
                _NO_PLAYERS,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back to groups", callback_data="wb:g")]]),
            )
            return
        keyboard = _build_player_keyboard(
            players, group_id=group_id, page=0, total=total, per_page=settings.PLAYERS_PER_PAGE
        )
        await query.edit_message_text("Select a player:", reply_markup=keyboard)


def register_admin_handlers(app: Application) -> None:
    """Register the admin weight-change wizard handlers on the Application."""
    app.add_handler(CommandHandler(CHANGE_WEIGHT_COMMAND, start_weight_change, filters=filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(on_group_page, pattern=r"^wg:\d+$"))
    app.add_handler(CallbackQueryHandler(on_player_page, pattern=r"^wp:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(on_weight_select, pattern=r"^ws:\d+$"))
    app.add_handler(CallbackQueryHandler(on_weight_confirm, pattern=r"^wc:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(on_weight_back, pattern=r"^wb:(g|p:\d+)$"))
