"""Unit and integration tests for the SQLModel table definitions."""
from __future__ import annotations

import os
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from models import Draw, Group, Player


def test_group_instantiation_defaults() -> None:
    g = Group(status="active", telegram_id=123456789012345, telegram_title="Test")
    assert g.approved is False
    assert g.approval_messages is None
    assert g.telegram_id == 123456789012345


def test_group_approval_messages_accepts_dict() -> None:
    g = Group(
        status="active",
        telegram_id=1,
        telegram_title="t",
        approval_messages={"foo": "bar"},
    )
    assert g.approval_messages == {"foo": "bar"}


def test_player_default_weight() -> None:
    p = Player(status="active", group_id=1, telegram_id=1, telegram_first_name="A")
    assert p.weight == 3


def test_draw_default_date_is_today() -> None:
    d = Draw(group_id=1, player_id=1)
    assert d.draw_date == date.today()


def test_relationship_descriptors_exist() -> None:
    assert "players" in Group.__sqlmodel_relationships__
    assert "draws" in Group.__sqlmodel_relationships__
    assert "group" in Player.__sqlmodel_relationships__
    assert "draws" in Player.__sqlmodel_relationships__
    assert "group" in Draw.__sqlmodel_relationships__
    assert "player" in Draw.__sqlmodel_relationships__


def test_table_constraints_registered() -> None:
    player_constraint_names = {c.name for c in Player.__table__.constraints}
    assert "uix_group_telegram_id" in player_constraint_names

    draw_constraint_names = {c.name for c in Draw.__table__.constraints}
    assert "uix_group_date" in draw_constraint_names


_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark_integration = pytest.mark.skipif(
    not _TEST_DB_URL, reason="TEST_DATABASE_URL not set"
)


@pytest.fixture
async def session() -> AsyncSession:  # type: ignore[misc]
    assert _TEST_DB_URL is not None
    engine = create_async_engine(_TEST_DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytestmark_integration
async def test_unique_group_telegram_id(session: AsyncSession) -> None:
    g = Group(status="active", telegram_id=111, telegram_title="G")
    session.add(g)
    await session.commit()
    g2 = Group(status="active", telegram_id=111, telegram_title="G2")
    session.add(g2)
    with pytest.raises(IntegrityError):
        await session.commit()


@pytestmark_integration
async def test_db_accepts_out_of_bounds_weight(session: AsyncSession) -> None:
    """Verify that weight bounds are enforced only in Python: the database layer accepts any integer."""
    g = Group(status="active", telegram_id=222, telegram_title="G")
    session.add(g)
    await session.flush()
    assert g.id is not None
    await session.execute(
        text(
            "INSERT INTO player (status, group_id, telegram_id, telegram_first_name, weight) "
            "VALUES ('active', :gid, 1, 'X', 99)"
        ),
        {"gid": g.id},
    )
    await session.commit()


@pytestmark_integration
async def test_clamp_player_weights_normalizes_out_of_bounds(session: AsyncSession) -> None:
    from queries import clamp_player_weights

    g = Group(status="active", telegram_id=444, telegram_title="G")
    session.add(g)
    await session.flush()
    assert g.id is not None
    await session.execute(
        text(
            "INSERT INTO player (status, group_id, telegram_id, telegram_first_name, weight) "
            "VALUES ('active', :gid, 1, 'A', 99), "
            "('active', :gid, 2, 'B', 0), "
            "('active', :gid, 3, 'C', 3)"
        ),
        {"gid": g.id},
    )
    await session.commit()

    raised, lowered = await clamp_player_weights(session, min_weight=1, max_weight=5)
    await session.commit()

    assert raised == 1
    assert lowered == 1
    weights = sorted(
        (await session.execute(text("SELECT weight FROM player ORDER BY telegram_id"))).scalars().all()
    )
    assert weights == [1, 3, 5]


@pytestmark_integration
async def test_unique_group_date_draw(session: AsyncSession) -> None:
    g = Group(status="active", telegram_id=333, telegram_title="G")
    session.add(g)
    await session.flush()
    assert g.id is not None
    p = Player(
        status="active", group_id=g.id, telegram_id=1, telegram_first_name="X"
    )
    session.add(p)
    await session.flush()
    assert p.id is not None
    today = date.today()
    session.add(Draw(group_id=g.id, player_id=p.id, draw_date=today))
    await session.commit()
    session.add(Draw(group_id=g.id, player_id=p.id, draw_date=today))
    with pytest.raises(IntegrityError):
        await session.commit()
