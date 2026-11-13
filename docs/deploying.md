# Running a pilot, and deploying

Two operational tasks that are not the design: measuring the matching weights against one real
institution's statement, and putting the API somewhere. Referenced from the
[README](../README.md#running-it).

### Running a pilot on one real institution's statement

The matching weights are guesses until someone's own statement scores them, so the measuring path is a
tool rather than a plan:

```bash
python -m tools.pilot setup  --name "Sunrise PG" --members members.csv   # sample_data/ has the template
python -m tools.pilot import --institution 1 --statement june.csv --as-of 2026-06-30
python -m tools.pilot review --institution 1                             # one credit at a time
python -m tools.pilot measure --institution 1
```

`review` records, for every decision, which suggestions were on screen and where in that ranking the
answer came — so `measure` can report what share of credits matched with nobody involved, how often the
top suggestion was right, and each signal's precision. It refuses to tune the weights below 50 decisions
and prints how many more are needed, because a rate over six credits is not a rate.

Two rules about the data, and they are in the tool's own help. It runs against a local database that gets
dropped afterwards, and the statement is never committed (`.gitignore` covers `*_real.csv`, `real_*.csv`
and `pilot_data/`). And the statement is *not* anonymised first: payer names and UPI IDs are exactly what
matching is scored on, so anonymising before measuring would measure the anonymiser. `tools/anonymise_statement.py`
exists for the opposite job — turning a real export into a golden test fixture that can be committed.

### Deploying

```bash
docker build -t paysleuth .
docker run -e PAYSLEUTH_DATABASE_URL=... -p 8000:8000 paysleuth                  # API
docker run -e PAYSLEUTH_DATABASE_URL=... paysleuth python -m paysleuth.worker    # worker
```

One image serves both roles, so the API and the worker cannot drift apart. Settings:
`PAYSLEUTH_DATABASE_URL`, `PAYSLEUTH_MESSAGE_WEBHOOK`, `PAYSLEUTH_WEBHOOK_SECRET`,
`PAYSLEUTH_METRICS_TOKEN`, `PAYSLEUTH_POOL_SIZE`, `PAYSLEUTH_LOG_LEVEL`. The schema is applied on boot
under an advisory lock, so there is no migration step and two instances starting together cannot collide.

`render.yaml` is a blueprint for the free tier, and three of its decisions are about what free tiers
actually do rather than what they advertise:

- **The database is Neon, not Render's.** A free Render Postgres expires 30 days after creation — the
  clock starts at creation, not first use — and the data is deleted 14 days later. A demo with a fuse in
  it is worse than no demo, so the blueprint takes a connection string instead of provisioning a database.
- **No worker service**, because Render's free tier has none. Every job the worker runs is also an
  idempotent endpoint, so `.github/workflows/scheduled-jobs.yml` pokes them hourly in the right order
  (bill, remind, relay until it claims nothing, close last). Coarser than a 5-second poll, same work.
- **`.github/workflows/keep-warm.yml`** pings `/health` every 14 minutes between 06:00 and 00:00 IST,
  because a free service sleeps after 15 idle minutes and takes about a minute to wake. 18 hours a day is
  roughly 540 of the 750 free instance hours a month; round the clock would be about 720 and leave no
  margin, which is why the window is a window. It reads its own secret rather than sharing
  `PAYSLEUTH_URL`, so a host that does not sleep gets the scheduled jobs without the pinging.

`deploy/huggingface/` is the second target, for a host that asks for no card and does not sleep. A Space
is its own git repository and takes its configuration from YAML at the top of `README.md` — which would
put a stray table above the first line of this one, so that block lives in `deploy/huggingface/README.md`
and `sync.sh` swaps it in while publishing. The script publishes `git archive HEAD` rather than the
working directory, so an untracked scratch file, or a real institution's statement sitting in the folder,
cannot reach a public Space. The one thing it needed from the image was `UID 1000`: Spaces start the
container as that user, and files owned by anyone else become unreadable when the platform switches user.

**The role the provider gives you is not the role to run as.** A managed PostgreSQL hands you an owner
that belongs to the provider's own administrative role — `neon_superuser`, `supabase_admin` — and those
carry `BYPASSRLS`. Connect as one and every policy in `schema.sql` is skipped with no error and no
warning: one tenant can read another's rows. `check_role()` refuses to start as such a role, so the
deploy fails at the first migration instead of quietly later. `docker/initdb-hosted.sql` creates the
ordinary role to use instead — with SQL, because a role added through the provider's dashboard inherits
that same administrative role and fails the check for the same reason. The application then creates its
own tables and owns them, which is safe only because the schema declares `FORCE ROW LEVEL SECURITY`: a
table's owner bypasses a merely-enabled policy whatever its role attributes say. `tests/test_deployment.py`
asserts all 15 protected tables carry `FORCE`, since every tenancy test in the suite depends on it.

One change the hosted database forced on the code. The free services put a transaction-mode pooler in
front of PostgreSQL, which hands the next transaction a different backend — so a statement psycopg
prepared is not there the second time it runs, and the error (`prepared statement _pg3_0 already
exists`) appears only under load, after a query has been executed enough times to be promoted.
`make_pool` recognises a pooled DSN and turns preparation off for it. Nothing else about the design
minds a pooler: row-level security is set with `SET LOCAL` and the migration lock is transaction-scoped,
so both stay on one backend for exactly as long as they need to. `tests/test_deployment.py` holds the
detection and what it does to the connection.

Outgoing messages are signed `X-PaySleuth-Signature: t=<unix>,v1=<hex HMAC-SHA256>` over
`"<t>.<body bytes>"` — signing the timestamp *with* the body is what stops a captured message being
replayed with a fresh `t`. The verifier lives in the same module, is tested against the signer through a
real HTTP gateway, and can be copied into a gateway as it is. `/metrics` needs either the scrape token
(all institutions) or a staff key (that institution only). Logs mask phone numbers and UPI IDs
(`98****3210`, `su****@oksbi`) in a filter on the handler — including exception tracebacks — so a line
added later anywhere is masked without the author remembering.

---
