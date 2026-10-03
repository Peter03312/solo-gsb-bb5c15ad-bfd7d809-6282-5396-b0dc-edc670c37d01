"""现场人工裁定 API 端到端测试：需要可用的 PostgreSQL（在 Compose 环境内运行）。

运行方式：docker compose exec app pytest tests/test_rulings.py
"""

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app

TABLES = (
    "shift_results",
    "manual_rulings",
    "event_versions",
    "event_sources",
    "shifts",
    "devices",
    "change_counter",
)

DEVICE = {"id": "CNC-01", "name": "加工中心1号"}
SHIFT = {
    "device_id": "CNC-01",
    "start": "2026-10-01T08:00:00+08:00",
    "end": "2026-10-01T16:00:00+08:00",
}
E_FAIL = {  # 故障 10:00-12:00
    "device_id": "CNC-01",
    "source_id": "PLC-9",
    "version": 1,
    "start": "2026-10-01T10:00:00+08:00",
    "end": "2026-10-01T12:00:00+08:00",
    "reason": "failure",
}


@pytest.fixture()
def client():
    with TestClient(app) as c:  # 触发 lifespan：建库建表与迁移
        with engine.begin() as conn:
            for t in TABLES:
                conn.execute(text(f"TRUNCATE {t} CASCADE"))
        yield c


def _settled_shift(client, *events) -> int:
    assert client.post("/devices", json=DEVICE).status_code == 201
    r = client.post("/shifts", json=SHIFT)
    assert r.status_code == 201
    sid = r.json()["id"]
    for e in events:
        assert client.post("/events", json=e).status_code == 201
    assert client.post(f"/shifts/{sid}/settle").status_code == 200
    return sid


def _ruling(no: str, start: str, end: str, reason: str, note: str = "") -> dict:
    return {
        "ruling_no": no,
        "start": start,
        "end": end,
        "reason": reason,
        "note": note,
    }


R_MAINT_10_11 = _ruling(
    "J-001",
    "2026-10-01T10:00:00+08:00",
    "2026-10-01T11:00:00+08:00",
    "maintenance",
    "现场判定为保养",
)
R_FAIL_10_12 = _ruling(
    "J-002",
    "2026-10-01T10:00:00+08:00",
    "2026-10-01T12:00:00+08:00",
    "failure",
)


# ---------------------------------------------------------------- 前置条件与参数校验


def test_ruling_unknown_shift_and_unsettled(client):
    # 未知班次
    r = client.post("/shifts/99999/rulings", json=R_MAINT_10_11)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "SHIFT_NOT_FOUND"

    # 已建班但未结算
    client.post("/devices", json=DEVICE)
    sid = client.post("/shifts", json=SHIFT).json()["id"]
    r = client.post(f"/shifts/{sid}/rulings", json=R_MAINT_10_11)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "SHIFT_NOT_SETTLED"

    # 未结算班次上撤销单号：不存在即 RULING_NOT_FOUND（404）
    r = client.post(f"/shifts/{sid}/rulings/J-X/revoke")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "RULING_NOT_FOUND"


