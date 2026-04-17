"""Integration tests for the query helpers in ``queries.py``."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from models import Draw, Group, Player
from queries import (
    count_players,
    get_group_by_telegram_id,
    get_leaderboard,
    get_player,
    get_player_stats,
)
from tests.conftest import requires_db


async def _make_group(session: AsyncSession, telegram_id: int, approved: bool = True) -> Group:
    group = Group(status="member", telegram_id=telegram_id, telegram_title="G", approved=approved)
    session.add(group)
    await session.commit()
    return group


async def _make_player(
    session: AsyncSession,
    *,
    group_id: int,
    telegram_id: int,
    username: str | None = None,
) -> Player:
    player = Player(
        status="member",
        group_id=group_id,
        telegram_id=telegram_id,
        telegram_first_name="P",
        telegram_username=username,
        weight=3,
    )
    session.add(player)
    await session.commit()
    return player


async def _add_draws(session: AsyncSession, *, group_id: int, player_id: int, count: int, start_day: int = 0) -> None:
    """Insert ``count`` ``Draw`` rows for the given player, one per distinct day.

    The per-day offset is required because the unique constraint on
    ``(group_id, draw_date)`` forbids two draws per group per day.
    """
    base = date.today() - timedelta(days=1000)
    for i in range(count):
        session.add(
            Draw(
                draw_date=base + timedelta(days=start_day + i),
                group_id=group_id,
                player_id=player_id,
            )
        )
    await session.commit()


@requires_db
async def test_get_group_by_telegram_id_returns_group(session: AsyncSession) -> None:
    group = await _make_group(session, telegram_id=1001)
    found = await get_group_by_telegram_id(session, 1001)
    assert found is not None
    assert found.id == group.id


@requires_db
async def test_get_group_by_telegram_id_missing_returns_none(session: AsyncSession) -> None:
    assert await get_group_by_telegram_id(session, 9999) is None


@requires_db
async def test_get_player_found_and_missing(session: AsyncSession) -> None:
    group = await _make_group(session, telegram_id=1002)
    assert group.id is not None
    await _make_player(session, group_id=group.id, telegram_id=42, username="found")

    found = await get_player(session, group.id, 42)
    assert found is not None and found.telegram_username == "found"
    assert await get_player(session, group.id, 43) is None


@requires_db
async def test_count_players(session: AsyncSession) -> None:
    group = await _make_group(session, telegram_id=1003)
    assert group.id is not None
    assert await count_players(session, group.id) == 0
    for i in range(4):
        await _make_player(session, group_id=group.id, telegram_id=100 + i)
    assert await count_players(session, group.id) == 4


@requires_db
async def test_get_leaderboard_orders_by_count_desc_and_respects_limit(
    session: AsyncSession,
) -> None:
    group = await _make_group(session, telegram_id=1004)
    assert group.id is not None
    players = []
    draw_counts = [5, 3, 8, 1, 2, 4]
    start = 0
    for i, count in enumerate(draw_counts):
        player = await _make_player(session, group_id=group.id, telegram_id=200 + i)
        assert player.id is not None
        players.append(player)
        await _add_draws(
            session,
            group_id=group.id,
            player_id=player.id,
            count=count,
            start_day=start,
        )
        start += count

    result = await get_leaderboard(session, group.id, limit=3)
    assert [c for _, c in result] == [8, 5, 4]

    all_result = await get_leaderboard(session, group.id, limit=10)
    assert [c for _, c in all_result] == [8, 5, 4, 3, 2, 1]


@requires_db
async def test_get_leaderboard_excludes_zero_draw_players(session: AsyncSession) -> None:
    group = await _make_group(session, telegram_id=1005)
    assert group.id is not None
    drawn = await _make_player(session, group_id=group.id, telegram_id=301)
    await _make_player(session, group_id=group.id, telegram_id=302)
    assert drawn.id is not None
    await _add_draws(session, group_id=group.id, player_id=drawn.id, count=2)

    result = await get_leaderboard(session, group.id)
    assert len(result) == 1
    assert result[0][0].telegram_id == 301


@requires_db
async def test_get_leaderboard_empty_group(session: AsyncSession) -> None:
    group = await _make_group(session, telegram_id=1006)
    assert group.id is not None
    assert await get_leaderboard(session, group.id) == []


@requires_db
async def test_get_player_stats_competition_ranking_ties(session: AsyncSession) -> None:
    """Verify competition-ranking ties: draw counts ``(5, 5, 3, 2)`` map to ranks ``(1, 1, 3, 4)``."""
    group = await _make_group(session, telegram_id=1007)
    assert group.id is not None
    counts = [5, 5, 3, 2]
    players = []
    offset = 0
    for i, c in enumerate(counts):
        p = await _make_player(session, group_id=group.id, telegram_id=400 + i)
        assert p.id is not None
        players.append(p)
        await _add_draws(session, group_id=group.id, player_id=p.id, count=c, start_day=offset)
        offset += c

    expected_ranks = [1, 1, 3, 4]
    for player, expected_count, expected_rank in zip(players, counts, expected_ranks, strict=True):
        assert player.id is not None
        dc, rank = await get_player_stats(session, group.id, player.id)
        assert dc == expected_count
        assert rank == expected_rank


@requires_db
async def test_get_player_stats_zero_draws(session: AsyncSession) -> None:
    group = await _make_group(session, telegram_id=1008)
    assert group.id is not None
    player = await _make_player(session, group_id=group.id, telegram_id=500)
    assert player.id is not None
    assert await get_player_stats(session, group.id, player.id) == (0, 0)


@requires_db
async def test_get_player_stats_scoped_by_group(session: AsyncSession) -> None:
    g1 = await _make_group(session, telegram_id=1009)
    g2 = await _make_group(session, telegram_id=1010)
    assert g1.id is not None and g2.id is not None
    p1 = await _make_player(session, group_id=g1.id, telegram_id=600)
    # A high-draw-count player in a different group must not inflate p1's rank.
    p2 = await _make_player(session, group_id=g2.id, telegram_id=601)
    assert p1.id is not None and p2.id is not None
    await _add_draws(session, group_id=g1.id, player_id=p1.id, count=1)
    await _add_draws(session, group_id=g2.id, player_id=p2.id, count=50)

    dc, rank = await get_player_stats(session, g1.id, p1.id)
    assert dc == 1 and rank == 1
