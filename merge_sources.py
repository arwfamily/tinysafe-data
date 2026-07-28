"""
TinySafe — merge CPSC + FDA + NHTSA into one store.

Order matters and each step reports, per TINYSAFE_DATABASE_ARCHITECTURE.md §10.

  1. normalise every source onto the unified schema
  2. apply the id crosswalk so the pipeline's 5-digit CPSC ids become canonical
  3. dedupe within FDA (two id schemes for one event)
  4. cross-agency check NHTSA against CPSC (travel systems)
  5. write, and report what happened at every stage

Never merges across record_type: a CPSC safety warning and a CPSC recall for the
same product are two events, and the later one is new information.
"""
import datetime
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, '.')
import tinysafe_curate as tc
import tinysafe_hazard as th
import tinysafe_categories as tcat
import tinysafe_audience as taud

# Words that appear inside brand strings but identify no firm. Without these a
# make like "Play Yard Inc" matches every CPSC record containing "play".
GENERIC = set("""baby babies infant infants toddler kids child children youth junior
play playard yard seat seats car auto safety products product company corp inc llc
group international industries usa america north american juvenile""".split())

BOILER = set("""recall recalls recalled recalling due to risk of serious injury death hazard
hazards consumer consumers product products safety commission cpsc fda nhtsa announce
announces announced cooperation with the a an and or for by in on from violation federal
standard mandatory posing pose poses sold at online exclusively new expanded""".split())


def toks(s):
    return {t for t in tc.key(s).split() if t not in BOILER and len(t) > 2 and not t.isdigit()}


def _daydiff(a, b):
    """Real day count. Integer subtraction on YYYYMMDD is not one."""
    try:
        fmt = '%Y%m%d'
        return abs((datetime.datetime.strptime(a, fmt) - datetime.datetime.strptime(b, fmt)).days)
    except Exception:
        return None


def brandroot(s):
    s = tc.key(s)
    s = re.split(r'\bdba\b|\bd b a\b', s)[-1]
    s = re.sub(r'\b(inc|llc|corp|corporation|co|ltd|company|group|holdings|usa|us|brands?|imports?|trading)\b', '', s)
    return ' '.join(s.split())[:26]


# The app decodes a fixed set of keys and silently coerces or skips what is
# missing. A record without `display_category` lands in "Other" with no chip; one
# without `is_urgent` never enters the Most-urgent section. Neither failure
# raises, so the only way to catch them is to count fields on the merged output —
# which is how 1,506 FDA imports were found sitting outside every section.
LEGACY_CAT = [
    ("Medications", r"\bdrops\b|medication|acetaminophen|ibuprofen|gripe water|supplement|vitamin|probiotic|ointment|tablet|capsule"),
    ("Food & Formula", r"formula|baby food|puree|pouch|cereal|snack|yogurt|milk|juice|water|beverage"),
    ("Wipes", r"\bwipe"),
    ("Baby Sunscreen", r"sunscreen|spf\b|sun ?block"),
    ("Skincare", r"lotion|baby oil|diaper cream|balm|shampoo|baby wash|powder|moisturiz"),
    ("Oral Care", r"toothpaste|teether|teething|toothbrush|pacifier"),
    ("Medical", r"ventilat|resuscitat|catheter|neonatal|nicu|syringe|forceps|intubation|surgical|clinical"),
]
FAMILY_TO_LEGACY = {
    "Food & formula": "Food & Formula", "Medications & supplements": "Medications",
    "Skincare, bath & diapering": "Skincare", "Oral care & teething": "Oral Care",
    "Medical devices": "Medical", "Personal care & medicine": "Medications",
}


def _legacy_category(rec):
    """The 9-value field the app still filters on. Derived from the new family
    where there is a mapping, from text otherwise."""
    fam = rec.get("category_family")
    if fam in FAMILY_TO_LEGACY:
        return FAMILY_TO_LEGACY[fam]
    t = tc.key(f"{rec.get('product_name','')} {rec.get('reason','')}")
    for name, pat in LEGACY_CAT:
        if re.search(pat, t):
            return name
    return "Toys & Gear" if fam and fam != "Other" else "Other"


