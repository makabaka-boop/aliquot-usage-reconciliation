"""Consumption vouchers: irreversible volume draw-down with idempotent
receipts, and the per-root conservation audit.

A consumption deducts from a tube's balance, bumps its revision and appends an
immutable voucher in one SQLite transaction. The conservation audit reads a
root's whole subtree in one snapshot and checks
    sum(balances) + sum(consumed) == initial volume.
"""

import json
import sqlite3
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from conftest import make_consumption, make_split, total_balance


def _consume(client, tube, revision, key, amount, purpose="assay"):
    return client.post("/consumptions", json=make_consumption(tube, revision, key, amount, purpose))


# ---------------------------------------------------------------------------
# Basic consumption semantics
# ---------------------------------------------------------------------------


def test_basic_consumption(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 1000})
    resp = _consume(client, "root", 0, "c1", 250, "qc-assay")
    assert resp.status_code == 201
    body = resp.json()
    assert body["tube"] == {"id": "root", "balance_ul": 750, "revision": 1}
    assert body["amount_ul"] == 250
    assert body["purpose"] == "qc-assay"
    assert body["consumption_id"] == 1

    root = client.get("/tubes/root").json()
    assert root["balance_ul"] == 750 and root["revision"] == 1
    # the registered initial volume is frozen, not the current balance
    assert root["initial_ul"] == 1000

    vouchers = client.get("/tubes/root/consumptions").json()["consumptions"]
    assert len(vouchers) == 1
    assert vouchers[0]["amount_ul"] == 250
    assert vouchers[0]["purpose"] == "qc-assay"
    assert vouchers[0]["expected_revision"] == 0
    # consumed volume left the ledger: balances alone no longer sum to initial
    assert total_balance(client) == 750


