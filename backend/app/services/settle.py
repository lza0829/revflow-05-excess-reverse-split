from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import connect, rows_to_dicts
from app.modules.excess_split import (
    REASON_EXCESS,
    STATUS_FULL,
    STATUS_PARTIAL,
    STATUS_QUARANTINED,
    assert_split_invariant,
    split_reversal,
)
from app.modules.fail_closed_settle import transaction


SETTLED_REVERSAL_STATUSES = (STATUS_FULL, STATUS_PARTIAL)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _applied_expression() -> str:
    # 旧数据没有分栏时，按整笔冲正处理
    return "COALESCE(applied_cents, amount_cents)"


def _applied_by_target(conn) -> dict[str, int]:
    rows = conn.execute(
        f"""
        SELECT target_entry_key AS k, SUM({_applied_expression()}) AS s
        FROM ledger_entries
        WHERE kind = '冲正'
          AND status IN (?, ?)
          AND target_entry_key IS NOT NULL
        GROUP BY target_entry_key
        """,
        SETTLED_REVERSAL_STATUSES,
    ).fetchall()
    return {r["k"]: r["s"] for r in rows}


def _remaining_exposure_cents(conn) -> int:
    """全部已归并正向尚未被冲正配平的敞口合计（分）。"""
    applied = _applied_by_target(conn)
    total = 0
    for row in conn.execute(
        "SELECT entry_key, amount_cents FROM ledger_entries WHERE kind = '正向' AND status = '已归并'"
    ):
        total += max(0, row["amount_cents"] - applied.get(row["entry_key"], 0))
    return total


def _recompute_balances(conn) -> dict[int, int]:
    """余额完全由流水派生：已归并正向 − 已配平冲正。"""
    rows = conn.execute(
        f"""
        SELECT a.id,
          COALESCE((
            SELECT SUM(e.amount_cents) FROM ledger_entries e
            WHERE e.account_id = a.id AND e.kind = '正向' AND e.status = '已归并'
          ), 0)
          -
          COALESCE((
            SELECT SUM({_applied_expression()}) FROM ledger_entries e
            WHERE e.account_id = a.id AND e.kind = '冲正' AND e.status IN (?, ?)
          ), 0) AS balance_cents
        FROM accounts a
        """,
        SETTLED_REVERSAL_STATUSES,
    ).fetchall()
    return {r["id"]: r["balance_cents"] for r in rows}


def _insert_quarantine(
    conn,
    *,
    entry_key: str,
    account_id: int,
    reason: str,
    amount_cents: int,
    settle_run_id: int,
    created_at: str,
    parent_entry_key: str,
    split_applied_cents: int,
    exposure_cents: int,
    split_kind: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO quarantine_items(
          entry_key, account_id, reason, amount_cents, settle_run_id, created_at,
          parent_entry_key, split_applied_cents, exposure_cents, split_kind
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry_key,
            account_id,
            reason,
            amount_cents,
            settle_run_id,
            created_at,
            parent_entry_key,
            split_applied_cents,
            exposure_cents,
            split_kind,
        ),
    )
    return int(cur.lastrowid)


def _check_run_totals(conn, run_id: int, applied_total: int, excess_total: int, remaining: int) -> None:
    """落库前自校验：报告字段必须与分栏、隔离表、余额同源。"""
    db_applied = conn.execute(
        f"""
        SELECT COALESCE(SUM({_applied_expression()}), 0) AS s FROM ledger_entries
        WHERE kind = '冲正' AND settle_run_id = ? AND status IN (?, ?)
        """,
        (run_id, *SETTLED_REVERSAL_STATUSES),
    ).fetchone()["s"]
    db_excess = conn.execute(
        """
        SELECT COALESCE(SUM(amount_cents), 0) AS s FROM quarantine_items
        WHERE settle_run_id = ? AND reason = ?
        """,
        (run_id, REASON_EXCESS),
    ).fetchone()["s"]
    db_excess_columns = conn.execute(
        """
        SELECT COALESCE(SUM(excess_cents), 0) AS s FROM ledger_entries
        WHERE kind = '冲正' AND settle_run_id = ? AND excess_cents IS NOT NULL
        """,
        (run_id,),
    ).fetchone()["s"]
    db_balance = conn.execute("SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts").fetchone()["s"]
    expected_balance = sum(_recompute_balances(conn).values())
    # 剩余敞口独立重算：正向合计 − 全部已配平冲正合计
    db_fwd = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries WHERE kind = '正向' AND status = '已归并'"
    ).fetchone()["s"]
    db_rev_applied = conn.execute(
        f"""
        SELECT COALESCE(SUM({_applied_expression()}), 0) AS s FROM ledger_entries
        WHERE kind = '冲正' AND status IN (?, ?)
        """,
        SETTLED_REVERSAL_STATUSES,
    ).fetchone()["s"]
    expected_remaining = db_fwd - db_rev_applied

    problems = []
    if db_applied != applied_total:
        problems.append(f"已归并冲正额 {applied_total} != 分栏合计 {db_applied}")
    if db_excess != excess_total:
        problems.append(f"超额隔离额 {excess_total} != 隔离表合计 {db_excess}")
    if db_excess_columns != excess_total:
        problems.append(f"超额隔离额 {excess_total} != 冲正超额分栏合计 {db_excess_columns}")
    if _remaining_exposure_cents(conn) != remaining:
        problems.append("剩余敞口与流水敞口不一致")
    if remaining != expected_remaining:
        problems.append(f"剩余敞口 {remaining} != 正向−已配平 {expected_remaining}")
    if db_balance != expected_balance:
        problems.append(f"账户余额 {db_balance} != 流水派生余额 {expected_balance}")
    if problems:
        raise RuntimeError("归并自校验失败：" + "；".join(problems))


