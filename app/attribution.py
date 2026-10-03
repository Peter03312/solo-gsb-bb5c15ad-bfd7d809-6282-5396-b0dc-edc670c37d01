"""停机归因核心算法。

规则：
- 事件与班次均为半开区间 [start, end)；
- 区间重叠时优先级：保养(maintenance) > 故障(failure) > 待判(pending)；
- 每分钟只归因一次（高优先级覆盖低优先级）。

现场人工裁定（rulings）：
- 每条裁定是班次内一个半开区间 + 原因，seq 为班次内的生效顺序（小者先生效）；
- 每分钟以**生效顺序最新**（seq 最大）的未撤销裁定覆盖自动事件归因；
  重叠裁定撤销后，该分钟自然回退至此前仍有效的裁定；
  无裁定覆盖的分钟再按当前最新事件版本（自动归因）计算。

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


@dataclass(frozen=True)
class RulingView:
    """归因计算所需的现场人工裁定（当前未撤销）。

    seq 为班次内的生效顺序：数值越大生效越晚，重叠分钟由其覆盖较早裁定。
    """

    ruling_no: str
    seq: int
    reason: str
    start: datetime
    end: datetime
    note: str = ""


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


def _ruled_minutes(
    shift_start: datetime,
    shift_end: datetime,
    rulings: Iterable[RulingView],
) -> tuple[dict[str, list[Interval]], dict[str, list[Interval]]]:
    """计算人工裁定覆盖的分钟。

    按生效顺序（seq）从早到晚处理：每条裁定先落入其原因桶，再扣除**更晚生效**
    裁定所覆盖的分钟。因此重叠区间只保留 seq 最大（最新）的裁定；较晚裁定一旦
    撤销，其区间不再出现在输入中，较早裁定的原区间无需任何补偿即重新生效。

    返回 (每原因的裁定有效区间, 每条裁定实际贡献分钟的区间)。
    """
    clipped: list[tuple[Interval, RulingView]] = []
    for rv in rulings:
        if rv.reason not in PRIORITY:
            raise ValueError(f"unknown reason: {rv.reason!r}")
        iv = _clip(rv.start, rv.end, shift_start, shift_end)
        if iv is not None:
            clipped.append((iv, rv))

    # 更晚生效（seq 更大）裁定区间的并集：按 seq 顺序预计算
    seqs = sorted({rv.seq for _, rv in clipped})
    newer_than: dict[int, set[int]] = {
        seq: set(seqs[i + 1 :]) for i, seq in enumerate(seqs)
    }

    by_reason: dict[str, list[Interval]] = {r: [] for r in REASONS}
    by_ruling: dict[str, list[Interval]] = {}
    for iv, rv in clipped:
        eff = subtract(
            [iv], [x for x, r in clipped if r.seq in newer_than[rv.seq]]
        )
        if not eff:
            continue  # 实际分钟全部被更晚的裁定覆盖
        by_reason[rv.reason].extend(eff)
        by_ruling[rv.ruling_no] = merge(by_ruling.get(rv.ruling_no, []) + eff)

    by_reason = {r: merge(ivs) for r, ivs in by_reason.items()}
    return by_reason, by_ruling


def attribute(
    shift_start: datetime,
    shift_end: datetime,
    events: Iterable[EventView],
    rulings: Iterable[RulingView] = (),
) -> tuple[dict[str, int], list[dict], list[dict]]:
    """计算班次窗口 [shift_start, shift_end) 内各原因的停机分钟数。

    返回 (totals, used_versions, used_rulings)：
      totals        : {"maintenance": m, "failure": f, "pending": p}
      used_versions : 实际贡献了分钟数的事件版本列表
                      [{"source_id": ..., "version": ..., "reason": ...}, ...]，
                      按优先级（保养→故障→待判）排列；
      used_rulings  : 实际贡献了分钟数的现场裁定列表
                      [{"ruling_no": ..., "reason": ...}, ...]，按生效顺序排列。
    被裁定覆盖的分钟不再参与自动事件归因。
    """
    # ------------------------------------------------------------ 人工裁定层
    ruled, ruled_by_no = _ruled_minutes(shift_start, shift_end, rulings)

    # ------------------------------------------------------------ 自动事件层
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
    m_auto = merge(m_raw)
    f_auto = merge(subtract(f_raw, m_raw))
    p_auto = merge(subtract(p_raw, m_raw + f_raw))

    # 自动事件归因再扣除人工裁定覆盖的全部分钟
    ruled_covers = [iv for ivs in ruled.values() for iv in ivs]
    m_eff = subtract(m_auto, ruled_covers)
    f_eff = subtract(f_auto, ruled_covers)
    p_eff = subtract(p_auto, ruled_covers)

    totals = {
        REASON_MAINTENANCE: total_minutes(ruled[REASON_MAINTENANCE])
        + total_minutes(m_eff),
        REASON_FAILURE: total_minutes(ruled[REASON_FAILURE]) + total_minutes(f_eff),
        REASON_PENDING: total_minutes(ruled[REASON_PENDING]) + total_minutes(p_eff),
    }

    effective_auto = {
        REASON_MAINTENANCE: m_eff,
        REASON_FAILURE: f_eff,
        REASON_PENDING: p_eff,
    }
    used: list[dict] = []
    for reason in REASONS:
        for iv, ev in clipped[reason]:
            if any(s < iv[1] and iv[0] < e for s, e in effective_auto[reason]):
                used.append(
                    {"source_id": ev.source_id, "version": ev.version, "reason": reason}
                )

    # 实际贡献了分钟的裁定，按生效顺序（seq）升序返回
    seq_of = {rv.ruling_no: rv.seq for rv in rulings}
    reason_of = {rv.ruling_no: rv.reason for rv in rulings}
    used_rulings = [
        {"ruling_no": no, "reason": reason_of[no]}
        for no in sorted(ruled_by_no, key=lambda no: (seq_of.get(no, 0), no))
    ]
    return totals, used, used_rulings