def test_consumption_may_consume_entire_balance(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    resp = _consume(client, "root", 0, "c1", 100)
    assert resp.status_code == 201
    assert client.get("/tubes/root").json()["balance_ul"] == 0


def test_insufficient_balance_422_and_state_unchanged(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    resp = _consume(client, "root", 0, "c1", 101)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INSUFFICIENT_BALANCE"

    root = client.get("/tubes/root").json()
    assert root["balance_ul"] == 100 and root["revision"] == 0
    assert client.get("/tubes/root/consumptions").json()["consumptions"] == []
    # key not burned: the corrected retry goes through
    assert _consume(client, "root", 0, "c1", 40).status_code == 201


def test_stale_revision_412(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    assert _consume(client, "root", 0, "c1", 10).status_code == 201
    resp = _consume(client, "root", 0, "c2", 10)
    assert resp.status_code == 412
    assert resp.json()["error"]["code"] == "REVISION_CONFLICT"
    # nothing was applied for the failed request
    assert len(client.get("/tubes/root/consumptions").json()["consumptions"]) == 1


def test_unknown_tube_404(client):
    resp = _consume(client, "ghost", 0, "c1", 10)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TUBE_NOT_FOUND"


def test_consumption_validation_errors(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 1000})
    base = make_consumption("root", 0, "c1", 10)

    cases = []
    cases.append(make_consumption("root", 0, "c1", 0))            # zero amount
    cases.append(make_consumption("root", 0, "c1", -3))           # negative amount
    cases.append(make_consumption("root", 0, "c1", 2.5))          # fractional amount
    cases.append(make_consumption("root", 0, "c1", "10"))         # string amount
    cases.append(make_consumption("root", 0, "c1", 2**63))        # beyond int64
    cases.append(make_consumption("root", 0, "c1", 10, ""))       # empty purpose
    cases.append(make_consumption("root", 0, "c1", 10, "   "))    # whitespace-only purpose
    cases.append(make_consumption("root", -1, "c1", 10))          # negative revision
    cases.append(dict(base, unexpected=1))                        # unknown field
    missing_key = dict(base)
    del missing_key["request_key"]                                # missing request key
    cases.append(missing_key)

    for payload in cases:
        resp = client.post("/consumptions", json=payload)
        assert resp.status_code == 422, payload
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    # nothing was applied, no key was burned
    root = client.get("/tubes/root").json()
    assert root["balance_ul"] == 1000 and root["revision"] == 0
    assert _consume(client, "root", 0, "c1", 10).status_code == 201


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_retry_same_key_same_body_returns_original(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 1000})
    payload = make_consumption("root", 0, "retry-1", 300, "assay")

    first = client.post("/consumptions", json=payload)
    second = client.post("/consumptions", json=payload)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()

    # the consumption happened exactly once
    root = client.get("/tubes/root").json()
    assert root["balance_ul"] == 700 and root["revision"] == 1
    assert len(client.get("/tubes/root/consumptions").json()["consumptions"]) == 1


def test_retry_with_reordered_json_keys_is_still_a_replay(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    first = client.post(
        "/consumptions",
        content=json.dumps(
            {"tube_id": "root", "expected_revision": 0, "request_key": "rk",
             "amount_ul": 40, "purpose": "assay"}
        ),
        headers={"content-type": "application/json"},
    )
    second = client.post(
        "/consumptions",
        content=json.dumps(
            {"purpose": "assay", "amount_ul": 40, "request_key": "rk",
             "expected_revision": 0, "tube_id": "root"}
        ),
        headers={"content-type": "application/json"},
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()
    assert client.get("/tubes/root").json()["balance_ul"] == 60


def test_same_key_different_body_409(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 1000})
    assert _consume(client, "root", 0, "dup", 100).status_code == 201

    resp = _consume(client, "root", 0, "dup", 101)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "REQUEST_KEY_CONFLICT"

    resp = _consume(client, "root", 0, "dup", 100, "other-purpose")
    assert resp.status_code == 409

    # nothing extra was applied
    assert client.get("/tubes/root").json()["balance_ul"] == 900
    assert len(client.get("/tubes/root/consumptions").json()["consumptions"]) == 1


def test_consumption_and_split_keys_are_independent_namespaces(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 1000})
    # the same request_key may be used once for a split and once for a
    # consumption; each flow replays against its own receipts
    assert client.post("/splits", json=make_split("root", 0, "k1", [("a", 100)])).status_code == 201
    assert _consume(client, "root", 1, "k1", 50).status_code == 201
    assert client.get("/tubes/root").json()["balance_ul"] == 850


# ---------------------------------------------------------------------------
# Multi-level splits + consumption, conservation audit
# ---------------------------------------------------------------------------


def test_multi_level_split_then_consume_conservation(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 1000})
    assert client.post("/splits", json=make_split("root", 0, "s1", [("mid", 400), ("sib", 100)])).status_code == 201
    assert client.post("/splits", json=make_split("mid", 0, "s2", [("leaf", 150)])).status_code == 201

    # consume at every level of the tree
    assert _consume(client, "root", 1, "c1", 50, "qc").status_code == 201
    assert _consume(client, "mid", 1, "c2", 30, "assay").status_code == 201
    assert _consume(client, "leaf", 0, "c3", 20, "assay").status_code == 201

    audit = client.get("/tubes/root/conservation").json()
    assert audit["initial_ul"] == 1000
    assert audit["total_consumed_ul"] == 100
    assert audit["total_balance_ul"] == 900
    assert audit["conserved"] is True
    assert {t["id"] for t in audit["tubes"]} == {"root", "mid", "sib", "leaf"}
    assert len(audit["consumptions"]) == 3
    # every descendant row carries its own frozen initial volume
    initials = {t["id"]: t["initial_ul"] for t in audit["tubes"]}
    assert initials == {"root": 1000, "mid": 400, "sib": 100, "leaf": 150}

    # the invariant also holds for any subtree
    sub = client.get("/tubes/mid/conservation").json()
    assert sub["initial_ul"] == 400
    assert sub["total_balance_ul"] == 350  # mid 220 + leaf 130
    assert sub["total_consumed_ul"] == 50
    assert sub["conserved"] is True

    # unknown tube -> 404
    assert client.get("/tubes/ghost/conservation").status_code == 404