def dashboard() -> dict:
    with connect() as conn:
        accounts = rows_to_dicts(conn.execute("SELECT * FROM accounts ORDER BY id").fetchall())
        fwd = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE kind = '正向'"
        ).fetchone()["c"]
        rev = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE kind = '冲正'"
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE status = '待归并'"
        ).fetchone()["c"]
        q = conn.execute("SELECT COUNT(*) AS c FROM quarantine_items").fetchone()["c"]
        excess_q = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM quarantine_items WHERE reason = ?",
            (REASON_EXCESS,),
        ).fetchone()["s"]
        bal = conn.execute("SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts").fetchone()["s"]
        last = conn.execute("SELECT * FROM settle_runs ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "账户数": len(accounts),
            "正向笔数": fwd,
            "冲正笔数": rev,
            "待归并": pending,
            "未平隔离": q,
            "超额隔离额分": excess_q,
            "余额合计分": bal,
            "最近归并": dict(last) if last else None,
            "账户": accounts,
        }


def list_accounts() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(conn.execute("SELECT * FROM accounts ORDER BY id").fetchall())


def list_entries(kind: str | None = None) -> list[dict]:
    with connect() as conn:
        if kind:
            rows = conn.execute(
                """
                SELECT e.*, a.code AS account_code, a.name AS account_name
                FROM ledger_entries e
                JOIN accounts a ON a.id = e.account_id
                WHERE e.kind = ?
                ORDER BY e.id
                """,
                (kind,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT e.*, a.code AS account_code, a.name AS account_name
                FROM ledger_entries e
                JOIN accounts a ON a.id = e.account_id
                ORDER BY e.id
                """
            ).fetchall()
        return rows_to_dicts(rows)


def list_quarantine() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT q.*, a.code AS account_code
            FROM quarantine_items q
            JOIN accounts a ON a.id = q.account_id
            ORDER BY q.id DESC
            """
        ).fetchall()
        return rows_to_dicts(rows)


def list_settle_runs() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(
            conn.execute("SELECT * FROM settle_runs ORDER BY id DESC").fetchall()
        )


def list_verify_reports() -> list[dict]:
    with connect() as conn:
        rows = rows_to_dicts(
            conn.execute("SELECT * FROM verify_reports ORDER BY id DESC").fetchall()
        )
    for r in rows:
        try:
            r["detail"] = json.loads(r.get("detail_json") or "{}")
        except json.JSONDecodeError:
            r["detail"] = {}
    return rows


def run_settle() -> dict:
    """净额归并：超额冲正拆成「配平前缀 + 超额隔离」，整轮失败闭。"""
    with connect() as conn:
        try:
            with transaction(conn):
                run_id = _run_settle_in_transaction(conn)
        except HTTPException:
            raise
        except Exception as exc:  # 任何落库/自校验失败都整轮回滚
            raise HTTPException(500, f"归并失败，已整轮回滚：{exc}") from exc
        return get_settle_run(run_id)


def _run_settle_in_transaction(conn) -> int:
    now = _now()
    cur = conn.execute(
        "INSERT INTO settle_runs(status, started_at) VALUES ('进行中', ?)",
        (now,),
    )
    run_id = int(cur.lastrowid)

    forwards = rows_to_dicts(
        conn.execute(
            "SELECT * FROM ledger_entries WHERE kind = '正向' AND status = '待归并' ORDER BY id"
        ).fetchall()
    )
    reversals = rows_to_dicts(
        conn.execute(
            "SELECT * FROM ledger_entries WHERE kind = '冲正' AND status = '待归并' ORDER BY id"
        ).fetchall()
    )
    all_forwards = {
        r["entry_key"]: dict(r)
        for r in conn.execute("SELECT * FROM ledger_entries WHERE kind = '正向'").fetchall()
    }
    applied_by_target = _applied_by_target(conn)

    settled = 0
    quarantined = 0
    applied_total = 0
    excess_total = 0

    for r in reversals:
        target = r.get("target_entry_key")
        if not target or target not in all_forwards:
            _insert_quarantine(
                conn,
                entry_key=r["entry_key"],
                account_id=r["account_id"],
                reason="冲正无原单",
                amount_cents=r["amount_cents"],
                settle_run_id=run_id,
                created_at=now,
                parent_entry_key=r["entry_key"],
                split_applied_cents=0,
                exposure_cents=0,
                split_kind="整笔隔离",
            )
            conn.execute(
                "UPDATE ledger_entries SET status = ?, settle_run_id = ? WHERE id = ?",
                (STATUS_QUARANTINED, run_id, r["id"]),
            )
            quarantined += 1
            continue

        fwd = all_forwards[target]
        if fwd["account_id"] != r["account_id"]:
            _insert_quarantine(
                conn,
                entry_key=r["entry_key"],
                account_id=r["account_id"],
                reason="冲正账户与原单不一致",
                amount_cents=r["amount_cents"],
                settle_run_id=run_id,
                created_at=now,
                parent_entry_key=r["entry_key"],
                split_applied_cents=0,
                exposure_cents=0,
                split_kind="整笔隔离",
            )
            conn.execute(
                "UPDATE ledger_entries SET status = ?, settle_run_id = ? WHERE id = ?",
                (STATUS_QUARANTINED, run_id, r["id"]),
            )
            quarantined += 1
            continue

        remaining = max(0, fwd["amount_cents"] - applied_by_target.get(target, 0))
        split = split_reversal(r["amount_cents"], remaining)
        assert_split_invariant(split, r["amount_cents"])

        # 超额段单独落隔离行，和原冲正共享 parent_entry_key
        if split.excess_cents > 0:
            _insert_quarantine(
                conn,
                entry_key=r["entry_key"],
                account_id=r["account_id"],
                reason=REASON_EXCESS,
                amount_cents=split.excess_cents,
                settle_run_id=run_id,
                created_at=now,
                parent_entry_key=r["entry_key"],
                split_applied_cents=split.applied_cents,
                exposure_cents=remaining,
                split_kind=split.split_kind,
            )
            quarantined += 1

        if split.applied_cents > 0:
            applied_by_target[target] = applied_by_target.get(target, 0) + split.applied_cents
            settled += 1
            applied_total += split.applied_cents
        if split.excess_cents > 0:
            excess_total += split.excess_cents

        conn.execute(
            """
            UPDATE ledger_entries
            SET status = ?, applied_cents = ?, excess_cents = ?, settle_run_id = ?
            WHERE id = ?
            """,
            (split.status, split.applied_cents, split.excess_cents, run_id, r["id"]),
        )

    for f in forwards:
        conn.execute(
            "UPDATE ledger_entries SET status = '已归并', settle_run_id = ? WHERE id = ?",
            (run_id, f["id"]),
        )
        settled += 1

    # 余额由已归并流水整体重算，避免多轮累加漂移
    balances = _recompute_balances(conn)
    for acc_id, bal in balances.items():
        conn.execute(
            "UPDATE accounts SET balance_cents = ?, last_settle_run_id = ? WHERE id = ?",
            (bal, run_id, acc_id),
        )
    net_total = sum(balances.values())

    remaining_exposure = _remaining_exposure_cents(conn)
    _check_run_totals(conn, run_id, applied_total, excess_total, remaining_exposure)

    finished = _now()
    conn.execute(
        """
        UPDATE settle_runs
        SET status = '完成',
            forward_count = ?,
            reversal_count = ?,
            settled_count = ?,
            quarantine_count = ?,
            net_cents = ?,
            applied_reversal_cents = ?,
            excess_quarantine_cents = ?,
            remaining_exposure_cents = ?,
            finished_at = ?
        WHERE id = ?
        """,
        (
            len(forwards),
            len(reversals),
            settled,
            quarantined,
            net_total,
            applied_total,
            excess_total,
            remaining_exposure,
            finished,
            run_id,
        ),
    )
    return run_id


def get_settle_run(run_id: int) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM settle_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, "归并运行不存在")
        return dict(row)


