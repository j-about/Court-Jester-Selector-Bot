"""Database query helpers used by handlers and startup routines."""
from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from models import Draw, Group, Player

__all__ = [
    "clamp_player_weights",
    "count_players",
    "get_admin_groups_by_status_page",
    "get_admin_player_ids_for_group",
    "get_approved_groups",
    "get_approved_groups_page",
    "get_group_by_telegram_id",
    "get_leaderboard",
    "get_player",
    "get_player_stats",
    "get_players_page",
]


async def clamp_player_weights(session: AsyncSession, *, min_weight: int, max_weight: int) -> tuple[int, int]:
    """Clamp every player's weight into ``[min_weight, max_weight]``.

    Returns:
        A ``(raised_count, lowered_count)`` tuple of the number of rows
        whose weight was raised to ``min_weight`` and the number lowered
        to ``max_weight``.
    """
    raised = await session.execute(
        update(Player).where(Player.weight < min_weight).values(weight=min_weight)  # ty: ignore[invalid-argument-type]
    )
    lowered = await session.execute(
        update(Player).where(Player.weight > max_weight).values(weight=max_weight)  # ty: ignore[invalid-argument-type]
    )
    return int(raised.rowcount or 0), int(lowered.rowcount or 0)  # ty: ignore[unresolved-attribute]


async def get_group_by_telegram_id(session: AsyncSession, telegram_chat_id: int) -> Group | None:
    """Return the ``Group`` with the given Telegram chat id, or ``None``."""
    result = await session.execute(select(Group).where(Group.telegram_id == telegram_chat_id))
    return result.scalars().first()


async def get_player(session: AsyncSession, group_id: int, telegram_user_id: int) -> Player | None:
    """Return the ``Player`` row for ``telegram_user_id`` in ``group_id``, or ``None``."""
    result = await session.execute(
        select(Player).where(
            Player.group_id == group_id,
            Player.telegram_id == telegram_user_id,
        )
    )
    return result.scalars().first()


async def count_players(session: AsyncSession, group_id: int) -> int:
    """Return the number of ``Player`` rows attached to ``group_id``."""
    result = await session.execute(select(func.count()).select_from(Player).where(Player.group_id == group_id))
    return int(result.scalar_one())


async def get_leaderboard(session: AsyncSession, group_id: int, limit: int = 10) -> list[tuple[Player, int]]:
    """Return up to ``limit`` ``(player, draw_count)`` tuples ordered by draw count descending.

    Players with zero draws are excluded via the inner join. A
    ``telegram_id`` tie-breaker keeps the ordering deterministic; the
    caller is responsible for collapsing ties into a shared display rank.
    """
    draw_count = func.count().label("draw_count")
    stmt = (
        select(Player, draw_count)
        .join(Draw, Draw.player_id == Player.id)  # ty: ignore[invalid-argument-type]
        .where(Player.group_id == group_id)
        .group_by(Player.id)  # ty: ignore[invalid-argument-type]
        .order_by(draw_count.desc(), Player.telegram_id.asc())  # ty: ignore[unresolved-attribute]
        .limit(limit)
    )
    result = await session.execute(stmt)
    return [(player, int(count)) for player, count in result.all()]


