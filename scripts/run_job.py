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


# name -> (callable, minimum hours between scheduled runs)
JOBS = {
    "contract_scan": (_contract_scan, 22.0),
}


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args or args[0] not in JOBS:
        print(f"usage: run_job.py <{'|'.join(JOBS)}> [--if-due]", file=sys.stderr)
        return 2
    name = args[0]
    fn, interval_h = JOBS[name]

    from app.jobs import run_job, is_due

    # --if-due is what lets one frequently-ticking CronJob serve both the schedule AND "run now":
    # it exits in milliseconds unless a human asked for a run or the interval has elapsed.
    if "--if-due" in flags:
        due, why = is_due(name, interval_h * 3600)
        if not due:
            print(f"{name}: not due ({why})")
            return 0
        print(f"{name}: due ({why})")

    res = run_job(name, fn, ttl=3600)
    print(res)
    # A job skipped because another replica holds the lease is a success, not a failure — otherwise
    # Kubernetes would mark a perfectly healthy overlap as a failed run and start alerting.
    return 1 if res.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
