"""Private service health: fixed categories and numeric aggregates, never identifiers or errors."""
import json
import logging
import math
import time

import httpx

log = logging.getLogger('evepi.access')
_OPERATIONS = {'planet_scan', 'skills_refresh', 'alliance_refresh', 'token_refresh', 'esi_request'}
_OUTCOMES = {'success', 'failed', 'partial', 'cached', 'reauthorization_required'}
_REASONS = {'none', 'timeout', 'network', 'unauthorized', 'forbidden', 'rate_limited',
            'not_found', 'upstream', 'http_client', 'auth_rejected', 'internal'}


def failure_reason(error):
    if isinstance(error, httpx.TimeoutException):
        return 'timeout'
    if isinstance(error, httpx.RequestError):
        return 'network'
    if isinstance(error, httpx.HTTPStatusError):
        return status_reason(error.response.status_code)
    return 'internal'


def status_reason(status):
    return {401: 'unauthorized', 403: 'forbidden', 404: 'not_found', 420: 'rate_limited',
            429: 'rate_limited'}.get(status, 'upstream' if status >= 500 else 'http_client' if status >= 400 else 'none')


def note(operation, outcome, *, reason='none', fetched=0, skipped=0, failed=0):
    """Only fixed operation/outcome/reason names and bounded numeric counts can enter the log."""
    try:
        from app.profiling import profiling_enabled
        if operation not in _OPERATIONS or outcome not in _OUTCOMES or reason not in _REASONS:
            return
        if not profiling_enabled():
            return
        counts = (fetched, skipped, failed)
        if any(not isinstance(n, int) or not 0 <= n <= 1_000_000 for n in counts):
            return
        log.info(json.dumps({'event': 'operation', 'operation': operation, 'outcome': outcome,
                             'reason': reason, 'fetched': fetched, 'skipped': skipped, 'failed': failed},
                            separators=(',', ':')))
    except Exception:
        pass  # Observability must never change a scan, authorization or upstream request.


def _collect():
    from app import cache, db
    from app.jobs import KNOWN_JOBS
    from app.profiling import profiling_enabled
    if not profiling_enabled():
        return []
    cached = cache.cache_get_json('profiling:operations:v1')
    if isinstance(cached, list):
        return cached
    samples = []

    def put(name, value, help_text, *, kind='gauge', job=None):
        if value is not None and math.isfinite(float(value)):
            samples.append(dict(name=name, value=float(value), help=help_text, kind=kind, job=job))

    now = time.time()
    jobs = [job for job, _, _ in KNOWN_JOBS]
    marks = ','.join('?' for _ in jobs)
    try:
        with db.get_connection() as con:
            row = con.execute(
                "SELECT COUNT(*) AS characters, "
                "SUM(CASE WHEN COALESCE(refresh_token,'')='' THEN 1 ELSE 0 END) AS reauth, "
                "SUM(CASE WHEN COALESCE(scan_failed_at,0)>0 THEN 1 ELSE 0 END) AS scan_failed "
                "FROM pp_characters WHERE COALESCE(is_dummy,0)=0").fetchone()
            put('evepi_characters_reauthorization_required', row['reauth'] or 0,
                'Real characters without a stored refresh token; access-token expiry alone is not deauthorization')
            put('evepi_characters_scan_failed', row['scan_failed'] or 0,
                'Real characters with an outstanding alert-rescan failure marker; not every scan failure')
            totals = {r['job']: dict(r) for r in con.execute(
                "SELECT job, SUM(CASE WHEN status IN ('ok','error') THEN 1 ELSE 0 END) AS completed, "
                "SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS failures, "
                "SUM(CASE WHEN status='error' AND ended_at>=? THEN 1 ELSE 0 END) AS failures_24h, "
                "SUM(CASE WHEN status='running' AND ended_at IS NULL THEN 1 ELSE 0 END) AS unfinished, "
                "MAX(CASE WHEN status='ok' THEN ended_at END) AS last_success, "
                "MAX(CASE WHEN status='error' THEN ended_at END) AS last_failure "
                f"FROM pp_job_runs WHERE job IN ({marks}) GROUP BY job", [now-86400, *jobs]).fetchall()}
            latest = {r['job']: dict(r) for r in con.execute(
                "SELECT job, started_at, ended_at, status FROM (SELECT job,started_at,ended_at,status, "
                "ROW_NUMBER() OVER (PARTITION BY job ORDER BY started_at DESC,id DESC) AS rank "
                f"FROM pp_job_runs WHERE job IN ({marks})) runs WHERE rank=1", jobs).fetchall()}
            leases = {r['job']: r['lease_until'] for r in con.execute(
                f"SELECT job,lease_until FROM pp_job_leases WHERE job IN ({marks})", jobs).fetchall()}
            config = {r['job']: r['enabled'] for r in con.execute(
                f"SELECT job,enabled FROM pp_job_config WHERE job IN ({marks})", jobs).fetchall()}
        for job in jobs:
            row = totals.get(job, {})
            put('evepi_job_runs_total', row.get('completed', 0), 'Recorded completed scheduler runs', kind='counter', job=job)
            put('evepi_job_failures_total', row.get('failures', 0), 'Recorded scheduler runs ending with an exception', kind='counter', job=job)
            put('evepi_job_failures_24h', row.get('failures_24h', 0), 'Recorded scheduler exceptions in the last 24 hours', job=job)
            put('evepi_job_unfinished_runs', row.get('unfinished', 0), 'Run records without an end marker; includes interrupted historical runs, not current workers', job=job)
            put('evepi_job_last_success_timestamp_seconds', row.get('last_success'), 'Last recorded successful scheduler completion timestamp', job=job)
            put('evepi_job_last_failure_timestamp_seconds', row.get('last_failure'), 'Last recorded scheduler exception timestamp', job=job)
            put('evepi_job_lease_active', int((leases.get(job) or 0)>now), 'Whether the scheduler lease is unexpired; not proof the worker is alive', job=job)
            put('evepi_job_enabled', int(bool(config.get(job, 1))), 'Whether the scheduler job is enabled', job=job)
            last = latest.get(job, {})
            put('evepi_job_last_start_timestamp_seconds', last.get('started_at'), 'Latest recorded scheduler start timestamp', job=job)
            if last.get('ended_at') is not None:
                put('evepi_job_last_duration_seconds', max(0, last['ended_at']-last['started_at']), 'Duration of the latest run if completed', job=job)
        put('evepi_operations_available', 1, 'Whether anonymous job and character health statistics could be read')
    except Exception:
        samples = [dict(name='evepi_operations_available', value=0., help='Whether anonymous job and character health statistics could be read', kind='gauge', job=None)]
    cache.cache_set_json('profiling:operations:v1', samples, ttl=60)
    return samples


def prometheus_lines():
    """Render numeric series; job labels come exclusively from the application's fixed registry."""
    try:
        lines, seen = [], set()
        for sample in _collect():
            name = sample['name']
            if name not in seen:
                lines.extend([f"# HELP {name} {sample['help']}", f"# TYPE {name} {sample['kind']}"])
                seen.add(name)
            # 'job' is Prometheus's scrape label; use a distinct name so it isn't renamed.
            labels = ('{task="'+sample['job']+'"}') if sample['job'] else ''
            lines.append(f"{name}{labels} {sample['value']}")
        return lines
    except Exception:
        return ['# TYPE evepi_operations_available gauge', 'evepi_operations_available 0']
