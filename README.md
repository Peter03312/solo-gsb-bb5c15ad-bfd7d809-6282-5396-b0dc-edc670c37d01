# 设备停机归因服务（Downtime Attribution）

按**班次**对设备停机时间进行归因的 HTTP 服务：输入设备、班次（带时区、整分钟）与停机事件
（来源编号 + 连续递增版本 + 起止 + 原因），输出各班次的**保养 / 故障 / 待判**分钟数及所依据
的事件版本；支持事件幂等上报、版本链校验、迟到更正重算与结算修订查询；已结算班次还可登记
**现场人工裁定**（唯一单号 + 整分钟半开区间 + 原因/说明，可按单号撤销），每分钟以生效顺序
最新的未撤销裁定覆盖自动事件归因；并通过**全局递增游标**的变更流（`GET /changes`）供下游
报表持续同步每次结算、更正与裁定修订。

技术栈：Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL 16 · Docker Compose

## 快速启动

```bash
docker compose up --build
```

一条命令完成依赖安装、PostgreSQL 建库建表（含扩展与约束）与服务启动。

| 地址 | 说明 |
| --- | --- |
| `http://localhost:8000` | API 根地址 |
| `http://localhost:8000/docs` | Swagger 交互文档（可在线试调用） |
| `http://localhost:8000/health` | 健康检查 |
| `localhost:5432` | PostgreSQL（用户/密码 `postgres`，库 `downtime`） |

冒烟验证（覆盖下文全部流程）：

```bash
./scripts/smoke.sh                 # 或指定地址 ./scripts/smoke.sh http://localhost:8000
```

运行测试：

```bash
docker compose exec app pytest            # 算法单测 + API 端到端测试
```

## 配置

通过环境变量配置（`docker-compose.yml` 已为 `app` 服务注入）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://postgres:postgres@localhost:5432/downtime` | SQLAlchemy 连接串 |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `postgres` / `postgres` / `downtime` | Compose 中数据库初始化参数 |

端口映射：`8000`（API）、`5432`（数据库），可在 `docker-compose.yml` 中调整。

## 业务规则

- **半开区间**：班次与事件均为 `[start, end)`；端点相接不算重叠、不漏分钟。
- **时间戳**：必须带时区且精确到整分钟（秒须为 `00`），否则 `422`。
- **班次**：同一设备的班次区间不可重叠（应用校验 + 数据库排他约束双保险），冲突 `409`。
- **事件版本链**：每个 `(设备, 来源)` 的版本从 1 开始连续递增。
  - 同来源 + 同版本 + 同内容 → 幂等返回（`200`，`idempotent=true`），不产生新数据；
  - 同版本异内容 → `409 VERSION_CONFLICT`；跳号 → `409 VERSION_GAP`；
  - 新版本即对旧版本的更正，当前生效事件 = 每个来源的最新版本。
- **归因**：窗口内每分钟只归因一次，重叠时 **保养 > 故障 > 待判**；结果含各原因分钟数
  与实际贡献分钟的事件版本列表。
- **结算与修订**：`POST /shifts/{id}/settle` 结算生成修订 1 并永久保留；迟到更正按事件时间
  （新旧版本区间的并集）重算受影响的已结算班次，结果变化时追加新修订并记录与上一修订的
  差额（`delta`）；查询可用 `?revision=N` 指定历史修订。
- **现场人工裁定**（仅对已结算班次）：`POST /shifts/{id}/rulings` 提交，载荷含班次内唯一
  `ruling_no`、整分钟半开区间（必须落在班次窗口内）、原因（保养/故障/待判）与说明。
  - 每分钟以**生效顺序最新**的未撤销裁定覆盖自动事件归因；裁定区间重叠时后提交者（seq 更大）
    生效，撤销后者后重叠分钟自动回退至此前仍有效的裁定，全部撤销则回到最新事件版本归因；
  - 相同单号 + 相同内容重试幂等；相同单号 + 不同内容 `409 RULING_CONFLICT`；
    已撤销单号不可复用 `409 RULING_REVOKED`；未结算 `409 SHIFT_NOT_SETTLED`；
  - `POST /shifts/{id}/rulings/{no}/revoke` 按单号撤销（软删除，重复撤销幂等）；
    未知班次 `404 SHIFT_NOT_FOUND`，未知单号 `404 RULING_NOT_FOUND`；
  - 提交/撤销**仅在各原因分钟数变化时**产生班次修订（`trigger` 为 `ruling_submit` /
    `ruling_revoke`）与差额；无变化时响应明确标示 `revision_created=false`、`revision=null`。
