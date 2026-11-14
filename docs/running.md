# Running it

Local setup, and the endpoints worth looking at first. The design is in the
[README](../README.md); this is the part you need once you want to see it move.

```bash
docker compose up -d db
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

python -m tools.walkthrough        # the five beats in the GIF above
python -m tools.seed_demo          # a centre mid-month, for clicking through /docs
uvicorn paysleuth.main:app &       # API docs at http://localhost:8000/docs
pytest -q                          # 447 tests
python tools/mutation_check.py     # 32 safety mechanisms, each deleted and caught
```

`/docs` is the interface: 37 endpoints, and the review queue is the one to show someone. For putting it
somewhere other than a laptop, see [deploying.md](deploying.md).
