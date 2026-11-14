"""Remove one safety mechanism at a time and check that the test written for it goes red.

    python tools/mutation_check.py           # all mutations
    python tools/mutation_check.py -k skip   # only mutations whose name contains "skip"

Each mutation edits a source file in place, runs the one test that should
notice, and restores the file in a finally block. Exit status is non-zero if
any mutation survives, so CI fails if a lock stops being covered by a test.
"""

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Mutation:
    name: str
    file: str
    edits: tuple            # ((old_text, new_text), ...), each old_text must occur exactly once
    test: str


MUTATIONS = [
    Mutation("rate limiter hands out a token it does not have", "paysleuth/ratelimit.py",
             (("if bucket.tokens >= 1.0:", "if bucket.tokens >= 0.0:"),),
             "tests/test_ratelimit.py::test_a_caller_may_burst_then_is_refused"),
    Mutation("rate limiter takes no lock", "paysleuth/ratelimit.py",
             (("with self._lock:\n            bucket = self._buckets.get(key)",
               "if True:\n            bucket = self._buckets.get(key)"),),
             "tests/test_ratelimit.py::test_concurrent_takes_hand_out_exactly_the_burst"),
    Mutation("every caller shares one bucket", "paysleuth/ratelimit.py",
             (("bucket = self._buckets.get(key)", "bucket = self._buckets.get(\"all\")"),
              ("bucket = self._buckets[key] = _Bucket", "bucket = self._buckets[\"all\"] = _Bucket")),
             "tests/test_ratelimit.py::test_callers_do_not_share_a_bucket"),
    Mutation("page size is not clamped to the maximum", "paysleuth/paging.py",
             (("size = DEFAULT if limit is None else max(1, min(int(limit), MAX))",
               "size = DEFAULT if limit is None else max(1, int(limit))"),),
             "tests/test_paging.py::test_a_page_size_is_clamped"),
    Mutation("a missing page size means the whole table", "paysleuth/paging.py",
             (("size = DEFAULT if limit is None else", "size = MAX if limit is None else"),),
             "tests/test_paging.py::test_a_page_size_is_clamped"),
    Mutation("FOR UPDATE removed from lock_members", "paysleuth/db.py",
             (("ORDER BY id FOR UPDATE\",", "ORDER BY id\","),),
             "tests/test_concurrency.py::test_20_payments_for_one_member_at_once_never_pay_a_fee_twice"),
    Mutation("members locked in request order, not id order", "paysleuth/db.py",
             (("wanted = sorted(set(member_ids))", "wanted = list(dict.fromkeys(member_ids))"),
              ("rows = conn.execute(\"SELECT id FROM members WHERE id = ANY(%s) ORDER BY id FOR UPDATE\",\n"
               "                        (wanted,)).fetchall()",
               "rows = [r for m in wanted for r in conn.execute(\n"
               "        \"SELECT id FROM members WHERE id = %s FOR UPDATE\", (m,)).fetchall()]")),
             "tests/test_concurrency.py::test_opposite_reassignments_at_once_never_deadlock"),
    Mutation("bank credit row lock removed", "paysleuth/payments.py",
             (("\"SELECT * FROM bank_credits WHERE id = %s FOR UPDATE\"", "\"SELECT * FROM bank_credits WHERE id = %s\""),),
             "tests/test_concurrency.py::test_two_people_resolving_the_same_credit_differently"),
    Mutation("FOR UPDATE SKIP LOCKED removed from the outbox claim", "paysleuth/outbox.py",
             (("                      FOR UPDATE SKIP LOCKED)", "                      )"),),
             "tests/test_messages.py::test_many_workers_deliver_every_message_exactly_once"),
    Mutation("per-member outbox ordering removed", "paysleuth/outbox.py",
             (("                      AND NOT EXISTS (SELECT 1 FROM outbox e\n"
               "                                       WHERE e.member_id = o.member_id AND e.kind = o.kind\n"
               "                                         AND e.status IN ('pending', 'sending') AND e.id < o.id)\n",
               ""),),
             "tests/test_messages.py::test_a_members_receipts_arrive_in_payment_order_even_while_one_is_retried"),
    Mutation("UNIQUE (institution_id, utr) removed", "paysleuth/schema.sql",
             (("ELSE true END),\n    UNIQUE (institution_id, utr)\n);", "ELSE true END)\n);"),),
             "tests/test_concurrency.py::test_the_same_transfer_arriving_20_times_at_once_counts_once"),
    Mutation("optimistic version check removed", "paysleuth/members.py",
             (("f\"WHERE id = %(id)s AND version = %(expected)s RETURNING version\")",
               "f\"WHERE id = %(id)s RETURNING version\")"),),
             "tests/test_concurrency.py::test_ten_people_saving_the_same_member_at_once"),
    Mutation("late-fee re-check removed", "paysleuth/charges.py",
             (("            if not _unpaid_monthly_fee(conn, candidate[\"member_id\"], candidate[\"period\"]):\n"
               "                continue                              # paid while we waited for the lock\n", ""),),
             "tests/test_concurrency.py::test_late_fee_is_rechecked_after_waiting_for_the_lock"),
    Mutation("row-level security policy set to USING (true)", "paysleuth/schema.sql",
             (("                USING (institution_id = NULLIF(current_setting('app.institution_id', true), '')::bigint)",
               "                USING (true)"),),
             "tests/test_suggestions_and_tenancy.py::"
             "test_another_institution_cannot_see_members_even_without_a_where_clause"),
    Mutation("migration advisory lock removed", "paysleuth/db.py",
             (("        conn.execute(\"SELECT pg_advisory_xact_lock(%s)\", (MIGRATION_LOCK,))\n", ""),),
             "tests/test_concurrency.py::test_processes_starting_together_can_all_migrate"),
    Mutation("union-find replaced by \"first candidate\"", "paysleuth/importer.py",
             (("            for member_id in members:\n                sets.union(node, (\"member\", member_id))",
               "            if members:\n                sets.union(node, (\"member\", members[0]))"),),
             "tests/test_import.py::test_parallel_import_gives_exactly_the_books_of_a_serial_import"),
    Mutation("append-only triggers removed", "paysleuth/schema.sql",
             (("            EXECUTE format('CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON %I '\n"
               "                           'FOR EACH ROW EXECUTE FUNCTION forbid_change()', t);", "            NULL;"),),
             "tests/test_payments.py::test_the_database_refuses_to_change_or_delete_money_rows"),
    Mutation("self-approval allowed", "paysleuth/approvals.py",
             (("        if actor.staff_id == request[\"requested_by\"]:\n"
               "            raise Forbidden(\"the person who asked for a reassignment cannot be the one who approves it\")\n",
               ""),),
             "tests/test_staff.py::test_nobody_can_approve_their_own_request"),
    Mutation("approval row lock removed", "paysleuth/approvals.py",
             (("\"SELECT * FROM approval_requests WHERE id = %s FOR UPDATE\"",
               "\"SELECT * FROM approval_requests WHERE id = %s\""),),
             "tests/test_staff.py::test_two_approvers_at_once_apply_it_once"),
    Mutation("webhook timestamp not covered by the signature", "paysleuth/webhooks.py",
             (("    return str(int(timestamp)).encode() + b\".\" + body", "    return body"),),
             "tests/test_webhooks.py::test_a_captured_signature_cannot_be_reused_with_a_fresh_timestamp"),
    Mutation("webhook timestamp window removed", "paysleuth/webhooks.py",
             (("    if abs(moment - timestamp) > tolerance:", "    if False:"),),
             "tests/test_webhooks.py::test_an_old_message_sent_again_is_refused"),
    Mutation("statement lines not checked against the books", "paysleuth/importer.py",
             (("            statement.update(check_credits_landed(conn, inst, statement[\"id\"],\n"
               "                                                  list(parsed.credits) + rejected, unreadable))",
               "            pass"),),
             "tests/test_close.py::test_a_credit_line_that_cannot_be_imported_is_reported_as_missing"),
    Mutation("generic parser keeps a masked UPI ID", "paysleuth/statements.py",
             (("                    \"payer_upi_id\": full_upi_id(m[\"upi\"]), \"note\": _clean(m[\"note\"])}",
               "                    \"payer_upi_id\": m[\"upi\"].lower(), \"note\": _clean(m[\"note\"])}"),),
             "tests/test_close.py::test_a_masked_upi_id_in_an_unrecognised_layout_no_longer_loses_the_line"),
    Mutation("reversals skipped in the parallel import", "paysleuth/importer.py",
             (("    reversed_count = _apply_reversals(pool, inst, reversals, problems)",
               "    reversed_count = 0"),),
             "tests/test_import.py::test_parallel_import_gives_exactly_the_books_of_a_serial_import"),
    Mutation("statement check skipped when its import did not finish", "paysleuth/importer.py",
             (("    if not statement[\"already_checked\"]:", "    if not statement[\"already_imported\"]:"),),
             "tests/test_close.py::test_a_statement_whose_import_died_is_never_read_as_clean"),
    Mutation("a line carrying money but no readable date is dropped", "paysleuth/statements.py",
             (("            if not (carries_money and recognised):", "            if True:"),),
             "tests/test_close.py::test_a_credit_whose_date_cannot_be_read_is_still_reported"),
    Mutation("printed UTR not normalised before the landed check", "paysleuth/close.py",
             (("            utr = normalise_utr(credit.utr)", "            utr = credit.utr"),),
             "tests/test_close.py::test_a_utr_printed_differently_is_not_reported_as_missing"),
]