LEGACY_VALUES = {"Food & Formula", "Oral Care", "Baby Sunscreen", "Skincare", "Wipes",
                 "Toys & Gear", "Medications", "Medical", "Other"}


AGENCY_URL = {
    "CPSC": "https://www.cpsc.gov/Recalls",
    "FDA": "https://www.accessdata.fda.gov/scripts/ires/index.cfm#tabNav_advancedSearch",
    "NHTSA": "https://www.nhtsa.gov/recalls",
}


# CPSC archive records carry only the recall title, never a separate brand.
# My Brands matches on `brand`, so without this every one of the 4,158 archive
# records is invisible to a parent following Graco or Fisher-Price.
_STOPHEAD = re.compile(
    r'^(the|a|an|certain|all|various|assorted|new|recalled|children\'?s?|childrens|'
    r'kids?|baby|babies|infant|infants|toddler|toddlers|girls?|boys?|youth|juvenile|'
    r'wooden|wood|plastic|metal|organic|natural|mini|large|small|portable|'
    r'\d+[a-z]*)\s+', re.I)
_PRODNOUN = re.compile(
    r'\b(recalls?|recalled|brand|children|childrens|kids|baby|babies|infant|toddler|'
    r'crib|cribs|stroller|strollers|toy|toys|seat|seats|chair|chairs|bassinet|lounger|'
    r'walker|walkers|helmet|helmets|swing|swings|carrier|carriers|mattress|mattresses|'
    r'pajama|pajamas|sleepwear|dresser|dressers|bumper|bumpers|monitor|monitors|'
    r'set|sets|kit|kits|with|and|due|for)\b', re.I)


def brand_from_title(title):
    """Leading proper-noun run of a CPSC recall title. Strips generic openers
    repeatedly — "Children's Wooden Toy Blocks" must not yield "childrens"."""
    t = tc.clean(title or '').strip()
    for _ in range(4):
        n = _STOPHEAD.sub('', t).strip()
        if n == t:
            break
        t = n
    if not t:
        return ''
    m = _PRODNOUN.search(t)
    head = t[:m.start()] if m and m.start() > 0 else t
    words = [w for w in head.split() if w]
    return ' '.join(words[:3]).strip(' ,.-\'"') if words else ''


# A lot list is for checking one tin against, not for reading. The longest in
# the corpus runs to 524,440 characters — a fifth of the whole database in one
# field, and no parent scrolls it. Cap it and say how many were dropped, so the
# record stays honest about being partial.
CODE_CAP = 2000


def trim_code_info(rec):
    # `more_code_info` arrives from the openFDA path and bypassed this cap
    # entirely — 59 records carrying 17 MB between them.
    for k in ("more_code_info",):
        v = (rec.get(k) or "").strip()
        if len(v) > CODE_CAP:
            cut = v[:CODE_CAP].rsplit(" ", 1)[0]
            rec[k] = f"{cut} ... (+{len(v) - len(cut):,} more characters)"
    c = (rec.get("code_info") or "").strip()
    if len(c) <= CODE_CAP:
        return rec
    cut = c[:CODE_CAP].rsplit(" ", 1)[0]
    dropped = len(c) - len(cut)
    rec["code_info"] = f"{cut} ... (+{dropped:,} more characters — see the official notice)"
    rec["code_info_truncated"] = True
    return rec


# The original 5-value `category`. Distinct from display_category (9) and
# category_family (25), and something downstream still reads it.
_COARSE = {
    'Medical devices': 'Medical Devices',
    'Medications & supplements': 'Drugs',
    'Personal care & medicine': 'Drugs',
    'Skincare, bath & diapering': 'Cosmetics',
    'Food & formula': 'Food & Beverages',
    'Feeding & high chairs': 'Baby & Kids',
}


def _coarse_category(rec):
    fam = rec.get('category_family')
    if fam in _COARSE:
        return _COARSE[fam]
    dc = rec.get('display_category')
    if dc == 'Medical':
        return 'Medical Devices'
    if dc in ('Medications',):
        return 'Drugs'
    if dc in ('Skincare', 'Baby Sunscreen', 'Wipes', 'Oral Care'):
        return 'Cosmetics'
    if dc == 'Food & Formula':
        return 'Food & Beverages'
    return 'Baby & Kids'


