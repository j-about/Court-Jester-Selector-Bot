"""Aggregation of the bot's Telegram handler registrars.

Each ``register_*`` function attaches a thematically related group of
handlers to a ``telegram.ext.Application``. They are re-exported here so the
application entry point can wire them up with a single import.
"""
from handlers.admin import register_admin_handlers
from handlers.commands import register_command_handlers
from handlers.draw import register_draw_handlers
from handlers.lifecycle import register_lifecycle_handlers
from handlers.registration import register_registration_handlers

__all__ = [
    "register_admin_handlers",
    "register_command_handlers",
    "register_draw_handlers",
    "register_lifecycle_handlers",
    "register_registration_handlers",
]