- **变更同步**：每次结算、每次导致结果变化的更正以及每次导致结果变化的裁定提交/撤销产生一条
  变更记录（`seq` 全局递增），`GET /changes?cursor=&limit=` 按游标分页读取；修订与变更记录
  同一行存储、同事务提交/回滚，序号顺序即提交顺序，已返回条目不可变；空页保持原游标，
  非法游标/超限页大小返回 `422`。
- **错误**：未知设备 `404 DEVICE_NOT_FOUND`，非法区间 `422`，未结算结果 `404 RESULT_NOT_FOUND`，
  未知班次/修订/单号 `404`，裁定越界 `422 RULING_OUT_OF_RANGE`。
  错误体统一为 `{"detail": {"code": "...", "message": "..."}}`。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/devices` | 注册设备（同 id 重复提交幂等返回 200） |
| `GET` | `/devices` | 设备列表 |
| `POST` | `/shifts` | 创建班次（重叠 409 / 未知设备 404 / 非法区间 422） |
| `GET` | `/shifts/{id}` | 班次详情 |
| `POST` | `/events` | 上报事件版本（新建 201 / 幂等 200 / 冲突·跳号 409） |
| `POST` | `/shifts/{id}/settle` | 结算班次（重复调用幂等返回最新修订） |
| `POST` | `/shifts/{id}/rulings` | 提交现场人工裁定（201 / 幂等 200 / 冲突 409 / 越界 422） |
| `POST` | `/shifts/{id}/rulings/{no}/revoke` | 按单号撤销裁定（重复撤销幂等；未知单号 404） |
| `GET` | `/shifts/{id}/rulings` | 裁定列表（含已撤销，按生效顺序） |
| `GET` | `/shifts/{id}/result?revision=N` | 查询归因结果，缺省最新修订 |
| `GET` | `/shifts/{id}/revisions` | 全部修订及差额 |
| `GET` | `/changes?cursor=&limit=` | 变更同步：按全局递增游标分页读取结算/更正变更 |
| `GET` | `/health` | 健康检查 |

原因枚举：`maintenance`（保养）、`failure`（故障）、`pending`（待判）。

错误体统一为 `{"detail": {"code": "...", "message": "..."}}`：

| HTTP | code | 含义 |
| --- | --- | --- |
| 404 | `DEVICE_NOT_FOUND` | 未知设备 |
| 404 | `SHIFT_NOT_FOUND` | 未知班次 |
| 404 | `RESULT_NOT_FOUND` | 班次尚未结算 |
| 404 | `REVISION_NOT_FOUND` | 指定修订不存在 |
| 404 | `RULING_NOT_FOUND` | 未知裁定单号（撤销时） |
| 409 | `SHIFT_OVERLAP` | 同设备班次区间重叠 |
| 409 | `VERSION_CONFLICT` | 同来源同版本但内容不同 |
| 409 | `VERSION_GAP` | 版本跳号（含首版本不为 1） |
| 409 | `SHIFT_NOT_SETTLED` | 班次未结算，不能登记裁定 |
| 409 | `RULING_CONFLICT` | 相同裁定单号但内容不同 |
| 409 | `RULING_REVOKED` | 单号已撤销，不可复用 |
| 422 | `RULING_OUT_OF_RANGE` | 裁定区间越出班次窗口 |
| 422 | `VALIDATION_ERROR` | 非法参数：区间无效、缺时区、未对齐整分钟、原因非法、单号非法、游标/页大小越界等 |
| 422 | `INVALID_CURSOR` | 同步游标超过当前最大变更序号（客户端进度异常，需从较小游标重新同步） |

## 调用示例

以下完整走一遍主流程（假设服务运行于 `http://localhost:8000`）。

