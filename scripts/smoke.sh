#!/usr/bin/env bash
# End-to-end demo of the usage billing settlement API.
# Usage: ./scripts/smoke.sh [BASE_URL]   (default http://localhost:8000)
set -euo pipefail

BASE="${1:-http://localhost:8000}"
TENANT="acme-$(date +%s)-$$"   # unique per run, so the demo is re-runnable
JQ="jq"
command -v jq >/dev/null 2>&1 || JQ="cat"

echo "== 1. create price plan with graduated tiers =="
PLAN=$(curl -sS -X POST "$BASE/plans" -H 'Content-Type: application/json' -d '{
  "name": "standard", "currency": "USD"}')
echo "$PLAN" | $JQ
PLAN_ID=$(echo "$PLAN" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

echo "== 2. price version v1: 0-100 @0.10, 100+ @0.05 per api_call =="
curl -sS -X POST "$BASE/plans/$PLAN_ID/versions" -H 'Content-Type: application/json' -d '{
  "version": 1,
  "effective_from": "2026-01-01T00:00:00Z",
  "effective_to": null,
  "tiers": [
    {"metric": "api_calls", "up_to": "100", "unit_price": "0.10"},
    {"metric": "api_calls", "up_to": null,  "unit_price": "0.05"}
  ]}' | $JQ

echo "== 3. create tenant: $TENANT =="
curl -sS -X POST "$BASE/tenants" -H 'Content-Type: application/json' -d "{
  \"code\": \"$TENANT\", \"name\": \"Acme Corp\", \"plan_id\": $PLAN_ID}" | $JQ

echo "== 4. ingest usage (idempotent) =="
curl -sS -X POST "$BASE/usage" -H 'Content-Type: application/json' -d "{
  \"tenant_code\": \"$TENANT\", \"source\": \"meter\", \"event_id\": \"evt-1\",
  \"occurred_at\": \"2026-01-10T10:00:00Z\", \"quantity\": \"120\",
  \"metric\": \"api_calls\", \"dimensions\": {\"region\": \"eu\"}}" | $JQ
echo "-- replay same event (deduplicated) --"
curl -sS -X POST "$BASE/usage" -H 'Content-Type: application/json' -d "{
  \"tenant_code\": \"$TENANT\", \"source\": \"meter\", \"event_id\": \"evt-1\",
  \"occurred_at\": \"2026-01-10T10:00:00Z\", \"quantity\": \"120\",
  \"metric\": \"api_calls\", \"dimensions\": {\"region\": \"eu\"}}" | $JQ
echo "-- same key, different content => 409 conflict --"
curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$BASE/usage" -H 'Content-Type: application/json' -d "{
  \"tenant_code\": \"$TENANT\", \"source\": \"meter\", \"event_id\": \"evt-1\",
  \"occurred_at\": \"2026-01-10T10:00:00Z\", \"quantity\": \"999\",
  \"metric\": \"api_calls\", \"dimensions\": {}}"

curl -sS -X POST "$BASE/usage" -H 'Content-Type: application/json' -d "{
  \"tenant_code\": \"$TENANT\", \"source\": \"meter\", \"event_id\": \"evt-2\",
  \"occurred_at\": \"2026-01-11T10:00:00Z\", \"quantity\": \"30\",
  \"metric\": \"api_calls\", \"dimensions\": {}}" | $JQ

echo "== 5. open January period, dry-run preview, then close =="
PERIOD=$(curl -sS -X POST "$BASE/periods" -H 'Content-Type: application/json' -d "{
  \"tenant_code\": \"$TENANT\",
  \"period_start\": \"2026-01-01T00:00:00Z\",
  \"period_end\":   \"2026-02-01T00:00:00Z\"}")
P1=$(echo "$PERIOD" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -sS -X POST "$BASE/periods/$P1/preview" | $JQ
BILL1=$(curl -sS -X POST "$BASE/periods/$P1/close")
echo "$BILL1" | $JQ
B1=$(echo "$BILL1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

echo "== 6. per-event pricing trace of the bill =="
curl -sS "$BASE/bills/$B1/trace" | $JQ

echo "== 7. late correction (after cutoff) + February period =="
curl -sS -X POST "$BASE/usage/meter/evt-1/corrections" -H 'Content-Type: application/json' -d "{
  \"tenant_code\": \"$TENANT\",
  \"occurred_at\": \"2026-01-10T10:00:00Z\", \"quantity\": \"150\",
  \"metric\": \"api_calls\", \"dimensions\": {\"region\": \"eu\"},
  \"correction_event_id\": \"evt-1-corr-1\"}" | $JQ
P2=$(curl -sS -X POST "$BASE/periods" -H 'Content-Type: application/json' -d "{
  \"tenant_code\": \"$TENANT\",
  \"period_start\": \"2026-02-01T00:00:00Z\",
  \"period_end\":   \"2026-03-01T00:00:00Z\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
BILL2=$(curl -sS -X POST "$BASE/periods/$P2/close")
echo "$BILL2" | $JQ
B2=$(echo "$BILL2" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

echo "== 8. both bills recompute identically from frozen inputs =="
curl -sS -X POST "$BASE/bills/$B1/verify" | $JQ
curl -sS -X POST "$BASE/bills/$B2/verify" | $JQ

echo "== 9. closed period cannot be reopened =="
curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$BASE/periods/$P1/close"
echo "demo done."
