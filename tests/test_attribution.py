"""归因算法单元测试（仅依赖标准库）。

可用 pytest 运行，也可直接：python3 tests/test_attribution.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.attribution import (  # noqa: E402
    EventView,
    RulingView,
    attribute,
    merge,
    subtract,
    total_minutes,
)

UTC = timezone.utc
BASE = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


def h(hours: float = 0, minutes: float = 0) -> datetime:
    return BASE + timedelta(hours=hours, minutes=minutes)


def ev(source: str, version: int, reason: str, start: datetime, end: datetime) -> EventView:
    return EventView(source_id=source, version=version, reason=reason, start=start, end=end)


def rv(no: str, seq: int, reason: str, start: datetime, end: datetime) -> RulingView:
    return RulingView(ruling_no=no, seq=seq, reason=reason, start=start, end=end)


# ---------------------------------------------------------------- 区间原语


def test_merge_overlapping_and_touching():
    assert merge([(h(1), h(2)), (h(2), h(3)), (h(1, 30), h(4))]) == [(h(1), h(4))]
    assert merge([]) == []
    assert merge([(h(1), h(1))]) == []  # 空区间被丢弃


def test_subtract_basic():
    assert subtract([(h(0), h(10))], [(h(3), h(5))]) == [(h(0), h(3)), (h(5), h(10))]


def test_subtract_full_cover_and_no_cover():
    assert subtract([(h(0), h(10))], [(h(0), h(10))]) == []
    assert subtract([(h(0), h(2))], [(h(5), h(6))]) == [(h(0), h(2))]


def test_subtract_multiple_covers():
    assert subtract([(h(0), h(10))], [(h(1), h(2)), (h(4), h(6)), (h(9), h(20))]) == [
        (h(0), h(1)),
        (h(2), h(4)),
        (h(6), h(9)),
    ]


def test_total_minutes():
    assert total_minutes([(h(0), h(1)), (h(2), h(2), )]) == 60
    assert total_minutes([(h(0), h(0, 30))]) == 30


# ---------------------------------------------------------------- 归因


def test_no_events():
    totals, used, _used_r = attribute(h(8), h(16), [])
    assert totals == {"maintenance": 0, "failure": 0, "pending": 0}
    assert used == []


def test_half_open_interval():
    # [08:00, 10:00) 恰好 120 分钟，10:00 这一分钟不计入
    totals, _, _r = attribute(h(8), h(16), [ev("S1", 1, "failure", h(8), h(10))])
    assert totals["failure"] == 120
    # 相接事件不重叠、不漏分钟
    totals, _, _r = attribute(
        h(8),
        h(16),
        [ev("S1", 1, "failure", h(8), h(10)), ev("S2", 1, "failure", h(10), h(11))],
    )
    assert totals["failure"] == 180


def test_event_clipped_to_shift_window():
    totals, _, _r = attribute(h(8), h(16), [ev("S1", 1, "failure", h(7), h(9))])
    assert totals["failure"] == 60
    # 完全在班次外的事件不参与归因，也不出现在依据版本中
    totals, used, _used_r = attribute(h(8), h(16), [ev("S1", 1, "failure", h(2), h(3))])
    assert totals["failure"] == 0
    assert used == []


def test_priority_maintenance_over_failure():
    totals, used, _used_r = attribute(
        h(8),
        h(16),
        [
            ev("M1", 1, "maintenance", h(10), h(11)),
            ev("F1", 1, "failure", h(10, 30), h(12)),
        ],
    )
    assert totals == {"maintenance": 60, "failure": 60, "pending": 0}
    assert {u["source_id"] for u in used} == {"M1", "F1"}


def test_priority_failure_over_pending():
    totals, used, _used_r = attribute(
        h(8),
        h(16),
        [
            ev("F1", 1, "failure", h(10), h(11)),
            ev("P1", 1, "pending", h(10, 30), h(12)),
        ],
    )
    assert totals == {"maintenance": 0, "failure": 60, "pending": 60}


def test_pending_subtracts_maintenance_and_failure():
    # 待判 [10:00, 12:00)，保养 [10:00,10:30)，故障 [11:00,11:30)
    totals, _, _r = attribute(
        h(8),
        h(16),
        [
            ev("M1", 1, "maintenance", h(10), h(10, 30)),
            ev("F1", 1, "failure", h(11), h(11, 30)),
            ev("P1", 1, "pending", h(10), h(12)),
        ],
    )
    assert totals == {"maintenance": 30, "failure": 30, "pending": 60}


def test_each_minute_attributed_once():
    # 三者完全重叠：全部归保养
    totals, used, _used_r = attribute(
        h(8),
        h(16),
        [
            ev("M1", 1, "maintenance", h(10), h(11)),
            ev("F1", 1, "failure", h(10), h(11)),
            ev("P1", 1, "pending", h(10), h(11)),
        ],
    )
    assert totals == {"maintenance": 60, "failure": 0, "pending": 0}
    # 被高优先级完全覆盖的事件不出现在依据版本中
    assert [u["source_id"] for u in used] == ["M1"]


def test_same_reason_events_union():
    # 同原因重叠事件合并计算，不重复计分钟
    totals, _, _r = attribute(
        h(8),
        h(16),
        [
            ev("M1", 1, "maintenance", h(10), h(11)),
            ev("M2", 1, "maintenance", h(10, 30), h(11, 30)),
        ],
    )
    assert totals["maintenance"] == 90


def test_used_versions_reflect_contribution():
    totals, used, _used_r = attribute(
        h(8),
        h(16),
        [
            ev("M1", 3, "maintenance", h(10), h(11)),
            ev("F1", 2, "failure", h(10), h(10, 30)),  # 完全被保养覆盖
            ev("F2", 1, "failure", h(12), h(13)),
        ],
    )
    assert totals == {"maintenance": 60, "failure": 60, "pending": 0}
    by_source = {u["source_id"]: u["version"] for u in used}
    assert by_source == {"M1": 3, "F2": 1}


# ---------------------------------------------------------------- 现场人工裁定


def test_ruling_overrides_event_on_same_minutes():
    # 自动归因：10:00-12:00 故障 120；裁定 10:00-11:00 为保养
    totals, used, used_rulings = attribute(
        h(8),
        h(16),
        [ev("F1", 1, "failure", h(10), h(12))],
        [rv("J1", 1, "maintenance", h(10), h(11))],
    )
    assert totals == {"maintenance": 60, "failure": 60, "pending": 0}
    assert used_rulings == [{"ruling_no": "J1", "reason": "maintenance"}]
    # 故障事件仍在 11:00-12:00 贡献，保留在依据版本中
    assert [u["source_id"] for u in used] == ["F1"]


def test_ruling_without_events_sets_reason_minutes():
    # 无自动事件，裁定直接产生待判分钟
    totals, used, used_rulings = attribute(
        h(8), h(16), [], [rv("J1", 1, "pending", h(9), h(10, 30))]
    )
    assert totals == {"maintenance": 0, "failure": 0, "pending": 90}
    assert used == []
    assert used_rulings == [{"ruling_no": "J1", "reason": "pending"}]


def test_overlapping_rulings_latest_seq_wins():
    # J1(seq=1) 保养 10:00-12:00；J2(seq=2) 故障 11:00-12:00 覆盖重叠分钟
    totals, _used, used_rulings = attribute(
        h(8),
        h(16),
        [],
        [
            rv("J1", 1, "maintenance", h(10), h(12)),
            rv("J2", 2, "failure", h(11), h(12)),
        ],
    )
    assert totals == {"maintenance": 60, "failure": 60, "pending": 0}
    assert used_rulings == [
        {"ruling_no": "J1", "reason": "maintenance"},
        {"ruling_no": "J2", "reason": "failure"},
    ]


def test_latest_ruling_revoked_falls_back_to_earlier_ruling():
    # 撤销 J2（不出现在输入中）后，11:00-12:00 回退给 J1 的保养
    totals, _used, used_rulings = attribute(
        h(8), h(16), [], [rv("J1", 1, "maintenance", h(10), h(12))]
    )
    assert totals == {"maintenance": 120, "failure": 0, "pending": 0}
    assert used_rulings == [{"ruling_no": "J1", "reason": "maintenance"}]


def test_revoke_all_rulings_falls_back_to_events():
    # 自动故障 10:00-12:00；曾被保养裁定全覆盖，裁定全部撤销后回到自动归因
    totals, used, used_rulings = attribute(
        h(8), h(16), [ev("F1", 1, "failure", h(10), h(12))], []
    )
    assert totals == {"maintenance": 0, "failure": 120, "pending": 0}
    assert [u["source_id"] for u in used] == ["F1"]
    assert used_rulings == []


def test_three_rulings_chain_only_latest_per_minute():
    # 同一区间三层裁定：J1 保养、J2 故障、J3 待判 —— 全部归待判，J1/J2 无贡献
    totals, _used, used_rulings = attribute(
        h(8),
        h(16),
        [ev("F1", 1, "failure", h(10), h(11))],
        [
            rv("J1", 1, "maintenance", h(10), h(11)),
            rv("J2", 2, "failure", h(10), h(11)),
            rv("J3", 3, "pending", h(10), h(11)),
        ],
    )
    assert totals == {"maintenance": 0, "failure": 0, "pending": 60}
    assert used_rulings == [{"ruling_no": "J3", "reason": "pending"}]
    # 撤销 J3 → 回退 J2（故障）
    totals, _, used_rulings = attribute(
        h(8),
        h(16),
        [ev("F1", 1, "failure", h(10), h(11))],
        [
            rv("J1", 1, "maintenance", h(10), h(11)),
            rv("J2", 2, "failure", h(10), h(11)),
        ],
    )
    assert totals == {"maintenance": 0, "failure": 60, "pending": 0}
    assert used_rulings == [{"ruling_no": "J2", "reason": "failure"}]
    # 再撤销 J2 → 回退 J1（保养）
    totals, _, used_rulings = attribute(
        h(8),
        h(16),
        [ev("F1", 1, "failure", h(10), h(11))],
        [rv("J1", 1, "maintenance", h(10), h(11))],
    )
    assert totals == {"maintenance": 60, "failure": 0, "pending": 0}


def test_ruling_clipped_to_shift_and_partial_contribution():
    # 裁定区间超出班次窗口时裁剪；J2 与 J1 部分重叠，二者各计贡献区间
    totals, _used, used_rulings = attribute(
        h(8),
        h(16),
        [],
        [
            rv("J1", 1, "failure", h(7), h(9)),        # 裁剪为 08:00-09:00
            rv("J2", 2, "maintenance", h(8, 30), h(17)),  # 裁剪为 08:30-16:00
        ],
    )
    assert totals == {"maintenance": 450, "failure": 30, "pending": 0}
    assert [r["ruling_no"] for r in used_rulings] == ["J1", "J2"]


def test_ruling_then_correction_interaction():
    # 裁定 10:00-11:00 保养；事件更正把故障从 10:00-12:00 改为 11:00-12:00：
    # 裁定分钟不受影响，自动分钟跟随最新事件版本
    totals, used, used_rulings = attribute(
        h(8),
        h(16),
        [ev("F1", 2, "failure", h(11), h(12))],
        [rv("J1", 1, "maintenance", h(10), h(11))],
    )
    assert totals == {"maintenance": 60, "failure": 60, "pending": 0}
    assert [u["source_id"] for u in used] == ["F1"]
    assert used_rulings == [{"ruling_no": "J1", "reason": "maintenance"}]


def test_same_seq_rulings_same_reason_union():
    # 同 seq（不会由正常流程产生，仅验证算法健壮性）：互不扣除，同原因取并集
    totals, _, used_rulings = attribute(
        h(8),
        h(16),
        [],
        [
            rv("J1", 1, "failure", h(10), h(11)),
            rv("J2", 1, "failure", h(10, 30), h(12)),
        ],
    )
    assert totals == {"maintenance": 0, "failure": 120, "pending": 0}
    assert [r["ruling_no"] for r in used_rulings] == ["J1", "J2"]


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