**1. 注册设备**

```bash
curl -X POST http://localhost:8000/devices \
  -H 'Content-Type: application/json' \
  -d '{"id": "CNC-01", "name": "加工中心1号"}'
# 201 {"id":"CNC-01","name":"加工中心1号"}
```

**2. 创建班次**（早班 08:00–16:00，+08:00）

```bash
curl -X POST http://localhost:8000/shifts \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","start":"2026-10-01T08:00:00+08:00","end":"2026-10-01T16:00:00+08:00"}'
# 201 {"id":1,"device_id":"CNC-01","start":"2026-10-01T00:00:00Z","end":"2026-10-01T08:00:00Z"}
```

重叠班次返回 `409 {"detail":{"code":"SHIFT_OVERLAP", ...}}`；端点相接（如 16:00 起）允许。

**3. 上报停机事件**：保养 10:00–11:00

```bash
curl -X POST http://localhost:8000/events \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"MES-1","version":1,
       "start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00",
       "reason":"maintenance"}'
# 201 {"device_id":"CNC-01","source_id":"MES-1","version":1,"idempotent":false,"recomputed":[]}
```

**4. 幂等与版本校验**

```bash
# 原样重发 → 200，idempotent=true
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"MES-1","version":1,
       "start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","reason":"maintenance"}'

# 同版本异内容 → 409 VERSION_CONFLICT
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"MES-1","version":1,
       "start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:30:00+08:00","reason":"maintenance"}'

# 跳号（应为 2 却发 3）→ 409 VERSION_GAP
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"MES-1","version":3,
       "start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","reason":"maintenance"}'
```

**5. 再上报一条故障** 10:30–12:00（另一来源）

```bash
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"PLC-9","version":1,
       "start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T12:00:00+08:00","reason":"failure"}'
```

**6. 结算班次**

```bash
curl -X POST http://localhost:8000/shifts/1/settle
```

```json
{
  "settled": true,
  "result": {
    "shift_id": 1, "revision": 1, "latest_revision": 1,
    "totals": {"maintenance": 60, "failure": 60, "pending": 0},
    "delta":  {"maintenance": 60, "failure": 60, "pending": 0},
    "event_versions": [
      {"source_id": "MES-1", "version": 1, "reason": "maintenance"},
      {"source_id": "PLC-9", "version": 1, "reason": "failure"}
    ],
    "trigger": "settle", "..."
  }
}
```

> 10:30–11:00 与保养重叠，按优先级归保养；故障只计 11:00–12:00 的 60 分钟。

**7. 迟到更正**：故障实际只到 11:30，上报 `PLC-9` 的版本 2

```bash
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"PLC-9","version":2,
       "start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T11:30:00+08:00","reason":"failure"}'
```

```json
{
  "version": 2, "idempotent": false,
  "recomputed": [{
    "shift_id": 1, "revision": 2,
    "totals": {"maintenance": 60, "failure": 30, "pending": 0},
    "delta":  {"maintenance": 0,  "failure": -30, "pending": 0}
  }]
}
```

**8. 查询结果与修订**

```bash
curl http://localhost:8000/shifts/1/result                # 最新修订（revision=2）
curl 'http://localhost:8000/shifts/1/result?revision=1'   # 指定修订：结算时的原结果（failure=60）
curl http://localhost:8000/shifts/1/revisions             # 全部修订及每次差额
```

**8b. 现场人工裁定**：把 10:00–11:00 现场改判为“保养”

> 此时最新事件版本为：保养 10:00–11:00（MES-1 v1）、故障 10:30–11:30（PLC-9 v2）。
> 下列第一条裁定与自动归因分钟完全相同，因此**不产生修订**（演示“无修订明确标示”）；
> 第二条改判才改变分钟数。

```bash
curl -X POST http://localhost:8000/shifts/1/rulings \
  -H 'Content-Type: application/json' \
  -d '{"ruling_no":"J-20261001-001",
       "start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00",
       "reason":"maintenance","note":"现场确认保养工单已执行"}'
```