def test_ruling_out_of_range_and_invalid_params(client):
    sid = _settled_shift(client, E_FAIL)
    base = "2026-10-01T"

    # 起/止越出班次窗口（早于 08:00、晚于 16:00）→ 422 RULING_OUT_OF_RANGE
    for payload in (
        _ruling("J-X", "2026-10-01T07:59:00+08:00", "2026-10-01T09:00:00+08:00", "failure"),
        _ruling("J-X", "2026-10-01T15:00:00+08:00", "2026-10-01T16:01:00+08:00", "failure"),
    ):
        r = client.post(f"/shifts/{sid}/rulings", json=payload)
        assert r.status_code == 422, payload
        assert r.json()["detail"]["code"] == "RULING_OUT_OF_RANGE"

    # 恰好铺满班次窗口允许（半开区间端点可取班次边界）
    r = client.post(
        f"/shifts/{sid}/rulings",
        json=_ruling("J-FULL", f"{base}08:00:00+08:00", f"{base}16:00:00+08:00", "pending"),
    )
    assert r.status_code == 201

    # 非法原因 / 未对齐整分钟 / 缺时区 / start>=end / 单号为空或含非法字符
    # → 422 VALIDATION_ERROR
    bad = [
        {**R_MAINT_10_11, "ruling_no": "J-OTHER", "reason": "unknown"},
        {**R_MAINT_10_11, "ruling_no": "J-OTHER", "start": f"{base}10:00:30+08:00"},
        {**R_MAINT_10_11, "ruling_no": "J-OTHER", "end": "2026-10-01T10:00:00"},
        {**R_MAINT_10_11, "ruling_no": "J-OTHER", "end": "2026-10-01T10:00:00+08:00"},
        {**R_MAINT_10_11, "ruling_no": ""},
        {**R_MAINT_10_11, "ruling_no": "BAD/ID"},
    ]
    for payload in bad:
        r = client.post(f"/shifts/{sid}/rulings", json=payload)
        assert r.status_code == 422, payload
        assert r.json()["detail"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------- 提交 / 幂等 / 冲突


def test_submit_ruling_overrides_events_and_creates_revision(client):
    sid = _settled_shift(client, E_FAIL)  # 故障 120
    r = client.post(f"/shifts/{sid}/rulings", json=R_MAINT_10_11)
    assert r.status_code == 201
    ack = r.json()
    assert ack["ruling_no"] == "J-001"
    assert ack["status"] == "active"
    assert ack["idempotent"] is False
    assert ack["revision_created"] is True
    assert ack["revision"] == 2
    # 10:00-11:00 改为保养；故障只剩 11:00-12:00
    assert ack["result"]["totals"] == {"maintenance": 60, "failure": 60, "pending": 0}
    assert ack["result"]["delta"] == {"maintenance": 60, "failure": -60, "pending": 0}

    body = client.get(f"/shifts/{sid}/result").json()
    assert body["revision"] == 2
    assert body["trigger"] == "ruling_submit"
    assert body["rulings"] == [{"ruling_no": "J-001", "reason": "maintenance"}]
    # 故障事件在未被覆盖的 11:00-12:00 仍贡献，保留在依据版本中
    assert [u["source_id"] for u in body["event_versions"]] == ["PLC-9"]

    # 历史修订不可改写：修订 1 无裁定快照
    rev1 = client.get(f"/shifts/{sid}/result", params={"revision": 1}).json()
    assert rev1["rulings"] == []
    assert rev1["totals"] == {"maintenance": 0, "failure": 120, "pending": 0}


def test_ruling_idempotent_same_content(client):
    sid = _settled_shift(client, E_FAIL)
    assert client.post(f"/shifts/{sid}/rulings", json=R_MAINT_10_11).status_code == 201
    r = client.post(f"/shifts/{sid}/rulings", json=R_MAINT_10_11)
    assert r.status_code == 200
    ack = r.json()
    assert ack["idempotent"] is True
    assert ack["revision_created"] is False
    assert ack["revision"] is None
    assert ack["result"] is None
    # 无新修订
    revs = client.get(f"/shifts/{sid}/revisions").json()
    assert [x["revision"] for x in revs] == [1, 2]


def test_ruling_conflict_different_content(client):
    sid = _settled_shift(client, E_FAIL)
    client.post(f"/shifts/{sid}/rulings", json=R_MAINT_10_11)
    # 同单号、异内容（改区间/原因/说明均算）
    for payload in (
        {**R_MAINT_10_11, "end": "2026-10-01T11:30:00+08:00"},
        {**R_MAINT_10_11, "reason": "pending"},
        {**R_MAINT_10_11, "note": "改了说明"},
    ):
        r = client.post(f"/shifts/{sid}/rulings", json=payload)
        assert r.status_code == 409, payload
        assert r.json()["detail"]["code"] == "RULING_CONFLICT"
    # 冲突不产生修订
    assert client.get(f"/shifts/{sid}/result").json()["revision"] == 2


def test_ruling_conflict_takes_precedence_over_range(client):
    sid = _settled_shift(client, E_FAIL)
    client.post(f"/shifts/{sid}/rulings", json=R_MAINT_10_11)
    # 同单号异内容且新区间越界：仍按单号语义报 RULING_CONFLICT（409 优先于 422）
    payload = {
        **R_MAINT_10_11,
        "reason": "pending",
        "start": "2026-10-01T07:00:00+08:00",
        "end": "2026-10-01T09:00:00+08:00",
    }
    r = client.post(f"/shifts/{sid}/rulings", json=payload)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "RULING_CONFLICT"


def test_ruling_same_effect_no_revision(client):
    sid = _settled_shift(client, E_FAIL)  # 自动归因：10:00-12:00 故障 120
    # 裁定与自动归因分钟、原因完全一致 → 明确标示无修订
    r = client.post(f"/shifts/{sid}/rulings", json=R_FAIL_10_12)
    assert r.status_code == 201
    ack = r.json()
    assert ack["revision_created"] is False
    assert ack["revision"] is None
    assert ack["result"] is None
    body = client.get(f"/shifts/{sid}/result").json()
    assert body["revision"] == 1 and body["rulings"] == []
    # 裁定本身仍可列出
    rows = client.get(f"/shifts/{sid}/rulings").json()
    assert [x["ruling_no"] for x in rows] == ["J-002"]
    assert rows[0]["status"] == "active"


# ---------------------------------------------------------------- 撤销与回退


def test_revoke_flow_fallback_and_idempotency(client):
    sid = _settled_shift(client, E_FAIL)  # 故障 120
    # J-001: 10:00-11:00 保养（seq=1）
    client.post(f"/shifts/{sid}/rulings", json=R_MAINT_10_11)
    # J-002: 10:00-12:00 故障（seq=2）—— 重叠分钟以更新者为准，
    # 10:00-11:00 回到故障，结果恢复为故障 120（与修订 1 相同的分钟数）
    r = client.post(f"/shifts/{sid}/rulings", json=R_FAIL_10_12)
    assert r.status_code == 201
    assert r.json()["result"]["totals"] == {"maintenance": 0, "failure": 120, "pending": 0}
    assert r.json()["result"]["delta"] == {"maintenance": -60, "failure": 60, "pending": 0}
    assert r.json()["revision"] == 3

    # 撤销 J-002 → 10:00-11:00 回退到 J-001 的保养
    r = client.post(f"/shifts/{sid}/rulings/J-002/revoke")
    assert r.status_code == 200
    ack = r.json()
    assert ack["status"] == "revoked"
    assert ack["idempotent"] is False
    assert ack["revision"] == 4
    assert ack["result"]["totals"] == {"maintenance": 60, "failure": 60, "pending": 0}

    body = client.get(f"/shifts/{sid}/result").json()
    assert body["trigger"] == "ruling_revoke"
    assert body["rulings"] == [{"ruling_no": "J-001", "reason": "maintenance"}]

    # 重复撤销幂等：不产生新修订
    r = client.post(f"/shifts/{sid}/rulings/J-002/revoke")
    assert r.status_code == 200
    assert r.json()["idempotent"] is True
    assert r.json()["revision_created"] is False
    assert client.get(f"/shifts/{sid}/result").json()["revision"] == 4

    # 已撤销单号不可复用
    r = client.post(f"/shifts/{sid}/rulings", json=R_FAIL_10_12)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "RULING_REVOKED"

    # 撤销 J-001 → 无裁定，回到自动事件归因（故障 120）
    r = client.post(f"/shifts/{sid}/rulings/J-001/revoke")
    assert r.json()["result"]["totals"] == {"maintenance": 0, "failure": 120, "pending": 0}
    assert client.get(f"/shifts/{sid}/result").json()["rulings"] == []

    # 未知单号 / 未知班次
    r = client.post(f"/shifts/{sid}/rulings/NOPE/revoke")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "RULING_NOT_FOUND"
    r = client.post("/shifts/99999/rulings/J-001/revoke")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "SHIFT_NOT_FOUND"


def test_revoke_without_minute_change_creates_no_revision(client):
    sid = _settled_shift(client, E_FAIL)
    # 与自动归因完全一致的裁定（无修订），撤销后分钟数仍不变 → 无修订
    client.post(f"/shifts/{sid}/rulings", json=R_FAIL_10_12)
    r = client.post(f"/shifts/{sid}/rulings/J-002/revoke")
    assert r.status_code == 200
    assert r.json()["revision_created"] is False
    assert r.json()["revision"] is None
    assert client.get(f"/shifts/{sid}/result").json()["revision"] == 1


def test_list_rulings_includes_revoked(client):
    sid = _settled_shift(client, E_FAIL)
    client.post(f"/shifts/{sid}/rulings", json=R_MAINT_10_11)
    client.post(f"/shifts/{sid}/rulings/J-001/revoke")
    r = client.get(f"/shifts/{sid}/rulings")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["ruling_no"] == "J-001"
    assert rows[0]["seq"] == 1
    assert rows[0]["status"] == "revoked"
    assert rows[0]["revoked_at"] is not None
    assert rows[0]["note"] == "现场判定为保养"
    # 未知班次
    assert client.get("/shifts/99999/rulings").status_code == 404


# ---------------------------------------------------------------- 与事件更正叠加


def test_event_correction_respects_active_ruling(client):
    sid = _settled_shift(client, E_FAIL)  # 故障 10:00-12:00 → 120
    # 裁定 10:00-11:00 保养 → 保养 60 / 故障 60（修订 2）
    client.post(f"/shifts/{sid}/rulings", json=R_MAINT_10_11)

    # 故障事件更正：缩短为 11:00-11:30（只影响自动分钟；10-11 被裁定锁定）
    r = client.post(
        "/events",
        json={
            **E_FAIL,
            "version": 2,
            "start": "2026-10-01T11:00:00+08:00",
            "end": "2026-10-01T11:30:00+08:00",
        },
    )
    assert r.status_code == 201
    rec = r.json()["recomputed"]
    assert len(rec) == 1 and rec[0]["revision"] == 3
    assert rec[0]["totals"] == {"maintenance": 60, "failure": 30, "pending": 0}
    assert rec[0]["delta"] == {"maintenance": 0, "failure": -30, "pending": 0}
    # 修订 3 仍记录当前生效裁定
    body = client.get(f"/shifts/{sid}/result").json()
    assert body["trigger"] == "correction"
    assert body["rulings"] == [{"ruling_no": "J-001", "reason": "maintenance"}]

    # 更正只改变“已被裁定覆盖”的分钟 → 分钟数不变，不产生修订
    r = client.post(
        "/events",
        json={
            **E_FAIL,
            "version": 3,
            "start": "2026-10-01T10:30:00+08:00",  # 10:30-10:45 落在裁定区间内，
            "end": "2026-10-01T11:30:00+08:00",    # 11:00-11:30 仍是自动故障 30 分钟
        },
    )
    assert r.status_code == 201
    assert r.json()["recomputed"] == []
    assert client.get(f"/shifts/{sid}/result").json()["revision"] == 3


# ---------------------------------------------------------------- 变更流兼容


def test_ruling_revisions_enter_change_feed(client):
    sid = _settled_shift(client, E_FAIL)  # seq 1
    client.post(f"/shifts/{sid}/rulings", json=R_MAINT_10_11)  # seq 2
    client.post(f"/shifts/{sid}/rulings/J-001/revoke")  # seq 3

    body = client.get("/changes", params={"cursor": 0}).json()
    assert [c["seq"] for c in body["changes"]] == [1, 2, 3]
    assert body["next_cursor"] == 3
    c2, c3 = body["changes"][1], body["changes"][2]
    assert c2["trigger"] == "ruling_submit"
    assert c2["rulings"] == [{"ruling_no": "J-001", "reason": "maintenance"}]
    assert c2["delta"] == {"maintenance": 60, "failure": -60, "pending": 0}
    assert c3["trigger"] == "ruling_revoke"
    assert c3["rulings"] == []
    # 既有字段保持兼容
    for c in body["changes"]:
        assert set(["seq", "shift_id", "device_id", "shift_start", "shift_end",
                    "revision", "totals", "delta", "event_versions",
                    "trigger", "created_at"]).issubset(c)
    # 已返回条目不可变：重读一致
    again = client.get("/changes", params={"cursor": 0}).json()
    assert again == body


# ---------------------------------------------------------------- 并发不丢失更新


def test_concurrent_rulings_no_lost_update(client):
    sid = _settled_shift(client)  # 无事件：初始全 0
    n = 8
    reasons = ["maintenance", "failure", "pending"]
    barrier = threading.Barrier(n)
    errors: list[Exception] = []

    def worker(i: int):
        try:
            barrier.wait(timeout=10)
            # 每个工作者占用互不重叠的 30 分钟窗口（区间相接，半开不算重叠），
            # 每条裁定都必定实际贡献分钟并产生一个新修订
            start_min = 9 * 60 + i * 30  # 09:00 起，每 30 分钟一条
            end_min = start_min + 30
            sh, sm = divmod(start_min, 60)
            eh, em = divmod(end_min, 60)
            payload = _ruling(
                f"J-{i:03d}",
                f"2026-10-01T{sh:02d}:{sm:02d}:00+08:00",
                f"2026-10-01T{eh:02d}:{em:02d}:00+08:00",
                reasons[i % 3],
            )
            r = client.post(f"/shifts/{sid}/rulings", json=payload)
            assert r.status_code == 201, r.text
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    rows = client.get(f"/shifts/{sid}/rulings").json()
    assert len(rows) == n
    assert sorted(r["seq"] for r in rows) == list(range(1, n + 1))  # seq 无丢失

    # 每条裁定都改变分钟数，故各产生一个修订；连同结算共 n+1 条，序号连续无缺口
    body = client.get("/changes", params={"cursor": 0, "limit": 500}).json()
    assert [c["seq"] for c in body["changes"]] == list(range(1, n + 2))
    assert body["changes"][0]["trigger"] == "settle"
    assert all(c["trigger"] == "ruling_submit" for c in body["changes"][1:])
    latest = body["changes"][-1]
    # 最终结果：8 × 30 分钟，按各工作者原因汇总
    totals = {"maintenance": 0, "failure": 0, "pending": 0}
    for i in range(n):
        totals[reasons[i % 3]] += 30
    assert latest["totals"] == totals
    assert len(latest["rulings"]) == n
