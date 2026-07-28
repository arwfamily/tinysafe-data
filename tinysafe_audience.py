"""
TinySafe — who the product is for, and whether the child touches it.

The feed sorts on hazard tier, which answers "how bad" but not "how close to my
baby". Two records at tier 1 — an infant lounger and a youth bicycle helmet —
are not equally urgent to a parent of a six-month-old.

Two axes, both derived from the product rather than guessed from the hazard:

  age_band     infant (0-2) · toddler (2-4) · child (4+) · any
  direct_use   does the child handle, wear, eat, or sit in it

`direct_use` is not a proxy for danger. Furniture tip-over and blind cords are
environmental and they kill toddlers; they stay in the feed at their real tier.
The axis exists so that among equally severe records, the ones a baby is in
contact with today surface first.
"""
import re

# --------------------------------------------------------------------------
# Age. Infant terms are specific enough to be reliable; "child" and "youth"
# products are the ones this app is least about, so they sort last.
# --------------------------------------------------------------------------
INFANT = re.compile(r"""\b(
    infant|newborn|neonat\w*|preemie|premature|bab(?:y|ies)|nursing|nursery
  | crib|cribs|bassinet|cradle|moses\s?basket|co.?sleeper|swaddle|sleep\s?sack
  | lounger|napper|bouncer|rocker|swing|play\s?yard|playard|pack\s?and\s?play
  | formula|bottle|nipple|pacifier|soother|teether|teething|breast\s?pump
  | diaper|changing\s?table|wipe|bib
  | infant\s?(?:seat|carrier|walker)|carrier|sling|wrap
  | rattle|mobile|activity\s?gym|play\s?mat|tummy\s?time
)\b""", re.X | re.I)

TODDLER = re.compile(r"""\b(
    toddler|high\s?chair|highchair|booster|hook.?on\s?chair|sippy
  | potty|training\s?pants|step\s?stool|toddler\s?bed|bed\s?rail
  | stroller|pushchair|pram|convertible\s?(?:car\s?)?seat
  | walker|ride.?on|push\s?toy|wagon
)\b""", re.X | re.I)

OLDER = re.compile(r"""\b(
    youth|junior|teen|adolescent|school.?age
  | bicycle|bike|tricycle|scooter|skateboard|helmet|trampoline
  | \bage[sd]?\s*(?:[4-9]|1[0-9])\b
)\b""", re.X | re.I)

# --------------------------------------------------------------------------
# Contact. Everything a child wears, eats, mouths, sleeps in or rides in.
# --------------------------------------------------------------------------
DIRECT = re.compile(r"""\b(
    crib|cribs|bassinet|cradle|lounger|napper|mattress|bedding|blanket|sheet|
    sleep\s?sack|swaddle|pillow|bumper
  | bottle|nipple|pacifier|soother|teether|teething|sippy|cup|plate|bowl|spoon|
    fork|utensil|bib|formula|food|puree|pouch|cereal|snack|supplement|vitamin|
    drop|syrup|tablet|capsule|ointment|cream|lotion|shampoo|wash|powder|wipe|
    toothpaste|toothbrush|sunscreen
  | high\s?chair|highchair|booster|car\s?seat|restraint|stroller|carrier|sling|
    wrap|walker|bouncer|swing|rocker|jumper|exersaucer|play\s?yard|playard
  | toy|doll|plush|rattle|block|puzzle|figure|magnet|bead
  | pajama|sleepwear|clothing|apparel|shirt|pant|dress|coat|jacket|hat|glove|
    sock|shoe|sandal|boot|jewelry|necklace|bracelet|helmet
  | bath|swim|float|potty|diaper
)\b""", re.X | re.I)

# Families that are environmental by definition, whatever the title says.
ENVIRONMENTAL_FAMILIES = {
    'Nursery furniture & tip-over',
    'Window coverings & cords',
    'Household chemicals',
    'Lighters & fire hazards',
    'Gates, rails & childproofing',
    'Nursery electricals & monitors',
    'Medical devices',
}

# Families whose whole point is an infant, regardless of wording.
INFANT_FAMILIES = {
    'Sleep — cribs, bassinets, loungers',
    'Sleep — mattresses & bedding',
    'Feeding & high chairs',
    'Food & formula',
    'Oral care & teething',
    'Car seats & travel',
    'Strollers & carriers',
    'Walkers, swings & bouncers',
    'Skincare, bath & diapering',
}

AGE_RANK = {'infant': 0, 'toddler': 1, 'any': 2, 'child': 3}


def age_band(text, family=None):
    t = text or ''
    if family in INFANT_FAMILIES:
        return 'infant'
    if INFANT.search(t):
        return 'infant'
    if TODDLER.search(t):
        return 'toddler'
    if OLDER.search(t):
        return 'child'
    return 'any'


# Contact is a property of the product family, not of whether the title happens
# to contain the word "toy". "Oitnlaughter LED Finger Lights" is something a
# child holds; text matching alone called it environmental, along with 452 of
# the 963 records in Toys.
DIRECT_FAMILIES = {
    'Toys', 'Outdoor & play equipment', 'Walkers, swings & bouncers',
    'Sleepwear & apparel', 'Jewelry & accessories',
    'Sleep — cribs, bassinets, loungers', 'Sleep — mattresses & bedding',
    'Feeding & high chairs', 'Food & formula', 'Oral care & teething',
    'Car seats & travel', 'Strollers & carriers', 'Helmets & wheeled toys',
    'Bath & water safety', 'Skincare, bath & diapering',
    'Medications & supplements', 'Personal care & medicine',
    'Button cells & batteries',
}


def direct_use(text, family=None):
    """Does the child handle, wear, eat or sit in it."""
    if family in ENVIRONMENTAL_FAMILIES:
        return False
    if family in DIRECT_FAMILIES:
        return True
    return bool(DIRECT.search(text or ''))


# The app's comparator relies on `priority_rank // 100 == tier`. That holds
# because the age and contact terms can never sum to 100: age contributes at
# most 3*10 = 30 and contact at most 5, so the largest possible addition is 35.
# Asserted rather than assumed — if a future band is added past index 9 the
# invariant breaks silently and severity ordering goes wrong everywhere.
MAX_ADJUSTMENT = max(AGE_RANK.values()) * 10 + 5
assert MAX_ADJUSTMENT < 100, (
    f"priority() adjustment {MAX_ADJUSTMENT} would overflow the tier decade; "
    "the app sorts on rank // 100 == tier")


def priority(tier, band, direct):
    """Sort key for the feed. Lower is more urgent.

    Hazard tier leads — severity is not negotiable and a tier-1 environmental
    record still outranks a tier-3 one a baby holds. Age and contact only break
    ties inside a tier, which is where the feed actually needs help: 'Still
    active · serious risk' holds over a thousand records at tier 1-3 and they
    are currently ordered by nothing but date.
    """
    return (tier or 9) * 100 + AGE_RANK.get(band, 2) * 10 + (0 if direct else 5)