async def get_approved_groups(session: AsyncSession) -> list[Group]:
    """Return every approved ``Group``, ordered by ``telegram_title``."""
    stmt = (
        select(Group)
        .where(Group.approved.is_(True))  # ty: ignore[unresolved-attribute]
        .order_by(Group.telegram_title.asc())  # ty: ignore[unresolved-attribute]
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_admin_player_ids_for_group(
    session: AsyncSession,
    *,
    group_id: int,
    allowed_statuses: Sequence[str],
) -> list[int]:
    """Return distinct ``Player.telegram_id`` values in ``group_id`` whose status is in ``allowed_statuses``."""
    if not allowed_statuses:
        return []
    stmt = (
        select(Player.telegram_id)
        .where(
            Player.group_id == group_id,
            Player.status.in_(list(allowed_statuses)),  # ty: ignore[unresolved-attribute]
        )
        .distinct()
    )
    return [int(tg_id) for tg_id in (await session.execute(stmt)).scalars().all()]


async def get_approved_groups_page(session: AsyncSession, page: int, per_page: int) -> tuple[list[Group], int]:
    """Return a page of approved groups plus the total approved-group count.

    ``page`` is zero-indexed.
    """
    total_stmt = select(func.count()).select_from(Group).where(Group.approved.is_(True))  # ty: ignore[unresolved-attribute]
    total = int((await session.execute(total_stmt)).scalar_one())
    stmt = (
        select(Group)
        .where(Group.approved.is_(True))  # ty: ignore[unresolved-attribute]
        .order_by(Group.telegram_title.asc())  # ty: ignore[unresolved-attribute]
        .offset(page * per_page)
        .limit(per_page)
    )
    groups = list((await session.execute(stmt)).scalars().all())
    return groups, total


async def get_admin_groups_by_status_page(
    session: AsyncSession,
    telegram_id: int,
    allowed_statuses: Sequence[str],
    page: int,
    per_page: int,
) -> tuple[list[Group], int]:
    """Return a page of approved groups where ``telegram_id`` has an admin status, plus the total count."""
    if not allowed_statuses:
        return [], 0
    base = (
        select(Group.id)
        .join(Player, Player.group_id == Group.id)  # ty: ignore[invalid-argument-type]
        .where(
            Group.approved.is_(True),  # ty: ignore[unresolved-attribute]
            Player.telegram_id == telegram_id,
            Player.status.in_(list(allowed_statuses)),  # ty: ignore[unresolved-attribute]
        )
        .group_by(Group.id)  # ty: ignore[invalid-argument-type]
    )
    total = int((await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one())
    stmt = (
        select(Group)
        .where(Group.id.in_(base))  # ty: ignore[unresolved-attribute]
        .order_by(Group.telegram_title.asc())  # ty: ignore[unresolved-attribute]
        .offset(page * per_page)
        .limit(per_page)
    )
    groups = list((await session.execute(stmt)).scalars().all())
    return groups, total


async def get_players_page(session: AsyncSession, group_id: int, page: int, per_page: int) -> tuple[list[Player], int]:
    """Return a page of players in ``group_id`` ordered by first name, plus the total count."""
    total_stmt = select(func.count()).select_from(Player).where(Player.group_id == group_id)
    total = int((await session.execute(total_stmt)).scalar_one())
    stmt = (
        select(Player)
        .where(Player.group_id == group_id)
        .order_by(Player.telegram_first_name.asc(), Player.id.asc())  # ty: ignore[unresolved-attribute]
        .offset(page * per_page)
        .limit(per_page)
    )
    players = list((await session.execute(stmt)).scalars().all())
    return players, total


async def get_player_stats(session: AsyncSession, group_id: int, player_id: int) -> tuple[int, int]:
    """Return the player's draw count and competition rank inside ``group_id``.

    Competition ranking uses the 1, 2, 2, 4 pattern: ``rank`` equals one
    plus the number of players in the group with a strictly higher draw
    count. A player with zero draws yields ``(0, 0)``; callers treat that
    as "no selections yet" rather than a true rank.
    """
    own_count_stmt = (
        select(func.count()).select_from(Draw).where(Draw.group_id == group_id, Draw.player_id == player_id)
    )
    draw_count = int((await session.execute(own_count_stmt)).scalar_one())

    if draw_count == 0:
        return 0, 0

    per_player_counts = (
        select(func.count().label("c"))
        .select_from(Draw)
        .where(Draw.group_id == group_id)
        .group_by(Draw.player_id)  # ty: ignore[invalid-argument-type]
        .subquery()
    )
    higher_stmt = select(func.count()).select_from(per_player_counts).where(per_player_counts.c.c > draw_count)
    higher = int((await session.execute(higher_stmt)).scalar_one())
    return draw_count, higher + 1
