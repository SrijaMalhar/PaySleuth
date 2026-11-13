#!/usr/bin/env bash
# A month at a tuition centre, against a running server and an empty database.
#
#   docker compose up -d db
#   uvicorn paysleuth.main:app &
#   tools/demo.sh
#
# On a fresh database the members get ids 1 to 5, so the sample statement's
# reference code (PS1004) lines up with the students created here.
set -euo pipefail

API=${API:-http://localhost:8000}
cd "$(dirname "$0")/.."

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
json() { python3 -m json.tool; }
pick() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {'d': d}))" "$1"; }

say "Create the institution (the API key is shown once)"
KEY=$(python3 -m paysleuth.cli create-institution "Bright Minds Tuition" --late-fee 100 --grace-days 5 \
        2>/dev/null | sed -n 's/^api_key=//p')
echo "api key: ${KEY:0:7}..."
call() { curl -sS -H "X-API-Key: $KEY" -H "Content-Type: application/json" "$@"; }

say "Add five students (Rohan and Kabir are siblings sharing a parent's UPI ID)"
add() { call -X POST "$API/members" -d "$1" | pick "d['ref_code'] + '  ' + d['name'] + '  Rs ' + d['monthly_fee']"; }
add '{"name":"Diya Sharma","guardian_name":"Sunita Sharma","monthly_fee":"2000","billing_day":5,"start_period":"2026-06","upi_ids":["sunita.sharma@oksbi"]}'
add '{"name":"Rohan Verma","guardian_name":"Rajesh Kumar Verma","monthly_fee":"2000","billing_day":5,"start_period":"2026-06","upi_ids":["rk.verma@okhdfcbank"]}'
add '{"name":"Kabir Verma","guardian_name":"Rajesh Kumar Verma","monthly_fee":"1500","billing_day":5,"start_period":"2026-06","upi_ids":["rk.verma@okhdfcbank"]}'
add '{"name":"Arjun Nair","guardian_name":"Anil Nair","monthly_fee":"1800","billing_day":5,"start_period":"2026-06"}'
add '{"name":"Meera Iyer","guardian_name":"Lakshmi Iyer","monthly_fee":"2500","billing_day":5,"start_period":"2026-06"}'

say "Bill June"
call -X POST "$API/billing/run?as_of=2026-06-01" | json

say "Import the bank statement"
curl -sS -H "X-API-Key: $KEY" -F "file=@sample_data/june_2026_statement.csv" "$API/bank-credits/import" \
  | pick "{k: d[k] for k in ('recorded','matched','needs_review','duplicates','skipped_debits')}"

say "Review queue: what could not be matched safely, with suggestions"
REVIEW=$(call "$API/review")
echo "$REVIEW" | python3 -c "
import json, sys
for c in json.load(sys.stdin):
    print(f\"credit {c['id']}: Rs {c['amount']} from {c['payer_name']} ({c['payer_upi_id']}), note: {c['note']}\")
    for s in c['suggestions']:
        print(f\"    {s['score']:>3}  {s['ref_code']} {s['name']}: {'; '.join(s['reasons'])}\")
    if not c['suggestions']:
        print('    no suggestion')"

say "Assign Lakshmi Iyer's transfer to the top suggestion (her UPI ID is remembered)"
CREDIT=$(echo "$REVIEW" | pick "[c['id'] for c in d if c['suggestions']][0]")
MEMBER=$(echo "$REVIEW" | pick "[c['suggestions'][0]['member_id'] for c in d if c['suggestions']][0]")
call -X POST "$API/review/$CREDIT/assign" -d "{\"member_id\": $MEMBER}" \
  | pick "d['credit']['utr'] + ' -> ' + d['applied'][0]['ref_code'] + ', matched by ' + d['credit']['match_rule']"

say "July: the same payer now matches without anyone looking"
call -X POST "$API/billing/run?as_of=2026-07-01" >/dev/null
call -X POST "$API/bank-credits" -d '{"narration":"UPI/CR/618612345611/LAKSHMI IYER/UBIN/lakshmi.iyer@oksbi/july","amount":"2500","received_at":"2026-07-04T09:15:00"}' \
  | pick "d['credit']['utr'] + ' -> ' + d['applied'][0]['ref_code'] + ', matched by ' + d['credit']['match_rule']"

say "June collection report"
call "$API/reports/collection?period=2026-06" | json

say "Deliver receipts"
call -X POST "$API/outbox/relay" | json

say "Meera's statement"
call "$API/members/5/statement" | json
