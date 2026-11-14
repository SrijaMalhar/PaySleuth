"""Fail if anything in a log file is not a JSON object.

    python tools/check_json_logs.py /tmp/api.log

Used by CI against the container's output. It exists because the lines that
were not JSON were uvicorn's own -- its startup and access logs install their
own handlers and set propagate=False -- and a check that sampled the tail of
the file missed them entirely, since they come first.
"""

import json
import sys
from pathlib import Path


def offenders(text: str) -> list[str]:
    bad = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            bad.append(f"{n}: {line[:120]}")
            continue
        if not isinstance(entry, dict) or "message" not in entry:
            bad.append(f"{n}: parsed, but is not a log entry: {line[:100]}")
    return bad


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__.strip())
        return 2
    bad = offenders(Path(argv[1]).read_text(errors="replace"))
    if bad:
        print(f"{len(bad)} line(s) are not JSON log entries:", *bad, sep="\n  ")
        return 1
    print("every log line parses as a JSON entry")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
