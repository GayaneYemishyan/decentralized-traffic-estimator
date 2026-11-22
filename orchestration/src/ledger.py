"""Tamper-evident, append-only ledger (SQLite + SHA-256 hash chain).

Guarantees
  * Append-only at the database level: UPDATE and DELETE are aborted by triggers.
  * Tamper-evident: every entry commits to its predecessor
    (``prev_hash`` -> ``entry_hash``), so edits, deletions in the middle and
    reordering are detected by :meth:`Ledger.verify_chain`.
  * Durable: WAL + ``synchronous=FULL`` so an acknowledged append survives power loss.
  * Duplicate-safe: a given ``message_id`` can be recorded only once.

Known limit (inherent to any local hash chain): someone with file access who
rewrites the *whole* chain from the tampered entry onward, or truncates the
tail, produces a chain that is internally consistent. Defend against it by
anchoring :meth:`Ledger.head` somewhere the attacker cannot reach (printed
log, peer, removable media) and passing it as ``expected_head`` to
``verify_chain``.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .serializer import ContractError, canonical_json, utc_now_iso, validate_state

GENESIS_HASH = "0" * 64
SCHEMA_VERSION = 1
ENTRY_TYPES = ("state", "action")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    seq            INTEGER PRIMARY KEY,
    ts             TEXT NOT NULL,
    entry_type     TEXT NOT NULL CHECK (entry_type IN ('state', 'action')),
    origin_node_id TEXT NOT NULL,
    message_id     TEXT,
    payload        TEXT NOT NULL,
    prev_hash      TEXT NOT NULL,
    entry_hash     TEXT NOT NULL UNIQUE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_message_id ON entries(message_id);
CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE ON entries
BEGIN SELECT RAISE(ABORT, 'ledger is append-only: UPDATE forbidden'); END;
CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON entries
BEGIN SELECT RAISE(ABORT, 'ledger is append-only: DELETE forbidden'); END;
"""


# --------------------------------------------------------------------------- errors
class LedgerError(Exception):
    """Base class for ledger failures."""


class DuplicateMessageError(LedgerError):
    """The message_id is already recorded (duplicate peer envelope)."""


