"""
TinySafe — how urgent is this recall, to this parent, today.

The three agencies that matter here converge on the same three questions:
CPSC's hazard prioritisation asks severity × likelihood × exposure; FDA's
Class I definition turns on "reasonable probability of serious adverse health
consequences or death"; NHTSA weighs defect against safety risk.

**They do not multiply them.** A product of the three lets four million
mislabelled units outrank two hundred that can suffocate an infant, and no
regulator ranks that way. The structure is lexicographic: severity decides,
and everything else only breaks ties inside it.

The order below, and why each level sits where it does:

    1  tier            what the hazard can do
    2  deaths          what it has already done
    3  no remedy       whether anyone is coming
    4  units           whether it is likely in this house
    5  audience        whether this child would use it
    6  recency         whether it is likely still in this house

**Deaths sit inside tier rather than above it** because a reported death already
floors the tier to 1 (`tinysafe_incidents.tier_floor`). Evidence and estimate
are different kinds of claim — "can kill" versus "has killed" — and the floor is
where that distinction is enforced. Ranking deaths again above tier would
double-count it.

**No remedy comes before exposure** because a CPSC safety warning means the firm
refused to recall: there is no refund, no repair, and nobody will contact the
parent. A widely-sold product with a remedy has a system working on it. One
without has only the parent.

**Units comes before audience** because it is the better-covered signal —
89% of tier 1 versus 20% for deaths — and because ownership probability spans
four orders of magnitude, while audience spans a handful of bands.

**Recency is last and only breaks exact ties.** A 2019 recall that can still
suffocate an infant outranks a labelling defect from last week; the sections
already carry the time dimension.
"""
import re

# --------------------------------------------------------------------------
# Bands. Each is coarse on purpose — a rank is a sort key, not a score, and
# false precision in the middle of a scale reads as meaning that isn't there.
# --------------------------------------------------------------------------

def death_band(deaths):
    """0 = 10+ · 1 = 3-9 · 2 = 1-2 · 3 = none reported.

    Three bands collapsed 7 deaths and 100 into one, which put a magnetic ball
    set above Kids2 Rocking Sleepers (15 infant deaths). A death toll in double
    figures is a different fact from three.
    """
    if not deaths:
        return 3
    if deaths >= 10:
        return 0
    return 1 if deaths >= 3 else 2


def remedy_band(record_type, remedy_type):
    """0 = nothing is coming · 1 = a remedy exists.

    A safety warning is issued *because* the firm refused to recall. `Dispose`
    says the same thing in the remedy field: there is nothing to return it for.
    """
    if record_type == 'warning':
        return 0
    return 0 if str(remedy_type or '').strip().lower() == 'dispose' else 1


_UNITS = re.compile(r'([\d][\d,]*)')


def unit_count(units_text):
    """Leading figure from CPSC's "About 5,918" phrasing. 0 when unstated."""
    m = _UNITS.search(str(units_text or ''))
    if not m:
        return 0
    try:
        return int(m.group(1).replace(',', ''))
    except ValueError:
        return 0


def exposure_band(units):
    """0 = 1M+ · 1 = 100k+ · 2 = 10k+ · 3 = 1k+ · 4 = under 1k or unstated.

    Unstated sits with the smallest rather than the largest: claiming wide
    exposure on a missing field would push unknown records above known ones.
    """
    if units >= 1_000_000:
        return 0
    if units >= 100_000:
        return 1
    if units >= 10_000:
        return 2
    if units >= 1_000:
        return 3
    return 4


AUDIENCE = {'infant': 0, 'toddler': 1, 'any': 2, 'child': 3}

# How the product reaches the child. `direct_use` was binary — touched or not —
# which put a contaminated infant formula and a chest of drawers in the same
# band. A parent doesn't read those as equivalent, and they aren't: something
# swallowed acts on the whole body and acts tonight, while a dresser is
# dangerous only in a particular moment.
CONTACT = {
    'ingested': 0,        # formula, food, medicine, supplements
    'mouthed': 1,         # teethers, pacifiers, bottles, small-part toys
    'worn': 2,            # clothing, sleepwear, slept-on surfaces, car seats
    'handled': 3,         # toys and gear the child holds
    'environmental': 4,   # furniture, blinds, chemicals, monitors
}