def run_test(test_id: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", test_id],
                          cwd=ROOT, capture_output=True, text=True)


def apply(mutation: Mutation, original: str) -> str:
    text = original
    for old, new in mutation.edits:
        count = text.count(old)
        if count != 1:
            raise SystemExit(f"mutation {mutation.name!r}: expected the text once in {mutation.file}, "
                             f"found it {count} times; update tools/mutation_check.py")
        text = text.replace(old, new)
    return text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-k", default="", help="only mutations whose name contains this")
    args = parser.parse_args()
    chosen = [m for m in MUTATIONS if args.k.lower() in m.name.lower()]

    # The tests must pass before anything is mutated, or "caught" means nothing.
    for test in sorted({m.test for m in chosen}):
        baseline = run_test(test)
        if baseline.returncode != 0:
            print(f"baseline failed: {test}\n{baseline.stdout[-2000:]}")
            return 2

    survived = 0
    print(f"{'mutation':58} {'result':9} {'seconds':>7}")
    for mutation in chosen:
        path = ROOT / mutation.file
        original = path.read_text()
        started = time.perf_counter()
        try:
            path.write_text(apply(mutation, original))
            result = run_test(mutation.test)
        finally:
            path.write_text(original)
        caught = result.returncode != 0
        survived += not caught
        print(f"{mutation.name:58} {'caught' if caught else 'SURVIVED':9} {time.perf_counter() - started:7.1f}")
    print(f"\n{len(chosen) - survived}/{len(chosen)} mutations caught")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
