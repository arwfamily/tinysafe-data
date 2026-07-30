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
import collections
import datetime
import hashlib
import json
import os
import re
import sys
import urllib.parse
from collections import Counter, defaultdict

sys.path.insert(0, '.')
import tinysafe_curate as tc
import tinysafe_hazard as th
import tinysafe_categories as tcat
import tinysafe_audience as taud
import tinysafe_incidents as tinc
import tinysafe_severity as tsev

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


def _cpsc_url(recall_id, heading):
    """https://www.cpsc.gov/Recalls/{fiscal year}/{slug}

    Fiscal year comes from the recall number prefix (25-009 -> 2025), which is
    why a calendar-year construction breaks on every October-December recall.
    """
    m = re.match(r'^(\d\d)-', str(recall_id).strip())
    if not m:
        return ''
    yy = int(m.group(1))
    year = 2000 + yy if yy < 50 else 1900 + yy
    # No truncation. CPSC slugs the full published title, semicolon clauses
    # included, and a 120-character cut produced ".../from-Choking-Violat" where
    # the page is ".../from-Choking-Violate-Mandatory-Standard-for-Toys-Sold-on-
    # Amazon-by-YouRfocus". The construction was right; the input was clipped.
    slug = re.sub(r'[^A-Za-z0-9]+', '-', tc.clean(heading)).strip('-')
    slug = re.sub(r'-{2,}', '-', slug)
    return f'https://www.cpsc.gov/Recalls/{year}/{slug}' if slug else ''


def _cpsc_search(recall_id):
    """The bare index, for records with no heading to slug.

    `?search_api_fulltext=` looked like a pre-filled search and isn't: CPSC 302s
    it to /Recalls with the query stripped, so the parent lands on the generic
    list having been promised their recall. A link that quietly discards what it
    was asked for is the same failure as the empty NHTSA form — better to send
    them somewhere that is honestly just an index.
    """
    return AGENCY_URL['CPSC']


def _fda_search(rec):
    """The bare IRES form. Verified: a query parameter breaks the page.

    `?Product=H-0891-2026` makes the page's own JavaScript throw
    `TypeError: undefined is not an object (evaluating
    JSON.parse(returnData.responseText).RESULT.DATA)` — a visible error dialog,
    not an empty form. Worse than either the bare form or no link: a parent sees
    a stack-trace-shaped alert and concludes the app is broken.

    The bare form is also what the app already handled — it fills the product
    name in on arrival, so the prefill happens client-side and the URL doesn't
    need to carry it. Adding a parameter broke a path that was working.
    """
    return AGENCY_URL['FDA']


AGENCY_URL = {
    "CPSC": "https://www.cpsc.gov/Recalls",
    "FDA": "https://www.accessdata.fda.gov/scripts/ires/index.cfm#tabNav_advancedSearch",
    "NHTSA": "https://www.nhtsa.gov/recalls",
}


# CPSC archive records carry only the recall title, never a separate brand.
# My Brands matches on `brand`, so without this every one of the 4,158 archive
# records is invisible to a parent following Graco or Fisher-Price.
# A heading may open with a subordinate clause before naming the company. CPSC
# closes that clause with a comma; the URL slug drops punctuation, which is why
# three attempts to read the brand off the slug all failed. The heading itself
# is in recalls_full.jsonl on 4,002 records and carries the comma.
_LEAD_CLAUSE = re.compile(
    r'^(?:following|after|because of|due to|amid|citing|in (?:the )?wake of|'
    r'in response to|near|risk of|reports? of|death of|strangulation|'
    r'prompted by)\b[^,]{0,90},\s*', re.I)
# CPSC's title grammar is "<COMPANY> Recalls <PRODUCT> Due to <HAZARD>".
# Anything from the verb onward is not the company, which is what let
# `IKEA Reannounces`, `Kids2 Reannounces` and `Fisher-Price Reannounces` ship
# as brands - and those are the Rock 'n Play and Rocking Sleeper
# re-announcements, so My Brands missed them for the exact brands a parent
# would follow after an infant-sleep death.
_HEAD_VERB = re.compile(
    r'\s+(?:recalls?|recalled|reannounces?|reannounced|expands?|expanded|'
    r'announces?|announced|urges?|warns?|reissues?)\b', re.I)

_ANNOUNCEMENT = re.compile(
    r'^(?:cpsc|fda|nhtsa)\s+(?:urges?|warns?|announces?|alerts?|advises?)\s+'
    r'(?:consumers?|parents?|the\s+public)?\s*(?:to\s+)?'
    r'(?:stop\s+using|check|immediately|)\s*(?:their\s+homes?\s+for\s+)?'
    r'(?:numerous\s+)?', re.I)

# `new` is a generic opener only when it modifies the product word after it
# ("New Children's Pajamas"). In "New York Lighter Company", "New York Firm"
# and "New Port Sales" it is the first word of the name, and stripping it gave
# `York Lighter Company` - a plausible-looking brand that is simply wrong,
# which is why it was never caught. Require a generic word to follow.
_NEW_GENERIC = re.compile(
    r'^new\s+(?=(?:the|a|an|certain|all|various|assorted|recalled|children|'
    r'kids?|baby|babies|infant|toddler|girls?|boys?|youth|juvenile|model|'
    r'style|line)\b)', re.I)
_STOPHEAD = re.compile(
    r'^(the|a|an|certain|all|various|assorted|recalled|children\'?s?|childrens|'
    r'kids?|baby|babies|infant|infants|toddler|toddlers|girls?|boys?|youth|juvenile|'
    r'wooden|wood|plastic|metal|organic|natural|mini|large|small|portable|'
    r'\d+[a-z]*)\s+', re.I)