def test_conservation_audit_is_one_snapshot_under_interleaved_traffic(gated_server):
    url, gate = gated_server
    assert httpx.post(f"{url}/tubes", json={"id": "root", "balance_ul": 1000}, timeout=30).status_code == 201
    s1 = httpx.post(f"{url}/splits", json=make_split("root", 0, "s1", [("mid", 400)]), timeout=30)
    assert s1.status_code == 201

    # Park the audit right after it read the root row, before it walks the
    # descendants and the vouchers.
    gate.arm_once(
        lambda conn, sql, params: sql.startswith("SELECT * FROM tubes")
        and tuple(params) == ("root",)
    )
    box = {}

    def run_audit():
        box["resp"] = httpx.get(f"{url}/tubes/root/conservation", timeout=30)

    thread = threading.Thread(target=run_audit)
    thread.start()
    try:
        assert gate.entered.wait(timeout=15), "audit never read the root"
        # Interleave: consume from the root and split the child further.
        c1 = httpx.post(f"{url}/consumptions", json=make_consumption("root", 1, "c1", 100, "qc"), timeout=30)
        assert c1.status_code == 201
        s2 = httpx.post(f"{url}/splits", json=make_split("mid", 0, "s2", [("leaf", 150)]), timeout=30)
        assert s2.status_code == 201
    finally:
        gate.release.set()
    thread.join(timeout=15)

    audit = box["resp"].json()
    assert audit["conserved"] is True
    # The snapshot was taken before the interleaved writes: no voucher, no
    # grandchild, and the balances are the pre-consumption ones.
    assert audit["total_consumed_ul"] == 0
    assert audit["total_balance_ul"] == 1000
    assert {t["id"] for t in audit["tubes"]} == {"root", "mid"}

    # A fresh audit sees the new committed state — again self-consistent.
    fresh = httpx.get(f"{url}/tubes/root/conservation", timeout=30).json()
    assert fresh["conserved"] is True
    assert fresh["total_consumed_ul"] == 100
    assert fresh["total_balance_ul"] == 900
    assert {t["id"] for t in fresh["tubes"]} == {"root", "mid", "leaf"}


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_consumption_vouchers_are_immutable(client, db_path):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    assert _consume(client, "root", 0, "c1", 10).status_code == 201

    conn = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE consumptions SET amount_ul = 1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM consumptions")
    conn.rollback()
    conn.close()


def test_initial_ul_is_frozen(client, db_path):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})

    conn = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE tubes SET initial_ul = 999")
    conn.rollback()
    # balance/revision updates are unaffected by the freeze trigger
    conn.execute("UPDATE tubes SET balance_ul = 90, revision = revision + 1 WHERE id = 'root'")
    conn.commit()
    conn.close()
    root = client.get("/tubes/root").json()
    assert root["balance_ul"] == 90 and root["initial_ul"] == 100


# ---------------------------------------------------------------------------
# Rollback: a failed write leaves no half voucher
# ---------------------------------------------------------------------------


def test_failed_consumption_write_leaves_no_partial_state(db_path):
    app = create_app(db_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post("/tubes", json={"id": "root", "balance_ul": 100})

        # Sabotage the voucher insert at the database level: the balance UPDATE
        # has already run inside the same transaction when this fires.
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TRIGGER sabotage BEFORE INSERT ON consumptions "
            "BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END;"
        )
        conn.close()

        resp = client.post("/consumptions", json=make_consumption("root", 0, "c1", 10))
        assert resp.status_code == 500

        # no half voucher: balance, revision, vouchers and the request key are
        # all as if the request never happened
        root = client.get("/tubes/root").json()
        assert root["balance_ul"] == 100 and root["revision"] == 0
        assert client.get("/tubes/root/consumptions").json()["consumptions"] == []

        conn = sqlite3.connect(db_path)
        conn.execute("DROP TRIGGER sabotage")
        conn.close()

        # the failed request did not burn the key
        resp = client.post("/consumptions", json=make_consumption("root", 0, "c1", 10))
        assert resp.status_code == 201
        assert client.get("/tubes/root").json()["balance_ul"] == 90


