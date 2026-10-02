"""停机归因核心算法。

规则：
- 事件与班次均为半开区间 [start, end)；
- 区间重叠时优先级：保养(maintenance) > 故障(failure) > 待判(pending)；
- 每分钟只归因一次（高优先级覆盖低优先级）。

本模块只依赖标准库，可脱离 Web/数据库独立测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

REASON_MAINTENANCE = "maintenance"  # 保养
REASON_FAILURE = "failure"  # 故障
REASON_PENDING = "pending"  # 待判
REASONS = (REASON_MAINTENANCE, REASON_FAILURE, REASON_PENDING)

# 数值越小优先级越高
PRIORITY = {REASON_MAINTENANCE: 0, REASON_FAILURE: 1, REASON_PENDING: 2}

Interval = tuple[datetime, datetime]


@dataclass(frozen=True)
class EventView:
    """归因计算所需的事件（某来源的某个版本）。"""

    source_id: str
    version: int
    reason: str
    start: datetime
    end: datetime


def merge(intervals: Iterable[Interval]) -> list[Interval]:
    """合并重叠或相接的半开区间，返回按起点排序的不重叠区间列表。"""
    out: list[list[datetime]] = []
    for s, e in sorted(intervals):
        if s >= e:
            continue
        if out and s <= out[-1][1]:
            if e > out[-1][1]:
                out[-1][1] = e
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def subtract(intervals: Iterable[Interval], covers: Iterable[Interval]) -> list[Interval]:
    """从 intervals 中扣除 covers 的并集（均为半开区间）。"""
    merged_covers = merge(covers)
    out: list[Interval] = []
    for s, e in sorted(intervals):
        if s >= e:
            continue
        cur = s
        for cs, ce in merged_covers:
            if ce <= cur:
                continue
            if cs >= e:
                break
            if cs > cur:
                out.append((cur, cs))
            cur = max(cur, ce)
            if cur >= e:
                break
        if cur < e:
            out.append((cur, e))
    return out


def total_minutes(intervals: Iterable[Interval]) -> int:
    """区间列表的总分钟数（输入均已对齐整分钟）。"""
    return sum(int((e - s).total_seconds()) // 60 for s, e in intervals)


def _clip(s: datetime, e: datetime, lo: datetime, hi: datetime) -> Interval | None:
    """将区间裁剪到 [lo, hi) 内，无交集返回 None。"""
    s2, e2 = max(s, lo), min(e, hi)
    return (s2, e2) if s2 < e2 else None


def attribute(
    shift_start: datetime,
    shift_end: datetime,
    events: Iterable[EventView],
) -> tuple[dict[str, int], list[dict]]:
    """计算班次窗口 [shift_start, shift_end) 内各原因的停机分钟数。

    返回 (totals, used_versions)：
      totals        : {"maintenance": m, "failure": f, "pending": p}
      used_versions : 实际贡献了分钟数的事件版本列表
                      [{"source_id": ..., "version": ..., "reason": ...}, ...]，
                      按优先级（保养→故障→待判）排列。
    """
    clipped: dict[str, list[tuple[Interval, EventView]]] = {r: [] for r in REASONS}
    for ev in events:
        if ev.reason not in PRIORITY:
            raise ValueError(f"unknown reason: {ev.reason!r}")
        iv = _clip(ev.start, ev.end, shift_start, shift_end)
        if iv is not None:
            clipped[ev.reason].append((iv, ev))

    m_raw = [iv for iv, _ in clipped[REASON_MAINTENANCE]]
    f_raw = [iv for iv, _ in clipped[REASON_FAILURE]]
    p_raw = [iv for iv, _ in clipped[REASON_PENDING]]

    # 保养全量生效；故障扣除保养覆盖；待判扣除保养与故障覆盖 —— 保证每分钟只归因一次
    m_eff = merge(m_raw)
    f_eff = merge(subtract(f_raw, m_raw))
    p_eff = merge(subtract(p_raw, m_raw + f_raw))

    totals = {
        REASON_MAINTENANCE: total_minutes(m_eff),
        REASON_FAILURE: total_minutes(f_eff),
        REASON_PENDING: total_minutes(p_eff),
    }

    effective = {REASON_MAINTENANCE: m_eff, REASON_FAILURE: f_eff, REASON_PENDING: p_eff}
    used: list[dict] = []
    for reason in REASONS:
        for iv, ev in clipped[reason]:
            if any(s < iv[1] and iv[0] < e for s, e in effective[reason]):
                used.append(
                    {"source_id": ev.source_id, "version": ev.version, "reason": reason}
                )
    return totals, used