_PRODNOUN = re.compile(
    r'\b(recalls?|recalled|brand|children|childrens|kids|baby|babies|infant|toddler|'
    r'crib|cribs|stroller|strollers|toy|toys|seat|seats|chair|chairs|bassinet|lounger|'
    r'walker|walkers|helmet|helmets|swing|swings|carrier|carriers|mattress|mattresses|'
    r'pajama|pajamas|sleepwear|dresser|dressers|bumper|bumpers|monitor|monitors|'
    r'set|sets|kit|kits|with|and|due|for)\b', re.I)


# CPSC titles come in two voices. Active - "<COMPANY> Recalls <PRODUCT>" - puts
# the firm first. Passive - "<PRODUCT> Recalled by <FIRM>" - puts it last, and
# 425 headings are passive. Reading only the front of a passive title gave
# `Nightgowns` for two different firms (AOSKERA and AllMeInGeld), so two
# unrelated recalls shared one brand string and My Brands could match neither.
#
# The front is still preferred when it names a real consumer brand: in
# "Bunz Kidz Children's Sleepwear Sets Recalled by Stargate Apparel" a parent
# follows Bunz Kidz, not the manufacturer. The firm after `by` is the fallback
# when the front is nothing but product words, and is kept alongside it either
# way - Philips Avent and Philips Personal Health are both true.
_RECALLED_BY = re.compile(
    r'\brecalled by\s+(.+?)(?=\s+due to\b|\s+because\b|;|$)', re.I)
# Words that cannot be a brand on their own.
_GENERIC_ONLY = re.compile(
    r"^(?:[\s,&'-]|children'?s?|childrens|kids?|baby|babies|infant|infants|"
    r"toddlers?|girls?|boys?|youth|juvenile|toy|toys|pajamas?|nightgowns?|robes?|"
    r"sleepwear|garments?|blanket|sleepers?|bumpers?|crib|cribs|stroller|strollers|"
    r"walkers?|helmets?|swings?|dressers?|mattress(?:es)?|sets?|kits?|tools?|"
    r"boots?|jackets?|hoodies|sweatshirts?|apparel|clothing|bed|rails?|"
    r"and|with|due|the|a|an)+$", re.I)
# The agency is never the recalling company.
_AGENCY_ONLY = re.compile(r'^(?:cpsc|fda|nhtsa|u\.?s\.? cpsc)\b[\s,]*(?:again|announce\w*)?$', re.I)
# CPSC co-announces with the firm: "CPSC and Wear Me Apparel Corp. Recall Infant
# Boys' Rompers", "CPSC, Kids II Inc. Announce Recall of Doorway Baby Jumper".
# The agency guard blanked these because the heading opens with CPSC, losing the
# company that is sitting right after it.
_AGENCY_CO = re.compile(
    r'^(?:u\.?s\.?\s*)?(?:cpsc|fda|nhtsa)\s*(?:,|and)\s*'
    r'([A-Za-z0-9][\w&.\'\u2019-]*(?:\s+[A-Za-z0-9][\w&.\'\u2019-]*){0,3}?)'
    r'\s+(?:recall|announce|issue|urge|warn)', re.I)


# What is left after stripping cannot stand as a company name: a roman numeral,
# a single letter, a bare number.
_RESIDUE = re.compile(r'^(?:[ivxIVX]{1,4}|[A-Za-z]|\d+|jr|sr|inc|llc|co)\b', re.I)

_GENERIC_BRAND = re.compile(
    r'^(?:toys?|children|childrens|kids?|baby|babies|infants?|toddlers?|'
    r'pajamas?|sleepwear|robes?|nightgowns?|the|a|an|and|inc|llc|ltd|co|'
    r'company|official|store|stores|trading|technology|group|brand)$', re.I)


def firm_from_title(title):
    """The company named after a passive `Recalled by`, or ''."""
    m = _RECALLED_BY.search(tc.clean(title or ''))
    if not m:
        return ''
    firm = re.split(r'\s+(?:due|because|after|following)\b', m.group(1), 1, re.I)[0]
    words = firm.split()[:4]
    while words and words[-1].lower() in ('for', 'of', 'and', 'the', 'on', 'at', 'in'):
        words.pop()
    return ' '.join(words).strip(' ,.-\'"')


def brand_from_title(title):
    """Leading proper-noun run of a CPSC recall title. Strips generic openers
    repeatedly — "Children's Wooden Toy Blocks" must not yield "childrens"."""
    t = _ANNOUNCEMENT.sub('', tc.clean(title or '')).strip()
    m = _LEAD_CLAUSE.match(t)
    if m:
        t = t[m.end():].strip()
    mv = _HEAD_VERB.search(t)
    if mv and mv.start() > 0:
        t = t[:mv.start()].strip()
    # `kids` is a stopword, so "Kids II Recalls All Rocking Sleepers" stripped to
    # "II" and 19-112 - Kids II Rocking Sleepers, five infant deaths - shipped
    # with brand `II`. A stopword may only be taken while something recognisable
    # survives it, so each strip is kept only if the residue still reads as a
    # name. Same shape as `new` in "New York Lighter Company".
    for _ in range(4):
        cand = _NEW_GENERIC.sub('', t).strip()
        cand = _STOPHEAD.sub('', cand).strip()
        if cand == t or not cand or _RESIDUE.match(cand):
            break
        t = cand
    if not t:
        return ''
    m = _PRODNOUN.search(t)
    head = t[:m.start()] if m and m.start() > 0 else t
    words = [w for w in head.split() if w]
    out = ' '.join(words[:3]).strip(' ,.-\'"') if words else ''
    # Never assert a brand that is only product words or the agency name. An
    # empty brand is honest; a wrong one silently fails every match it is asked
    # to make and looks plausible on the card while doing it.
    if out and (_GENERIC_ONLY.match(out) or _AGENCY_ONLY.match(out)):
        # The agency co-announces with the firm and the firm is right after it,
        # so try that before the passive `Recalled by` fallback - otherwise the
        # first fallback blanks the name and the second never sees it.
        m2 = _AGENCY_CO.search(tc.clean(title or ''))
        out = (m2.group(1).strip(' ,.-') if m2 else '') or firm_from_title(title) or ''
    if out and _AGENCY_ONLY.match(out):
        out = ''
    return out


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