def fill_legacy(rec):
    """Every key the app decodes, on every record, whatever source it came from."""
    if rec.get("display_category") not in LEGACY_VALUES:
        rec["display_category"] = _legacy_category(rec)
    if not rec.get("display_name"):
        n = rec.get("product_name") or rec.get("brand") or rec.get("heading") or ""
        rec["display_name"] = (n[:77] + "...") if len(n) > 80 else n
    if not rec.get("plain_reason"):
        # Nine NHTSA campaigns from 1972-74 plus 95C035000 carry no defect or
        # consequence text at source. Rork left them blank rather than guessing,
        # which was right — but blank renders as nothing at all, so state the
        # absence instead of inventing a hazard.
        pr = (rec.get("hazard_text") or rec.get("reason") or rec.get("heading") or "").strip()
        if not pr:
            yr = str(rec.get("recall_date") or "")[:4]
            pr = (f"The {yr} recall notice does not state what the defect was. "
                  f"Check with the manufacturer before using this product."
                  if yr else "The recall notice does not state what the defect was.")
        rec["plain_reason"] = pr
    if not rec.get("brand"):
        rec["brand"] = (rec.get("product_name") or "")[:40]
    if not rec.get("recall_id"):
        rec["recall_id"] = f"{rec.get('source') or 'X'}-{abs(hash((rec.get('product_name') or '') + str(rec.get('recall_date') or '')))%10**8}"
    if not rec.get("plain_reason"):
        rec["plain_reason"] = rec.get("hazard_text") or rec.get("reason") or ""
    if not rec.get("url"):
        rec["url"] = AGENCY_URL.get(str(rec.get("source", "")).split()[0], "")
    if not rec.get("action"):
        rec["action"] = ("Stop using it now. Check the official recall notice for what to do "
                         "next. If your baby used it and has any symptoms, contact your "
                         "pediatrician.")
    if not rec.get('status') or (rec.get('record_type') == 'warning'
                                 and rec.get('status') == 'Recalled'):
        # CPSC never terminates a recall, so an archive record is open by
        # definition. NHTSA is the same. Only FDA carries a lifecycle.
        if rec.get('record_type') == 'warning':
            # Never "Recalled" — the firm refused to recall, which is the whole
            # reason the warning exists. Ongoing is true and lets it satisfy the
            # active-status checks rather than sitting outside them.
            rec['status'] = 'Ongoing'
        else:
            rec['status'] = 'Recalled' if rec.get('source') in ('CPSC', 'NHTSA') else 'Ongoing'
    tier = rec.get("tier") or 9
    rec["is_urgent"] = tier <= 3
    rec["urgent_rank"] = tier if tier <= 3 else 99
    if not rec.get("brand"):
        rec["brand"] = brand_from_title(rec.get("product_name") or rec.get("heading") or "")
    if not rec.get("product_name"):
        rec["product_name"] = rec.get("heading") or rec.get("display_name") or rec.get("brand") or ""
    # Match fields, built the same way sync_recalls.build_match_fields does.
    # My Brands filters on these, and the personalised tab that appears ahead of
    # "All" is driven entirely by them — a record missing match_words simply
    # never reaches a parent who follows that brand.
    b = (rec.get("brand") or "").lower().strip()
    text = f"{b} {(rec.get('product_name') or '').lower()}".strip()
    squashed = re.sub(r"[^a-z0-9]", "", text)
    rec["match_brand"] = b
    rec["match_text"] = text
    rec["match_words"] = text
    rec["match_squash"] = squashed
    rec["match_brand_squash"] = re.sub(r"[^a-z0-9]", "", b)
    rec.setdefault("match_ndc", "")
    return rec


