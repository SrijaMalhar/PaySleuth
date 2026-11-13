# Sample data

`june_2026_statement.csv` — a short statement in a bank layout with a running balance, used by
`tools/demo.sh`, `tools/seed_demo.py` and the tests. Invented names and UTRs.

`members_template.csv` — the shape `tools/pilot.py setup` expects for a real institution's member
list. Only `name`, `monthly_fee` and `start_period` are required; a member can hold several UPI IDs
separated by spaces or semicolons, and siblings share a parent's by listing the same one.

## Running a pilot on real data

```bash
python -m tools.pilot setup   --name "Their Centre" --members members.csv
python -m tools.pilot import  --institution 1 --statement their_statement.csv --as-of 2026-06-30
python -m tools.pilot review  --institution 1        # resolve the queue as the owner would
python -m tools.pilot measure --institution 1
```

Measure on the real statement, on a local database, and drop the database afterwards. Do **not**
anonymise first: the payer names and UPI IDs are exactly what matching is scored on, so measuring
anonymised data measures the anonymiser. `tools/anonymise_statement.py` is for the other job, preparing
a file to commit as a parser fixture, where only the layout matters.

Nothing in this folder, and no real statement, should ever be committed.