# Out of scope for a 0-4 feed, in scope for search. Mirrors the narrowing
# sync_recalls applies to freshly fetched records.
# The record naming its own audience beats any pattern word.
# FDA ships product_name as a lettered list - "a.) nara organics brand; ... b.)
# ...". The marker survived into display_name, and because the app ignores the
# FDA url and builds its own fda.gov search from display_name, H-1137-2026 -
# infant formula, Clostridium botulinum, tier 1 - searched fda.gov for the
# literal string "a.) nara organics brand".
#
# Only strip a marker that is part of a real enumeration: the source must carry
# a sibling. `B. toys` is Battat's actual brand and `B. Childhood` is a real
# company, so a rule keyed on shape alone renames a teether to "toys Firefly
# Frank Infant Teethers" and breaks every match against it. Four marker forms
# occur - a) a.) (a) and a bare `a.` - and reading only for `a)` misses the
# DYNAREX baby-wipe record.
_MARKER = r'(?:\(\s*([a-z])\s*\)|([a-z])\s*\.\s*\)|([a-z])\s*\)|([a-z])\s*\.(?=\s))'
_MARKER_RE = re.compile(_MARKER, re.I)
_LEAD_MARKER = re.compile(r'^\s*' + _MARKER + r'\s*', re.I)


def _is_enumeration(text):
    """True when the source really is a lettered list, not a brand initial."""
    seen = {next(g for g in m.groups() if g).lower()
            for m in _MARKER_RE.finditer(text or '')}
    return {'a', 'b'} <= seen


def strip_list_markers(name, source):
    """Remove an FDA enumeration marker from a name already in the store."""
    if not _is_enumeration(source):
        return name
    out = _LEAD_MARKER.sub('', name)
    out = re.split(r'\s+(?:\(\s*b\s*\)|b\s*\.\s*\)|b\s*\))\s', out, 1, re.I)[0]
    return out.strip(' ;,.-') or name


def build_display_name(rec):
    src = rec.get("product_name") or rec.get("brand") or rec.get("heading") or ""
    name = src
    if _is_enumeration(src):
        name = _LEAD_MARKER.sub('', name)
        # The first list member is the readable name; the rest are pack sizes
        # and UPCs that no card shows.
        name = re.split(r'\s+(?:\(\s*b\s*\)|b\s*\.\s*\)|b\s*\))\s', name, 1, re.I)[0]
    name = name.strip(' ;,.-')
    return (name[:77] + "...") if len(name) > 80 else name


# The collapsed card shows one line. The action's median length is 254
# characters and 87% of them open in agency voice - "Consumers should stop using
# the rocking sleeper immediately and contact Kids2 for a refund. It is
# illegal..." - so what a frightened parent actually reads is
# "Consumers should stop using the ro...", which says nothing.
#
# This does NOT write new copy. The instruction is already in the source text;
# it is just behind a prefix addressed to nobody. Strip the prefix, keep the
# first sentence, and the same words become a line that fits:
#   "Stop using the rocking sleeper immediately and contact Kids2 for a refund."
# Inventing a replacement would repeat the mistake the 37 templates already
# made - text written by an assistant that no parent has ever read.
_AGENCY_LEAD = re.compile(
    r'^\s*(?:the\s+)?(?:u\.?s\.?\s+)?(?:consumer product safety commission\s*'
    r'\(?cpsc\)?|cpsc|fda)?\s*'
    r'(?:is\s+)?(?:urges?|warns?|advises?|recommends?)?\s*'
    r'(?:consumers?|parents?|the public)?\s*'
    r'(?:should|to|are urged to|is warning consumers to)\s+', re.I)
_SENT_END = re.compile(r'(?<=[.!?])\s+(?=[A-Z])')


def short_action(action):
    """One readable line, taken from the source text rather than written."""
    t = re.sub(r'\s+', ' ', str(action or '')).strip()
    if not t:
        return ''
    t = _AGENCY_LEAD.sub('', t, count=1).strip()
    first = _SENT_END.split(t)[0].strip()
    if len(first) > 110:
        cut = first[:110].rsplit(' ', 1)[0]
        first = cut.rstrip(' ,;:') + '...'
    return first[:1].upper() + first[1:] if first else ''


# The same prefix problem in the explanation line: 97 records open with
# "The U.S. Consumer Product Safety Commission (CPSC) is warning consumers to
# immediately stop using..." where the card needs to say what the danger is.
_REASON_LEAD = re.compile(
    r'^\s*(?:the\s+)?(?:u\.?s\.?\s+)?(?:consumer product safety commission\s*'
    r'\(?cpsc\)?|cpsc)\s+(?:is\s+)?(?:warning|urging|advising)\s+'
    r'(?:consumers?|parents?|the public)\s+to\s+(?:immediately\s+)?', re.I)
# Stripping the prefix leaves "stop using and dispose of X because the loose,
# hazardous magnets can be swallowed" - still an instruction, and the card
# already has one in action_short. The hazard is the clause after `because`.
_REASON_CAUSE = re.compile(r'\bbecause\s+(?:they\s+|it\s+)?', re.I)