def enrich(rec):
    """Fill the derived fields on any record, whatever source it came from."""
    name = rec.get('product_name') or rec.get('display_name') or ''
    # Product name and title only. Including `reason` put every record whose
    # hazard text says "can slide out" into Outdoor & play equipment — the same
    # contamination already fixed once in tinysafe_categories, reintroduced here
    # because this function builds its own blob.
    blob = f"{name} {rec.get('heading') or ''}"
    # Always re-derive. Keeping an existing value froze every past mistake in
    # place: 595 FDA records sat in "Outdoor & play equipment" from an earlier
    # pattern set and no later run could correct them. That is the same failure
    # the hazard field was redesigned to avoid, reintroduced one field over.
    ptype = rec.get('product_type') or ''
    if rec.get('source') == 'NHTSA':
        # Every NHTSA record here is an RCLTYPECD == "C" child restraint
        # campaign. The source states it; guessing from a title like
        # "Cosco COSCO 13-168" put 184 of the 251 somewhere else.
        fam = 'Car seats & travel'
    elif ptype:
        fam = tcat.family_for_fda(ptype, tc.key(blob))
    else:
        # Name and title first. Fall back to the description only when they
        # yield nothing, so hazard prose can still help an otherwise unnamed
        # product without being able to override a clear product name.
        fam = (tcat.family(tc.key(blob))
               or tcat.family(tc.key(str(rec.get('reason') or '')[:300]))
               or 'Other')
    rec['category_family'] = fam
    rec['category_group'] = tcat.group(fam)
    rec['category_label'] = tcat.label(fam)
    rec['category_order'] = tcat.chip_order(fam)

    if rec.get('hazard_hand_assigned') and rec.get('hazard'):
        # NHTSA hazards were assigned by hand against each record's own text.
        # Never re-derive over a human decision.
        hz = [rec['hazard']]
        tier = th.TIER.get(rec['hazard'], th.UNMAPPED_TIER)
    else:
        text = rec.get('hazard_text') or rec.get('reason') or ''
        hz, tier = th.derive(tc.clean(text), tc.clean(name), fam)
    rec['hazards'] = hz
    rec['hazard'] = th.primary_of(hz)
    rec['tier'] = tier
    rec['band'] = th.BAND.get(tier, 'CHECK')
    rec.setdefault('record_type', 'recall')
    rec.setdefault('in_feed_scope', True)
    rec.setdefault('deaths_reported', None)
    rec.setdefault('injuries_reported', None)

    # Who it is for and whether the child touches it. Hazard tier says how bad;
    # these say how close to this parent's baby. They only break ties inside a
    # tier — "Still active · serious risk" holds over a thousand tier 1-3
    # records currently ordered by nothing but date.
    audience_text = f"{name} {rec.get('heading') or ''}"
    rec['age_band'] = taud.age_band(audience_text, fam)
    rec['direct_use'] = taud.direct_use(audience_text, fam)
    rec['priority_rank'] = taud.priority(rec['tier'], rec['age_band'], rec['direct_use'])
    return rec


