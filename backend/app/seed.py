from __future__ import annotations

import json
from pathlib import Path

from app.db import FIXTURES_DIR, connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  currency TEXT NOT NULL DEFAULT 'CNY',
  balance_cents INTEGER NOT NULL DEFAULT 0,
  last_settle_run_id INTEGER
);
CREATE TABLE IF NOT EXISTS ledger_entries (
  id INTEGER PRIMARY KEY,
  entry_key TEXT NOT NULL UNIQUE,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  kind TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  biz_ref TEXT NOT NULL DEFAULT '',
  target_entry_key TEXT,
  booked_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '待归并',
  note TEXT NOT NULL DEFAULT '',
  settle_run_id INTEGER,
  applied_cents INTEGER,
  excess_cents INTEGER
);
CREATE TABLE IF NOT EXISTS quarantine_items (
  id INTEGER PRIMARY KEY,
  entry_key TEXT NOT NULL,
  account_id INTEGER NOT NULL,
  reason TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  settle_run_id INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  parent_entry_key TEXT NOT NULL DEFAULT '',
  split_applied_cents INTEGER NOT NULL DEFAULT 0,
  exposure_cents INTEGER NOT NULL DEFAULT 0,
  split_kind TEXT NOT NULL DEFAULT '整笔'
);
CREATE TABLE IF NOT EXISTS settle_runs (
  id INTEGER PRIMARY KEY,
  status TEXT NOT NULL,
  forward_count INTEGER NOT NULL DEFAULT 0,
  reversal_count INTEGER NOT NULL DEFAULT 0,
  settled_count INTEGER NOT NULL DEFAULT 0,
  quarantine_count INTEGER NOT NULL DEFAULT 0,
  net_cents INTEGER NOT NULL DEFAULT 0,
  applied_reversal_cents INTEGER NOT NULL DEFAULT 0,
  excess_quarantine_cents INTEGER NOT NULL DEFAULT 0,
  remaining_exposure_cents INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS verify_reports (
  id INTEGER PRIMARY KEY,
  settle_run_id INTEGER REFERENCES settle_runs(id),
  ledger_net_cents INTEGER NOT NULL,
  balance_sum_cents INTEGER NOT NULL,
  quarantine_count INTEGER NOT NULL,
  consistent INTEGER NOT NULL,
  detail_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

# 旧库升级：缺列时补列（新增库由 CREATE TABLE 直接带上）
MIGRATIONS = {
    "ledger_entries": {
        "applied_cents": "INTEGER",
        "excess_cents": "INTEGER",
    },
    "quarantine_items": {
        "parent_entry_key": "TEXT NOT NULL DEFAULT ''",
        "split_applied_cents": "INTEGER NOT NULL DEFAULT 0",
        "exposure_cents": "INTEGER NOT NULL DEFAULT 0",
        "split_kind": "TEXT NOT NULL DEFAULT '整笔'",
    },
    "settle_runs": {
        "applied_reversal_cents": "INTEGER NOT NULL DEFAULT 0",
        "excess_quarantine_cents": "INTEGER NOT NULL DEFAULT 0",
        "remaining_exposure_cents": "INTEGER NOT NULL DEFAULT 0",
    },
}

# 分栏一旦写入即不可变：防住「改了分栏却不动隔离行/余额」的旁路
TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS ledger_split_immutable
BEFORE UPDATE OF applied_cents, excess_cents ON ledger_entries
WHEN OLD.applied_cents IS NOT NULL
  AND (NEW.applied_cents IS NOT OLD.applied_cents
       OR NEW.excess_cents IS NOT OLD.excess_cents)
BEGIN
  SELECT RAISE(ABORT, '冲正拆分分栏不可变');
END;
"""


def _migrate(conn) -> None:
    for table, columns in MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.executescript(TRIGGERS)
        n = conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"]
        if n:
            conn.commit()
            return
        seed_path = FIXTURES_DIR / "seed.json"
        data = json.loads(Path(seed_path).read_text(encoding="utf-8"))
        for a in data["accounts"]:
            conn.execute(
                "INSERT INTO accounts(id, code, name, currency, balance_cents) VALUES (?, ?, ?, ?, 0)",
                (a["id"], a["code"], a["name"], a["currency"]),
            )
        for e in data["forward_entries"]:
            conn.execute(
                """
                INSERT INTO ledger_entries(
                  id, entry_key, account_id, kind, amount_cents, biz_ref,
                  target_entry_key, booked_at, status, note
                ) VALUES (?, ?, ?, '正向', ?, ?, NULL, ?, '待归并', ?)
                """,
                (
                    e["id"],
                    e["entry_key"],
                    e["account_id"],
                    e["amount_cents"],
                    e.get("biz_ref", ""),
                    e["booked_at"],
                    e.get("note", ""),
                ),
            )
        # 冲正 id 与正向可能重叠，用独立自增：先取 max
        max_id = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM ledger_entries").fetchone()["m"]
        for i, e in enumerate(data["reversal_entries"], start=1):
            conn.execute(
                """
                INSERT INTO ledger_entries(
                  id, entry_key, account_id, kind, amount_cents, biz_ref,
                  target_entry_key, booked_at, status, note
                ) VALUES (?, ?, ?, '冲正', ?, ?, ?, ?, '待归并', ?)
                """,
                (
                    max_id + i,
                    e["entry_key"],
                    e["account_id"],
                    e["amount_cents"],
                    e.get("biz_ref", ""),
                    e.get("target_entry_key"),
                    e["booked_at"],
                    e.get("note", ""),
                ),
            )
        conn.commit()