def hazard_line(plain_reason):
    """What is dangerous, not what to do. Taken from the notice, not written.

    Repairs here as well as in fill_legacy, because this runs last and the field
    can be assigned from a source that was repaired earlier or not at all. A
    field that is cleaned once at ingest is a field that is dirty the next time
    something writes to it.
    """
    t = tc.strip_markup(tc.repair_text(plain_reason))
    t = re.sub(r'\s+', ' ', t).strip()
    if not t:
        return t
    t = _REASON_LEAD.sub('', t).strip()
    m = _REASON_CAUSE.search(t)
    if m and len(t) - m.end() > 25:
        t = t[m.end():].strip()
    return t[:1].upper() + t[1:] if t else t


# CPSC re-announces a recall when it is not working - more deaths, more units,
# a firm that did not comply. Those are separate events and must not be merged:
# 19-105 reports 30 infant deaths and 23-088 reports 100, and folding them
# deletes the fact that seventy more died after the first notice. But two rows
# reading "Rock 'n Play Sleepers" back to back look like a bug, so the records
# say they are related and the card decides how to show it.
#
# The link is only ever drawn where the notice SAYS SO. Name similarity alone
# put five separate Chinese sleepwear firms in one bucket; the discriminator is
# the source claiming a prior recall in its own words.
_SUPERSEDES = re.compile(
    r'original recall|previously recalled|reannounces?|re-announces?|'
    r'recall expansion|expands? (?:the |its )?recall|supersed|'
    r'first announced|initially announced', re.I)
# Cross-agency: a travel system is a car seat to NHTSA and a carrier to CPSC, so
# one event arrives twice. Token overlap alone paired "Capilene Base Layer" with
# a car seat base and a seahorse bath toy with Century infant seats, so the
# shared token must be a brand and both records must be the same product class.
_SEAT_CLASS = re.compile(r'car ?seat|infant (?:seat|carrier|restraint)|'
                         r'child restraint|carrier|booster', re.I)
_WEAK_TOKEN = {'seat', 'seats', 'child', 'children', 'infant', 'infants', 'base',
               'bases', 'with', 'kids', 'play', 'baby', 'safety', 'system',
               'model', 'models', 'recall', 'carriers', 'carrier', 'cloud'}


def _rel_key(rec):
    b = re.sub(r'[^a-z0-9]', '', str(rec.get('brand') or '').lower())[:12]
    p = re.sub(r'\ball models? of\b|\breannounce\w*\b|\bexpand\w*\b|\brecall\w*\b',
               '', str(rec.get('display_name') or '').lower())
    p = re.sub(r'(?:es|s)$', '', re.sub(r'[^a-z0-9]', '', p))[:22]
    return b, p


def link_related(recs):
    """related_ids, drawn only where the source states a prior recall."""
    groups = collections.defaultdict(list)
    for r in recs:
        b, p = _rel_key(r)
        if b and len(p) >= 7:
            groups[(b, p)].append(r)
    linked = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        blob = ' '.join(str(m.get(k) or '') for m in members
                        for k in ('units', 'reason', 'heading', 'action', 'incidents_text'))
        if not _SUPERSEDES.search(blob):
            continue
        ids = sorted({m['recall_id'] for m in members})
        for m in members:
            m['related_ids'] = [i for i in ids if i != m['recall_id']]
            m['related_reason'] = 'reannouncement'
            linked += 1
    # Cross-agency pairs, same event reported by NHTSA and CPSC.
    nh = [r for r in recs if r.get('source') == 'NHTSA']
    cp = [r for r in recs if r.get('source') == 'CPSC']

    def _toks(r):
        t = f"{r.get('brand') or ''} {r.get('display_name') or ''} {r.get('product_name') or ''}"
        return {w for w in re.findall(r'[a-z]{4,}', t.lower())} - _WEAK_TOKEN

    def _seat(r):
        return bool(_SEAT_CLASS.search(f"{r.get('display_name') or ''} "
                                       f"{r.get('product_name') or ''} "
                                       f"{r.get('category_family') or ''}"))
    ci = [(c, _days_since(c.get('recall_date')), _toks(c)) for c in cp if _seat(c)]
    cross = 0
    for n in nh:
        if not _seat(n):
            continue
        dn = _days_since(n.get('recall_date'))
        if dn is None:
            continue
        tn = _toks(n)
        for c, dc, tc_ in ci:
            if dc is None or abs(dc - dn) > 14 or not (tn & tc_):
                continue
            for a, b in ((n, c), (c, n)):
                a.setdefault('related_ids', [])
                if b['recall_id'] not in a['related_ids']:
                    a['related_ids'].append(b['recall_id'])
                    a.setdefault('related_reason', 'cross_agency')
            cross += 1
    print(f'=== related ===\n  {linked} re-announcement, {cross} cross-agency pair(s)')


def _neg_date(yyyymmdd):
    """Newest first, as an ascending key. Missing dates sort last."""
    d = str(yyyymmdd or '')
    return -int(d) if d.isdigit() else 0


def _days_since(yyyymmdd):
    s = str(yyyymmdd or '')
    try:
        d = datetime.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except (ValueError, TypeError):
        return None
    return (datetime.date.today() - d).days


