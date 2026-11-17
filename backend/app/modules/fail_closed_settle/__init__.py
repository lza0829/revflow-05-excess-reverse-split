"""归并整单失败闭：一轮归并要么全部落库，要么回到操作前。

使用 SQLite 显式事务（BEGIN IMMEDIATE），事务内任何异常都 ROLLBACK，
不留下「已扣余额但没有隔离行」「已写分栏但没有余额」等半成品。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """原子事务：成功 COMMIT，异常 ROLLBACK。"""
    previous_isolation = conn.isolation_level
    conn.isolation_level = None  # 关闭隐式事务，改为显式控制
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        else:
            conn.execute("COMMIT")
    finally:
        conn.isolation_level = previous_isolation