# --------------------------------------------------------------------------- types
@dataclass(frozen=True)
class LedgerEntry:
    """One immutable ledger row."""

    seq: int
    ts: str
    entry_type: str
    origin_node_id: str
    message_id: str | None
    payload: Mapping[str, Any]
    prev_hash: str
    entry_hash: str


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of a chain verification. Truthy when the chain is intact."""

    ok: bool
    entries_checked: int
    head_seq: int
    head_hash: str
    bad_seq: int | None = None
    error: str | None = None

    def __bool__(self) -> bool:
        return self.ok


def compute_entry_hash(
    seq: int,
    ts: str,
    entry_type: str,
    origin_node_id: str,
    message_id: str | None,
    payload: Mapping[str, Any],
    prev_hash: str,
) -> str:
    """SHA-256 over the canonical JSON of every field except ``entry_hash``."""
    body = {
        "seq": seq,
        "ts": ts,
        "entry_type": entry_type,
        "origin_node_id": origin_node_id,
        "message_id": message_id,
        "payload": payload,
        "prev_hash": prev_hash,
    }
    return hashlib.sha256(canonical_json(body)).hexdigest()


# --------------------------------------------------------------------------- ledger
class Ledger:
    """Append-only hash-chained ledger. Thread-safe (single writer lock)."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = str(path)
        self._clock = clock
        self._lock = threading.Lock()
        try:
            self._conn = sqlite3.connect(
                self._path, timeout=5.0, isolation_level=None, check_same_thread=False
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise LedgerError(
                    f"ledger schema version {version} unsupported (expected {SCHEMA_VERSION})"
                )
            if version == 0:
                self._conn.executescript(_SCHEMA)
                self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except sqlite3.Error as exc:
            raise LedgerError(f"cannot open ledger at {self._path}: {exc}") from exc

    # ---- lifecycle
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

 


    # ---- writes
    def append(
        self,
        entry_type: str,
        payload: Mapping[str, Any],
        *,
        origin_node_id: str,
        message_id: str | None = None,
    ) -> LedgerEntry:
        """Append one entry and return it.

        ``state`` entries are validated against the state contract, must have
        ``payload.node_id == origin_node_id`` and may carry the envelope's
        ``message_id`` for duplicate suppression. ``action`` entries are
        internal records (free-form payload) and must not carry a message_id.
        """
        if entry_type not in ENTRY_TYPES:
            raise LedgerError(f"unknown entry_type {entry_type!r}; expected one of {ENTRY_TYPES}")
        if not origin_node_id:
            raise LedgerError("origin_node_id must be non-empty")
        if entry_type == "state":
            try:
                validate_state(payload)
            except ContractError as exc:
                raise LedgerError(f"refusing to record invalid state: {exc}") from exc
            if payload["node_id"] != origin_node_id:
                raise LedgerError("state.node_id does not match origin_node_id")
        elif message_id is not None:
            raise LedgerError("action entries must not carry a message_id")
        try:
            payload_text = canonical_json(payload).decode("utf-8")
        except (TypeError, ValueError) as exc:
            raise LedgerError(f"payload is not serializable as strict JSON: {exc}") from exc



        ts = utc_now_iso(self._clock() if self._clock else None)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    if message_id is not None and self._conn.execute(
                        "SELECT 1 FROM entries WHERE message_id = ?", (message_id,)
                    ).fetchone():
                        raise DuplicateMessageError(f"message_id {message_id} already recorded")
                    row = self._conn.execute(
                        "SELECT seq, entry_hash FROM entries ORDER BY seq DESC LIMIT 1"
                    ).fetchone()
                    seq, prev_hash = (row[0] + 1, row[1]) if row else (1, GENESIS_HASH)
                    entry_hash = compute_entry_hash(
                        seq, ts, entry_type, origin_node_id, message_id,
                        json.loads(payload_text), prev_hash,
                    )
                    self._conn.execute(
                        "INSERT INTO entries (seq, ts, entry_type, origin_node_id, message_id,"
                        " payload, prev_hash, entry_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (seq, ts, entry_type, origin_node_id, message_id,
                         payload_text, prev_hash, entry_hash),
                    )
                    self._conn.execute("COMMIT")
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
            except sqlite3.Error as exc:
                raise LedgerError(f"ledger write failed: {exc}") from exc
        return LedgerEntry(seq, ts, entry_type, origin_node_id, message_id,
                           json.loads(payload_text), prev_hash, entry_hash)

    # ---- reads
    @staticmethod
    def _to_entry(row: tuple) -> LedgerEntry:
        return LedgerEntry(row[0], row[1], row[2], row[3], row[4], json.loads(row[5]), row[6], row[7])

    def _fetch_batch(self, after_seq: int, limit: int) -> list[tuple]:
        with self._lock:
            return self._conn.execute(
                "SELECT seq, ts, entry_type, origin_node_id, message_id, payload, prev_hash,"
                " entry_hash FROM entries WHERE seq > ? ORDER BY seq LIMIT ?",
                (after_seq, limit),
            ).fetchall()

    def get(self, seq: int) -> LedgerEntry | None:
        rows = self._fetch_batch(seq - 1, 1)
        return self._to_entry(rows[0]) if rows and rows[0][0] == seq else None

    def iter_entries(self, start_seq: int = 1, batch_size: int = 500) -> Iterator[LedgerEntry]:
        """Stream entries with seq >= start_seq using keyset pagination (low memory)."""
        after = max(start_seq, 1) - 1
        while True:
            rows = self._fetch_batch(after, batch_size)
            if not rows:
                return
            for row in rows:
                yield self._to_entry(row)
            after = rows[-1][0]

    def has_message(self, message_id: str) -> bool:
        with self._lock:
            return self._conn.execute(
                "SELECT 1 FROM entries WHERE message_id = ?", (message_id,)
            ).fetchone() is not None

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    def head(self) -> tuple[int, str]:
        """(seq, entry_hash) of the newest entry, or (0, GENESIS_HASH) if empty."""
        with self._lock:
            row = self._conn.execute(
                "SELECT seq, entry_hash FROM entries ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        return (row[0], row[1]) if row else (0, GENESIS_HASH)



    # ---- verification
    def verify_chain(self, expected_head: tuple[int, str] | None = None) -> VerifyResult:
        """Walk the whole chain and recompute every hash.

        Detects edited rows, gaps/deleted rows, relinked or reordered rows and
        non-canonical stored payloads. With ``expected_head`` (an externally
        anchored ``head()``), also detects truncation and full-chain rewrites.
        """
        prev_hash, expected_seq, checked = GENESIS_HASH, 1, 0

        def fail(bad_seq: int | None, msg: str) -> VerifyResult:
            return VerifyResult(False, checked, expected_seq - 1, prev_hash, bad_seq, msg)

        after = 0
        while True:
            rows = self._fetch_batch(after, 500)
            if not rows:
                break
            for (seq, ts, etype, origin, mid, payload_text, row_prev, row_hash) in rows:
                if seq != expected_seq:
                    return fail(expected_seq, f"sequence gap: expected {expected_seq}, found {seq}")
                if row_prev != prev_hash:
                    return fail(seq, "prev_hash does not match previous entry_hash")
                try:
                    payload = json.loads(payload_text)
                    canonical = canonical_json(payload).decode("utf-8")
                except (ValueError, TypeError):
                    return fail(seq, "payload is not valid strict JSON")
                if canonical != payload_text:
                    return fail(seq, "stored payload is not in canonical form")
                if compute_entry_hash(seq, ts, etype, origin, mid, payload, row_prev) != row_hash:
                    return fail(seq, "entry_hash mismatch (entry was modified)")
                prev_hash, expected_seq, checked = row_hash, expected_seq + 1, checked + 1
                after = seq
        head = (checked, prev_hash)
        if expected_head is not None and tuple(expected_head) != head:
            return VerifyResult(
                False, checked, head[0], head[1], None,
                f"head mismatch: expected {tuple(expected_head)}, chain ends at {head} "
                "(truncated or rewritten)",
            )
        return VerifyResult(True, checked, head[0], head[1])




    

   