```json
{"ruling_no":"J-20261001-001","status":"active","idempotent":false,
 "revision_created":false,"revision":null,"result":null}
```

```bash
# 再来一条更新生效的裁定：09:30–10:30 现场判为故障（与 J-...-001 在 10:00–10:30 重叠）
curl -X POST http://localhost:8000/shifts/1/rulings \
  -H 'Content-Type: application/json' \
  -d '{"ruling_no":"J-20261001-002",
       "start":"2026-10-01T09:30:00+08:00","end":"2026-10-01T10:30:00+08:00",
       "reason":"failure","note":"班前半小时实为故障停机"}'
```

```json
{"ruling_no":"J-20261001-002","status":"active","idempotent":false,
 "revision_created":true,"revision":3,
 "result":{"revision":3,
   "totals":{"maintenance":30,"failure":90,"pending":0},
   "delta":{"maintenance":-30,"failure":30,"pending":0}}}
```

- 10:00–10:30 被生效顺序更新的 `J-...-002` 覆盖（故障）；`J-...-001` 只剩 10:30–11:00 生效
  （保养 30）；故障 = 裁定 09:30–10:30 的 60 分钟 + 自动事件 11:00–11:30 的 30 分钟 = 90。
- 响应固定返回**单号、状态、本次修订号**；无修订时 `revision_created=false` 且
  `revision/result` 均为 `null`。

```bash
# 相同单号及内容重试 → 幂等（200，idempotent=true，不产生新数据）
curl -X POST http://localhost:8000/shifts/1/rulings -H 'Content-Type: application/json' \
  -d '{"ruling_no":"J-20261001-002",
       "start":"2026-10-01T09:30:00+08:00","end":"2026-10-01T10:30:00+08:00",
       "reason":"failure","note":"班前半小时实为故障停机"}'

# 相同单号异内容 → 409 RULING_CONFLICT
curl -X POST http://localhost:8000/shifts/1/rulings -H 'Content-Type: application/json' \
  -d '{"ruling_no":"J-20261001-002",
       "start":"2026-10-01T09:30:00+08:00","end":"2026-10-01T10:45:00+08:00",
       "reason":"failure"}'

# 撤销 J-...-002：10:00–10:30 回退给 J-...-001 的保养（重叠裁定回退）
curl -X POST http://localhost:8000/shifts/1/rulings/J-20261001-002/revoke
# 重复撤销幂等：idempotent=true、revision_created=false
curl -X POST http://localhost:8000/shifts/1/rulings/J-20261001-002/revoke

# 列出全部裁定（含已撤销，按生效顺序 seq）
curl http://localhost:8000/shifts/1/rulings

# 撤销 J-...-001：无裁定分钟全部回到“当前最新事件版本”自动归因
curl -X POST http://localhost:8000/shifts/1/rulings/J-20261001-001/revoke
```

裁定区间必须落在班次窗口内、整分钟、半开、带时区：

```bash
# 越界（07:00 早于班次 08:00）→ 422 RULING_OUT_OF_RANGE
curl -X POST http://localhost:8000/shifts/1/rulings -H 'Content-Type: application/json' \
  -d '{"ruling_no":"J-BAD",
       "start":"2026-10-01T07:00:00+08:00","end":"2026-10-01T09:00:00+08:00",
       "reason":"failure"}'
# 未结算班次 → 409 SHIFT_NOT_SETTLED；未知单号撤销 → 404 RULING_NOT_FOUND
```

**9. 错误示例**

```bash
# 非法区间（start ≥ end、秒非 00、缺时区）→ 422
curl -X POST http://localhost:8000/shifts -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","start":"2026-10-02T16:00:00+08:00","end":"2026-10-02T08:00:00+08:00"}'

# 未知设备 → 404 DEVICE_NOT_FOUND
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"device_id":"NOPE","source_id":"X","version":1,
       "start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","reason":"failure"}'
```

## 变更同步（下游报表订阅）

