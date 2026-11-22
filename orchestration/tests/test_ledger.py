"""Unit tests for src/ledger.py."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from src import ledger as ledger_mod
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


def test_anchor_stays_valid_after_new_appends(ledger, state):
    fill(ledger, state, 3)
    anchor = ledger.head()
    ledger.append("action", {"x": 1}, origin_node_id="node-a")
    ledger.append("action", {"x": 2}, origin_node_id="node-a")
    assert ledger.verify_chain(anchor=anchor)  # the tip moved on, the anchor is still true


def test_anchor_on_empty_ledger(ledger, state):
    anchor = ledger.head()
    assert anchor == (0, GENESIS_HASH) and ledger.verify_chain(anchor=anchor)
    fill(ledger, state, 2)
    assert ledger.verify_chain(anchor=anchor)
    assert not ledger.verify_chain(anchor=(0, "a" * 64))


def test_anchor_detects_truncation_below_it(db_path, ledger, state):
    fill(ledger, state, 5)
    anchor = ledger.head()
    attacker(db_path).execute("DELETE FROM entries WHERE seq > 3")
    assert ledger.verify_chain()  # internally consistent: the blind spot of a bare chain
    result = ledger.verify_chain(anchor=anchor)
    assert not result and "truncated" in result.error


def test_anchor_does_not_cover_changes_after_it(db_path, ledger, state):
    """Documented limit: an anchor only protects the chain up to its own seq."""
    fill(ledger, state, 5)
    anchor = (3, ledger.get(3).entry_hash)
    attacker(db_path).execute("DELETE FROM entries WHERE seq > 3")
    assert ledger.verify_chain(anchor=anchor)


def test_anchor_detects_full_chain_rewrite(db_path, ledger, state):
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
    assert ledger.verify_chain()  # consistent after the rewrite
    result = ledger.verify_chain(anchor=anchor)
    assert not result and "anchor mismatch" in result.error and result.bad_seq == 4
    assert not ledger.verify_chain(anchor=(2, anchor[1]))  # also wrong for an earlier seq


@pytest.mark.parametrize("bad", [(-1, "0" * 64), (1, "xyz"), (1, "A" * 64), (True, "0" * 64), "head", (1,), (1, "0" * 64, 2)])
def test_malformed_anchor_rejected(ledger, bad):
    with pytest.raises(LedgerError, match="anchor"):
        ledger.verify_chain(anchor=bad)


# ----------------------------------------------------------------- INSERT OR REPLACE and out-of-order inserts
def raw_insert(path, verb, seq, prev_hash, entry_hash, message_id=None):
    """Plain SQL from a brand-new connection: nothing but the database's own triggers protects us."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.execute(
            f"{verb} INTO entries (seq, ts, entry_type, origin_node_id, message_id, payload, prev_hash, entry_hash)"
            " VALUES (?, '2026-11-20T10:00:00.000Z', 'action', 'evil', ?, '{}', ?, ?)",
            (seq, message_id, prev_hash, entry_hash),
        )
    finally:
        conn.close()


@pytest.mark.parametrize("verb", ["INSERT OR REPLACE", "REPLACE", "INSERT"])
def test_cannot_overwrite_existing_seq(db_path, ledger, state, verb):
    fill(ledger, state, 3)
    before = list(ledger.iter_entries())
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw_insert(db_path, verb, 2, before[0].entry_hash, "e" * 64)
    assert list(ledger.iter_entries()) == before and ledger.verify_chain()


def test_replace_via_duplicate_message_id_cannot_delete_an_entry(db_path, ledger, state):
    """Subtle path: correct next seq and prev_hash, but REPLACE would evict the row holding m-1."""
    fill(ledger, state, 3)
    before = list(ledger.iter_entries())
    seq, head_hash = ledger.head()
    with pytest.raises(sqlite3.DatabaseError, match="message_id already recorded"):
        raw_insert(db_path, "INSERT OR REPLACE", seq + 1, head_hash, "e" * 64, message_id="m-1")
    assert list(ledger.iter_entries()) == before


