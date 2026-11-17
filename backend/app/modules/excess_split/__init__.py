"""超额冲正拆分：把冲正金额拆成「可配平前缀」与「超额隔离」两段。

约定（与 settle 服务共用）：
- 分栏写入 ledger_entries.applied_cents / excess_cents，且写入后不可再改；
- 超额段同时落 quarantine_items，entry_key 与原冲正键一致；
- 配平段计入余额，超额段不进余额。
"""
from __future__ import annotations

from dataclasses import dataclass

STATUS_FULL = "已归并"
STATUS_PARTIAL = "部分归并"
STATUS_QUARANTINED = "已隔离"

REASON_EXCESS = "冲正超额"

# 新拆分落库的隔离行标记；用于与历史「整笔超额」隔离行区分
SPLIT_KIND_PARTIAL = "部分配平"
SPLIT_KIND_PURE_EXCESS = "整笔超额"
SPLIT_KINDS = (SPLIT_KIND_PARTIAL, SPLIT_KIND_PURE_EXCESS)


@dataclass(frozen=True)
class Split:
    """一次冲正的拆分结果（分）。"""

    amount_cents: int
    applied_cents: int
    excess_cents: int

    @property
    def status(self) -> str:
        if self.amount_cents == 0:
            return STATUS_FULL
        if self.applied_cents > 0 and self.excess_cents > 0:
            return STATUS_PARTIAL
        if self.applied_cents > 0:
            return STATUS_FULL
        return STATUS_QUARANTINED

    @property
    def split_kind(self) -> str:
        if self.applied_cents > 0 and self.excess_cents > 0:
            return SPLIT_KIND_PARTIAL
        return SPLIT_KIND_PURE_EXCESS


def split_reversal(amount_cents: int, remaining_cents: int) -> Split:
    """按目标剩余敞口拆分冲正金额。

    - 剩余敞口 <= 0：整笔进入隔离；
    - 冲正 <= 敞口：全额配平；
    - 冲正 > 敞口 > 0：敞口部分配平，超出部分隔离。
    """
    if amount_cents < 0:
        raise ValueError("冲正金额不能为负")
    if remaining_cents < 0:
        remaining_cents = 0
    applied = min(amount_cents, remaining_cents)
    return Split(
        amount_cents=amount_cents,
        applied_cents=applied,
        excess_cents=amount_cents - applied,
    )


def assert_split_invariant(split: Split, amount_cents: int) -> None:
    if split.amount_cents != amount_cents:
        raise RuntimeError("冲正拆分金额与原冲正额不一致")
    if split.applied_cents + split.excess_cents != amount_cents:
        raise RuntimeError("冲正拆分不守恒：配平 + 超额 != 原冲正额")