# ---------------------------------------------------------------------------
# Concurrency: consumption vs split racing the same revision
# ---------------------------------------------------------------------------


def _race_requests(url, posts):
    barrier = threading.Barrier(len(posts))
    outcomes = []

    def fire(method_url, payload):
        barrier.wait(timeout=10)
        resp = httpx.post(f"{url}{method_url}", json=payload, timeout=30)
        outcomes.append((resp.status_code, resp.json()))

    threads = [threading.Thread(target=fire, args=p) for p in posts]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return outcomes


def test_consumption_and_split_race_same_revision_at_most_one_wins(server):
    httpx.post(f"{server}/tubes", json={"id": "root", "balance_ul": 1000}).raise_for_status()
    outcomes = _race_requests(
        server,
        [
            ("/splits", make_split("root", 0, "race-split", [("kid", 400)])),
            ("/consumptions", make_consumption("root", 0, "race-consume", 300, "assay")),
        ],
    )

    statuses = sorted(status for status, _ in outcomes)
    assert statuses == [201, 412]
    loser = next(body for status, body in outcomes if status == 412)
    assert loser["error"]["code"] == "REVISION_CONFLICT"

    root = httpx.get(f"{server}/tubes/root", timeout=30).json()
    assert root["revision"] == 1
    # exactly one of the two mutations was applied
    assert root["balance_ul"] in (600, 700)

    audit = httpx.get(f"{server}/tubes/root/conservation", timeout=30).json()
    assert audit["conserved"] is True
    assert audit["total_balance_ul"] + audit["total_consumed_ul"] == 1000


def test_two_consumptions_race_same_revision_at_most_one_wins(server):
    httpx.post(f"{server}/tubes", json={"id": "root", "balance_ul": 1000}).raise_for_status()
    outcomes = _race_requests(
        server,
        [
            ("/consumptions", make_consumption("root", 0, "race-a", 400, "assay")),
            ("/consumptions", make_consumption("root", 0, "race-b", 400, "assay")),
        ],
    )
    statuses = sorted(status for status, _ in outcomes)
    assert statuses == [201, 412]

    root = httpx.get(f"{server}/tubes/root", timeout=30).json()
    assert root["revision"] == 1 and root["balance_ul"] == 600
    vouchers = httpx.get(f"{server}/tubes/root/consumptions", timeout=30).json()["consumptions"]
    assert len(vouchers) == 1


# ---------------------------------------------------------------------------
# Restart: vouchers, idempotency and the audit survive
# ---------------------------------------------------------------------------


def test_restart_preserves_consumptions_and_conservation(db_path):
    consume_body = make_consumption("root", 1, "c1", 200, "assay")
    split_body = make_split("root", 0, "s1", [("a", 300), ("b", 100)])
    with TestClient(create_app(db_path)) as c1:
        c1.post("/tubes", json={"id": "root", "balance_ul": 900})
        first_split = c1.post("/splits", json=split_body)
        assert first_split.status_code == 201
        first_consume = c1.post("/consumptions", json=consume_body)
        assert first_consume.status_code == 201

    # "restart": a brand-new app instance over the same SQLite file
    with TestClient(create_app(db_path)) as c2:
        root = c2.get("/tubes/root").json()
        assert root["balance_ul"] == 300 and root["revision"] == 2
        assert root["initial_ul"] == 900

        # replays after the restart return the original recorded receipts
        replay = c2.post("/consumptions", json=consume_body)
        assert replay.status_code == 201
        assert replay.json() == first_consume.json()
        replay_split = c2.post("/splits", json=split_body)
        assert replay_split.status_code == 201
        assert replay_split.json() == first_split.json()
        # nothing was deducted twice
        assert c2.get("/tubes/root").json()["balance_ul"] == 300
        assert len(c2.get("/tubes/root/consumptions").json()["consumptions"]) == 1

        # the audit still balances
        audit = c2.get("/tubes/root/conservation").json()
        assert audit["initial_ul"] == 900
        assert audit["total_balance_ul"] == 700
        assert audit["total_consumed_ul"] == 200
        assert audit["conserved"] is True

        # and the ledger keeps accepting new work on the bumped revision
        assert c2.post("/consumptions", json=make_consumption("root", 2, "c2", 50, "qc")).status_code == 201
        assert c2.get("/tubes/root/conservation").json()["conserved"] is True