INGESTED_FAMILIES = {
    'Food & formula', 'Medications & supplements', 'Personal care & medicine',
}
MOUTHED_FAMILIES = {
    'Oral care & teething', 'Feeding & high chairs', 'Toys',
}
WORN_FAMILIES = {
    'Sleepwear & apparel', 'Jewelry & accessories', 'Car seats & travel',
    'Sleep — cribs, bassinets, loungers', 'Sleep — mattresses & bedding',
    'Skincare, bath & diapering', 'Helmets & wheeled toys',
}
HANDLED_FAMILIES = {
    'Strollers & carriers', 'Walkers, swings & bouncers',
    'Outdoor & play equipment', 'Bath & water safety',
    'Button cells & batteries',
}


def contact_band(family, direct_use=True):
    """How close the product gets. Lower is closer."""
    if family in INGESTED_FAMILIES:
        return CONTACT['ingested']
    if family in MOUTHED_FAMILIES:
        return CONTACT['mouthed']
    if family in WORN_FAMILIES:
        return CONTACT['worn']
    if family in HANDLED_FAMILIES:
        return CONTACT['handled']
    return CONTACT['handled'] if direct_use else CONTACT['environmental']


def audience_band(age_band, direct_use, family=None):
    """0-19. Age leads; how the product reaches the child separates within it."""
    return AUDIENCE.get(age_band, 2) * 5 + contact_band(family, direct_use)


# --------------------------------------------------------------------------
# The key
# --------------------------------------------------------------------------
# Scale: tier occupies its own thousand, and everything below it sums to at
# most 297, so `rank // 1000 == tier` holds with room for another band later.
#
# This changes the app-side invariant from `// 100` to `// 1000`. That is a
# coordinated change, not a silent one — the old scale had no headroom left
# (incidents 40 + age 30 + contact 5 = 75 of 100) and adding units would have
# pushed it over, which breaks the comparator quietly rather than loudly.
# Deaths outrank remedy: a recall with fifteen dead infants is more urgent
# than a warning with none, whatever the remedy situation. Remedy still
# outranks exposure — nobody coming beats widely sold.
# Each weight must exceed the full range of everything below it, or two
# different records collapse onto the same number and the decomposition lies.
# Grading contact 0-19 broke this at the old weights: audience 15 was
# indistinguishable from exposure 1 plus audience 5.
#
#   audience  0-19   -> exposure step must be >= 20
#   exposure  0-80   -> remedy step must be >= 100
#   remedy    0-100  -> death step must be >= 200
#   deaths    0-600  -> tier step 1000 clears it
DEATH_W, REMEDY_W, EXPOSURE_W = 200, 100, 20
MAX_ADJUSTMENT = 3 * DEATH_W + 1 * REMEDY_W + 4 * EXPOSURE_W + 19  # 799
assert MAX_ADJUSTMENT < 1000, (
    f'adjustment {MAX_ADJUSTMENT} would overflow the tier thousand; '
    'the app sorts on rank // 1000 == tier')


def severity_rank(tier, deaths=0, record_type='recall', remedy_type=None,
                  units=None, age_band='any', direct_use=True, family=None):
    """Sort key for one record. Lower is more urgent."""
    return ((tier or 9) * 1000
            + death_band(deaths) * DEATH_W
            + remedy_band(record_type, remedy_type) * REMEDY_W
            + exposure_band(unit_count(units)) * EXPOSURE_W
            + audience_band(age_band, direct_use, family))


def explain(rank):
    """Decompose a rank. For checking a screenshot against the model rather
    than arguing about what the number meant."""
    tier, rest = divmod(rank, 1000)
    d, rest = divmod(rest, DEATH_W)
    r, rest = divmod(rest, REMEDY_W)
    e, a = divmod(rest, EXPOSURE_W)
    d, r, e = min(d, 3), min(r, 1), min(e, 4)
    return {
        'tier': tier,
        'deaths': ['10+', '3-9', '1-2', 'none'][d],
        'remedy': ['none — dispose or warning', 'remedy exists'][r],
        'exposure': ['1M+', '100k+', '10k+', '1k+', '<1k or unstated'][e],
        'audience': f"{['infant', 'toddler', 'any', 'child'][min(a // 5, 3)]}, "
                    f"{['ingested', 'mouthed', 'worn', 'handled', 'environmental'][min(a % 5, 4)]}",
    }