def load_all(repo='.'):
    out, report = [], {}

    # --- CPSC archive (recalls + safety warnings), already curated -----------
    cpsc = [json.loads(l) for l in open(f'{repo}/cpsc_curated.jsonl', encoding='utf-8')]
    for r in cpsc:
        r['recall_id'] = r.pop('record_id')
        r['recall_date'] = (r.pop('date') or '').replace('-', '')
        r['reason'] = r.get('hazard_text')
    out += cpsc
    report['CPSC archive'] = len(cpsc)

    # --- existing unified store: FDA plus whatever CPSC it already held ------
    # Read from a distinct filename. This script writes recalls_unified.json,
    # so reading the same path meant the second run ingested its own output and
    # silently lost every flag the pipeline had set on the original records.
    src = f'{repo}/recalls_pipeline.json'
    if not os.path.exists(src):
        src = f'{repo}/recalls_unified.json'
    db = json.load(open(src, encoding='utf-8'))
    report['_input'] = 1 if src.endswith('pipeline.json') else 0
    fda = [r for r in db['recalls'] if 'FDA' in str(r.get('source', '')).upper()]
    out += fda
    report['FDA'] = len(fda)

    # The archive replaces the pipeline's CPSC records wholesale, which would
    # drop per-record flags the pipeline had set on them. Carry those across by
    # id rather than losing state that took a human to produce.
    CARRY = ('is_enforced', 'needs_review', 'grouped_products', 'product_count',
             'action_curated', 'action')
    carried = {}
    for r in db['recalls']:
        if 'CPSC' not in str(r.get('source', '')).upper():
            continue
        rid = re.sub(r'^CPSC-', '', str(r.get('recall_id', '')).strip())
        keep = {k: r[k] for k in CARRY if r.get(k) not in (None, '', [])}
        if keep:
            carried[rid] = keep
    try:
        cw = json.load(open(f'{repo}/id_crosswalk.json', encoding='utf-8'))
    except Exception:
        cw = {}
    for rid, keep in list(carried.items()):
        if cw.get(rid):
            carried.setdefault(cw[rid], keep)
    globals()['_CARRIED'] = carried
    report['carried CPSC flags'] = len(carried)

    # --- NHTSA -------------------------------------------------------------
    n = json.load(open(f'{repo}/nhtsa_child_restraints.json', encoding='utf-8'))
    nh = n['recalls'] if isinstance(n, dict) else n
    for r in nh:
        r['hazard_text'] = r.get('reason') or ''
    out += nh
    report['NHTSA'] = len(nh)

    before = len(out)
    kept = []
    for r in out:
        ok, _, why = tc.curate(product=r.get('product_name') or '',
                               heading=r.get('brand') or r.get('heading') or '',
                               description=r.get('product_name') or '',
                               hazard=r.get('reason') or r.get('hazard_text') or '')
        if ok or r.get('source') == 'NHTSA':
            kept.append(r)
    if len(kept) != before:
        print(f'  re-curated: dropped {before - len(kept)} records that no longer '
              f'pass the scope rules')
    out = kept

    carried = globals().get('_CARRIED', {})
    for r in out:
        keep = carried.get(str(r.get('recall_id', '')).strip())
        if keep:
            for k, v in keep.items():
                # a curated action always wins over anything we would generate
                if k == 'action' and not keep.get('action_curated'):
                    continue
                r.setdefault(k, v)
    return [trim_code_info(fill_legacy(enrich(r))) for r in out], report


def apply_crosswalk(recs, path='id_crosswalk.json'):
    """The pipeline stored CPSC recalls under the API's 5-digit id; the archive
    uses CPSC's public NN-NNN. Same recall, different identifier."""
    cw = json.load(open(path, encoding='utf-8'))
    by_id = {r.get('recall_id'): r for r in recs}
    hits = 0
    for old, new in cw.items():
        r = by_id.get(old) or by_id.get(f'CPSC-{old}')
        if r and new in by_id and r is not by_id[new]:
            r['_superseded_by'] = new
            hits += 1
    return hits


def dedupe_fda(recs):
    """FDA publishes one event twice — an enforcement report and a press
    release. Field-level merge: enforcement wins status and classification,
    the longest action wins, both urls are kept."""
    def scheme(i):
        """Three schemes, case-sensitive. DT- and dt- are different ingest paths
        for the same event, so folding them together with .lower() hid the
        15 exact pairs this function exists to find."""
        i = str(i)
        if i.startswith('DT-'):
            return 'press-date'
        if i.startswith('dt-'):
            return 'press-hash'
        return 'enforcement'

    cl = defaultdict(list)
    for r in recs:
        if 'FDA' not in str(r.get('source', '')).upper():
            continue
        cl[(str(r.get('recall_date', ''))[:8], brandroot(r.get('brand') or ''))].append(r)

    merged, drop = 0, set()
    for key, group in cl.items():
        if len(group) < 2 or not key[1]:
            continue
        if len({scheme(r.get('recall_id')) for r in group}) < 2:
            continue    # same scheme = a genuine multi-product event, not a dupe
        curated = [r for r in group if r.get('action_curated')]
        keep = (curated[0] if curated else
                next((r for r in group if scheme(r.get('recall_id')) == 'enforcement'), group[0]))
        for r in group:
            if r is keep:
                continue
            if r.get('action_curated') and not keep.get('action_curated'):
                keep['action'] = r['action']
                keep['action_curated'] = True
            elif not keep.get('action_curated') and len(r.get('action') or '') > len(keep.get('action') or ''):
                keep['action'] = r['action']
            keep.setdefault('alt_ids', []).append(r.get('recall_id'))
            keep.setdefault('alt_urls', []).append(r.get('url'))
            drop.add(id(r))
            merged += 1
    return [r for r in recs if id(r) not in drop], merged


