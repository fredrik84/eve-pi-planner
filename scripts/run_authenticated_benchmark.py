"""Privileged, opt-in production browsing with an existing, owner-authorized session.

No fixture setup, session minting, public bypass, forced refresh or configuration actions.
The operator must have permission from the named account owner and cluster exec access.
Credentials pass from a read-only DB query through process memory/stdin, never command args,
files or printed output. Requires the existing browser-tests Docker image.
"""
import argparse
import json
import subprocess
from pathlib import Path


LOOKUP = r'''
import json, os, sys
import psycopg2
con = psycopg2.connect(os.environ['DATABASE_URL'])
try:
    con.set_session(readonly=True)
    with con.cursor() as cur:
        cur.execute('SELECT DISTINCT context_id FROM pp_characters WHERE lower(character_name)=lower(%s)', (sys.argv[1],))
        contexts = [r[0] for r in cur.fetchall() if r[0]]
        if len(contexts) != 1:
            raise RuntimeError('Character must resolve to exactly one account')
        cur.execute('SELECT token FROM pp_sessions WHERE context_id=%s ORDER BY created_at DESC LIMIT 1', (contexts[0],))
        row = cur.fetchone()
        if not row:
            raise RuntimeError('No existing session; owner must log in first')
        print(json.dumps({'token': row[0]}))
finally:
    con.rollback()
    con.close()
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--character', required=True)
    parser.add_argument('--samples', type=int, default=1, choices=range(1, 6))
    args = parser.parse_args()
    lookup = subprocess.run([
        'sudo', 'k3s', 'kubectl', '-n', 'production', 'exec', '-i',
        'deploy/eve-pi-planner', '--', 'python3', '-c', LOOKUP, args.character,
    ], capture_output=True, text=True, timeout=30)
    if lookup.returncode:
        raise SystemExit('Session lookup failed; no browser tests were started. Ask the owner to log in or check cluster access.')
    token = json.loads(lookup.stdout)['token']
    if not isinstance(token, str) or not token or '\n' in token or '\r' in token:
        raise SystemExit('Invalid session encoding; refusing to run')
    # Docker config has no secret env value: the runner reads it from stdin only. The shell
    # does not echo stdin; the latency project disables traces, videos and screenshots.
    result = subprocess.run([
        'docker', 'compose', '--profile', 'test', 'run', '--rm', '--no-deps', '-T',
        '-e', 'BASE_URL=https://eveindustry.net', '-e', 'PP_SESSION=', '-e', 'LATENCY_TOKEN=',
        '-e', 'BENCH_BROWSE_ONLY=1', '-e', f'BENCH_SAMPLES={args.samples}',
        '-e', 'BENCH_EXPIRY_WAIT_SECONDS=0', 'browser-tests',
        'sh', '-c', 'IFS= read -r PP_SESSION; export PP_SESSION; exec npx playwright test --project=latency',
    ], input=token + '\n', text=True, timeout=args.samples * 360 + 120,
       cwd=Path(__file__).resolve().parents[1])
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
