"""业务逻辑：设备、班次、事件版本链、结算与迟到更正重算。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import attribution
from .attribution import EventView, RulingView
from .models import (
    ChangeCounter,
    Device,
    EventSource,
    EventVersion,
    ManualRuling,
    Shift,
    ShiftResult,
)
from .schemas import DeviceCreate, EventIn, RulingIn, ShiftCreate


class ApiError(HTTPException):
    """统一错误格式：{"detail": {"code": ..., "message": ...}}"""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(
            status_code=status_code, detail={"code": code, "message": message}
        )


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


def _content_hash(start: datetime, end: datetime, reason: str) -> str:
    raw = f"{start.isoformat()}|{end.isoformat()}|{reason}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _ruling_hash(
    start: datetime, end: datetime, reason: str, note: str
) -> str:
    raw = f"{start.isoformat()}|{end.isoformat()}|{reason}|{note}"
    return hashlib.sha256(raw.encode()).hexdigest()


def get_device_or_404(db: Session, device_id: str) -> Device:
    device = db.get(Device, device_id)
    if device is None:
        raise ApiError(404, "DEVICE_NOT_FOUND", f"unknown device: {device_id!r}")
    return device


# ---------------------------------------------------------------- 设备


def create_device(db: Session, payload: DeviceCreate) -> tuple[Device, bool]:
    existing = db.get(Device, payload.id)
    if existing is not None:
        return existing, False
    device = Device(id=payload.id, name=payload.name)
    db.add(device)
    try:
        db.commit()
    except IntegrityError:  # 并发重复创建
        db.rollback()
        return db.get(Device, payload.id), False
    return device, True


def list_devices(db: Session) -> list[Device]:
    return list(db.execute(select(Device).order_by(Device.id)).scalars().all())


# ---------------------------------------------------------------- 班次


def create_shift(db: Session, payload: ShiftCreate) -> Shift:
    get_device_or_404(db, payload.device_id)
    start, end = _utc(payload.start), _utc(payload.end)
    conflict = (
        db.execute(
            select(Shift).where(
                Shift.device_id == payload.device_id,
                Shift.start_ts < end,
                Shift.end_ts > start,
            )
        )
        .scalars()
        .first()
    )
    if conflict is not None:
        raise ApiError(
            409,
            "SHIFT_OVERLAP",
            f"shift overlaps existing shift id={conflict.id} "
            f"[{conflict.start_ts.isoformat()}, {conflict.end_ts.isoformat()}) "
            f"on device {payload.device_id!r}",
        )
    shift = Shift(device_id=payload.device_id, start_ts=start, end_ts=end)
    db.add(shift)
    try:
        db.commit()
    except IntegrityError:  # 排他约束兜底（并发创建）
        db.rollback()
        raise ApiError(
            409, "SHIFT_OVERLAP", "shift overlaps an existing shift on this device"
        )
    db.refresh(shift)
    return shift


def get_shift_or_404(db: Session, shift_id: int) -> Shift:
    shift = db.get(Shift, shift_id)
    if shift is None:
        raise ApiError(404, "SHIFT_NOT_FOUND", f"unknown shift id={shift_id}")
    return shift


# ---------------------------------------------------------------- 归因计算


def _allocate_seq(db: Session) -> int:
    """分配全局递增的变更序号。

    单例计数器行在写事务内 UPDATE ... RETURNING 递增，行锁持有至提交：
    序号顺序 == 提交顺序，同步游标因此不会越过尚未可见的较早变更；
    序号与修订在同一事务写入，同时提交或同时回滚。
    """
    db.execute(
        pg_insert(ChangeCounter).values(id=1, last_seq=0).on_conflict_do_nothing()
    )
    return db.execute(
        update(ChangeCounter)
        .where(ChangeCounter.id == 1)
        .values(last_seq=ChangeCounter.last_seq + 1)
        .returning(ChangeCounter.last_seq)
    ).scalar_one()


def _current_event_views(
    db: Session, device_id: str, start: datetime, end: datetime
) -> list[EventView]:
    """设备当前生效事件（每个来源取最新版本）中与窗口 [start, end) 重叠的部分。"""
    stmt = (
        select(EventVersion)
        .join(
            EventSource,
            and_(
                EventVersion.device_id == EventSource.device_id,
                EventVersion.source_id == EventSource.source_id,
                EventVersion.version == EventSource.last_version,
            ),
        )
        .where(
            EventVersion.device_id == device_id,
            EventVersion.start_ts < end,
            EventVersion.end_ts > start,
        )
    )
    rows = db.execute(stmt).scalars().all()
    return [
        EventView(
            source_id=r.source_id,
            version=r.version,
            reason=r.reason,
            start=r.start_ts,
            end=r.end_ts,
        )
        for r in rows
    ]


def _active_ruling_views(db: Session, shift_id: int) -> list[RulingView]:
    """班次当前未撤销的全部现场裁定（区间在落库时已裁剪约束在班次内）。

    按生效顺序 seq 升序返回；归因算法对重叠分钟只保留 seq 最大者，
    撤销（revoked=True）的裁定不参与，重叠分钟即回退到此前有效裁定。
    """
    rows = (
        db.execute(
            select(ManualRuling)
            .where(
                ManualRuling.shift_id == shift_id,
                ManualRuling.revoked.is_(False),
            )
            .order_by(ManualRuling.seq)
        )
        .scalars()
        .all()
    )
    return [
        RulingView(
            ruling_no=r.ruling_no,
            seq=r.seq,
            reason=r.reason,
            start=r.start_ts,
            end=r.end_ts,
            note=r.note,
        )
        for r in rows
    ]


def _latest_revision(db: Session, shift_id: int) -> ShiftResult | None:
    return (
        db.execute(
            select(ShiftResult)
            .where(ShiftResult.shift_id == shift_id)
            .order_by(ShiftResult.revision.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def _compute(db: Session, shift: Shift) -> tuple[dict, list[dict], list[dict]]:
    """按“当前最新事件版本 + 当前未撤销裁定”重算班次归因。

    返回 (totals, 实际贡献的事件版本, 实际贡献的裁定)。
    裁定覆盖分钟优先于自动事件；无裁定时与历史行为完全一致。
    """
    events = _current_event_views(db, shift.device_id, shift.start_ts, shift.end_ts)
    rulings = _active_ruling_views(db, shift.id)
    return attribution.attribute(
        shift.start_ts, shift.end_ts, events, rulings
    )


def _same_totals(totals: dict, rev: ShiftResult) -> bool:
    return (
        totals["maintenance"] == rev.maintenance_minutes
        and totals["failure"] == rev.failure_minutes
        and totals["pending"] == rev.pending_minutes
    )


def _new_revision(
    db: Session,
    shift: Shift,
    totals: dict,
    used: list[dict],
    trigger: str,
    base: ShiftResult | None,
    used_rulings: list[dict] | None = None,
) -> ShiftResult:
    if base is None:
        revision, bm, bf, bp = 1, 0, 0, 0
    else:
        revision = base.revision + 1
        bm, bf, bp = (
            base.maintenance_minutes,
            base.failure_minutes,
            base.pending_minutes,
        )
    rev = ShiftResult(
        shift_id=shift.id,
        revision=revision,
        seq=_allocate_seq(db),
        maintenance_minutes=totals["maintenance"],
        failure_minutes=totals["failure"],
        pending_minutes=totals["pending"],
        d_maintenance=totals["maintenance"] - bm,
        d_failure=totals["failure"] - bf,
        d_pending=totals["pending"] - bp,
        event_versions=used,
        rulings=used_rulings or [],
        trigger=trigger,
    )
    db.add(rev)
    return rev


# ---------------------------------------------------------------- 结算


def settle_shift(db: Session, shift_id: int) -> tuple[Shift, ShiftResult, bool]:
    """结算班次：计算并写入修订 1；已结算则幂等返回最新修订。"""
    shift = db.execute(
        select(Shift).where(Shift.id == shift_id).with_for_update()
    ).scalar_one_or_none()
    if shift is None:
        raise ApiError(404, "SHIFT_NOT_FOUND", f"unknown shift id={shift_id}")
    latest = _latest_revision(db, shift_id)
    if latest is not None:
        return shift, latest, False
    totals, used, used_rulings = _compute(db, shift)
    rev = _new_revision(
        db,
        shift,
        totals,
        used,
        trigger="settle",
        base=None,
        used_rulings=used_rulings,
    )
    db.commit()
    db.refresh(rev)
    return shift, rev, True


# ---------------------------------------------------------------- 事件上报


def ingest_event(
    db: Session, payload: EventIn
) -> tuple[EventVersion, bool, list[tuple[Shift, ShiftResult]]]:
    """上报事件版本。

    返回 (事件版本, 是否幂等命中, 因更正而重算的 [(班次, 新修订)])。
    """
    get_device_or_404(db, payload.device_id)
    start, end = _utc(payload.start), _utc(payload.end)
    digest = _content_hash(start, end, payload.reason)

    # 确保版本链游标存在并加行锁，串行化同一来源的并发上报
    db.execute(
        pg_insert(EventSource)
        .values(
            device_id=payload.device_id, source_id=payload.source_id, last_version=0
        )
        .on_conflict_do_nothing()
    )
    src = db.execute(
        select(EventSource)
        .where(
            EventSource.device_id == payload.device_id,
            EventSource.source_id == payload.source_id,
        )
        .with_for_update()
    ).scalar_one()

    if payload.version <= src.last_version:
        existing = db.execute(
            select(EventVersion).where(
                EventVersion.device_id == payload.device_id,
                EventVersion.source_id == payload.source_id,
                EventVersion.version == payload.version,
            )
        ).scalar_one()
        if existing.content_hash == digest:
            db.commit()
            return existing, True, []  # 同来源同版本同内容：幂等返回
        raise ApiError(
            409,
            "VERSION_CONFLICT",
            f"version {payload.version} of source {payload.source_id!r} "
            "already exists with different content",
        )
    if payload.version > src.last_version + 1:
        raise ApiError(
            409,
            "VERSION_GAP",
            f"source {payload.source_id!r} expects version "
            f"{src.last_version + 1}, got {payload.version}",
        )

    prev = None
    if src.last_version > 0:
        prev = db.execute(
            select(EventVersion).where(
                EventVersion.device_id == payload.device_id,
                EventVersion.source_id == payload.source_id,
                EventVersion.version == src.last_version,
            )
        ).scalar_one()

    ev = EventVersion(
        device_id=payload.device_id,
        source_id=payload.source_id,
        version=payload.version,
        start_ts=start,
        end_ts=end,
        reason=payload.reason,
        content_hash=digest,
    )
    db.add(ev)
    src.last_version = payload.version
    src.last_content_hash = digest
    db.flush()

    recomputed = _recompute_affected_shifts(db, payload.device_id, prev, ev)
    db.commit()
    return ev, False, recomputed


def _recompute_affected_shifts(
    db: Session,
    device_id: str,
    prev: EventVersion | None,
    new: EventVersion,
) -> list[tuple[Shift, ShiftResult]]:
    """迟到更正：按事件时间（旧版本与新版本区间的并集）找出受影响的已结算班次，
    用最新事件版本重算；结果有变化时追加新修订并记录差额。
    """
    conds = [and_(Shift.start_ts < new.end_ts, Shift.end_ts > new.start_ts)]
    if prev is not None:
        conds.append(and_(Shift.start_ts < prev.end_ts, Shift.end_ts > prev.start_ts))
    shifts = (
        db.execute(
            select(Shift)
            .where(Shift.device_id == device_id, or_(*conds))
            .order_by(Shift.id)  # 固定加锁顺序，降低并发更正相互死锁的概率
            .with_for_update()
        )
        .scalars()
        .all()
    )

    recomputed: list[tuple[Shift, ShiftResult]] = []
    for shift in shifts:
        latest = _latest_revision(db, shift.id)
        if latest is None:
            continue  # 尚未结算：结算时自然会使用最新事件版本
        # 同期事件更正与现场裁定叠加重算：裁定覆盖分钟不受事件版本变化影响
        totals, used, used_rulings = _compute(db, shift)
        if _same_totals(totals, latest):
            continue  # 各原因分钟数未变化，不产生空修订
        rev = _new_revision(
            db,
            shift,
            totals,
            used,
            trigger="correction",
            base=latest,
            used_rulings=used_rulings,
        )
        db.flush()
        recomputed.append((shift, rev))
    return recomputed


# ---------------------------------------------------------------- 现场人工裁定


def _get_ruling(
    db: Session, shift_id: int, ruling_no: str, for_update: bool = False
) -> ManualRuling | None:
    stmt = select(ManualRuling).where(
        ManualRuling.shift_id == shift_id,
        ManualRuling.ruling_no == ruling_no,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return db.execute(stmt).scalar_one_or_none()


def _next_ruling_seq(db: Session, shift_id: int) -> int:
    """班次内的裁定生效顺序：max(seq)+1。调用方须已持有班次行锁，
    故同班次的提交/撤销被串行化，seq 分配与并发事件更正都不会丢失更新。
    """
    return (
        db.execute(
            select(func.coalesce(func.max(ManualRuling.seq), 0)).where(
                ManualRuling.shift_id == shift_id
            )
        ).scalar_one()
    ) + 1


def submit_ruling(
    db: Session, shift_id: int, payload: RulingIn
) -> tuple[ManualRuling, ShiftResult | None, bool]:
    """提交现场人工裁定（仅限已结算班次）。

    返回 (裁定, 新修订或 None, 是否幂等命中)。
    - 相同单号 + 相同内容重试：幂等返回当前状态，不产生新数据/新修订；
    - 相同单号 + 不同内容：409 RULING_CONFLICT；
    - 单号已撤销：409 RULING_REVOKED（单号不复用）；
    - 区间越出班次窗口：422 RULING_OUT_OF_RANGE。
    各原因分钟数变化时才追加修订并进入全局变更流。
    """
    shift = db.execute(
        select(Shift).where(Shift.id == shift_id).with_for_update()
    ).scalar_one_or_none()
    if shift is None:
        raise ApiError(404, "SHIFT_NOT_FOUND", f"unknown shift id={shift_id}")
    latest = _latest_revision(db, shift_id)
    if latest is None:
        raise ApiError(
            409,
            "SHIFT_NOT_SETTLED",
            f"shift id={shift_id} has not been settled; settle it before ruling",
        )

    start, end = _utc(payload.start), _utc(payload.end)
    digest = _ruling_hash(start, end, payload.reason, payload.note)

    # 先识别同单号重试/冲突（即使新载荷越界或非法，也明确按单号语义报错）
    existing = _get_ruling(db, shift_id, payload.ruling_no, for_update=True)
    if existing is not None:
        if existing.revoked:
            raise ApiError(
                409,
                "RULING_REVOKED",
                f"ruling_no {payload.ruling_no!r} was revoked and cannot be reused",
            )
        if existing.content_hash != digest:
            raise ApiError(
                409,
                "RULING_CONFLICT",
                f"ruling_no {payload.ruling_no!r} already exists with different content",
            )
        db.commit()  # 尽早释放班次行锁
        return existing, None, True  # 相同单号及内容重试：幂等

    if start < shift.start_ts or end > shift.end_ts:
        raise ApiError(
            422,
            "RULING_OUT_OF_RANGE",
            f"ruling [{start.isoformat()}, {end.isoformat()}) is not within "
            f"shift [{shift.start_ts.isoformat()}, {shift.end_ts.isoformat()})",
        )

    ruling = ManualRuling(
        shift_id=shift_id,
        ruling_no=payload.ruling_no,
        seq=_next_ruling_seq(db, shift_id),
        start_ts=start,
        end_ts=end,
        reason=payload.reason,
        note=payload.note,
        content_hash=digest,
        revoked=False,
    )
    db.add(ruling)
    db.flush()

    rev: ShiftResult | None = None
    totals, used, used_rulings = _compute(db, shift)
    if not _same_totals(totals, latest):
        rev = _new_revision(
            db,
            shift,
            totals,
            used,
            trigger="ruling_submit",
            base=latest,
            used_rulings=used_rulings,
        )
        db.flush()
    db.commit()
    db.refresh(ruling)
    if rev is not None:
        db.refresh(rev)
    return ruling, rev, False


def revoke_ruling(
    db: Session, shift_id: int, ruling_no: str
) -> tuple[ManualRuling, ShiftResult | None, bool]:
    """按单号撤销现场人工裁定。

    返回 (裁定, 新修订或 None, 是否幂等命中)。
    - 重复撤销幂等：已撤销则原样返回，不产生新数据/新修订；
    - 未知班次 404 SHIFT_NOT_FOUND；未知单号 404 RULING_NOT_FOUND；
    - 撤销使重叠分钟回退到此前有效裁定/自动事件，各原因分钟数变化时才追加修订。
    """
    shift = db.execute(
        select(Shift).where(Shift.id == shift_id).with_for_update()
    ).scalar_one_or_none()
    if shift is None:
        raise ApiError(404, "SHIFT_NOT_FOUND", f"unknown shift id={shift_id}")

    ruling = _get_ruling(db, shift_id, ruling_no, for_update=True)
    if ruling is None:
        raise ApiError(
            404,
            "RULING_NOT_FOUND",
            f"unknown ruling_no {ruling_no!r} on shift id={shift_id}",
        )
    latest = _latest_revision(db, shift_id)  # 裁定存在即已结算；作为差额基准
    if ruling.revoked:
        db.commit()  # 尽早释放班次行锁
        return ruling, None, True  # 重复撤销：幂等返回

    ruling.revoked = True
    ruling.revoked_at = datetime.now(timezone.utc)
    db.flush()

    rev: ShiftResult | None = None
    totals, used, used_rulings = _compute(db, shift)
    if not _same_totals(totals, latest):
        rev = _new_revision(
            db,
            shift,
            totals,
            used,
            trigger="ruling_revoke",
            base=latest,
            used_rulings=used_rulings,
        )
        db.flush()
    db.commit()
    db.refresh(ruling)
    if rev is not None:
        db.refresh(rev)
    return ruling, rev, False


def list_rulings(db: Session, shift_id: int) -> tuple[Shift, list[ManualRuling]]:
    shift = get_shift_or_404(db, shift_id)
    rows = (
        db.execute(
            select(ManualRuling)
            .where(ManualRuling.shift_id == shift_id)
            .order_by(ManualRuling.seq)
        )
        .scalars()
        .all()
    )
    return shift, list(rows)


# ---------------------------------------------------------------- 结果查询


def get_result(
    db: Session, shift_id: int, revision: int | None
) -> tuple[Shift, ShiftResult, int]:
    """返回 (班次, 指定/最新修订, 最新修订号)。"""
    shift = get_shift_or_404(db, shift_id)
    latest = _latest_revision(db, shift_id)
    if latest is None:
        raise ApiError(
            404, "RESULT_NOT_FOUND", f"shift id={shift_id} has not been settled"
        )
    if revision is None:
        return shift, latest, latest.revision
    rev = db.execute(
        select(ShiftResult).where(
            ShiftResult.shift_id == shift_id, ShiftResult.revision == revision
        )
    ).scalar_one_or_none()
    if rev is None:
        raise ApiError(
            404, "REVISION_NOT_FOUND", f"shift id={shift_id} has no revision {revision}"
        )
    return shift, rev, latest.revision


def list_revisions(db: Session, shift_id: int) -> tuple[Shift, list[ShiftResult]]:
    shift = get_shift_or_404(db, shift_id)
    revs = (
        db.execute(
            select(ShiftResult)
            .where(ShiftResult.shift_id == shift_id)
            .order_by(ShiftResult.revision)
        )
        .scalars()
        .all()
    )
    return shift, list(revs)


# ---------------------------------------------------------------- 变更同步

#: GET /changes 允许的页大小上限
MAX_PAGE_SIZE = 500


def list_changes(
    db: Session, cursor: int, limit: int
) -> tuple[list[tuple[ShiftResult, Shift]], int]:
    """按全局递增游标分页读取结算/更正变更，返回 ([(修订, 班次)], 下一游标)。

    - 返回 seq > cursor 的变更，按 seq 升序，最多 limit 条；
    - 空页保持原游标（客户端已追平，可原地轮询）；
    - 游标超过当前最大序号视为非法（客户端进度与服务端不一致），
      明确拒绝且不影响任何同步进度。
    """
    if cursor > 0:
        max_seq = db.execute(select(func.max(ShiftResult.seq))).scalar() or 0
        if cursor > max_seq:
            raise ApiError(
                422,
                "INVALID_CURSOR",
                f"cursor {cursor} is beyond the latest change seq {max_seq}; "
                "restart sync from a smaller cursor (0 replays from the beginning)",
            )
    rows = db.execute(
        select(ShiftResult, Shift)
        .join(Shift, Shift.id == ShiftResult.shift_id)
        .where(ShiftResult.seq > cursor)
        .order_by(ShiftResult.seq)
        .limit(limit)
    ).all()
    page = [(rev, shift) for rev, shift in rows]
    next_cursor = page[-1][0].seq if page else cursor
    return page, next_cursor
