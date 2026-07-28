"""
TinySafe — hazard derivation.

Two deliberate departures from the pipeline this replaces.

1. ALL matches are emitted, not the first one. The old deriver ran 18 regexes in
   a fixed order and kept whichever fired first, so a record reading "posing
   entrapment and strangulation hazards" got exactly one of them depending on
   list order. 179 of 827 records matched two or more patterns, which means the
   ordering was silently deciding severity for a fifth of the feed.

2. Severity is computed from the match set at read time, never frozen at ingest.
   A record stores its hazard list; the tier is a function of that list. When the
   table changes, nothing needs backfilling.

Tier semantics, from the filter spec: 1-3 ACT NOW, 4-5 ACT SOON, 6-8 CHECK,
9 unranked. Unmapped hazards resolve to 4, not 9 — an unrecognised hazard is
unknown, not minor, and defaulting it to the bottom would bury it.
"""
import re

# --------------------------------------------------------------------------
# Patterns. Order is irrelevant — every one that matches is recorded.
# Written against the vocabulary actually present in the CPSC + FDA corpus,
# not an invented taxonomy.
# --------------------------------------------------------------------------
PATTERNS = {
    # tier 1 — kills quickly and often silently, usually while unattended
    'suffocation':   r'suffocat|asphyxiat|smother|obstruct\w*\s+(?:the\s+)?(?:infant|child|baby)?\s*\w*\s*breath|unable to breathe',
    'strangulation': r'strangulat|strangl',
    'entrapment':    r'entrap|become trapped|get trapped|wedge\w*\s+between|pinned',
    'drowning':      r'drown',
    'botulism':      r'botulism|clostridium botulinum',

    # tier 2 — lethal, but the mechanism needs an event or an ingestion
    'magnet':        r'magnet',
    'battery':       r'button cell|coin batter|lithium coin|swallow\w*\s+\w*\s*batter',
    'tipover':       r'tip[\s-]?over|tip\w*\s+forward|unstable if.{0,40}not anchored|topple',
    'bacteria':      r'salmonella|listeria|e\.?\s?coli|cronobacter|staphyloc|bacteri\w+\s+contam',
    'contamination': r'contaminat|foreign (?:material|object|matter|substance|bod)|metal (?:fragment|piece)|plastic fragment|glass (?:fragment|piece)|wood fragment|particulate',

    # tier 3 — act now
    'choking':       r'choking|choke|aspirat|small parts?\s+(?:ban|hazard|violation)|lodge\w*\s+in\s+the\s+throat',
    'fire':          r'\bfires?\b|ignit|combust|flame\s?jett',
    'burn':          r'\bburns?\b|burn injur|scald|thermal burn',
    'flammable':     r'flammab|fail\w*\s+to\s+meet.{0,40}flammability|sleepwear\s+standard',
    'fall':          r'\bfalls?\b|\bfalling\b|fall hazard|collapse|give way',
    'laceration':    r'laceration|amputat|sever\w*\s+the|deep cut|puncture',
    'crash':         r'crash|collision|restraint\s+fail|harness\s+(?:fail|releas)',
    'electrical':    r'electrocut|electric shock|\bshocks?\b\s+hazard|energiz',
    'lead':          r'\blead\b(?!\s+(?:to|in\s+the\s+event))|lead paint|lead poison',

    # tier 4 — act soon
    'chemical':      r'toxic|poison|methanol|ethylene glycol|formaldehyde|phthalat|benzene|corrosive|caustic',
    'entanglement':  r'entangl|wrap\w*\s+around|drawstring',
    'overheat':      r'overheat|excessive heat|thermal (?:event|incident)',
    'mold':          r'\bmold\b|\bmould\b|fungal|mycotoxin|patulin|aflatoxin|yeast',
    # --- FDA vocabulary. The CPSC corpus never produces these, and without
    # them 31% of the live store fell through to 'general'.
    'sanitation':    r'insanitary|unsanitary|rodent|pest (?:activity|infestation)|vermin|filth|sanitation',
    'sterility':     r'steril\w*\s+(?:assurance|failure|process)|non-?sterile|loss of sterility|fail\w*\s+to\s+steriliz|calibration of.{0,40}steriliz|pyrogen|endotoxin',
    'manufacturing': r'\bcgmp\b|good manufacturing practice|manufacturing deviation|process deviation|out of specification|stability failure',
    'device_failure': r'ventilat\w*\s+(?:fail|interrupt|stop)|deliver\w*\s+(?:incorrect|inaccurate)|malfunction|'
                      r'software (?:error|defect|issue)|circuit board|electrode|disconnect|inaccurate (?:reading|measurement|dose)|'
                      r'fail\w*\s+to\s+(?:alarm|alert|deliver|operate)|premature (?:failure|wear)|leak\w*\s+(?:oxygen|gas|air)',
    'unapproved':    r'510\(k\)|premarket (?:clearance|approval|notification)|unapproved (?:drug|device|new drug)|without .{0,20}clearance|not cleared|misbrand|adulterat',
    'carbonmonoxide': r'carbon monoxide',

    # tier 5-8
    'asbestos':      r'asbestos',
    'subpotent':     r'subpotent|superpotent|potency',
    'undeclared':    r'undeclared|allergen|misbrand',
    'labeling':      r'labeling|labelling|missing (?:warning|label)|required (?:warning|marking)',
    'quality':       r'quality|specification|purity',
}
PATTERNS = {k: re.compile(v, re.I) for k, v in PATTERNS.items()}

