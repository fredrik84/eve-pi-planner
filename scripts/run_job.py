#!/usr/bin/env python3
"""Entrypoint for Kubernetes CronJobs.

Runs one named job in its own pod, using the same image and the same code path the in-process
scheduler uses — so a job behaves identically whether it's triggered here or there, and the lease
still guarantees a single runner if both ever fire at once.

Deliberately NOT an HTTP endpoint the CronJob calls: the whole reason to move a 15-minute contract
scan out of the web pods is that it doesn't belong in a request-serving process. Poking an endpoint
would put it right back, and a rollout mid-scan would kill it — which is exactly what kept happening
while this was developed.

    python scripts/run_job.py contract_scan
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _contract_scan() -> str:
    from app.industry.bpc import _run_scan, _scan_state, THE_FORGE
    _run_scan(THE_FORGE)
    st = _scan_state(THE_FORGE)
    return f"examined {st.get('seen', 0)} contracts, indexed {st.get('indexed', 0)} blueprints"


JOBS = {
    "contract_scan": _contract_scan,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in JOBS:
        print(f"usage: run_job.py <{'|'.join(JOBS)}>", file=sys.stderr)
        return 2
    name = sys.argv[1]
    from app.jobs import run_job
    res = run_job(name, JOBS[name], ttl=3600)
    print(res)
    # A job skipped because another replica holds the lease is a success, not a failure — otherwise
    # Kubernetes would mark a perfectly healthy overlap as a failed run and start alerting.
    return 1 if res.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