def test_replace_via_duplicate_entry_hash_cannot_delete_an_entry(db_path, ledger, state):
    fill(ledger, state, 3)
    before = list(ledger.iter_entries())
    seq, head_hash = ledger.head()
    with pytest.raises(sqlite3.DatabaseError, match="entry_hash already recorded"):
        raw_insert(db_path, "INSERT OR REPLACE", seq + 1, head_hash, before[0].entry_hash)
    assert list(ledger.iter_entries()) == before


def test_insert_with_seq_gap_rejected(db_path, ledger, state):
    fill(ledger, state, 2)
    seq, head_hash = ledger.head()
    with pytest.raises(sqlite3.DatabaseError, match="seq must be"):
        raw_insert(db_path, "INSERT", seq + 2, head_hash, "e" * 64)
    assert ledger.count() == 2


def test_insert_with_wrong_prev_hash_rejected(db_path, ledger, state):
    fill(ledger, state, 2)
    seq, _ = ledger.head()
    with pytest.raises(sqlite3.DatabaseError, match="prev_hash must equal"):
        raw_insert(db_path, "INSERT", seq + 1, "f" * 64, "e" * 64)
    assert ledger.count() == 2


def test_first_insert_must_link_to_genesis(db_path, ledger):
    with pytest.raises(sqlite3.DatabaseError, match="prev_hash must equal"):
        raw_insert(db_path, "INSERT", 1, "f" * 64, "e" * 64)
    assert ledger.count() == 0


def triggers(path) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    finally:
        conn.close()


ALL_TRIGGERS = {"entries_no_update", "entries_no_delete", "entries_sequential_insert"}


def test_missing_triggers_are_restored_on_open(db_path, state):
    with Ledger(db_path) as led:
        fill(led, state, 2)
    assert triggers(db_path) == ALL_TRIGGERS
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    for name in ALL_TRIGGERS:
        conn.execute(f"DROP TRIGGER {name}")
    conn.close()
    assert triggers(db_path) == set()
    with Ledger(db_path) as led:
        assert triggers(db_path) == ALL_TRIGGERS
        assert led.verify_chain()


def test_v1_database_is_migrated(db_path, state):
    with Ledger(db_path) as led:
        fill(led, state, 2)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("DROP TRIGGER entries_sequential_insert")
    conn.execute("PRAGMA user_version=1")
    conn.close()
    with Ledger(db_path) as led:
        assert led._conn.execute("PRAGMA user_version").fetchone()[0] == ledger_mod.SCHEMA_VERSION
        assert triggers(db_path) == ALL_TRIGGERS
        assert led.verify_chain() and led.count() == 2
        seq, h = led.head()
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            raw_insert(db_path, "INSERT OR REPLACE", seq, h, "e" * 64)


# ----------------------------------------------------------------- hash definition (golden) and field coverage
GOLDEN_ARGS = (1, "2026-11-20T10:00:00.000Z", "action", "node-a", None, {"b": 2, "a": [1, 0.5]}, GENESIS_HASH)
GOLDEN_HASH = "3ef533cdb16447544d718a679ef615ac525043ebfe820bd9486683437d50bfa7"  # recomputed independently with hashlib+json


def test_golden_hash():
    """Pins the exact hash input. If this changes, every existing ledger stops verifying."""
    assert compute_entry_hash(*GOLDEN_ARGS) == GOLDEN_HASH


def test_hash_input_is_exactly_these_fields(monkeypatch):
    seen: dict = {}
    real = ledger_mod.canonical_json

    def spy(obj):
        seen.update(obj)
        return real(obj)

    monkeypatch.setattr(ledger_mod, "canonical_json", spy)
    compute_entry_hash(*GOLDEN_ARGS)
    assert set(seen) == {"seq", "ts", "entry_type", "origin_node_id", "message_id", "payload", "prev_hash"}


