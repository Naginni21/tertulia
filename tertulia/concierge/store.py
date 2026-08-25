"""SQLite persistence for the concierge.

One connection guarded by a re-entrant lock: the HTTP handlers, the Telegram
poller and the main tick loop all run in different threads and this keeps the
store boringly correct. Timestamps are Unix epoch seconds (floats).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS delegates (
    id                     INTEGER PRIMARY KEY,
    owner_name             TEXT    NOT NULL,
    owner_telegram_user_id INTEGER,
    token_hash             TEXT    NOT NULL UNIQUE,
    agent_name             TEXT,
    joined_at              REAL,
    last_seen_at           REAL,
    created_at             REAL    NOT NULL,
    revoked                INTEGER NOT NULL DEFAULT 0
);

-- The room transcript: everything that was posted in the Telegram group.
CREATE TABLE IF NOT EXISTS messages (
    id                  INTEGER PRIMARY KEY,
    at                  REAL    NOT NULL,
    sender_kind         TEXT    NOT NULL,   -- human | delegate | concierge
    sender_name         TEXT    NOT NULL,
    sender_owner        TEXT,               -- delegates only: the owner's name
    delegate_id         INTEGER,
    text                TEXT    NOT NULL,
    telegram_message_id INTEGER,
    ritual_id           INTEGER,
    turn_id             INTEGER
);

-- Per-delegate inbox. Delegates poll events with seq > their cursor.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    delegate_id INTEGER NOT NULL,
    seq         INTEGER NOT NULL,
    kind        TEXT    NOT NULL,
    payload     TEXT    NOT NULL,           -- JSON
    at          REAL    NOT NULL,
    UNIQUE (delegate_id, seq)
);

CREATE TABLE IF NOT EXISTS rituals (
    id          INTEGER PRIMARY KEY,
    kind        TEXT    NOT NULL,
    status      TEXT    NOT NULL,           -- running | done | aborted
    state       TEXT    NOT NULL,           -- JSON snapshot of the state machine
    started_at  REAL    NOT NULL,
    finished_at REAL
);

CREATE TABLE IF NOT EXISTS turns (
    id          INTEGER PRIMARY KEY,
    ritual_id   INTEGER NOT NULL,
    round_id    TEXT    NOT NULL,
    delegate_id INTEGER NOT NULL,
    issued_at   REAL    NOT NULL,
    deadline_at REAL    NOT NULL,
    status      TEXT    NOT NULL            -- open | answering | answered | passed | expired
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class Delegate:
    id: int
    owner_name: str
    owner_telegram_user_id: Optional[int]
    agent_name: Optional[str]
    joined_at: Optional[float]
    last_seen_at: Optional[float]
    created_at: float
    revoked: bool

    @property
    def display_name(self) -> str:
        return self.agent_name or f"delegate#{self.id}"

    def to_api(self, *, online: bool) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_name": self.agent_name,
            "owner_name": self.owner_name,
            "joined": self.joined_at is not None,
            "online": online,
        }


@dataclass
class Message:
    id: int
    at: float
    sender_kind: str
    sender_name: str
    sender_owner: Optional[str]
    delegate_id: Optional[int]
    text: str
    telegram_message_id: Optional[int]
    ritual_id: Optional[int]
    turn_id: Optional[int]

    def to_api(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Turn:
    id: int
    ritual_id: int
    round_id: str
    delegate_id: int
    issued_at: float
    deadline_at: float
    status: str


def _delegate(row: sqlite3.Row) -> Delegate:
    return Delegate(
        id=row["id"],
        owner_name=row["owner_name"],
        owner_telegram_user_id=row["owner_telegram_user_id"],
        agent_name=row["agent_name"],
        joined_at=row["joined_at"],
        last_seen_at=row["last_seen_at"],
        created_at=row["created_at"],
        revoked=bool(row["revoked"]),
    )


def _message(row: sqlite3.Row) -> Message:
    return Message(**{k: row[k] for k in Message.__dataclass_fields__})


def _turn(row: sqlite3.Row) -> Turn:
    return Turn(**{k: row[k] for k in Turn.__dataclass_fields__})


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ----------------------------------------------------------------- delegates

    def create_delegate(
        self, owner_name: str, owner_telegram_user_id: int | None = None, *, now: float | None = None
    ) -> tuple[Delegate, str]:
        """Create a delegate slot for ``owner_name`` and return it with its
        plaintext token. Only the hash is stored; the token is shown once."""
        token = "tt_" + secrets.token_urlsafe(32)
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO delegates (owner_name, owner_telegram_user_id, token_hash, created_at)"
                " VALUES (?, ?, ?, ?)",
                (owner_name, owner_telegram_user_id, hash_token(token), now or time.time()),
            )
            delegate = self.delegate(cur.lastrowid)
        assert delegate is not None
        return delegate, token

    def delegate(self, delegate_id: int) -> Delegate | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM delegates WHERE id = ?", (delegate_id,)).fetchone()
        return _delegate(row) if row else None

    def delegate_by_token(self, token: str) -> Delegate | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM delegates WHERE token_hash = ? AND revoked = 0", (hash_token(token),)
            ).fetchone()
        return _delegate(row) if row else None

    def delegates(self, *, joined_only: bool = False) -> list[Delegate]:
        sql = "SELECT * FROM delegates WHERE revoked = 0"
        if joined_only:
            sql += " AND joined_at IS NOT NULL"
        sql += " ORDER BY COALESCE(joined_at, created_at), id"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [_delegate(r) for r in rows]

    def register_hello(self, delegate_id: int, agent_name: str, *, now: float) -> bool:
        """Record a delegate's hello. Returns True the first time it joins."""
        with self._tx() as c:
            row = c.execute("SELECT joined_at FROM delegates WHERE id = ?", (delegate_id,)).fetchone()
            first_time = row is not None and row["joined_at"] is None
            c.execute(
                "UPDATE delegates SET agent_name = ?, last_seen_at = ?,"
                " joined_at = COALESCE(joined_at, ?) WHERE id = ?",
                (agent_name, now, now, delegate_id),
            )
        return first_time

    def touch(self, delegate_id: int, *, now: float) -> None:
        with self._tx() as c:
            c.execute("UPDATE delegates SET last_seen_at = ? WHERE id = ?", (now, delegate_id))

    def revoke(self, delegate_id: int) -> None:
        with self._tx() as c:
            c.execute("UPDATE delegates SET revoked = 1 WHERE id = ?", (delegate_id,))

    # ------------------------------------------------------------------ messages

    def add_message(
        self,
        *,
        at: float,
        sender_kind: str,
        sender_name: str,
        text: str,
        sender_owner: str | None = None,
        delegate_id: int | None = None,
        telegram_message_id: int | None = None,
        ritual_id: int | None = None,
        turn_id: int | None = None,
    ) -> Message:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO messages (at, sender_kind, sender_name, sender_owner, delegate_id, text,"
                " telegram_message_id, ritual_id, turn_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (at, sender_kind, sender_name, sender_owner, delegate_id, text,
                 telegram_message_id, ritual_id, turn_id),
            )
            row = c.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _message(row)

    def recent_messages(self, limit: int = 40, *, ritual_id: int | None = None) -> list[Message]:
        """Last ``limit`` messages in chronological order."""
        sql = "SELECT * FROM messages"
        params: tuple[Any, ...] = ()
        if ritual_id is not None:
            sql += " WHERE ritual_id = ?"
            params = (ritual_id,)
        sql += " ORDER BY id DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, params + (limit,)).fetchall()
        return [_message(r) for r in reversed(rows)]

    def spontaneous_count(self, delegate_id: int, *, since: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE delegate_id = ? AND turn_id IS NULL AND at >= ?",
                (delegate_id, since),
            ).fetchone()
        return int(row["n"])

    def last_message_at(self, delegate_id: int) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(at) AS t FROM messages WHERE delegate_id = ?", (delegate_id,)
            ).fetchone()
        return row["t"]

    def consecutive_delegate_tail(self) -> int:
        """How many spontaneous delegate messages sit at the tail of the
        transcript with no human message in between. Concierge notes and
        ritual turns are transparent (neither count nor reset)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT sender_kind, turn_id FROM messages ORDER BY id DESC LIMIT 100"
            ).fetchall()
        n = 0
        for row in rows:
            if row["sender_kind"] == "human":
                break
            if row["sender_kind"] == "delegate" and row["turn_id"] is None:
                n += 1
        return n

    # -------------------------------------------------------------------- events

    def push_event(self, delegate_id: int, kind: str, payload: dict[str, Any], *, at: float) -> int:
        with self._tx() as c:
            row = c.execute(
                "SELECT COALESCE(MAX(seq), 0) AS s FROM events WHERE delegate_id = ?", (delegate_id,)
            ).fetchone()
            seq = int(row["s"]) + 1
            c.execute(
                "INSERT INTO events (delegate_id, seq, kind, payload, at) VALUES (?, ?, ?, ?, ?)",
                (delegate_id, seq, kind, json.dumps(payload, ensure_ascii=False), at),
            )
        return seq

    def events_after(self, delegate_id: int, after_seq: int, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, kind, payload, at FROM events WHERE delegate_id = ? AND seq > ?"
                " ORDER BY seq LIMIT ?",
                (delegate_id, after_seq, limit),
            ).fetchall()
        return [
            {"seq": r["seq"], "kind": r["kind"], "at": r["at"], **json.loads(r["payload"])}
            for r in rows
        ]

    def latest_seq(self, delegate_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS s FROM events WHERE delegate_id = ?", (delegate_id,)
            ).fetchone()
        return int(row["s"])

    # --------------------------------------------------------------------- turns

    def create_turn(
        self, *, ritual_id: int, round_id: str, delegate_id: int, issued_at: float, deadline_at: float
    ) -> Turn:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO turns (ritual_id, round_id, delegate_id, issued_at, deadline_at, status)"
                " VALUES (?, ?, ?, ?, ?, 'open')",
                (ritual_id, round_id, delegate_id, issued_at, deadline_at),
            )
            row = c.execute("SELECT * FROM turns WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _turn(row)

    def turn(self, turn_id: int) -> Turn | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
        return _turn(row) if row else None

    def set_turn_status(self, turn_id: int, status: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE turns SET status = ? WHERE id = ?", (status, turn_id))

    # ------------------------------------------------------------------- rituals

    def create_ritual(self, kind: str, state: dict[str, Any], *, now: float) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO rituals (kind, status, state, started_at) VALUES (?, 'running', ?, ?)",
                (kind, json.dumps(state), now),
            )
        return int(cur.lastrowid)

    def save_ritual_state(self, ritual_id: int, state: dict[str, Any]) -> None:
        with self._tx() as c:
            c.execute("UPDATE rituals SET state = ? WHERE id = ?", (json.dumps(state), ritual_id))

    def finish_ritual(self, ritual_id: int, status: str, *, now: float) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE rituals SET status = ?, finished_at = ? WHERE id = ?", (status, now, ritual_id)
            )

    def abort_running_rituals(self, *, now: float) -> int:
        """Mark every 'running' ritual as aborted (used on startup: a ritual
        does not survive a concierge restart in v0). Returns how many."""
        with self._tx() as c:
            cur = c.execute(
                "UPDATE rituals SET status = 'aborted', finished_at = ? WHERE status = 'running'", (now,)
            )
            c.execute("UPDATE turns SET status = 'expired' WHERE status IN ('open', 'answering')")
        return cur.rowcount

    # ------------------------------------------------------------------------ kv

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
