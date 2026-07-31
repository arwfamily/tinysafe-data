"""
TinySafe — push a parent when a NEW recall matches a brand they watch.

Runs daily in the sync workflow, after merge_sources.py has published the
store. The whole design answers one promise: "you don't have to open the app;
we tell you." Three sources meet here:

    recalls_unified.json   what was recalled (this repo, just rebuilt)
    Firestore users/{uid}  watchedBrands: [{id, name, slug}, ...]
    OneSignal              delivery, targeted by external_id == Firebase uid

Rules that keep this from ever misfiring:

  - A ledger (notified_ids.json, committed) records every recall_id already
    considered. Only ids absent from the ledger are "new". On the very first
    run the ledger does not exist: every current id is written and NOTHING is
    sent — otherwise day one would blast 6,000 historical recalls at everyone.
  - A record is only pushable if its recall_date is within the last 14 days.
    Backfills import old records as "new ids"; a 2016 recall arriving in the
    database today is not news to push about.
  - Matching reuses the pipeline's normalization, including the declared
    Kids II <-> Kids2 alias. A push that disagrees with the My Brands tab is
    worse than no push.
  - Every network failure is non-fatal and the ledger only advances for
    recalls that were fully processed, so a failed day retries tomorrow.

Test mode (send one real push to yourself before trusting the automation):

    python notify_new_matches.py . --test-uid <your-firebase-uid>

sends the most recent in-feed recall to that uid only, ignoring brands and
the ledger. Nothing is written.

Secrets expected in the environment (set in GitHub Actions):
    ONESIGNAL_APP_ID, ONESIGNAL_REST_API_KEY, FIREBASE_SERVICE_ACCOUNT
"""
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta

LEDGER = 'notified_ids.json'
STORE = 'recalls_unified.json'
MAX_AGE_DAYS = 14
BATCH = 2000                      # OneSignal external_id cap per call


# --- matching: same shape as the pipeline -----------------------------------
_ALIAS = [('kidsii', 'kids2')]


def _key(s):
    k = re.sub(r'[^a-z0-9]', '', str(s or '').lower())
    for old, new in _ALIAS:
        k = k.replace(old, new)
    return k


def _matches(user_keys, rec):
    rec_keys = {_key(b) for b in (rec.get('brands') or []) if b}
    rec_keys.discard('')
    for uk in user_keys:
        for rk in rec_keys:
            # equality, or the record's longer legal name starting with the
            # brand a parent follows: 'kidsiiinc' startswith 'kids2'-folded key.
            if uk == rk or rk.startswith(uk):
                return True
    return False


# --- firestore ---------------------------------------------------------------
def _firestore_users(project):
    """Yield (uid, [brand keys]) for every user with watchedBrands."""
    import google.auth.transport.requests
    from google.oauth2 import service_account
    info = json.loads(os.environ['FIREBASE_SERVICE_ACCOUNT'])
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=['https://www.googleapis.com/auth/datastore'])
    creds.refresh(google.auth.transport.requests.Request())
    base = (f'https://firestore.googleapis.com/v1/projects/{project}'
            f'/databases/(default)/documents/users')
    token, page = creds.token, ''
    while True:
        url = base + '?pageSize=300' + (f'&pageToken={page}' if page else '')
        req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
        for doc in data.get('documents', []):
            uid = doc['name'].rsplit('/', 1)[-1]
            arr = (doc.get('fields', {}).get('watchedBrands', {})
                   .get('arrayValue', {}).get('values', []))
            keys = set()
            for v in arr:
                f = v.get('mapValue', {}).get('fields', {})
                for field in ('slug', 'name', 'id'):
                    s = f.get(field, {}).get('stringValue')
                    if s:
                        keys.add(_key(s))
            keys.discard('')
            if keys:
                yield uid, keys
        page = data.get('nextPageToken')
        if not page:
            return


