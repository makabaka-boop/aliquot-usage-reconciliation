from __future__ import annotations

import sqlite3
from pathlib import Path

# tubes holds *current state* (balance, revision) and is mutable. initial_ul is
# the volume frozen at creation (registered volume for roots, the amount the
# creating split carried for children): written once at INSERT and guarded by
# the tubes_initial_ul_frozen trigger below.
# splits / split_children / lineage_edges / consumptions are *history*:
# insert-only, enforced by triggers below so no code path (or manual SQL) can
# rewrite the past.
SCHEMA = """
CREATE TABLE IF NOT EXISTS tubes (
    id          TEXT PRIMARY KEY,
    balance_ul  INTEGER NOT NULL CHECK (balance_ul >= 0),
    revision    INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    initial_ul  INTEGER CHECK (initial_ul > 0),
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS splits (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id          TEXT NOT NULL REFERENCES tubes (id),
    request_key        TEXT NOT NULL,
    expected_revision  INTEGER NOT NULL,
    total_amount_ul    INTEGER NOT NULL CHECK (total_amount_ul > 0),
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_splits_parent ON splits (parent_id);

CREATE TABLE IF NOT EXISTS split_children (
    split_id   INTEGER NOT NULL REFERENCES splits (id),
    child_id   TEXT NOT NULL REFERENCES tubes (id),
    amount_ul  INTEGER NOT NULL CHECK (amount_ul > 0),
    position   INTEGER NOT NULL,
    PRIMARY KEY (split_id, child_id)
);

CREATE TABLE IF NOT EXISTS lineage_edges (
    child_id   TEXT PRIMARY KEY REFERENCES tubes (id),
    parent_id  TEXT NOT NULL REFERENCES tubes (id),
    split_id   INTEGER NOT NULL REFERENCES splits (id),
    amount_ul  INTEGER NOT NULL CHECK (amount_ul > 0)
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    request_key   TEXT PRIMARY KEY,
    request_hash  TEXT NOT NULL,
    request_body  TEXT NOT NULL,
    response_body TEXT NOT NULL,
    split_id      INTEGER NOT NULL REFERENCES splits (id),
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consumptions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    tube_id            TEXT NOT NULL REFERENCES tubes (id),
    request_key        TEXT NOT NULL,
    expected_revision  INTEGER NOT NULL,
    amount_ul          INTEGER NOT NULL CHECK (amount_ul > 0),
    purpose            TEXT NOT NULL,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_consumptions_tube ON consumptions (tube_id);

CREATE TABLE IF NOT EXISTS consumption_idempotency_keys (
    request_key    TEXT PRIMARY KEY,
    request_hash   TEXT NOT NULL,
    request_body   TEXT NOT NULL,
    response_body  TEXT NOT NULL,
    consumption_id INTEGER NOT NULL REFERENCES consumptions (id),
    created_at     TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS splits_no_update
BEFORE UPDATE ON splits
BEGIN SELECT RAISE(ABORT, 'splits history is immutable'); END;

CREATE TRIGGER IF NOT EXISTS splits_no_delete
BEFORE DELETE ON splits
BEGIN SELECT RAISE(ABORT, 'splits history is immutable'); END;

CREATE TRIGGER IF NOT EXISTS split_children_no_update
BEFORE UPDATE ON split_children
BEGIN SELECT RAISE(ABORT, 'split children history is immutable'); END;

CREATE TRIGGER IF NOT EXISTS split_children_no_delete
BEFORE DELETE ON split_children
BEGIN SELECT RAISE(ABORT, 'split children history is immutable'); END;

CREATE TRIGGER IF NOT EXISTS lineage_edges_no_update
BEFORE UPDATE ON lineage_edges
BEGIN SELECT RAISE(ABORT, 'lineage history is immutable'); END;

CREATE TRIGGER IF NOT EXISTS lineage_edges_no_delete
BEFORE DELETE ON lineage_edges
BEGIN SELECT RAISE(ABORT, 'lineage history is immutable'); END;

CREATE TRIGGER IF NOT EXISTS consumptions_no_update
BEFORE UPDATE ON consumptions
BEGIN SELECT RAISE(ABORT, 'consumption vouchers are immutable'); END;

CREATE TRIGGER IF NOT EXISTS consumptions_no_delete
BEFORE DELETE ON consumptions
BEGIN SELECT RAISE(ABORT, 'consumption vouchers are immutable'); END;

-- initial_ul is frozen at creation: any attempt to rewrite it is rejected.
-- The legacy backfill in _migrate_initial_ul runs before this trigger exists.
CREATE TRIGGER IF NOT EXISTS tubes_initial_ul_frozen
BEFORE UPDATE OF initial_ul ON tubes
BEGIN SELECT RAISE(ABORT, 'initial_ul is frozen at registration'); END;
"""


def connect(db_path: str) -> sqlite3.Connection:
    # isolation_level=None -> autocommit; transactions are opened explicitly
    # with BEGIN IMMEDIATE so the write lock is taken before any read.
    # check_same_thread=False: FastAPI may run dependency setup/teardown and
    # the endpoint itself on different threadpool threads; each connection is
    # still strictly confined to a single request.
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _migrate_initial_ul(conn: sqlite3.Connection) -> None:
    """One-time upgrade for databases created before initial_ul existed.

    Reconstructs the frozen initial volume exactly once, then the freeze trigger
    (created by SCHEMA afterwards) makes it immutable. Roots recover their
    registered volume as current balance + everything their direct splits
    dispensed (legacy databases predate consumption vouchers, so nothing else
    left the tree) — never the current balance alone, which would understate
    the registered volume. Children recover the amount their creating split
    carried, from the lineage edge.
    """
    columns = [row["name"] for row in conn.execute("PRAGMA table_info(tubes)")]
    if not columns or "initial_ul" in columns:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE tubes ADD COLUMN initial_ul INTEGER")
        conn.execute(
            "UPDATE tubes SET initial_ul = ("
            "  SELECT amount_ul FROM lineage_edges WHERE child_id = tubes.id"
            ") WHERE id IN (SELECT child_id FROM lineage_edges)"
        )
        conn.execute(
            "UPDATE tubes SET initial_ul = balance_ul + COALESCE(("
            "  SELECT SUM(total_amount_ul) FROM splits WHERE parent_id = tubes.id"
            "), 0) WHERE id NOT IN (SELECT child_id FROM lineage_edges)"
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def init_db(db_path: str) -> None:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        _migrate_initial_ul(conn)
        conn.executescript(SCHEMA)
    finally:
        conn.close()
