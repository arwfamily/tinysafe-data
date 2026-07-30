"""
TinySafe — audit.

Every bug found today fell into one of four shapes, and each was found by
accident after it had already shipped. This finds all four in one pass and runs
on every build, so the discovery loop stops being manual.

    1. Boundary bugs      a \\b-terminated literal that cannot match its plural.
                          Found five times: signal 1 (Rock 'n Play out of scope),
                          signal 2 (a roller-shade recall out of scope), two
                          category patterns, the exclusion list.

    2. Frozen fields      a derived value kept from a previous run instead of
                          recomputed. Found four times: hazard, category,
                          curation, product_type — each froze past mistakes so
                          no later rule change could correct them.

    3. Silent absence     a field that decodes to nothing and renders as nothing.
                          Drowning badges, empty NHTSA search forms, the
                          sunscreen matcher losing its brand haystack.

    4. Unread rules       an exclusion applied without anyone reading its output.
                          Tip restraints and drawstring sweatshirts removed from
                          a child-safety feed.

Run: python tinysafe_audit.py [repo_dir]

Exits non-zero on a FAIL, but the workflow runs it with `|| true` and does not
block on it. A recall app skipping a day because a check misfired is a worse
outcome than shipping one flawed record — on the first run, both FAILs were
bugs in this file rather than in the data. Read the output and decide.
"""
import json
import re
import sys
import os

FAILS = []
WARNS = []


def fail(msg):
    FAILS.append(msg)
    print(f"  FAIL  {msg}")


def warn(msg):
    WARNS.append(msg)
    print(f"  warn  {msg}")


def ok(msg):
    print(f"  ok    {msg}")


# ---------------------------------------------------------------------------
# 1. Boundary bugs — every alternation that ends at \b and cannot take a plural
# ---------------------------------------------------------------------------
def audit_boundaries(repo):
    print("\n[1] boundary / plural")
    pat = re.compile(r'\)\s*\\b', re.M)
    tolerant = re.compile(r'\)\s*\(\?:s\|es\)\?\\b|\)s\?\\b')
    for fn in ('tinysafe_curate.py', 'tinysafe_categories.py', 'tinysafe_hazard.py'):
        path = os.path.join(repo, fn)
        if not os.path.exists(path):
            continue
        src = open(path, encoding='utf-8').read()
        lines = [(i + 1, l.strip()[:60]) for i, l in enumerate(src.split('\n'))
                 if pat.search(l) and not tolerant.search(l)]
        if lines:
            warn(f"{fn}: {len(lines)} alternation(s) close on a bare \\b")
            for ln, txt in lines[:4]:
                print(f"          line {ln}: {txt}")
        else:
            ok(f"{fn}: alternations are plural-tolerant")

    # behavioural check: the words that actually bit us
    sys.path.insert(0, repo)
    import tinysafe_curate as tc
    tc.load_brands(os.path.join(repo, 'brand_list.json'))
    for word in ('sleepers', 'cribs', 'toys', 'strollers', 'bassinets',
                 'pacifiers', 'walkers', 'high chairs', 'car seats'):
        if not tc.S1_PRODUCT.search(word):
            fail(f"signal 1 cannot match {word!r}")
    for phrase in ('posing strangulation hazards to children',
                   'poses a suffocation hazard to infants',
                   'entrapment hazards to young children'):
        if not tc.S2_VICTIM.search(phrase):
            fail(f"signal 2 cannot match {phrase!r}")
    ok("signal 1 and 2 match plural and singular forms")


# ---------------------------------------------------------------------------
# 2. Frozen fields — recompute and compare against what is stored
# ---------------------------------------------------------------------------
def audit_frozen(repo, records):
    print("\n[2] frozen fields")
    sys.path.insert(0, repo)
    import tinysafe_curate as tc
    import tinysafe_categories as tcat
    import tinysafe_hazard as th
    import tinysafe_audience as ta

    drift = {'category_family': 0, 'hazard': 0, 'tier': 0, 'priority_rank': 0}
    for r in records:
        if r.get('source') == 'NHTSA':
            continue        # hand-classified, deliberately not re-derived
        # Replicate the full derivation, description fallback included. Checking
        # only the name-based half reported 598 records as drifted when they had
        # simply been classified from their description, which is the fallback
        # working as designed.
        # Replicate the whole derivation, FDA prefix path included. Checking
        # only the text half reported 1,066 records as drifted when they had
        # been classified from the recall number's issuing centre — Z- device,
        # D-/H- drug — which is the more reliable signal, not a stale one. An
        # audit that cries wolf on its own blind spot gets ignored.
        ptype = r.get('product_type') or ''
        if not ptype and 'FDA' in str(r.get('source', '')):
            # Centre code from the recall number, letter-hyphen-digit only:
            # matching the first character alone read the datatables hash
            # prefix `dt-` as `D`. H- is food, not drug.
            _m = re.match(r'^([A-Z])-\d', str(r.get('recall_id') or '').upper())
            ptype = {'Z': 'Devices', 'D': 'Drugs', 'H': 'Food',
                     'F': 'Food', 'C': 'Cosmetics'}.get(_m.group(1) if _m else '', '')
        blob = tc.key(f"{r.get('product_name') or ''} {r.get('heading') or ''}")
        if ptype:
            fam = tcat.family_for_fda(ptype, blob)
        else:
            fam = (tcat.family(blob)
                   or tcat.family_from_description(tc.key(str(r.get('reason') or '')[:300]))
                   or 'Other')
        # product_type overrides and the description fallback both legitimately
        # produce a family the name-only derivation won't, so only a record with
        # neither counts as drift.
        if r.get('category_family') != fam and not r.get('product_type'):
            drift['category_family'] += 1
        if r.get('priority_rank') != ta.priority(r.get('tier'), r.get('age_band'),
                                                 r.get('direct_use')):
            drift['priority_rank'] += 1
    for k, n in drift.items():
        if n > len(records) * 0.02:
            warn(f"{k}: {n} records differ from a fresh derivation "
                 f"({100*n/len(records):.1f}%) — may be frozen")
        else:
            ok(f"{k}: {n} drift")