# ---------------------------------------------------------------------------
# Legacy database upgrade
# ---------------------------------------------------------------------------

# The schema as it existed before consumption vouchers and initial_ul.
LEGACY_SCHEMA = """
CREATE TABLE tubes (
    id          TEXT PRIMARY KEY,
    balance_ul  INTEGER NOT NULL CHECK (balance_ul >= 0),
    revision    INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at  TEXT NOT NULL
);
CREATE TABLE splits (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id          TEXT NOT NULL REFERENCES tubes (id),
    request_key        TEXT NOT NULL,
    expected_revision  INTEGER NOT NULL,
    total_amount_ul    INTEGER NOT NULL CHECK (total_amount_ul > 0),
    created_at         TEXT NOT NULL
);
CREATE TABLE split_children (
    split_id   INTEGER NOT NULL REFERENCES splits (id),
    child_id   TEXT NOT NULL REFERENCES tubes (id),
    amount_ul  INTEGER NOT NULL CHECK (amount_ul > 0),
    position   INTEGER NOT NULL,
    PRIMARY KEY (split_id, child_id)
);
CREATE TABLE lineage_edges (
    child_id   TEXT PRIMARY KEY REFERENCES tubes (id),
    parent_id  TEXT NOT NULL REFERENCES tubes (id),
    split_id   INTEGER NOT NULL REFERENCES splits (id),
    amount_ul  INTEGER NOT NULL CHECK (amount_ul > 0)
);
CREATE TABLE idempotency_keys (
    request_key   TEXT PRIMARY KEY,
    request_hash  TEXT NOT NULL,
    request_body  TEXT NOT NULL,
    response_body TEXT NOT NULL,
    split_id      INTEGER NOT NULL REFERENCES splits (id),
    created_at    TEXT NOT NULL
);
"""

TS = "2026-01-01T00:00:00.000+00:00"