# --- onesignal ---------------------------------------------------------------
def _push(uids, title, body, recall_id, alias_field='external_id'):
    # Production targets accounts (external_id). 'subscription' targets one
    # device directly by its Subscription ID - test-only, exists to prove APNs
    # delivery before the app links accounts. The OneSignal *user* ID is not a
    # valid targeting alias; the API rejected it, which is how we learned.
    if alias_field == 'subscription':
        targeting = {'include_subscription_ids': uids}
    else:
        targeting = {'include_aliases': {alias_field: uids},
                     'target_channel': 'push'}
    payload = json.dumps({
        'app_id': os.environ['ONESIGNAL_APP_ID'],
        **targeting,
        'headings': {'en': title},
        'contents': {'en': body},
        'data': {'recall_id': recall_id},
    }).encode()
    req = urllib.request.Request(
        'https://api.onesignal.com/notifications',
        data=payload, method='POST',
        headers={'Content-Type': 'application/json',
                 'Authorization': 'Key ' + os.environ['ONESIGNAL_REST_API_KEY']})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _age_days(rec):
    s = str(rec.get('recall_date') or '').replace('-', '')[:8]
    try:
        return (date.today() - date(int(s[:4]), int(s[4:6]), int(s[6:8]))).days
    except Exception:
        return 10 ** 6


def _card_line(rec):
    name = str(rec.get('display_name') or rec.get('product_name') or 'A product')
    if len(name) > 60:
        name = name[:57] + '...'
    return f'{name} has been recalled. Tap to see what to do.'


def main(repo):
    os.chdir(repo)
    recs = json.load(open(STORE, encoding='utf-8'))['recalls']

    # --test-uid: one real push to one person, nothing recorded.
    if '--test-uid' in sys.argv:
        uid = sys.argv[sys.argv.index('--test-uid') + 1]
        # A 36-char hyphenated value is a OneSignal ID (the dashboard's own
        # identifier); anything else is a Firebase uid targeted by external_id.
        # The OneSignal-ID path exists to prove APNs delivery end-to-end before
        # the app links accounts - production always targets external_id.
        alias = ('subscription'
                 if re.fullmatch(r'[0-9a-fA-F-]{36}', uid) else 'external_id')
        rec = max((r for r in recs if r.get('in_feed_scope')),
                  key=lambda r: str(r.get('recall_date') or ''))
        out = _push([uid], f"Recall alert: {rec.get('brand') or 'TinySafe'}",
                    _card_line(rec), rec['recall_id'], alias_field=alias)
        print('test push:', rec['recall_id'], f'({alias})', '->', out)
        return

    first_run = not os.path.exists(LEDGER)
    ledger = set() if first_run else set(json.load(open(LEDGER, encoding='utf-8')))

    fresh = [r for r in recs
             if r.get('in_feed_scope')
             and r['recall_id'] not in ledger
             and _age_days(r) <= MAX_AGE_DAYS]

    if first_run:
        json.dump(sorted(r['recall_id'] for r in recs), open(LEDGER, 'w'))
        print(f'=== notify ===\n  first run: ledger seeded with {len(recs)} ids, '
              f'nothing sent (by design)')
        return
    if not fresh:
        print('=== notify ===\n  no new recalls to announce')
        return

    try:
        users = list(_firestore_users('tinysafe-9b632'))
    except Exception as e:
        print(f'=== notify ===\n  firestore unavailable ({e}); will retry tomorrow')
        return

    sent_records = []
    for rec in fresh:
        matched = [uid for uid, keys in users if _matches(keys, rec)]
        ok = True
        for i in range(0, len(matched), BATCH):
            try:
                _push(matched[i:i + BATCH],
                      f"Recall alert: {rec.get('brand') or 'a brand you follow'}",
                      _card_line(rec), rec['recall_id'])
            except Exception as e:
                print(f'  push failed for {rec["recall_id"]}: {e}')
                ok = False
                break
        if ok:
            sent_records.append((rec['recall_id'], len(matched)))
            ledger.add(rec['recall_id'])

    json.dump(sorted(ledger), open(LEDGER, 'w'))
    print('=== notify ===')
    for rid, n in sent_records:
        print(f'  {rid}: {n} parent(s) notified')
    if not sent_records:
        print('  nothing sent')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '.')
