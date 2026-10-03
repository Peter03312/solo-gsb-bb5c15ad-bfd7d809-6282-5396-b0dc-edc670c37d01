#!/usr/bin/env bash
# 冒烟脚本：走完 设备→班次→事件→幂等/冲突→结算→迟到更正→修订查询 全流程。
# 用法：./scripts/smoke.sh [BASE_URL]   （默认 http://localhost:8000）
set -euo pipefail

BASE="${1:-http://localhost:8000}"

req() {  # req <method> <path> [json]
  local method="$1" path="$2" body="${3:-}"
  echo "==> $method $path"
  if [ -n "$body" ]; then
    curl -sS -X "$method" "$BASE$path" -H 'Content-Type: application/json' -d "$body"
  else
    curl -sS -X "$method" "$BASE$path"
  fi
  echo; echo
}

req GET /health

req POST /devices '{"id":"CNC-01","name":"加工中心1号"}'

req POST /shifts '{"device_id":"CNC-01","start":"2026-10-01T08:00:00+08:00","end":"2026-10-01T16:00:00+08:00"}'

echo "-- 以下请求预期 409（班次重叠）--"
req POST /shifts '{"device_id":"CNC-01","start":"2026-10-01T15:00:00+08:00","end":"2026-10-01T20:00:00+08:00"}'

req POST /events '{"device_id":"CNC-01","source_id":"MES-1","version":1,"start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","reason":"maintenance"}'

echo "-- 重复提交同一事件版本，预期 200 且 idempotent=true --"
req POST /events '{"device_id":"CNC-01","source_id":"MES-1","version":1,"start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","reason":"maintenance"}'

echo "-- 同版本异内容，预期 409 VERSION_CONFLICT --"
req POST /events '{"device_id":"CNC-01","source_id":"MES-1","version":1,"start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:30:00+08:00","reason":"maintenance"}'

echo "-- 跳号，预期 409 VERSION_GAP --"
req POST /events '{"device_id":"CNC-01","source_id":"MES-1","version":3,"start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","reason":"maintenance"}'

req POST /events '{"device_id":"CNC-01","source_id":"PLC-9","version":1,"start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T12:00:00+08:00","reason":"failure"}'

req POST /shifts/1/settle

echo "-- 迟到更正：故障缩短为 10:30-11:30，预期 recomputed 中 revision=2、failure 差额 -30 --"
req POST /events '{"device_id":"CNC-01","source_id":"PLC-9","version":2,"start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T11:30:00+08:00","reason":"failure"}'

req GET "/shifts/1/result"
req GET "/shifts/1/result?revision=1"
req GET "/shifts/1/revisions"

echo "-- 变更同步：从游标 0 回放（每页 1 条），再用 next_cursor 翻页 --"
req GET "/changes?cursor=0&limit=1"
req GET "/changes?cursor=1&limit=10"

echo "-- 现场人工裁定：10:00-11:00 改判保养（与更正后事件分钟相同 → 无修订，revision 为 null）--"
req POST /shifts/1/rulings '{"ruling_no":"J-20261001-001","start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","reason":"maintenance","note":"现场判定保养"}'

echo "-- 相同单号及内容重试，预期 200 且 idempotent=true、无修订（revision 为 null）--"
req POST /shifts/1/rulings '{"ruling_no":"J-20261001-001","start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","reason":"maintenance","note":"现场判定保养"}'

echo "-- 相同单号异内容，预期 409 RULING_CONFLICT --"
req POST /shifts/1/rulings '{"ruling_no":"J-20261001-001","start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:30:00+08:00","reason":"maintenance","note":"现场判定保养"}'

echo "-- 第二条裁定 10:00-12:00 故障（生效顺序更新，覆盖 J-...-001 的重叠分钟）--"
req POST /shifts/1/rulings '{"ruling_no":"J-20261001-002","start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T12:00:00+08:00","reason":"failure","note":"复核仍为故障"}'

echo "-- 撤销 J-...-002：重叠分钟回退至 J-...-001 的保养，预期 revision=4 --"
req POST /shifts/1/rulings/J-20261001-002/revoke

echo "-- 重复撤销，预期幂等：idempotent=true、无修订 --"
req POST /shifts/1/rulings/J-20261001-002/revoke

echo "-- 列出全部裁定（含已撤销，按生效顺序）--"
req GET /shifts/1/rulings

echo "-- 裁定错误：越界区间 422 RULING_OUT_OF_RANGE / 未知单号 404 RULING_NOT_FOUND --"
req POST /shifts/1/rulings '{"ruling_no":"J-BAD","start":"2026-10-01T07:00:00+08:00","end":"2026-10-01T09:00:00+08:00","reason":"failure"}'
req POST /shifts/1/rulings/NOPE/revoke

req GET "/shifts/1/result"
req GET "/changes?cursor=1&limit=20"

echo "-- 已追平：空页保持原游标（当前最大 seq=4；首个保养裁定与事件分钟相同，无修订）--"
req GET "/changes?cursor=4"

echo "-- 非法游标 / 超限页大小，预期 422 --"
req GET "/changes?cursor=-1"
req GET "/changes?cursor=999"
req GET "/changes?limit=1000"

echo "-- 非法区间 / 未知设备，预期 422 / 404 --"
req POST /shifts '{"device_id":"CNC-01","start":"2026-10-02T16:00:00+08:00","end":"2026-10-02T08:00:00+08:00"}'
req POST /events '{"device_id":"NOPE","source_id":"X","version":1,"start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","reason":"failure"}'