@pytest.mark.parametrize("index,changed", [
    (0, 2), (1, "2026-11-20T10:00:00.001Z"), (2, "state"), (3, "node-b"),
    (4, "m-1"), (5, {"b": 2, "a": [1, 0.6]}), (6, "f" * 64),
])
def test_every_hashed_field_changes_the_hash(index, changed):
    args = list(GOLDEN_ARGS)
    args[index] = changed
    assert compute_entry_hash(*args) != GOLDEN_HASH


def test_hash_ignores_payload_key_order():
    a = compute_entry_hash(*GOLDEN_ARGS)
    b = compute_entry_hash(*GOLDEN_ARGS[:5], {"a": [1, 0.5], "b": 2}, GENESIS_HASH)
    assert a == b


@pytest.mark.parametrize("column,value,bad_seq", [
    ("ts", "2026-11-20T10:00:01.000Z", 2),
    ("origin_node_id", "node-evil", 2),
    ("entry_type", "action", 2),
    ("message_id", "m-forged", 2),
    ("seq", 99, 2),
])
def test_detects_modification_of_each_stored_field(db_path, ledger, state, column, value, bad_seq):
    fill(ledger, state, 4)
    attacker(db_path).execute(f"UPDATE entries SET {column} = ? WHERE seq = 2", (value,))
    result = ledger.verify_chain()
    assert not result and result.bad_seq == bad_seq


# ----------------------------------------------------------------- rollback and duplicates
def test_duplicate_leaves_no_trace_and_chain_valid(ledger, state):
    first = ledger.append("state", state, origin_node_id="node-a", message_id="dup")
    with pytest.raises(DuplicateMessageError):
        ledger.append("state", {**state, "seq": 99}, origin_node_id="node-a", message_id="dup")
    assert not ledger._conn.in_transaction
    assert list(ledger.iter_entries()) == [first] and ledger.verify_chain()


@pytest.mark.parametrize("boom,expected", [(RuntimeError("bug"), RuntimeError), (sqlite3.OperationalError("disk I/O error"), LedgerError)])
def test_failure_inside_transaction_rolls_back(ledger, state, monkeypatch, boom, expected):
    ledger.append("state", state, origin_node_id="node-a", message_id="ok-1")

    def explode(*args, **kwargs):
        raise boom

    monkeypatch.setattr(ledger_mod, "compute_entry_hash", explode)
    with pytest.raises(expected):
        ledger.append("action", {"x": 1}, origin_node_id="node-a")
    monkeypatch.undo()
    assert not ledger._conn.in_transaction
    assert ledger.count() == 1
    e = ledger.append("action", {"x": 2}, origin_node_id="node-a")  # ledger still fully usable
    assert e.seq == 2 and ledger.verify_chain()


# ----------------------------------------------------------------- two instances on one database
def test_two_instances_serialize_writes(db_path, state):
    a, b = Ledger(db_path), Ledger(db_path)
    try:
        def worker(led: Ledger, tag: str) -> None:
            for i in range(40):
                led.append("state", {**state, "seq": i}, origin_node_id="node-a", message_id=f"{tag}-{i}")

        threads = [threading.Thread(target=worker, args=(led, tag)) for led, tag in ((a, "a"), (b, "b"))]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert a.count() == 80
        assert [e.seq for e in b.iter_entries()] == list(range(1, 81))
        result = a.verify_chain()
        assert result and result.entries_checked == 80
    finally:
        a.close()
        b.close()


def test_two_instances_racing_on_one_message_id(db_path, state):
    a, b = Ledger(db_path), Ledger(db_path)
    try:
        barrier = threading.Barrier(2)
        outcomes: list[object] = []

        def worker(led: Ledger) -> None:
            barrier.wait()
            try:
                outcomes.append(led.append("state", state, origin_node_id="node-a", message_id="same"))
            except DuplicateMessageError as exc:
                outcomes.append(exc)

        threads = [threading.Thread(target=worker, args=(led,)) for led in (a, b)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert sum(isinstance(o, DuplicateMessageError) for o in outcomes) == 1
        assert a.count() == 1 and a.verify_chain()
    finally:
        a.close()
        b.close()


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