"""Tamper-evident, append-only ledger (SQLite + SHA-256 hash chain).

Three layers, each with a stated limit:

1. **Database triggers**, enforced for every connection to the file. UPDATE and
   DELETE are aborted, and INSERT must be strictly sequential: ``seq`` is
   ``max(seq) + 1``, ``prev_hash`` is the previous ``entry_hash``, and
   ``message_id`` / ``entry_hash`` are not already present. The last two rules
   matter because ``INSERT OR REPLACE`` resolves a conflict by deleting the old
   row *without firing DELETE triggers*; checking before the insert closes that
   path. Limit: whoever can DROP TRIGGER, edit the schema or swap the file can
   bypass this layer. The triggers are re-created every time the ledger is opened.
2. **Hash chain**: every entry commits to its predecessor
   (``prev_hash`` -> ``entry_hash``), so :meth:`Ledger.verify_chain` detects edited
   fields, deleted or reordered rows and non-canonical payloads, even if layer 1
   was bypassed. Limit: an attacker who rewrites the chain consistently from the
   edited entry onward, or cuts off the tail, leaves a chain that is internally valid.
3. **External anchor**: persist :meth:`Ledger.head` somewhere the attacker cannot
   reach (log line, peer, removable media) and pass it as ``anchor`` to
   ``verify_chain``. It checks that the entry at the anchored ``seq`` still has the
   anchored hash, so an anchor stays valid as new entries are appended. It detects
   rewrites and truncation at or before the anchor, but not changes made only to
   entries *after* it, so anchor often.

Also: durable (WAL + ``synchronous=FULL``) and duplicate-safe (a ``message_id`` is
recorded once).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .serializer import ContractError, canonical_json, utc_now_iso, validate_state

GENESIS_HASH = "0" * 64
SCHEMA_VERSION = 2  # 2: added the sequential-insert trigger
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
CREATE TRIGGER IF NOT EXISTS entries_sequential_insert BEFORE INSERT ON entries
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only: seq must be max(seq)+1')
    WHERE NEW.seq IS NOT (SELECT COALESCE(MAX(seq), 0) + 1 FROM entries);
    SELECT RAISE(ABORT, 'ledger is append-only: prev_hash must equal the previous entry_hash')
    WHERE NEW.prev_hash IS NOT COALESCE(
        (SELECT entry_hash FROM entries ORDER BY seq DESC LIMIT 1), '__GENESIS__');
    SELECT RAISE(ABORT, 'ledger is append-only: message_id already recorded')
    WHERE NEW.message_id IS NOT NULL
      AND EXISTS (SELECT 1 FROM entries WHERE message_id = NEW.message_id);
    SELECT RAISE(ABORT, 'ledger is append-only: entry_hash already recorded')
    WHERE EXISTS (SELECT 1 FROM entries WHERE entry_hash = NEW.entry_hash);
END;
""".replace("__GENESIS__", GENESIS_HASH)


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
            self._conn.execute("PRAGMA recursive_triggers=ON")  # defence in depth for this connection
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, SCHEMA_VERSION):
                raise LedgerError(
                    f"ledger schema version {version} unsupported (expected {SCHEMA_VERSION})"
                )
            # Idempotent: creates the table on a new file, migrates v1 (adds the insert
            # trigger) and silently restores any trigger removed while the node was down.
            self._conn.executescript(_SCHEMA)
            if version != SCHEMA_VERSION:
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
    @staticmethod
    def _check_anchor(anchor: object) -> tuple[int, str]:
        if (not isinstance(anchor, tuple) or len(anchor) != 2
                or isinstance(anchor[0], bool) or not isinstance(anchor[0], int) or anchor[0] < 0
                or not isinstance(anchor[1], str) or not re.fullmatch(r"[0-9a-f]{64}", anchor[1])):
            raise LedgerError("anchor must be a (seq >= 0, 64-char lowercase hex hash) tuple, as returned by head()")
        return anchor

    def verify_chain(self, anchor: tuple[int, str] | None = None) -> VerifyResult:
        """Walk the whole chain and recompute every hash.

        Detects edited rows (any hashed field), gaps/deleted rows, relinked or
        reordered rows and non-canonical stored payloads. With ``anchor`` (a
        ``head()`` saved earlier, outside the database) it also checks that the
        entry at ``anchor`` seq still carries the anchored hash, which exposes
        truncation and full-chain rewrites at or before that point. New entries
        appended after the anchor do not invalidate it.
        """
        anchor_seq, anchor_hash = self._check_anchor(anchor) if anchor is not None else (None, None)
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
                if seq == anchor_seq and row_hash != anchor_hash:
                    return fail(seq, f"anchor mismatch: entry {seq} no longer has the anchored hash (chain was rewritten)")
                prev_hash, expected_seq, checked = row_hash, expected_seq + 1, checked + 1
                after = seq
        if anchor_seq == 0 and anchor_hash != GENESIS_HASH:
            return fail(None, "anchor mismatch: seq 0 must carry the genesis hash")
        if anchor_seq is not None and anchor_seq > checked:
            return fail(None, f"chain ends at seq {checked}, before anchored seq {anchor_seq} (truncated)")
        return VerifyResult(True, checked, checked, prev_hash)