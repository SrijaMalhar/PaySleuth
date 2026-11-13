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

-- Row-level security: a transaction sees only the institution named in
-- app.institution_id, and sees nothing at all when none is set.
--
-- institutions has no policy on purpose: the API looks an institution up by
-- its key before it knows which institution it is serving.
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['members', 'audit_log'] LOOP
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
