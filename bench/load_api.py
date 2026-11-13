"""Load-test the API against a real uvicorn and report p50, p95 and p99 per endpoint.

    python bench/load_api.py                       # 500 members, 8 clients, 60s of requests
    python bench/load_api.py --clients 16 --seconds 20

Starts uvicorn in this process on a free port, seeds an institution, then
drives a mix of reads and writes from several threads and reports the
percentiles each endpoint actually reached. Percentiles, not averages: an
owner who waits 4 seconds once in twenty does not care about the mean.
"""

import argparse
import random
import socket
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from conftest import DSN, fresh_pool  # noqa: E402
from factory import build_institution  # noqa: E402

from paysleuth.api import create_app  # noqa: E402
from paysleuth.institutions import create_institution  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """uvicorn on a background thread, so the load generator talks over real HTTP."""

    def __init__(self, app, port):
        self.config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=30)


def percentiles(samples: list[float]) -> dict:
    ordered = sorted(samples)

    def at(p):
        # Nearest-rank: the value such that p% of requests were at most this slow.
        return ordered[min(len(ordered) - 1, max(0, round(p / 100 * len(ordered)) - 1))]

    return {"n": len(ordered), "p50": at(50), "p95": at(95), "p99": at(99), "max": ordered[-1]}


def run(clients: int, seconds: int, members: int) -> dict:
    with fresh_pool(max_size=clients + 8) as pool:
        with pool.connection() as conn:
            inst, key = create_institution(conn, "Load Test Centre", late_fee=10000, grace_days=3)
            families = build_institution(conn, inst, random.Random(5), pairs=members // 4,
                                         singles=members // 2, bill_through=date(2026, 7, 1))
        codes = [m[1] for f in families for m in f["members"]]
        upis = [f["upi"] for f in families if len(f["members"]) == 2]
        app = create_app(DSN, lambda payload: None, pool_options=pool.options, pool_size=clients + 8)
        port = _free_port()

        samples: dict[str, list[float]] = {}
        errors: dict[str, int] = {}
        lock = threading.Lock()

        with Server(app, port) as _:
            base = f"http://127.0.0.1:{port}"
            stop = time.monotonic() + seconds
            counter = [0]

            def work(worker: int):
                rng = random.Random(worker)
                mine: dict[str, list[float]] = {}
                mine_errors: dict[str, int] = {}
                with httpx.Client(base_url=base, headers={"X-API-Key": key}, timeout=60) as client:
                    while time.monotonic() < stop:
                        with lock:
                            counter[0] += 1
                            n = counter[0]
                        roll = rng.random()
                        if roll < 0.45:
                            label, call = "POST /bank-credits", lambda: client.post("/bank-credits", json={
                                "utr": f"9{worker:02d}{n:09d}", "amount": "2000",
                                "received_at": (datetime(2026, 7, 10) + timedelta(seconds=n)).isoformat(),
                                "note": rng.choice(codes)})
                        elif roll < 0.6:
                            label, call = "POST /bank-credits (split)", lambda: client.post(
                                "/bank-credits", json={
                                    "utr": f"8{worker:02d}{n:09d}", "amount": "3500",
                                    "received_at": (datetime(2026, 7, 10) + timedelta(seconds=n)).isoformat(),
                                    "payer_upi_id": rng.choice(upis)})
                        elif roll < 0.7:
                            label, call = "GET /review", lambda: client.get("/review")
                        elif roll < 0.8:
                            label, call = "GET /reports/collection", lambda: client.get(
                                "/reports/collection", params={"period": "2026-07"})
                        elif roll < 0.9:
                            label, call = "GET /reports/ageing", lambda: client.get("/reports/ageing")
                        else:
                            label, call = "GET /members/{id}/statement", lambda: client.get(
                                f"/members/{rng.randrange(1, members)}/statement")
                        started = time.perf_counter()
                        try:
                            response = call()
                            elapsed = (time.perf_counter() - started) * 1000
                            if response.status_code >= 400:
                                mine_errors[label] = mine_errors.get(label, 0) + 1
                            mine.setdefault(label, []).append(elapsed)
                        except Exception:
                            mine_errors[label] = mine_errors.get(label, 0) + 1
                with lock:
                    for label, values in mine.items():
                        samples.setdefault(label, []).extend(values)
                    for label, count in mine_errors.items():
                        errors[label] = errors.get(label, 0) + count

            threads = [threading.Thread(target=work, args=(i,)) for i in range(clients)]
            started = time.perf_counter()
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            elapsed = time.perf_counter() - started

        total = sum(len(v) for v in samples.values())
        return {"seconds": elapsed, "requests": total, "per_second": total / elapsed,
                "errors": errors,
                "endpoints": {label: percentiles(values) for label, values in sorted(samples.items())}}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clients", type=int, default=8)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--members", type=int, default=500)
    args = parser.parse_args()

    result = run(args.clients, args.seconds, args.members)
    print(f"{args.clients} clients, {args.members} members, {result['seconds']:.0f}s: "
          f"{result['requests']} requests, {result['per_second']:.0f}/s\n")
    print(f"{'endpoint':32} {'n':>6} {'p50 ms':>8} {'p95 ms':>8} {'p99 ms':>8} {'max ms':>8}")
    for label, p in result["endpoints"].items():
        print(f"{label:32} {p['n']:>6} {p['p50']:>8.0f} {p['p95']:>8.0f} {p['p99']:>8.0f} {p['max']:>8.0f}")
    if result["errors"]:
        print(f"\nerrors: {result['errors']}")
        return 1
    print("\nno errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