def fill_legacy(rec):
    # Repair the stored text. clean() ran when the archive was built, so any
    # mojibake or orphaned combining mark baked in then is still there —
    # "LullaBear\u00e2\u201e\u00a2", "LDLXLHTE\u2019 \u0304Crib Bumpers". Re-running it here is what
    # makes a fix to the repair table reach records that already exist.
    # `hazard_text` was missing from this list, and plain_reason is populated
    # from it further down - AFTER the repair - so 271 records shipped with
    # "violate the https://www.ecfr.gov/... lang="EN-US">mandatory standard"
    # in the line the card renders, while `reason` next to it was clean.
    for _k in ('product_name', 'brand', 'display_name', 'heading', 'plain_reason',
               'reason', 'action', 'sold_at', 'incidents_text', 'hazard_text'):
        if rec.get(_k):
            rec[_k] = tc.strip_markup(tc.repair_text(rec[_k]))

    """Every key the app decodes, on every record, whatever source it came from."""
    # Always recompute. The guard was "only if the stored value isn't a legal
    # legacy value", which meant a record already carrying "Other" kept it
    # forever — 599 Medical devices and 414 more badged "Toys & Gear" while
    # sitting in the Medical devices chip, and 46 Food & formula records still
    # saying "Medications". Sixth field found frozen at ingest; the shape is
    # always a guard that treats "has a plausible value" as "is correct".
    rec["display_category"] = _legacy_category(rec)
    # Recompute every merge. This was the last field still guarded by
    # "only if empty", which is the same shape as the five already fixed above:
    # a stored value that looks plausible is treated as correct, so no rule
    # change ever reaches the records that already exist. Two different
    # truncators had written it (1,594 values sit at exactly 80 chars, 253 end
    # in an ellipsis character and 246 in three dots) and neither was this line.
    # Repair, do not rewrite. Re-deriving the field from product_name changed
    # 2,103 of 6,256 names - mostly a different truncation of the same string -
    # which is a blast radius nobody asked for and would bury the four records
    # this is actually about. So: run the repair over the stored value every
    # merge (that is the unfreeze), and only build from source when it is empty.
    if rec.get("display_name"):
        rec["display_name"] = strip_list_markers(
            rec["display_name"], rec.get("product_name") or rec["display_name"])
    else:
        rec["display_name"] = build_display_name(rec)
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
    # A brand opening with a conjunction or article is a sentence fragment, not
    # a name: "Children's and adult chests and dressers" yielded "and adult
    # chests". CPSC names the company in the heading — "IKEA Recalls MALM Chests
    # and Dressers" — so fall back there.
    if re.match(r'^(and|the|or|with|for|due|a|an)\b',
                str(rec.get("brand") or "").strip(), re.I):
        rec["brand"] = ""
    if not rec.get("brand"):
        # Proper extraction first; the slice below is only for titles it can't
        # parse at all, and it cuts on a word boundary rather than mid-word.
        rec["brand"] = (brand_from_title(rec.get("heading") or "")
                        or brand_from_title(rec.get("product_name") or ""))
    if not rec.get("brand"):
        # The heading named no company. CPSC's remedy sentence often does.
        rec["brand"] = tc.firm_from_action(rec.get("action") or "")
    # One recall can have more than one true brand and `brand` holds only one.
    # "Philips Avent ... Recalled by Philips Personal Health" - a parent follows
    # the first, the recall is filed under the second, and both should match.
    # NHTSA pre-2010 campaigns carry joined makes ("Cosco / Eddie Bauer") for the
    # same reason. Rork built BrandMatcher to read this array; the pipeline was
    # not emitting it, so the matcher had nothing to consume.
    _brands, _seen = [], set()
    for _b in ([rec.get("brand")]
               + [x.strip() for x in re.split(r'\s*/\s*', str(rec.get("brand") or ''))]
               + [firm_from_title(rec.get("heading") or "")]
               + [tc.firm_from_action(rec.get("action") or "")]):
        _b = (_b or '').strip(' ,.-')
        _k = re.sub(r'[^a-z0-9]', '', _b.lower())
        # A single generic word is not a brand. "Toys R Us and Babies R Us"
        # yields `Babies`, which matches nothing a parent follows and would
        # match things they did not mean.
        if _GENERIC_BRAND.match(_b.strip()):
            continue
        if len(_k) > 1 and _k not in _seen:
            _seen.add(_k)
            _brands.append(_b)
    rec["brands"] = _brands or None
    if not rec.get("brand"):
        words = (rec.get("product_name") or "").split()
        out = ""
        for w in words:
            if len(out) + len(w) + 1 > 40:
                break
            out = f"{out} {w}".strip()
        rec["brand"] = out
    if not rec.get("recall_id"):
        # A recall id is an agency identifier — it is how a parent looks the
        # recall up officially, and how dedup finds the same event arriving from
        # two feeds. Inventing one papers over a parse failure: the record can
        # never match its real twin, and if the true id turns up later anything
        # a user saved against the synthetic one silently disappears.
        #
        # So it is generated, but never disguised. `id_generated` and
        # `needs_review` say so, and the id carries a GEN- segment that cannot
        # collide with a real agency number.
        _seed = f"{rec.get('product_name') or ''}{rec.get('recall_date') or ''}"
        rec["recall_id"] = (f"{rec.get('source') or 'X'}-GEN-"
                            f"{hashlib.sha1(_seed.encode()).hexdigest()[:10]}")
        rec["id_generated"] = True
        rec["needs_review"] = True
        # The flag tells the app; this tells the parent. A synthetic id shown as
        # if it were an agency number is worse than none — they search cpsc.gov,
        # find nothing, and reasonably conclude the app invents things. Say what
        # is true instead: the agency published this notice without a number.
        rec["id_note"] = ("The agency published this notice without a recall "
                          "number. This reference is ours, for looking it up in "
                          "this app only.")
    if not rec.get("plain_reason"):
        rec["plain_reason"] = rec.get("hazard_text") or rec.get("reason") or ""
    # FDA records arrive carrying the bare IRES form, so an "is it empty"
    # check never fires on them. Clear it first: a search box with nothing
    # typed in is the same dead end as no link at all.
    if "FDA" in str(rec.get("source", "")) and "index.cfm#" in str(rec.get("url") or ""):
        rec["url"] = ""
    if not rec.get("url"):
        # CPSC publishes a page per recall, slugged from the heading under the
        # FISCAL year — the 2024-10-10 Snuga recall (25-009) lives under /2025/.
        # The bulk CSV carries no url column, so the archive lost every one of
        # them and 4,036 records were pointing at the generic index instead.
        src = str(rec.get("source", "")).split()[0]
        if src == "CPSC":
            # Safety warnings are NOT under /Recalls/. Constructing that path
            # for them produced 404s on all 215 — verified in a browser. CPSC
            # publishes them somewhere else and the CSV carries the announcement
            # text rather than a url, so there is nothing to build from yet.
            # Until the real path is known they go to the index: a browsable
            # list beats a link that 404s.
            if rec.get("record_type") == "warning":
                # CPSC has no per-warning URL. /Recalls carries two tabs —
                # Recalls and Product Safety Warnings — and a warning is only
                # reachable by switching tabs and searching. The tab isn't in
                # the query string, and after being wrong three times today on
                # URL shapes I'd rather not guess a fourth.
                #
                # So: send them to the page that does have it, and say what to
                # do there. Same approach as id_note — the link is honest about
                # what it can and can't do instead of implying a direct landing.
                rec["url"] = AGENCY_URL["CPSC"]
                rec["url_note"] = (
                    "On the CPSC page, open the \u201cProduct Safety Warnings\u201d "
                    f"tab and search {rec.get('recall_id') or 'the warning number'}.")
            else:
                rec["url"] = ((_cpsc_url(rec["recall_id"], rec["heading"])
                               if rec.get("heading") and rec.get("recall_id") else "")
                              or AGENCY_URL["CPSC"])
        elif src == "FDA":
            rec["url"] = _fda_search(rec)
        else:
            rec["url"] = AGENCY_URL.get(src, "")
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
    # Retirement. CPSC never terminates a recall, so status cannot retire
    # anything and a time cut has to do it - but a flat three years removed 729
    # records including 31 that report deaths, among them Rock 'n Play (about a
    # hundred infants), Kids2 Rocking Sleepers, Boppy and IKEA MALM. Those are
    # the records the history backfill was done to obtain.
    #
    # Three exceptions, in order of how sure we are:
    #   a reported death   evidence the hazard already materialised
    #   tier <= 2          can kill in one ordinary use
    #   a durable family   the product is still in houses and still resold
    #
    # What retires is what has genuinely gone: 135 sleepwear (outgrown),
    # 57 personal care and 35 medications (used up), 13 household chemicals.
    # One line the collapsed card can actually show, derived not invented.
    rec["action_short"] = short_action(rec.get("action"))
    # Same prefix problem in the explanation line.
    rec["plain_reason"] = (hazard_line(rec.get("plain_reason"))
                           or rec.get("plain_reason") or "")
    # CPSC runs enforcement sweeps, so one hazard arrives as dozens of near
    # identical records from different firms: 83 magnet, 133 sleepwear, 27 drain
    # covers. Ranking puts them together correctly and the feed then shows
    # eleven magnetic ball sets in a row, which reads as a broken list rather
    # than as eleven separate recalls. These are NOT duplicates and must not be
    # merged - different firms, different products. The app can collapse them
    # visually; the data just has to say which ones belong together.
    rec["cluster_key"] = f'{rec.get("hazard") or "general"}|{rec.get("category_family") or "Other"}'
    rec["durable"] = tcat.is_durable(rec.get("category_family"))
    _age_days = _days_since(rec.get("recall_date"))
    rec["feed_retired"] = bool(
        _age_days is not None and _age_days > 1095
        and not (rec.get("deaths_reported") or 0)
        and (rec.get("tier") or 9) > 2
        and not rec["durable"])
    if not rec.get("brand"):
        rec["brand"] = brand_from_title(rec.get("product_name") or rec.get("heading") or "")
    if not rec.get("product_name"):
        rec["product_name"] = rec.get("heading") or rec.get("display_name") or rec.get("brand") or ""
    # Match fields, built the same way sync_recalls.build_match_fields does.
    # My Brands filters on these, and the personalised tab that appears ahead of
    # "All" is driven entirely by them — a record missing match_words simply
    # never reaches a parent who follows that brand.
    # The heading goes into the haystack. CPSC titles a recall "IKEA Recalls
    # MALM Chests and Dressers" and names the product "Children's and adult
    # chests and dressers" — so the brand appears only in the heading, and a
    # parent following IKEA never saw the largest furniture tip-over recall in
    # history: 29 million units, four child deaths.
    b = (rec.get("brand") or "").lower().strip()
    _head = (rec.get('heading') or '').lower()
    text = f"{b} {(rec.get('product_name') or '').lower()} {_head}".strip()
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
    if not ptype and 'FDA' in str(rec.get('source', '')):
        # The FDA recall number prefix encodes the issuing center, so a record
        # that never carried product_type still says what it is: Z- device,
        # D-/H- drug, F- food, C- cosmetic. Without this, "Maquet BEQ-TOP MCV
        # INFANT ECC" and a King Systems laryngoscope fell through to text
        # matching and landed in Feeding.
        # Centre code from the recall number. Requires letter-hyphen-digit:
        # matching the first character alone read the datatables hash prefix
        # `dt-` as `D` and filed all 25 press-release records as drugs —
        # Gerber teething sticks, baby powder, ground cinnamon, infant
        # formula and apple puree among them.
        #
        # H- is food, not drug. Every H- record is formula, baby food,
        # bottled water or food colouring.
        _m = re.match(r'^([A-Z])-\d', str(rec.get('recall_id') or '').upper())
        ptype = {'Z': 'Devices', 'D': 'Drugs', 'H': 'Food',
                 'F': 'Food', 'C': 'Cosmetics'}.get(_m.group(1) if _m else '', '')
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
        # The federal standard a recall cites names the category better than the
        # product title does. "Zigjoy Toddler Sleep Sacks with Feet" reads like
        # bedding and is cited under 16 CFR 1615/1616 — the children's sleepwear
        # flammability standard — which is what it actually is. Same for blanket
        # sleepers and hooded robes.
        _hz = str(rec.get('hazard_text') or rec.get('reason') or '')
        if re.search(r"children'?s?\s+sleepwear", _hz, re.I):
            fam = 'Sleepwear & apparel'
        else:
            fam = (tcat.family(tc.key(blob))
                   or tcat.family_from_description(tc.key(str(rec.get('reason') or '')[:300]))
                   )
        # the full-pattern fallback on hazard prose is gone: it put
        # "Walk-Behind Mowers" in Feeding on a word in its defect text.
        # DEFINITIVE-only above is the whole fallback now.
        fam = (fam
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
    # Deaths already reported. Read before the tier is finalised, because a
    # recall that has killed a child cannot sit at tier 4 no matter what the
    # hazard vocabulary matched — Rock 'n Play was `general` at tier 4 with
    # roughly a hundred infant deaths, described in words the table didn't hold.
    _d = tinc.deaths(rec.get('incidents_text'), rec.get('reason'), rec.get('heading'))
    _i = tinc.injuries(rec.get('incidents_text'), rec.get('reason'))
    rec['deaths_reported'] = _d or None
    rec['injuries_reported'] = _i or None
    tier = tinc.tier_floor(tier, _d)
    # FDA Class I is the one agency grade that means what tier 1 means:
    # "a reasonable probability that use of, or exposure to, the product will
    # cause serious adverse health consequences or death". 363 records carry it
    # and 359 of them were below tier 1 - 111 at tier 4, 26 at tier 6, 5 at
    # tier 8, i.e. the app was saying "check when you get to it" about a product
    # the FDA says can kill. Our hazard vocabulary missing the word does not
    # outrank the agency saying so.
    #
    # Class II and III get NO mapping, and the reason is that the numbers lining
    # up is a coincidence of counting, not a shared meaning. Class II is
    # "temporary or medically reversible, or the probability of serious
    # consequences is remote" and Class III is "not likely to cause adverse
    # health consequences" - those measure medical reversibility, while tier
    # measures how fast a parent has to move. Mapping II->2 would lift 1,379
    # records into ACT NOW, and III->3 would put "cookies may contain mold" and
    # an undeclared allergen there too, which is backwards: III is the mildest
    # class there is.
    #
    # The rule this follows: an agency statement may only RAISE urgency, never
    # lower it. Same shape as the death floor above.
    if str(rec.get('classification') or '').strip() == 'Class I':
        tier = 1
    # A death floored the tier but the notice used no hazard word, so the card
    # would say "Serious risk" where a teether says "Choking risk". The family
    # names the mechanism where it is not in doubt; elsewhere `general` stands.
    if _d:
        hz = th.backfill_fatal_hazard(hz, fam)
    rec['hazards'] = hz
    rec['hazard'] = th.primary_of(hz)
    rec['tier'] = tier
    rec['band'] = th.BAND.get(tier, 'CHECK')
    rec.setdefault('record_type', 'recall')
    # Two conditions, not one. The name promises "in the feed's scope", and the
    # feed has always had an age scope as well as a date window: the pipeline
    # deliberately keeps bicycles, scooters, youth apparel and tip-restraints out
    # of a 0-4 feed while leaving them searchable.
    #
    # sync_recalls computes that narrowing, and this function was overwriting it
    # with a bare date check — putting 51 records back into the feed that the
    # narrowing had removed. Archive records never had it computed at all, so
    # doing it here is also what makes the two halves consistent.
    # Feed scope is the date window and nothing else.
    #
    # There used to be an age narrowing here — bicycles, scooters, youth gear
    # out of a 0-4 feed — and it was wrong three times in a row. "SEGMART
    # Toddler Trampolines" left the feed because `trampoline` outvoted
    # `toddler`; "Schwinn Bicycle Child Carriers" left it while sitting in the
    # Strollers & carriers family. Each fix added a rule to correct the previous
    # rule, and each new rule found its own counter-example.
    #
    # A single word cannot decide who a product is for, and `priority_rank`
    # already answers the question better: `age_band` puts infant products above
    # `child` ones inside every tier, continuously, with no way to be wrong in
    # the direction that hides something. A youth bike helmet ranks low and is
    # visible; a toddler trampoline ranks high. Ordering beats exclusion.
    _d = str(rec.get('recall_date') or rec.get('date') or '')[:4]
    rec['in_feed_scope'] = (_d >= '2016') if _d.isdigit() else True
    rec.setdefault('deaths_reported', None)
    rec.setdefault('injuries_reported', None)

    # Who it is for and whether the child touches it. Hazard tier says how bad;
    # these say how close to this parent's baby. They only break ties inside a
    # tier — "Still active · serious risk" holds over a thousand tier 1-3
    # records currently ordered by nothing but date.
    audience_text = f"{name} {rec.get('heading') or ''}"
    rec['age_band'] = taud.age_band(audience_text, fam)
    rec['direct_use'] = taud.direct_use(audience_text, fam)
    # Lexicographic, agency-shaped: tier, then what already happened, then
    # whether anyone is coming, then how likely it is in this house, then who
    # would use it. See tinysafe_severity for why these are ordered and not
    # multiplied.
    rec['priority_rank'] = tsev.severity_rank(
        tier=rec['tier'],
        deaths=rec.get('deaths_reported') or 0,
        record_type=rec.get('record_type'),
        remedy_type=rec.get('remedy_type'),
        units=rec.get('units'),
        age_band=rec['age_band'],
        direct_use=rec['direct_use'],
        family=rec.get('category_family'),
        name=rec.get('product_name') or '')
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

    # Re-curation removes records that now match an EXCLUSION. It does not
    # re-test for signals.
    #
    # The distinction matters: the archive was curated from the source CSV with
    # the full Description field, and stores only a subset of that text. Asking
    # "does this still show a child signal?" against less text than the original
    # decision had drops records for missing information rather than for failing
    # the rule — six CPSC safety warnings went that way, including two sling
    # carriers and a set of water beads, all of which pass on the full text.
    #
    # An exclusion match is different: it is positive evidence that arrived
    # later, and it stays reliable on the reduced text.
    before = len(out)
    kept = []
    for r in out:
        if r.get('source') == 'NHTSA':
            kept.append(r)
            continue
        blob = tc.key(f"{r.get('product_name') or ''} {r.get('brand') or ''} "
                      f"{r.get('heading') or ''}")
        if tc.EXCLUDE.search(blob) or tc.PRODUCE.search(tc.clean(
                f"{r.get('product_name') or ''} {r.get('heading') or ''}")):
            continue
        if tc._adult_equipment(r.get('product_name') or ''):
            continue
        kept.append(r)
    if len(kept) != before:
        print(f'  re-curated: dropped {before - len(kept)} records that now match '
              f'an exclusion')
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
    tc.load_brands(f'{repo}/brand_list.json')
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
        # `urgent_rank` removed on Rork's confirmation that nothing app-side
        # decodes it. It was a pure derivative anyway - it equals
        # `tier if tier <= 3 else 99` on all 6,235 records, so it carried no
        # information the tier did not, and sorting on it tied every tier-1
        # record together. `is_urgent` stays: it is decoded and load-bearing for
        # Most-urgent membership, the banner count and the personal urgent lists.
        'hazard', 'action', 'plain_reason', 'is_urgent', 'reason',
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
        'durable', 'feed_retired', 'brands', 'action_short', 'cluster_key',
        'related_ids', 'related_reason',
        # The heading was dropped from the app payload on all 6,256 records while
        # sitting complete on 4,042 in the archive, and three separate problems
        # were unsolvable without it: the brand for 16-204 (the company is named
        # only here), the adult/child split on bed rails (`JOKOSIS Adult Portable
        # Bed Rails` in the heading, `JOKOSIS Portable Bed Rails` in the product
        # name), and every search that read display_name and found no company.
        # It is the authoritative source string; deriving from it and then
        # discarding it means no later rule can ever check its own work.
        'heading',
        # Units matter as much as deaths and cover far more records: a recall of
        # four million cribs and one of two hundred differ by four orders of
        # magnitude in whether this parent owns it. Deaths say the hazard
        # happened; units say whether it happened near you.
        'deaths_reported', 'injuries_reported', 'units',
        'id_generated', 'id_note', 'url_note',
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

    # Only stamp `updated` when the data actually moved.
    #
    # The file is written minified, so it is one line to git — a changed
    # timestamp alone stores a fresh 14 MB blob with no delta possible. Two of
    # every three daily runs were doing exactly that: "1 insertion(+), 1
    # deletion(-)" and a new pack object for nothing. Comparing the record list
    # rather than the envelope lets the workflow's `git diff --cached --quiet`
    # do its job.
    def _previous(path):
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return None

    # Preserve the previous `updated` when nothing moved. recalls_unified.json is
    # minified to a single line, so a changed timestamp alone stores a fresh
    # 14 MB blob that git cannot delta-compress — 7/26 and 7/27 were both
    # "1 insertion, 1 deletion" for exactly that reason.
    prev_records = prev_updated = None
    try:
        with open(f'{repo}/recalls_unified.json', encoding='utf-8') as f:
            prev = json.load(f)
        prev_records = prev.get('recalls')
        prev_updated = prev.get('updated')
    except Exception:
        pass

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
    prev = _previous(f'{repo}/recalls_unified.json')
    if prev and prev.get('recalls') == app:
        # byte-identical record set — keep the old timestamp so the file is
        # unchanged and the commit step skips it
        meta['updated'] = prev.get('updated', meta['updated'])
        meta['version'] = prev.get('version', meta['version'])
        print('  no record changes — timestamp preserved, file unchanged')
    if prev_records is not None and prev_updated and prev_records == app:
        meta['updated'] = prev_updated
        print('  no record changes — keeping the previous updated stamp so the '
              'commit stays empty')
    link_related(recs)
    # Emit in severity order. The array was in merge order (58.8% of adjacent
    # pairs ascending, i.e. none), so any client that renders file order, or
    # that sorts on a key with ties, fell back to an arbitrary sequence. Sorting
    # here costs nothing and means the file is correct even for a reader that
    # does no sorting at all. Ties break on the newer recall first.
    # THE SORT CONTRACT. The app re-sorts on every refresh, so this ordering is
    # not what the screen shows - but the two must agree, or a diff of the file
    # stops being a way to check what the app will do, and two orderings that
    # drift apart are how "it looks unsorted" becomes impossible to diagnose.
    #
    # This was ordering ties OLDEST first while the app orders them NEWEST
    # first, and it had no warning term at all. Now identical to the app:
    #
    #   1  priority_rank ascending   tier, then deaths, then contact, then age,
    #                                then no-remedy, then units
    #   2  warnings before recalls   a warning means the firm refused; there is
    #                                no refund and nobody is coming
    #   3  recall_date descending    the newer notice first
    #   4  recall_id                 so the file is byte-stable between runs and
    #                                an unchanged record set produces no commit
    app.sort(key=lambda r: (
        r.get('priority_rank') or 99999,
        0 if r.get('record_type') == 'warning' else 1,
        _neg_date(r.get('recall_date')),
        str(r.get('recall_id') or '')))
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