def cross_agency(recs, window_days=200):
    """Travel systems get recalled by CPSC for the stroller and NHTSA for the
    seat. Report candidates; never auto-merge — two agency records for one
    incident is worth showing as two records, but we need to know it happens.

    Matching on a brand-root key fails here: the CPSC archive records carry no
    `brand` field, only `product_name`, so brandroot() of the whole title never
    equals brandroot() of a make. Index CPSC by its title tokens instead and
    look each NHTSA make up in that index.
    """
    nh = [r for r in recs if r.get('source') == 'NHTSA']
    cp = [r for r in recs if r.get('source') == 'CPSC']

    index = defaultdict(list)
    for r in cp:
        for t in toks(f"{r.get('product_name') or ''} {r.get('brand') or ''}"):
            index[t].append(r)

    pairs, seen = [], set()
    for r in nh:
        makes = {m for b in (r.get('brands') or [r.get('brand') or ''])
                 for m in toks(b)} - GENERIC
        cands = {id(c): c for m in makes for c in index.get(m, [])}
        for c in cands.values():
            d = _daydiff(str(r.get('recall_date', ''))[:8], str(c.get('recall_date', ''))[:8])
            if d is None or d > window_days:
                continue
            key = (r['recall_id'], c['recall_id'])
            if key in seen:
                continue
            seen.add(key)
            shared = makes & toks(c.get('product_name') or '')
            pairs.append((r['recall_id'], c['recall_id'], sorted(shared), d))
    return sorted(pairs, key=lambda x: x[3])


