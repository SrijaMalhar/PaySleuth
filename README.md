# PaySleuth

Fee reconciliation for tuition centres, PGs and housing societies: read the bank
statement, work out which member each UPI credit pays for, and send receipts.

Work in progress.

## Local database

```bash
docker compose up -d db
psql postgresql://paysleuth:paysleuth@localhost:5432/paysleuth -c 'select 1'
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
```