def _build_legacy_db(db_path):
    """A pre-upgrade database: root registered 1000, split mid 400 / sib 100,
    mid split leaf 150. Balances: root 500, mid 250, sib 100, leaf 150."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(LEGACY_SCHEMA)
    conn.execute("INSERT INTO tubes VALUES ('root', 500, 1, ?)", (TS,))
    conn.execute("INSERT INTO tubes VALUES ('mid', 250, 1, ?)", (TS,))
    conn.execute("INSERT INTO tubes VALUES ('sib', 100, 0, ?)", (TS,))
    conn.execute("INSERT INTO tubes VALUES ('leaf', 150, 0, ?)", (TS,))
    conn.execute("INSERT INTO splits VALUES (1, 'root', 's1', 0, 500, ?)", (TS,))
    conn.execute("INSERT INTO splits VALUES (2, 'mid', 's2', 0, 150, ?)", (TS,))
    conn.execute("INSERT INTO split_children VALUES (1, 'mid', 400, 0)")
    conn.execute("INSERT INTO split_children VALUES (1, 'sib', 100, 1)")
    conn.execute("INSERT INTO split_children VALUES (2, 'leaf', 150, 0)")
    conn.execute("INSERT INTO lineage_edges VALUES ('mid', 'root', 1, 400)")
    conn.execute("INSERT INTO lineage_edges VALUES ('sib', 'root', 1, 100)")
    conn.execute("INSERT INTO lineage_edges VALUES ('leaf', 'mid', 2, 150)")
    # a recorded split receipt from the old world, keyed for replay
    body = make_split("root", 0, "s1", [("mid", 400), ("sib", 100)])
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    import hashlib

    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    response = {
        "split_id": 1,
        "request_key": "s1",
        "parent": {"id": "root", "balance_ul": 500, "revision": 1},
        "children": [
            {"id": "mid", "balance_ul": 400, "revision": 0},
            {"id": "sib", "balance_ul": 100, "revision": 0},
        ],
        "total_amount_ul": 500,
        "created_at": TS,
    }
    conn.execute(
        "INSERT INTO idempotency_keys VALUES (?, ?, ?, ?, 1, ?)",
        ("s1", digest, canonical, json.dumps(response, ensure_ascii=False), TS),
    )
    conn.commit()
    conn.close()
    return body, response


def test_legacy_database_upgrade_recovers_initial_and_keeps_working(db_path):
    split_body, split_response = _build_legacy_db(db_path)

    with TestClient(create_app(db_path)) as client:
        # initial volumes recovered once and frozen: root = balance + direct
        # split outflow (500 + 500), children = their lineage-edge amounts —
        # never the current balance passed off as the initial volume
        expected_initials = {"root": 1000, "mid": 400, "sib": 100, "leaf": 150}
        for tube_id, initial in expected_initials.items():
            tube = client.get(f"/tubes/{tube_id}").json()
            assert tube["initial_ul"] == initial, tube_id

        # the audit balances with zero consumption in the old world
        audit = client.get("/tubes/root/conservation").json()
        assert audit["initial_ul"] == 1000
        assert audit["total_balance_ul"] == 1000
        assert audit["total_consumed_ul"] == 0
        assert audit["conserved"] is True

        # the old idempotent receipt still replays; nothing is re-applied
        replay = client.post("/splits", json=split_body)
        assert replay.status_code == 201
        assert replay.json() == split_response
        assert client.get("/tubes/root").json()["balance_ul"] == 500
        assert len(client.get("/tubes/root/splits").json()["splits"]) == 1
        assert len(client.get("/tubes/mid/splits").json()["splits"]) == 1

        # old history was not rewritten
        chain = client.get("/tubes/leaf/ancestry").json()
        assert [hop["tube"]["id"] for hop in chain["chain"]] == ["root", "mid", "leaf"]
        assert chain["chain"][1]["via"]["split"]["request_key"] == "s1"

        # new work continues on the upgraded database: consume from a leaf,
        # split the root further, and the audit keeps balancing
        assert _consume(client, "leaf", 0, "c1", 50, "assay").status_code == 201
        assert client.post("/splits", json=make_split("root", 1, "s3", [("r2", 200)])).status_code == 201
        audit = client.get("/tubes/root/conservation").json()
        assert audit["total_balance_ul"] == 950
        assert audit["total_consumed_ul"] == 50
        assert audit["conserved"] is True

        # the freeze trigger now guards the recovered initial volumes
        conn = sqlite3.connect(db_path)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE tubes SET initial_ul = 1 WHERE id = 'root'")
        conn.rollback()
        conn.close()


def test_legacy_upgrade_is_idempotent_across_restarts(db_path):
    _build_legacy_db(db_path)
    with TestClient(create_app(db_path)) as c1:
        assert c1.get("/tubes/root").json()["initial_ul"] == 1000
        assert _consume(c1, "root", 1, "c1", 100, "assay").status_code == 201
    # second startup must not re-run the backfill or clobber anything
    with TestClient(create_app(db_path)) as c2:
        root = c2.get("/tubes/root").json()
        assert root["initial_ul"] == 1000
        assert root["balance_ul"] == 400 and root["revision"] == 2
        audit = c2.get("/tubes/root/conservation").json()
        assert audit["conserved"] is True
        assert audit["total_consumed_ul"] == 100
