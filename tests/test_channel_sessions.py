import asyncio
from pathlib import Path

from neoson_core.memory.channel_sessions import SqliteChannelSessions
from neoson_core.memory.session_store import SqliteConversationStore


def test_channel_sessions_persist_and_remain_isolated(tmp_path: Path) -> None:
    db = tmp_path / "neoson.db"
    session = asyncio.run(SqliteConversationStore(db).create_session("external"))
    sessions = SqliteChannelSessions(db)
    asyncio.run(sessions.bind("external", "42", session))

    reopened = SqliteChannelSessions(db)
    assert asyncio.run(reopened.get("external", "42")) == session
    assert asyncio.run(reopened.get("other", "42")) is None
    asyncio.run(reopened.forget("external", "42"))
    assert asyncio.run(reopened.get("external", "42")) is None
