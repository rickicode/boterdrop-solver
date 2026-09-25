#!/usr/bin/env python3
"""solve.py — CLI wrapper for the local Boterdrop Turnstile solver.

Usage:
    python3 solve.py <url> <sitekey> [action]
    python3 solve.py --health
    python3 solve.py --serve          # show server status

Server: http://127.0.0.1:20011  (Docker container: boterdrop-solver)
"""
import sys
import time

import requests

SOLVER = "http://127.0.0.1:20011"


def health() -> int:
    try:
        r = requests.get(f"{SOLVER}/openapi.json", timeout=8)
        if r.status_code != 200:
            print(f"DEAD: HTTP {r.status_code}")
            return 1
        paths = sorted(r.json().get("paths", {}).keys())
        print(f"UP {SOLVER}")
        print("endpoints: " + ", ".join(paths))
        return 0
    except Exception as e:
        print(f"DEAD: {e}")
        return 1


def solve(url: str, sitekey: str, action=None, timeout: int = 180) -> int:
    params = {"url": url, "sitekey": sitekey}
    if action:
        params["action"] = action
    r = requests.get(f"{SOLVER}/turnstile", params=params, timeout=30)
    if r.status_code != 202:
        print(f"CREATE FAILED: HTTP {r.status_code} {r.text[:200]}")
        return 1
    task_id = r.json()["task_id"]
    print(f"task {task_id}", file=sys.stderr, flush=True)

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.5)
        try:
            r2 = requests.get(f"{SOLVER}/result", params={"id": task_id}, timeout=15)
        except Exception as e:
            print(f"poll error: {e}", file=sys.stderr)
            continue
        data = r2.json()
        st = data.get("status")
        if st == "success":
            print(data.get("value", ""))
            print(f"elapsed {data.get('elapsed_time')}s", file=sys.stderr)
            return 0
        if st == "error" or r2.status_code in (408, 422):
            print(f"FAILED: {data}", file=sys.stderr)
            return 2
    print("TIMEOUT waiting for solver", file=sys.stderr)
    return 3


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    if args[0] == "--health":
        sys.exit(health())
    if len(args) < 2:
        print(__doc__)
        sys.exit(1)
    sys.exit(solve(args[0], args[1], args[2] if len(args) > 2 else None))