if __name__ == '__main__':
    repo = sys.argv[1] if len(sys.argv) > 1 else '.'
    recs, report = load_all(repo)
    print('=== sources ===')
    for k, v in report.items():
        if k == '_input':
            print(f"  input: {'recalls_pipeline.json' if v else 'recalls_unified.json'}")
        else:
            print(f'  {v:6d}  {k}')
    print(f'  {len(recs):6d}  total before dedup')

    cw = apply_crosswalk(recs, f'{repo}/id_crosswalk.json')
    print(f'\n=== id crosswalk ===\n  {cw} pipeline ids mapped to canonical CPSC numbers')

    recs, merged = dedupe_fda(recs)
    print(f'\n=== FDA dedup ===\n  {merged} duplicate records folded, {len(recs)} remain')

    pairs = cross_agency(recs)
    print(f'\n=== NHTSA x CPSC cross-agency ===\n  {len(pairs)} candidate pairs')
    for a, b, shared, d in pairs[:15]:
        print(f'    {a:18s} <-> {b:10s}  {d:4d}d apart  shared={shared}')

    seen, deduped = {}, []
    for r in recs:
        rid = r.get("recall_id")
        prev = seen.get(rid)
        if prev is None:
            seen[rid] = r
            deduped.append(r)
        elif sum(1 for v in r.values() if v) > sum(1 for v in prev.values() if v):
            deduped[deduped.index(prev)] = r
            seen[rid] = r
    if len(deduped) != len(recs):
        print(f"\n=== id dedup ===\n  {len(recs) - len(deduped)} duplicate ids folded")
        recs = deduped

    print('\n=== final ===')
    print('  by source:', dict(Counter(r.get('source') for r in recs)))
    print('  by type  :', dict(Counter(r.get('record_type') for r in recs)))
    print('  by band  :', dict(Counter(r.get('band') for r in recs)))
    print('  by category:', dict(Counter(r.get('category_family') for r in recs).most_common(8)))
    # Two outputs, because they have different jobs.
    #
    # The app decodes 28 keys; the merge produces 73. Shipping all of them costs
    # 7 MB of payload the client throws away, and a 17 MB file is unwieldy to
    # even open. But the other 45 are how the data stays reproducible — lot
    # dates, event ids, curation signals, hand-assigned NHTSA rationales — so
    # they belong in the repo, not in the download.
    # Never drop a field the live store already carries. The decoder was
    # described as reading 21 keys; the file actually holds 36, and there is no
    # way to know from the data side which of the other 15 something reads.
    # Dropping `match_words` alone would have silently broken My Brands, and
    # dropping `action_curated` would have let the next re-derivation overwrite
    # ten hand-written actions. Additive only.
    LIVE_FIELDS = [
        'recall_id', 'source', 'product_name', 'brand', 'recall_date',
        'classification', 'status', 'url', 'display_category', 'display_name',
        'hazard', 'action', 'plain_reason', 'is_urgent', 'urgent_rank', 'reason',
        # `match_words` is the only match field the app decodes — measured, not
        # assumed. match_text / match_squash / match_brand / match_brand_squash
        # cost 2.29 MB of payload that is parsed and thrown away.
        'match_words', 'match_ndc', 'category', 'action_curated',
        'grouped_products', 'product_count', 'is_enforced', 'needs_review',
    ]
    NEW_FIELDS = [
        'hazards', 'tier', 'band', 'category_family', 'category_group',
        'record_type', 'in_feed_scope', 'code_info', 'remedy_type', 'incidents_text',
        'age_band', 'direct_use', 'priority_rank', 'category_label', 'category_order',
    ]
    APP_FIELDS = LIVE_FIELDS + NEW_FIELDS
    meta = {
        'version': '6.0',
        'description': ('TinySafe unified baby/kids recall DB — CPSC recalls and safety '
                        'warnings, FDA enforcement reports and press releases, NHTSA child '
                        'restraints. Veterinary and adult-only products excluded.'),
        'total_products': sum(int(r.get('product_count') or 1) for r in recs),
        'updated': datetime.datetime.utcnow().isoformat() + 'Z',
        'total': len(recs),
        'schema': {
            'hazards': 'every matching hazard, not the first',
            'tier': '1-8, computed from the hazard list',
            'band': 'ACT NOW 1-3 / ACT SOON 4-5 / CHECK 6-8',
            'category_family': 'product family',
            'category_group': 'browse group',
            'record_type': 'recall | warning — a warning has no remedy',
            'in_feed_scope': 'false = searchable but outside the 0-4 feed',
            'code_info': 'lot numbers, capped at 2000 chars',
        },
        'categories': sorted({r.get('category_family') for r in recs if r.get('category_family')}),
        'groups': sorted({r.get('category_group') for r in recs if r.get('category_group')}),
        'sources': sorted({r.get('source') for r in recs if r.get('source')}),
    }

    app = []
    for r in recs:
        rec = {k: v for k, v in r.items() if k in APP_FIELDS and v not in (None, '', [])}
        # fields every live record carries today, so they must not go missing
        rec.setdefault('category', _coarse_category(r))
        rec.setdefault('match_words', rec.get('match_text', ''))
        rec.setdefault('product_count', r.get('product_count') or 1)
        rec.setdefault('reason', r.get('hazard_text') or r.get('plain_reason') or '')
        rec.setdefault('action_curated', bool(r.get('action_curated')))
        app.append(rec)
    with open(f'{repo}/recalls_unified.json', 'w', encoding='utf-8') as f:
        json.dump({**meta, 'recalls': app}, f, ensure_ascii=False)

    # Full record set, all 73 fields, one record per line. Written every run so
    # it can't drift out of date behind the payload — a stale 17 MB file that
    # looks current is worse than not having one.
    #
    # It is too large for GitHub's web uploader, but the Action commits it
    # directly and that path has no such limit.
    with open(f'{repo}/recalls_full.jsonl', 'w', encoding='utf-8') as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')

    a = os.path.getsize(f'{repo}/recalls_unified.json') / 1048576
    b = os.path.getsize(f'{repo}/recalls_full.jsonl') / 1048576
    b = os.path.getsize(f'{repo}/recalls_full.jsonl') / 1048576
    print(f'\n  recalls_unified.json  {len(app):5d} records  {a:5.1f} MB  (app payload)')
    print(f'  recalls_full.jsonl    {len(recs):5d} records  {b:5.1f} MB  (full field set)')