`GET /changes` 把每次**结算**、每次**导致结果变化的更正**与每次**导致结果变化的裁定
提交/撤销**暴露为按全局递增序号 `seq` 排序的变更流，供下游报表持续同步。每条变更包含：
班次（id、设备、起止）、修订号、各原因分钟数（`totals`）、与上一修订的差额（`delta`）、
触发类型（`trigger`：`settle` / `correction` / `ruling_submit` / `ruling_revoke`）、
所依据的事件版本、实际贡献分钟的现场裁定（`rulings`）与产生时间。

**游标语义**

- 请求参数 `cursor` 表示"已消费的最大 `seq`"，返回 `seq > cursor` 的变更，按 `seq` 升序；
  `cursor=0`（或缺省）表示从头回放全部既有修订。
- 每页返回 `next_cursor`（本页最后一条的 `seq`），作为下一次请求的 `cursor` 即可翻页。
- **空页保持原游标**：已追平时返回 `{"changes": [], "next_cursor": <原 cursor>}`，
  客户端可用同一游标轮询等待新变更。
- 页大小 `limit` 缺省 100，上限 500。

**一致性保证**

- 变更记录与对应修订是同一行（`shift_results.seq`），同一事务提交或回滚，不会半截可见；
- 序号由单例计数器在写事务内加锁分配，**序号顺序 == 提交顺序**：并发结算/更正时，
  游标不会越过尚未可见的较早变更；
- 条目只追加不修改，重读已返回的内容不变；
- 幂等重复上报/结算、重复撤销、结果未变化的更正与裁定提交/撤销均**不产生**变更记录。

**参数约束**：`cursor ≥ 0` 且不得超过当前最大序号；`1 ≤ limit ≤ 500`。
非法游标或超限页大小返回 `422`（只读请求，不影响任何同步进度）。

**调用示例**（接上面主流程，此时已有结算 seq=1、更正 seq=2 两条变更）

```bash
# 1) 从头回放：第一页（每页 1 条）
curl 'http://localhost:8000/changes?cursor=0&limit=1'
```

```json
{
  "changes": [{
    "seq": 1, "shift_id": 1, "device_id": "CNC-01",
    "shift_start": "2026-10-01T00:00:00Z", "shift_end": "2026-10-01T08:00:00Z",
    "revision": 1,
    "totals": {"maintenance": 60, "failure": 60, "pending": 0},
    "delta":  {"maintenance": 60, "failure": 60, "pending": 0},
    "event_versions": [
      {"source_id": "MES-1", "version": 1, "reason": "maintenance"},
      {"source_id": "PLC-9", "version": 1, "reason": "failure"}
    ],
    "rulings": [],
    "trigger": "settle", "created_at": "..."
  }],
  "next_cursor": 1
}
```

> `rulings` 为本修订实际贡献分钟的现场裁定 `[{"ruling_no","reason"}]`；
> 结算/更正等无裁定的修订与全部历史修订为 `[]`。裁定修订示例：
> `trigger="ruling_submit"`，`rulings=[{"ruling_no":"J-20261001-001","reason":"maintenance"}]`。

```bash
# 2) 用上一页的 next_cursor 翻页
curl 'http://localhost:8000/changes?cursor=1&limit=100'
# → changes[0] 为 seq=2 的更正（trigger="correction"，delta.failure=-30），next_cursor=2

# 3) 已追平：空页保持原游标，可轮询等待
curl 'http://localhost:8000/changes?cursor=2'
# → {"changes": [], "next_cursor": 2}

# 4) 非法参数 → 422，同步进度不受影响
curl 'http://localhost:8000/changes?cursor=-1'     # VALIDATION_ERROR
curl 'http://localhost:8000/changes?limit=1000'    # VALIDATION_ERROR（上限 500）
curl 'http://localhost:8000/changes?cursor=999'    # INVALID_CURSOR（超过当前最大序号）
```

## 设计要点

