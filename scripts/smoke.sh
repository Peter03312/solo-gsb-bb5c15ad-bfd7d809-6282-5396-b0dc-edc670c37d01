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

echo "-- 已追平：空页保持原游标 --"
req GET "/changes?cursor=2"

echo "-- 非法游标 / 超限页大小，预期 422 --"
req GET "/changes?cursor=-1"
req GET "/changes?cursor=999"
req GET "/changes?limit=1000"

echo "-- 非法区间 / 未知设备，预期 422 / 404 --"
req POST /shifts '{"device_id":"CNC-01","start":"2026-10-02T16:00:00+08:00","end":"2026-10-02T08:00:00+08:00"}'
req POST /events '{"device_id":"NOPE","source_id":"X","version":1,"start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","reason":"failure"}'
