"""Unit tests for src/ledger.py."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from src import serializer as s
from src.ledger import GENESIS_HASH, DuplicateMessageError, Ledger, LedgerError, compute_entry_hash

CLOCK = lambda: datetime(2026, 11, 20, 10, 0, 0, tzinfo=timezone.utc)  # noqa: E731


@pytest.fixture
def state() -> dict:
    return json.loads((s.contracts_dir() / "examples" / "state.example.json").read_text())


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "ledger.db"


@pytest.fixture
def ledger(db_path):
    with Ledger(db_path, clock=CLOCK) as led:
        yield led


def fill(led: Ledger, state: dict, n: int) -> None:
    for i in range(n):
        led.append("state", {**state, "seq": i}, origin_node_id="node-a", message_id=f"m-{i}")


def attacker(db_path) -> sqlite3.Connection:
    """A second connection simulating someone with raw file access who removes the triggers."""
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("DROP TRIGGER entries_no_update")
    conn.execute("DROP TRIGGER entries_no_delete")
    return conn


# ----------------------------------------------------------------- basics
def test_empty_ledger(ledger):
    assert ledger.head() == (0, GENESIS_HASH)
    assert ledger.count() == 0
    result = ledger.verify_chain()
    assert result and result.entries_checked == 0


def test_first_entry_links_to_genesis(ledger, state):
    e = ledger.append("state", state, origin_node_id="node-a")
    assert (e.seq, e.prev_hash) == (1, GENESIS_HASH)
    assert e.ts == "2026-11-20T10:00:00.000Z"
    assert e.entry_hash == compute_entry_hash(1, e.ts, "state", "node-a", None, state, GENESIS_HASH)


def test_chain_links(ledger, state):
    fill(ledger, state, 5)
    entries = list(ledger.iter_entries())
    for prev, cur in zip(entries, entries[1:]):
        assert cur.prev_hash == prev.entry_hash
        assert cur.seq == prev.seq + 1
    assert ledger.verify_chain()


def test_action_entry(ledger):
    e = ledger.append("action", {"action": "alert", "score": 0.91}, origin_node_id="node-a")
    assert ledger.get(e.seq) == e


def test_get_and_iter_start(ledger, state):
    fill(ledger, state, 7)
    assert ledger.get(4).seq == 4
    assert ledger.get(99) is None
    assert [e.seq for e in ledger.iter_entries(start_seq=5, batch_size=2)] == [5, 6, 7]


# ----------------------------------------------------------------- persistence
def test_persistence_and_continuation(db_path, state):
    with Ledger(db_path) as led:
        fill(led, state, 3)
        head = led.head()
    with Ledger(db_path) as led:
        assert led.head() == head
        e = led.append("action", {"x": 1}, origin_node_id="node-a")
        assert (e.seq, e.prev_hash) == (4, head[1])
        assert led.verify_chain()


def test_unsupported_schema_version(db_path):
    with Ledger(db_path):
        pass
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version=99")
    conn.close()
    with pytest.raises(LedgerError, match="unsupported"):
        Ledger(db_path)


# ----------------------------------------------------------------- append-only enforcement
def test_update_forbidden(ledger, state):
    fill(ledger, state, 1)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        ledger._conn.execute("UPDATE entries SET payload = '{}' WHERE seq = 1")


def test_delete_forbidden(ledger, state):
    fill(ledger, state, 1)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        ledger._conn.execute("DELETE FROM entries WHERE seq = 1")
    assert ledger.count() == 1


# ----------------------------------------------------------------- input rules
def test_duplicate_message_id_rejected(ledger, state):
    ledger.append("state", state, origin_node_id="node-a", message_id="dup")
    with pytest.raises(DuplicateMessageError):
        ledger.append("state", {**state, "seq": 43}, origin_node_id="node-a", message_id="dup")
    assert ledger.count() == 1
    assert ledger.has_message("dup") and not ledger.has_message("other")
    assert ledger.append("action", {"x": 1}, origin_node_id="node-a").seq == 2  # still usable


def test_invalid_state_not_written(ledger, state):
    state["state"] = "jammed"
    with pytest.raises(LedgerError, match="invalid state"):
        ledger.append("state", state, origin_node_id="node-a")
    assert ledger.count() == 0


def test_state_origin_mismatch(ledger, state):
    with pytest.raises(LedgerError, match="origin_node_id"):
        ledger.append("state", state, origin_node_id="node-z")


def test_unknown_entry_type(ledger):
    with pytest.raises(LedgerError, match="entry_type"):
        ledger.append("gossip", {}, origin_node_id="node-a")


def test_action_with_message_id_rejected(ledger):
    with pytest.raises(LedgerError, match="message_id"):
        ledger.append("action", {}, origin_node_id="node-a", message_id="x")


def test_nan_payload_rejected(ledger):
    with pytest.raises(LedgerError, match="strict JSON"):
        ledger.append("action", {"score": float("nan")}, origin_node_id="node-a")
    assert ledger.count() == 0


def test_empty_origin_rejected(ledger):
    with pytest.raises(LedgerError):
        ledger.append("action", {}, origin_node_id="")


# ----------------------------------------------------------------- tamper detection
def test_detects_modified_payload(db_path, ledger, state):
    fill(ledger, state, 5)
    attacker(db_path).execute(
        "UPDATE entries SET payload = ? WHERE seq = 3",
        (s.canonical_json({**state, "congestion_score": 0.01, "seq": 2}).decode(),),
    )
    result = ledger.verify_chain()
    assert not result and result.bad_seq == 3 and "modified" in result.error


def test_detects_non_canonical_payload(db_path, ledger, state):
    fill(ledger, state, 2)
    attacker(db_path).execute(
        "UPDATE entries SET payload = ? WHERE seq = 2", (json.dumps({**state, "seq": 1}, indent=2),)
    )
    result = ledger.verify_chain()
    assert not result and result.bad_seq == 2 and "canonical" in result.error


def test_detects_deleted_middle_entry(db_path, ledger, state):
    fill(ledger, state, 5)
    attacker(db_path).execute("DELETE FROM entries WHERE seq = 3")
    result = ledger.verify_chain()
    assert not result and result.bad_seq == 3 and "gap" in result.error


def test_detects_relinked_entry(db_path, ledger, state):
    fill(ledger, state, 3)
    attacker(db_path).execute("UPDATE entries SET prev_hash = ? WHERE seq = 2", ("f" * 64,))
    result = ledger.verify_chain()
    assert not result and result.bad_seq == 2


def test_tail_truncation_needs_anchor(db_path, ledger, state):
    fill(ledger, state, 5)
    anchor = ledger.head()
    attacker(db_path).execute("DELETE FROM entries WHERE seq > 3")
    assert ledger.verify_chain()  # internally consistent: the documented blind spot
    result = ledger.verify_chain(expected_head=anchor)
    assert not result and "head mismatch" in result.error


def test_full_chain_rewrite_needs_anchor(db_path, ledger, state):
    """Attacker edits entry 2 and recomputes every later hash."""
    fill(ledger, state, 4)
    anchor = ledger.head()
    conn = attacker(db_path)
    prev = ledger.get(1).entry_hash
    for e in list(ledger.iter_entries(start_seq=2)):
        payload = {**e.payload, "congestion_score": 0.0} if e.seq == 2 else dict(e.payload)
        h = compute_entry_hash(e.seq, e.ts, e.entry_type, e.origin_node_id, e.message_id, payload, prev)
        conn.execute(
            "UPDATE entries SET payload=?, prev_hash=?, entry_hash=? WHERE seq=?",
            (s.canonical_json(payload).decode(), prev, h, e.seq),
        )
        prev = h
    assert ledger.verify_chain()  # consistent after rewrite
    assert not ledger.verify_chain(expected_head=anchor)


# ----------------------------------------------------------------- concurrency
def test_concurrent_appends_keep_chain_valid(ledger, state):
    def worker(w: int) -> None:
        for i in range(25):
            ledger.append("state", {**state, "seq": w * 100 + i},
                          origin_node_id="node-a", message_id=f"w{w}-{i}")

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ledger.count() == 200
    result = ledger.verify_chain()
    assert result and result.entries_checked == 200