TIER = {
    'suffocation': 1, 'strangulation': 1, 'entrapment': 1, 'drowning': 1, 'botulism': 1,
    'magnet': 2, 'battery': 2, 'tipover': 2, 'bacteria': 2, 'contamination': 2,
    'crash_protection': 2,          # NHTSA — restraint fails when it is needed
    'choking': 3, 'fire': 3, 'burn': 3, 'flammable': 3, 'fall': 3,
    'laceration': 3, 'crash': 3, 'electrical': 3,
    'lead': 4,            # raised to 3 when the product is mouthable — see below
    'chemical': 4, 'entanglement': 4, 'overheat': 4, 'mold': 4, 'carbonmonoxide': 4,
    'sanitation': 3, 'sterility': 3,          # infection route — act now
    'device_failure': 3,                       # NICU equipment that stops working
    'manufacturing': 4,                        # process failed, hazard unstated
    'unapproved': 6,                           # regulatory, not a physical hazard
    'label_instruction': 4,         # NHTSA — missing instructions cause misinstallation
    'asbestos': 5,
    'subpotent': 6, 'undeclared': 6,
    'labeling': 7,
    'quality': 8,
    'general': UNMAPPED_TIER if False else 4,
}
UNMAPPED_TIER = 4

# Lead is a special case. On something a child puts in their mouth it is an
# immediate ingestion route; on a wall-mounted item it is not. CPSC does not
# make this distinction, so the product family does.
MOUTHABLE = {
    'Feeding & high chairs', 'Toys', 'Jewelry & accessories',
    'Personal care & medicine', 'Household chemicals', 'Button cells & batteries',
}


def derive(hazard_text: str, product_text: str = '', family: str = None):
    """Returns (hazards: sorted list, tier: int)."""
    blob = f'{hazard_text} {product_text}'
    hits = sorted(k for k, p in PATTERNS.items() if p.search(blob))
    if not hits:
        # Named rather than empty: the app has a field to render, and 'general'
        # reads as "recalled for a safety risk we could not classify", which is
        # the truth. Tier stays at 4 — unknown is not minor.
        return ['general'], UNMAPPED_TIER
    tiers = []
    for h in hits:
        t = TIER.get(h, UNMAPPED_TIER)
        if h == 'lead' and family in MOUTHABLE:
            t = 3
        tiers.append(t)
    return hits, min(tiers)


# When two hazards share a tier, the primary is chosen by how specific it is,
# not alphabetically. 'lead' tells a parent more than 'chemical'; picking by
# alphabet reintroduced exactly the arbitrariness that dropping first-match-wins
# was meant to remove (it silently relabelled 12 lead recalls as 'chemical').
SPECIFICITY = ['botulism', 'bacteria', 'contamination', 'lead', 'magnet', 'battery',
               'crash_protection', 'label_instruction',
               'asbestos', 'mold', 'carbonmonoxide', 'drowning', 'suffocation',
               'strangulation', 'entrapment', 'tipover', 'crash', 'electrical',
               'sterility', 'sanitation', 'device_failure',
               'flammable', 'burn', 'fire', 'choking', 'laceration', 'fall',
               'entanglement', 'overheat', 'chemical', 'manufacturing', 'unapproved',
               'subpotent', 'undeclared',
               'labeling', 'quality', 'general']
_RANK = {h: i for i, h in enumerate(SPECIFICITY)}


def primary_of(hazards):
    """Most severe hazard; ties broken by specificity, never by alphabet."""
    if not hazards:
        return 'general'
    return min(hazards, key=lambda h: (TIER.get(h, UNMAPPED_TIER), _RANK.get(h, 99)))


BAND = {1: 'ACT NOW', 2: 'ACT NOW', 3: 'ACT NOW',
        4: 'ACT SOON', 5: 'ACT SOON',
        6: 'CHECK', 7: 'CHECK', 8: 'CHECK', 9: 'CHECK'}
