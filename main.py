"""Process entry point.

Builds the Telegram ``Application``, registers every handler group, wires
up post-init and post-shutdown hooks (database clamp, Sentry flush, bot
command menu reconciliation), and starts long-polling.
"""

from __future__ import annotations

import logging
import signal

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application

from config import Settings
from database import close_db, get_engine, get_session
from handlers import (
    register_admin_handlers,
    register_command_handlers,
    register_draw_handlers,
    register_lifecycle_handlers,
    register_registration_handlers,
)
from observability import configure_sentry, flush_sentry
from queries import clamp_player_weights
from utils.command_menu import reconcile_all_menus

logger = logging.getLogger(__name__)


async def _post_init(app: Application) -> None:
    """Run one-time bot startup steps.

    Clamps every stored player weight into the configured bounds, clears
    any stale default command menu, and reconciles per-chat command menus
    against the bot's current knowledge of approved groups and admins.
    """
    settings: Settings = app.bot_data["settings"]
    async with get_session() as session:
        raised, lowered = await clamp_player_weights(
            session,
            min_weight=settings.MIN_WEIGHT,
            max_weight=settings.MAX_WEIGHT,
        )
    if raised or lowered:
        logger.info(
            "bot.startup.weight_clamp",
            extra={
                "raised_to_min": raised,
                "lowered_to_max": lowered,
                "min_weight": settings.MIN_WEIGHT,
                "max_weight": settings.MAX_WEIGHT,
            },
        )

    try:
        await app.bot.delete_my_commands()
    except TelegramError:
        logger.exception("bot.startup.delete_default_commands failed")
    await reconcile_all_menus(app.bot, settings)


async def _post_shutdown(_app: Application) -> None:
    """Close the database engine and flush any pending Sentry events."""
    logger.info("bot.shutdown")
    await close_db()
    flush_sentry()


def build_application(settings: Settings) -> Application:
    """Build and wire the Telegram ``Application`` with every handler group."""
    app = Application.builder().token(settings.TG_BOT_TOKEN).post_init(_post_init).post_shutdown(_post_shutdown).build()
    app.bot_data["settings"] = settings
    app.bot_data["db_engine"] = get_engine()
    register_lifecycle_handlers(app)
    register_registration_handlers(app)
    register_draw_handlers(app)
    register_command_handlers(app)
    register_admin_handlers(app)
    return app


def main() -> None:
    """Configure logging, load settings, build the bot, and start polling."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = Settings()  # ty: ignore[missing-argument]
    configure_sentry(settings)
    app = build_application(settings)
    logger.info("bot.startup")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
        stop_signals=(signal.SIGTERM, signal.SIGINT),
    )


if __name__ == "__main__":
    main()