- **归因算法**（`app/attribution.py`，纯标准库可独立测试）：按原因收集事件区间并裁剪到班次
  窗口；保养区间合并后全量生效，故障区间扣除保养覆盖，待判区间再扣除保养与故障覆盖。
  人工裁定作为更高一层：裁定区间按生效顺序（seq）处理，每条只保留“未被更晚裁定覆盖”的分钟；
  自动事件区间再整体扣除裁定覆盖。区间运算保证每分钟只归因一次；`used_versions` /
  `used_rulings` 只保留实际贡献了分钟的事件版本与裁定。
- **版本链与幂等**：`event_sources` 表保存每个 `(设备, 来源)` 的 `last_version` 与内容哈希，
  上报时 `INSERT ... ON CONFLICT DO NOTHING` + `SELECT ... FOR UPDATE` 串行化校验；
  事件内容哈希 = `sha256(start_utc|end_utc|reason)`。
- **现场裁定与幂等**：`manual_rulings` 以 `(shift_id, ruling_no)` 唯一保存裁定（单号由调用方
  提供，作为业务幂等键），另用班次内单调递增的 `seq` 表示生效顺序，撤销为软删除
  （单号保留不可复用）；内容哈希 = `sha256(start_utc|end_utc|reason|note)`。提交/撤销均在
  持有班次行锁的单事务内完成：同单号同内容幂等返回、异内容 `RULING_CONFLICT`。
  撤销即从生效集合移除，重叠分钟按 seq 自然回退到此前裁定，无需补偿数据。
- **迟到更正与裁定叠加**：新版本落库后按"旧版本区间 ∪ 新版本区间"找出受影响的已结算班次，
  用全部来源最新版本 **+ 当前未撤销裁定** 重算；裁定覆盖的分钟不随事件版本变化。
  裁定提交/撤销与更正一样，仅当分钟数变化时追加新修订（避免空修订），差额 = 新 − 旧。
- **并发与一致性**：班次不重叠由 PostgreSQL 排他约束（`btree_gist` + `tstzrange '[)'`）兜底；
  结算、更正重算与裁定提交/撤销都对班次行加锁（裁定事务内还锁定目标裁定行），事件版本链对
  来源行加锁，全流程单事务提交 —— 并发裁定不会丢失 seq、修订或更新。
- **不可变历史**：`shift_results` 只追加不修改，`(shift_id, revision)` 唯一；每修订快照
  `event_versions` 与 `rulings`，历史修订与已返回变更不可改写。
- **变更订阅**：变更流直接读取修订表 —— `shift_results.seq` 即全局变更序号，修订与变更
  天然同事务提交/回滚；序号由单例计数器行（`change_counter`）在写事务内
  `UPDATE ... RETURNING` 递增分配，行锁持有至提交，故序号顺序 == 提交顺序，同步游标不会越过
  尚未可见的较早变更；条目不可变，重读一致。既有库启动时自动迁移：为 `shift_results`
  回填 `seq`（按原自增 id 顺序）、以最大序号初始化计数器，并补 `rulings JSONB NOT NULL
  DEFAULT '[]'`（历史修订裁定快照为空数组）。

## 项目结构

```
app/
  main.py         # FastAPI 路由
  services.py     # 业务逻辑：版本链、幂等、结算、更正重算、现场裁定
  attribution.py  # 归因算法（事件 + 裁定分层，半开区间运算，纯标准库）
  models.py       # SQLAlchemy 模型
  schemas.py      # Pydantic 请求/响应与校验
  db.py           # 引擎、会话、建库建表（含排他约束与既有库迁移）
  config.py       # 环境变量配置
tests/
  test_attribution.py  # 算法单测（无外部依赖，可 python3 直接跑）
  test_api.py          # API 端到端测试（需 PostgreSQL）
  test_changes.py      # 变更同步（游标分页/回放/并发）端到端测试（需 PostgreSQL）
  test_rulings.py      # 现场裁定（提交/幂等/冲突/撤销回退/叠加更正/并发）端到端测试
scripts/smoke.sh       # 冒烟脚本
Dockerfile / docker-compose.yml / requirements.txt
```

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
export DATABASE_URL='postgresql+psycopg://postgres:postgres@localhost:5432/downtime'
uvicorn app.main:app --reload --port 8000
pytest
```