def run_verify(settle_run_id: int | None = None) -> dict:
    with connect() as conn:
        if settle_run_id is None:
            last = conn.execute("SELECT id FROM settle_runs ORDER BY id DESC LIMIT 1").fetchone()
            if not last:
                raise HTTPException(400, "尚无归并运行，无法互证")
            settle_run_id = last["id"]
        run = conn.execute("SELECT * FROM settle_runs WHERE id = ?", (settle_run_id,)).fetchone()
        if not run:
            raise HTTPException(404, "归并运行不存在")

        fwd_sum = conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries
            WHERE kind = '正向' AND status = '已归并' AND settle_run_id = ?
            """,
            (settle_run_id,),
        ).fetchone()["s"]
        rev_sum = conn.execute(
            f"""
            SELECT COALESCE(SUM({_applied_expression()}), 0) AS s FROM ledger_entries
            WHERE kind = '冲正' AND status IN (?, ?) AND settle_run_id = ?
            """,
            (*SETTLED_REVERSAL_STATUSES, settle_run_id),
        ).fetchone()["s"]
        ledger_net = fwd_sum - rev_sum
        balance_sum = conn.execute(
            "SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts WHERE last_settle_run_id = ?",
            (settle_run_id,),
        ).fetchone()["s"]
        q_count = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine_items WHERE settle_run_id = ?",
            (settle_run_id,),
        ).fetchone()["c"]

        all_fwd = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries WHERE kind = '正向' AND status = '已归并'"
        ).fetchone()["s"]
        all_rev = conn.execute(
            f"""
            SELECT COALESCE(SUM({_applied_expression()}), 0) AS s FROM ledger_entries
            WHERE kind = '冲正' AND status IN (?, ?)
            """,
            SETTLED_REVERSAL_STATUSES,
        ).fetchone()["s"]
        all_ledger_net = all_fwd - all_rev
        all_bal = conn.execute("SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts").fetchone()["s"]

        # 报告口径三源互证：分栏（冲正表）、隔离表、余额
        applied_reversal = conn.execute(
            f"""
            SELECT COALESCE(SUM({_applied_expression()}), 0) AS s FROM ledger_entries
            WHERE kind = '冲正' AND status IN (?, ?) AND settle_run_id = ?
            """,
            (*SETTLED_REVERSAL_STATUSES, settle_run_id),
        ).fetchone()["s"]
        excess_quarantine = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM quarantine_items WHERE settle_run_id = ? AND reason = ?",
            (settle_run_id, REASON_EXCESS),
        ).fetchone()["s"]
        excess_columns = conn.execute(
            """
            SELECT COALESCE(SUM(excess_cents), 0) AS s FROM ledger_entries
            WHERE kind = '冲正' AND settle_run_id = ? AND excess_cents IS NOT NULL
            """,
            (settle_run_id,),
        ).fetchone()["s"]
        remaining_exposure = _remaining_exposure_cents(conn)
        # 逐笔溯源：每个超额隔离行必须能挂回同键的原冲正，且配平额与分栏一致
        traced = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM quarantine_items q
            JOIN ledger_entries e
              ON e.entry_key = q.parent_entry_key
             AND e.kind = '冲正'
             AND e.settle_run_id = q.settle_run_id
            WHERE q.settle_run_id = ?
              AND q.reason = ?
              AND q.entry_key = q.parent_entry_key
              AND q.split_applied_cents = COALESCE(e.applied_cents, 0)
            """,
            (settle_run_id, REASON_EXCESS),
        ).fetchone()["c"]
        excess_rows = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine_items WHERE settle_run_id = ? AND reason = ?",
            (settle_run_id, REASON_EXCESS),
        ).fetchone()["c"]
        split_columns = conn.execute(
            """
            SELECT COUNT(*) AS c FROM ledger_entries
            WHERE kind = '冲正' AND settle_run_id = ? AND COALESCE(excess_cents, 0) > 0
            """,
            (settle_run_id,),
        ).fetchone()["c"]

        consistent = (
            1
            if all_ledger_net == all_bal
            and excess_quarantine == excess_columns
            and traced == excess_rows == split_columns
            else 0
        )
        detail = {
            "本轮正向分": fwd_sum,
            "本轮冲正已配平分": rev_sum,
            "本轮流水净额分": ledger_net,
            "本轮触及余额合计分": balance_sum,
            "全量流水净额分": all_ledger_net,
            "全量账户余额分": all_bal,
            "本轮隔离数": q_count,
            "本轮已归并冲正额分": applied_reversal,
            "本轮超额隔离额分": excess_quarantine,
            "超额隔离分栏合计分": excess_columns,
            "超额隔离行数": excess_rows,
            "超额分栏笔数": split_columns,
            "可溯源超额行数": traced,
            "剩余敞口分": remaining_exposure,
        }
        now = _now()
        cur = conn.execute(
            """
            INSERT INTO verify_reports(
              settle_run_id, ledger_net_cents, balance_sum_cents,
              quarantine_count, consistent, detail_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                settle_run_id,
                all_ledger_net,
                all_bal,
                q_count,
                consistent,
                json.dumps(detail, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
        rid = cur.lastrowid
        row = conn.execute("SELECT * FROM verify_reports WHERE id = ?", (rid,)).fetchone()
        out = dict(row)
        out["detail"] = detail
        return out
