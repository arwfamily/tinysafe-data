"""
TinySafe link liveness audit - actually knock on every URL in the store.

Born from an honest correction: "every record has a link" was verified across
all 6,236 records; "every link opens a live page" never was, and a parent
found one that doesn't. This script closes that gap: it requests every unique
URL and reports the ones that fail, so "verified" means visited, not present.

Three result classes:
  DEAD      - HTTP >= 400, network error, or a CDRH page that answers 200 but
              says no record exists (ColdFusion apps 200 their error pages)
  SHELL     - the known IRES search-form URL (~605 records). The app stopped
              opening these in build 87 (Copy recall # instead), so they are
              listed for the record but are not failures.
  OK        - everything else that answered < 400

Run in GitHub Actions (Run link audit workflow) - takes ~20-30 minutes for
~5,000 unique URLs at a polite pace. Writes link_audit_report.txt and prints
a summary; exits 0 always (report, don't break).
"""
import json
import re
import sys
import time
import urllib.request

STORE = 'recalls_unified.json'
REPORT = 'link_audit_report.txt'
SHELL = 'ires/index.cfm'
UA = {'User-Agent': 'Mozilla/5.0 (TinySafe link audit; contact: tinysafe.app)'}
# CDRH res.cfm answers 200 for a missing id with an error body.
_CDRH_MISS = re.compile(
    r'no (?:matching )?records? (?:were )?found|could not be located|'
    r'invalid.{0,20}id', re.I)


def check(url):
    """Return (status, note). status: 'OK' | 'DEAD'."""
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as resp:
            code = resp.status
            if 'cfres/res.cfm' in url:
                body = resp.read(20000).decode('utf-8', 'ignore')
                if _CDRH_MISS.search(body):
                    return 'DEAD', f'{code} but page says record not found'
            return ('OK', str(code)) if code < 400 else ('DEAD', str(code))
    except Exception as e:
        return 'DEAD', type(e).__name__ + ': ' + str(e)[:80]


def main(repo='.'):
    import os
    os.chdir(repo)
    recs = json.load(open(STORE, encoding='utf-8'))['recalls']
    by_url = {}
    for r in recs:
        u = str(r.get('url') or '').strip()
        by_url.setdefault(u, []).append(r['recall_id'])

    missing = by_url.pop('', [])
    shells = {u: ids for u, ids in by_url.items() if SHELL in u}
    for u in shells:
        by_url.pop(u)

    print(f'unique URLs to visit: {len(by_url)} '
          f'(+{len(shells)} shell URLs listed, not visited; '
          f'{len(missing)} records with no url)')

    dead, ok = [], 0
    for i, (u, ids) in enumerate(sorted(by_url.items()), 1):
        status, note = check(u)
        if status == 'DEAD':
            dead.append((u, ids, note))
            print(f'  DEAD [{note}] {u[:90]}  ({len(ids)} record(s): '
                  f'{", ".join(ids[:4])}{"..." if len(ids) > 4 else ""})')
        else:
            ok += 1
        if i % 200 == 0:
            print(f'  ... {i}/{len(by_url)} visited, {len(dead)} dead so far')
        time.sleep(0.25)          # polite pace - this is a courtesy crawl

    with open(REPORT, 'w', encoding='utf-8') as f:
        f.write(f'TinySafe link audit - {time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())}\n')
        f.write(f'visited {len(by_url)} unique urls: {ok} OK, {len(dead)} DEAD\n')
        f.write(f'shell urls (not opened by app since build 87): '
                f'{sum(len(v) for v in shells.values())} records\n')
        if missing:
            f.write(f'records with NO url: {len(missing)}: {missing}\n')
        f.write('\n=== DEAD ===\n')
        for u, ids, note in dead:
            f.write(f'[{note}] {u}\n    records: {", ".join(ids)}\n')

    print(f'\n=== link audit ===\n  {ok} OK · {len(dead)} DEAD '
          f'(full list in {REPORT})')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '.')
