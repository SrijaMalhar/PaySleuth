-- PaySleuth schema. Safe to run any number of times: every statement is
-- IF NOT EXISTS or checks before it changes anything.
--
-- Money is bigint paise everywhere. Billing months are 'YYYY-MM' text.

CREATE TABLE IF NOT EXISTS institutions (
    id              bigserial   PRIMARY KEY,
    name            text        NOT NULL,
    api_key_hash    text        NOT NULL UNIQUE,   -- sha256 of the key; the key itself is never stored
    late_fee        bigint      NOT NULL DEFAULT 0 CHECK (late_fee >= 0),
    grace_days      integer     NOT NULL DEFAULT 0 CHECK (grace_days BETWEEN 0 AND 60),
    quiet_start     smallint    NOT NULL DEFAULT 21 CHECK (quiet_start BETWEEN 0 AND 23),
    quiet_end       smallint    NOT NULL DEFAULT 8  CHECK (quiet_end BETWEEN 0 AND 23),
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS members (
    id              bigserial   PRIMARY KEY,
    institution_id  bigint      NOT NULL REFERENCES institutions(id),
    ref_code        text        NOT NULL UNIQUE,   -- PS1001: what payers write in the UPI note
    name            text        NOT NULL,
    guardian_name   text,
    phone           text,
    billing_day     smallint    NOT NULL CHECK (billing_day BETWEEN 1 AND 31),
    start_period    text        NOT NULL CHECK (start_period ~ '^\d{4}-(0[1-9]|1[0-2])$'),
    billed_through  text        CHECK (billed_through ~ '^\d{4}-(0[1-9]|1[0-2])$'),
    concession_pct  smallint    NOT NULL DEFAULT 0 CHECK (concession_pct BETWEEN 0 AND 100),
    active          boolean     NOT NULL DEFAULT true,
    version         integer     NOT NULL DEFAULT 1,   -- optimistic locking, sent as the ETag
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS members_institution ON members (institution_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id              bigserial   PRIMARY KEY,
    institution_id  bigint      NOT NULL REFERENCES institutions(id),
    action          text        NOT NULL,
    entity          text        NOT NULL,
    entity_id       bigint,
    details         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_log_recent ON audit_log (institution_id, id DESC);

-- A fee holds from its effective month until the next change.
CREATE TABLE IF NOT EXISTS member_fees (
    institution_id   bigint      NOT NULL REFERENCES institutions(id),
    member_id        bigint      NOT NULL REFERENCES members(id),
    effective_period text        NOT NULL CHECK (effective_period ~ '^\d{4}-(0[1-9]|1[0-2])$'),
    monthly_fee      bigint      NOT NULL CHECK (monthly_fee > 0),
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (member_id, effective_period)
);

-- One UPI ID can belong to several siblings, so the key includes the member.
CREATE TABLE IF NOT EXISTS member_upi_ids (
    institution_id  bigint      NOT NULL REFERENCES institutions(id),
    upi_id          text        NOT NULL,
    member_id       bigint      NOT NULL REFERENCES members(id),
    source          text        NOT NULL CHECK (source IN ('registered', 'learned')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (institution_id, upi_id, member_id)
);
CREATE INDEX IF NOT EXISTS member_upi_ids_member ON member_upi_ids (member_id);

-- What a member owes. There is no balance column anywhere: outstanding and
-- credit are always computed from charges, payments and allocations.
CREATE TABLE IF NOT EXISTS charges (
    id              bigserial   PRIMARY KEY,
    institution_id  bigint      NOT NULL REFERENCES institutions(id),
    member_id       bigint      NOT NULL REFERENCES members(id),
    period          text        NOT NULL CHECK (period ~ '^\d{4}-(0[1-9]|1[0-2])$'),
    kind            text        NOT NULL CHECK (kind IN ('monthly_fee', 'late_fee')),
    gross           bigint      NOT NULL CHECK (gross > 0),
    concession      bigint      NOT NULL DEFAULT 0 CHECK (concession >= 0),
    amount          bigint      NOT NULL CHECK (amount > 0),
    due_date        date        NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CHECK (amount = gross - concession),
    UNIQUE (member_id, period, kind)          -- a month is billed once, enforced by the database
);
CREATE INDEX IF NOT EXISTS charges_member_due ON charges (member_id, due_date, id);

-- A credit line from the bank statement. The UTR is the idempotency key.
CREATE TABLE IF NOT EXISTS bank_credits (
    id              bigserial   PRIMARY KEY,
    institution_id  bigint      NOT NULL REFERENCES institutions(id),
    utr             text        NOT NULL,
    channel         text        NOT NULL DEFAULT 'UPI'
                                CHECK (channel IN ('UPI', 'NEFT', 'IMPS', 'RTGS', 'OTHER')),
    amount          bigint      NOT NULL CHECK (amount > 0),
    payer_upi_id    text,
    payer_name      text,
    note            text,
    narration       text,
    received_at     timestamptz NOT NULL,
    status          text        NOT NULL CHECK (status IN ('needs_review', 'matched')),
    match_rule      text        CHECK (match_rule IN ('ref_code', 'known_upi_id', 'sibling_split', 'manual')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    CHECK ((status = 'matched') = (match_rule IS NOT NULL)),
    UNIQUE (institution_id, utr)
);
CREATE INDEX IF NOT EXISTS bank_credits_queue ON bank_credits (institution_id, status, received_at);

-- The share of one bank credit that belongs to one member.
CREATE TABLE IF NOT EXISTS payments (
    id              bigserial   PRIMARY KEY,
    institution_id  bigint      NOT NULL REFERENCES institutions(id),
    bank_credit_id  bigint      NOT NULL REFERENCES bank_credits(id),
    member_id       bigint      NOT NULL REFERENCES members(id),
    amount          bigint      NOT NULL CHECK (amount > 0),
    received_at     timestamptz NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (bank_credit_id, member_id)
);
CREATE INDEX IF NOT EXISTS payments_member ON payments (member_id, received_at, id);

-- How much of a payment went to a charge. One running total per pair.
CREATE TABLE IF NOT EXISTS allocations (
    id              bigserial   PRIMARY KEY,
    institution_id  bigint      NOT NULL REFERENCES institutions(id),
    payment_id      bigint      NOT NULL REFERENCES payments(id),
    charge_id       bigint      NOT NULL REFERENCES charges(id),
    amount          bigint      NOT NULL CHECK (amount > 0),
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (payment_id, charge_id)
);
CREATE INDEX IF NOT EXISTS allocations_charge ON allocations (charge_id);

-- Row-level security: a transaction sees only the institution named in
-- app.institution_id, and sees nothing at all when none is set.
--
-- institutions has no policy on purpose: the API looks an institution up by
-- its key before it knows which institution it is serving.
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['members', 'audit_log', 'member_fees', 'member_upi_ids', 'charges',
                             'bank_credits', 'payments', 'allocations'] LOOP
        -- ALTER TABLE takes an exclusive lock, so skip it when already applied:
        -- a restart must not block live traffic.
        IF NOT (SELECT relrowsecurity AND relforcerowsecurity FROM pg_class
                 WHERE oid = format('%I', t)::regclass) THEN
            EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
            EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema()
                         AND tablename = t AND policyname = 'tenant_isolation') THEN
            EXECUTE format($p$CREATE POLICY tenant_isolation ON %I
                USING (institution_id = NULLIF(current_setting('app.institution_id', true), '')::bigint)
                WITH CHECK (institution_id = NULLIF(current_setting('app.institution_id', true), '')::bigint)$p$, t);
        END IF;
    END LOOP;
END $$;