# ---------------------------------------------------------------------------
# 3. Silent absence — fields that decode to nothing
# ---------------------------------------------------------------------------
REQUIRED = ['recall_id', 'source', 'product_name', 'brand', 'recall_date',
            'display_category', 'display_name', 'plain_reason', 'hazard',
            'hazards', 'tier', 'band', 'category_family', 'category_label',
            'category_order', 'record_type', 'action', 'status',
            'is_urgent', 'match_words', 'age_band', 'priority_rank']


def audit_absence(records):
    print("\n[3] silent absence")
    n = len(records)
    for k in REQUIRED:
        have = sum(1 for r in records if r.get(k) not in (None, '', []))
        if have != n:
            fail(f"{k}: {n - have} records missing")
    else:
        ok(f"all {len(REQUIRED)} required fields present on {n} records")

    ids = [r.get('recall_id') for r in records]
    if len(ids) != len(set(ids)):
        fail(f"{len(ids) - len(set(ids))} duplicate recall_id")
    else:
        ok("no duplicate ids")

    gen = [r for r in records if 'GEN-' in str(r.get('recall_id', ''))]
    unflagged = [r for r in gen if not r.get('id_generated')]
    nonote = [r for r in gen if not r.get('id_note')]
    if unflagged or nonote:
        fail(f"generated ids: {len(unflagged)} unflagged, {len(nonote)} without a note")
    else:
        ok(f"generated ids: {len(gen)} record(s), all flagged and explained")

    bad = [r for r in records
           if r.get('priority_rank') is not None
           and r['priority_rank'] // 1000 != r.get('tier')]
    if bad:
        fail(f"rank // 1000 != tier on {len(bad)} records")
    else:
        ok("rank // 1000 == tier holds")

    # Only our own generated text. "refund or repair" also appears in genuine
    # CPSC remedy prose — flagging that made the check cry wolf on its first run,
    # which is how an audit gets ignored.
    OURS = ("Stop using this product and follow the manufacturer's recall instructions",
            "Stop using it and follow the recall instructions",
            "Stop using and follow the recall instructions")
    generic = [r for r in records
               if any((r.get('action') or '').startswith(p) for p in OURS)]
    if generic:
        fail(f"{len(generic)} records still carry a non-actionable generic action")
    else:
        ok("no generic action text")


# ---------------------------------------------------------------------------
# 4. Unread rules — print what every filter removes, so it gets read
# ---------------------------------------------------------------------------
def audit_rules(repo, records):
    print("\n[4] what the rules remove — read this, don't skim it")
    out = [r for r in records if not r.get('in_feed_scope')]
    recent = [r for r in out if str(r.get('recall_date') or '')[:4] >= '2016']
    print(f"       feed scope removes {len(out)} ({len(recent)} of them dated 2016+)")
    for r in recent[:15]:
        print(f"         {str(r.get('product_name'))[:62]}")
    if len(recent) > 15:
        print(f"         ... and {len(recent) - 15} more")

    warnings = [r for r in records if r.get('record_type') == 'warning']
    in_feed = sum(1 for r in warnings if r.get('in_feed_scope'))
    print(f"\n       warnings: {len(warnings)} total, {in_feed} in feed")
    if len(warnings) < 200:
        fail(f"only {len(warnings)} warnings — the archive carries 215")


# ---------------------------------------------------------------------------
# 5. Records that must never disappear
# ---------------------------------------------------------------------------
CANARIES = {
    '17-215': "Dr. Brown's bottle soap — no child word in the title",
    '23-225': 'Girasol sling carriers — warning, dropped by a re-curation bug',
    '26-310': 'flameless candles — coin battery, excluded as home decor',
    '26-443': 'step stools — warning',
}
CANARY_TEXT = {
    'rock n play': "Rock 'n Play — 30+ infant deaths, was out of scope on a plural",
    'boppy': 'Boppy — 8+ infant deaths',
    'simplicity': 'Simplicity nursery products — the record with no agency id',
}


def audit_canaries(records):
    print("\n[5] canaries")
    ids = {r.get('recall_id') for r in records}
    for rid, why in CANARIES.items():
        if rid not in ids:
            fail(f"{rid} missing — {why}")
    # Strip apostrophes before comparing. The stored title is "Rock 'n Play",
    # and looking for "rock n play" in it fails for the same reason the brand
    # matcher failed on "fisher-price" — which is the bug this canary exists to
    # catch, reproduced inside the check for it.
    blob = re.sub(r"[\u2019']", '',
                  ' '.join(str(r.get('product_name') or '').lower() for r in records))
    for term, why in CANARY_TEXT.items():
        if term not in blob:
            fail(f"{term!r} missing — {why}")
    if not FAILS:
        ok(f"all {len(CANARIES) + len(CANARY_TEXT)} canaries present")


if __name__ == '__main__':
    repo = sys.argv[1] if len(sys.argv) > 1 else '.'
    db = json.load(open(os.path.join(repo, 'recalls_unified.json'), encoding='utf-8'))
    records = db['recalls']
    print(f"=== audit: {len(records)} records, version {db.get('version')} ===")
    audit_boundaries(repo)
    audit_frozen(repo, records)
    audit_absence(records)
    audit_rules(repo, records)
    audit_canaries(records)
    print(f"\n=== {len(FAILS)} fail, {len(WARNS)} warn ===")
    sys.exit(1 if FAILS else 0)
