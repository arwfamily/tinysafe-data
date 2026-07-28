"""
TinySafe — curation.

Decides which agency records concern children's products.

Three independent signals, per TINYSAFE_DATABASE_ARCHITECTURE.md §4. A record is
in scope if ANY fires. Signal 2 (who the hazard text says is at risk) is the
highest-value one and is the reason a product-name keyword filter alone is
wrong: CPSC routinely states the victim in the hazard sentence even when the
product name is generic.

Exclusions apply only to things a child cannot reach, and are checked last so
they can never override an explicit child-standard citation.
"""
import re
import unicodedata

# --------------------------------------------------------------------------
# text normalisation. CPSC uses U+2019, curly double quotes, en/em dashes and
# leaves &nbsp; in place. Normalising here means every downstream comparison
# and every stored value is clean.
# --------------------------------------------------------------------------
_MOJI = {'\u00e2\u20ac\u2122': "'", '\u00e2\u20ac\u0153': '"', '\u00e2\u20ac\u009d': '"',
         '\u00e2\u20ac\u201c': '-', '\u00e2\u20ac\u201d': '-', '\u00e2\u20ac\u00a6': '...',
         '\u00c3\u00a9': 'e', '\u00c2\u00a0': ' ', '\u00c2': '', '\u00e2\u20ac': "'"}

_ENT = {'&nbsp;': ' ', '&amp;': '&', '&quot;': '"', '&#39;': "'", '&rsquo;': "'"}


def clean(s) -> str:
    """Human-readable normalised text: entities gone, smart punctuation flattened."""
    s = '' if s is None else str(s)
    for k, v in _MOJI.items():
        s = s.replace(k, v)
    for k, v in _ENT.items():
        s = s.replace(k, v)
    s = unicodedata.normalize('NFKD', s)
    s = (s.replace('\u2019', "'").replace('\u2018', "'")
          .replace('\u201c', '"').replace('\u201d', '"')
          .replace('\u2013', '-').replace('\u2014', '-')
          .replace('\u00a0', ' ').replace('\u202f', ' '))
    s = ''.join(c for c in s if unicodedata.category(c)[0] != 'C' or c in '\n\t')
    return ' '.join(s.split())


def key(s) -> str:
    """Match key: lowercase, apostrophes removed entirely, punctuation to space."""
    return ' '.join(re.sub(r'[^a-z0-9 ]', ' ', clean(s).lower().replace("'", '')).split())


# --------------------------------------------------------------------------
# signal 1 — the product is a children's item by name
# --------------------------------------------------------------------------
S1_PRODUCT = re.compile(r"""\b(
  bab(?:y|ies)|infant|newborn|toddler|preschool|nursery|juvenile
 |crib|bassinet|cradle|play\s?yard|playard|pack\s?and\s?play|playpen|moses\s?basket
 |stroller|pushchair|pram|buggy|travel\s?system
 |car\s?seat|child\s?restraint|booster\s?seat
 |high\s?chair|hook\s?on\s?chair|feeding\s?(?:pillow|seat)|bottle\s?warmer|breast\s?pump
 |pacifier|soother|teether|teething|sippy|bib|formula
 |diaper|nappy|swaddle|onesie|romper|bodysuit
 |bouncer|jumper|exersaucer|activity\s?(?:center|centre|gym)|baby\s?swing|infant\s?swing
 |walker|baby\s?gate|safety\s?gate|bed\s?rail|changing\s?table|nursing\s?pillow
 |bath\s?seat|potty|training\s?pants
 |child(?:ren)?s?|kids?|youth|boys?|girls?
 |toy|doll|plush|stuffed\s?animal|rattle|teddy
 |sleepwear|pajama|pyjama|nightgown|sleep\s?sack|sleeper
)(?:s|es)?\b""", re.X)

# --------------------------------------------------------------------------
# signal 2 — the hazard text names the victim. Highest value signal.
# CPSC writes these constructions constantly:
#   "posing a choking hazard to young children"
#   "if ingested by children"
#   "can result in serious injuries or death to children"
#   "an infant could fall out of the enclosed opening"
# --------------------------------------------------------------------------
S2_VICTIM = re.compile(r"""(
   (?:hazards?|risks?|dangers?|injur\w+|deaths?|fatal\w*|harm\w*)\b[^.]{0,80}?\b
     (?:to|for|among)\s+(?:young\s+|small\s+)?(?:child|children|infant|infants|babies|baby|toddler|toddlers|kids|minors)
 | \b(?:child|children|infant|infants|babies|baby|toddler|toddlers|kids|young\s+children)\b[^.]{0,60}?
     \b(?:can|could|may|might)\b[^.]{0,60}?
     \b(?:swallow|ingest|choke|suffocat|strangl|entrap|drown|fall|slip|access|climb|reach|become)
 | (?:swallowed|ingested|inhaled|mouthed)\s+by\s+(?:young\s+)?(?:child|children|infant|infants|babies|toddlers|kids)
 | \b(?:child|children|infant|toddler|baby|babies|kids)\b[^.]{0,50}?
     \b(?:entrap|entangl|strangl|suffocat|asphyxiat|drown)
 | (?:choking|strangulation|suffocation|entrapment|ingestion|aspiration|entanglement)\s+hazards?\b[^.]{0,60}?
     \b(?:child|children|infant|toddler|baby|babies|kids)
 | \b(?:tip[\s-]?over)\b[^.]{0,80}?\b(?:child|children)
)""", re.X | re.I)

# --------------------------------------------------------------------------
# signal 3 — the record cites a child-specific federal standard. If any of
# these appear, the product is regulated *as* a children's product, whatever
# it happens to be called.
# --------------------------------------------------------------------------
S3_STANDARD = re.compile(r"""(
   child(?:ren)?s?\s+sleepwear | sleepwear\s+flammability
 | small\s+parts?\s+(?:ban|rule|regulation|requirement) | small\s+ball\s+ban
 | lead\s+paint\s+(?:ban|limit|standard)
 | reese\W?s?\s+law | button\s+cell | coin\s+batter
 | poison\s+prevention\s+packaging | child[\s-]?resistant\s+(?:packaging|closure)
 | safe\s+sleep\s+for\s+babies | crib\s+bumper\s+ban | inclined\s+sleeper
 | sturdy\s+act | clothing\s+storage\s+unit | tip[\s-]?restraint
 | virginia\s+graeme\s+baker | vgba
 | infant\s+sleep\s+products? | crib\s+mattress(?:es)? | bassinets?\s+and\s+cradles?
 | (?:standard|regulation|requirement)s?\s+for\s+(?:toys|play\s?yards?|strollers?|infant\s+walkers?|high\s+chairs?|bath\s+seats?|bed\s+rails?|carriers?|hook[\s-]?on\s+chairs?)
 | toy\s+safety\s+standard | astm\s+f96[35] | astm\s+f3096 | 16\s?cfr\s?170[05]
 | child(?:ren)?s?\s+gasoline\s+burn\s+prevention
 | child\s+care\s+articles?
 | (?:high[\s-]?powered|loose|separable|rare[\s-]?earth|neodymium)\s+magnets?
 | magnet\s+sets?\b | 16\s?cfr\s?1262 | magnet(?:ic)?\s+(?:ball|bead|stone|cube)s?
)""", re.X | re.I)

# --------------------------------------------------------------------------
# exclusions — only things a child cannot plausibly reach or operate.
# Deliberately narrow. Over-exclusion is the dangerous error in a safety app.
# --------------------------------------------------------------------------
EXCLUDE = re.compile(r"""\b(
   all[\s-]?terrain\s+vehicle|\batvs?\b|utvs?|off[\s-]?highway|side\s?by\s?side|snowmobile|snow\s?bike|dirt\s?bike
 | gun\s+safe|firearm|ammunition|rifle|handgun
 | pressure\s+washer|air\s+compressor|chainsaw|table\s+saw|miter\s+saw|nail\s?gun|welder|generator
 | boiler|furnace|water\s+heater|hvac|air\s+conditioner
 | elevator|escalator|forklift|scaffold|ladder\s+jack
 | work\s+boots?|steel\s+toe|power\s+strips?|surge\s+protectors?|extension\s+cords?|dissolved\s+oxygen|test\s+kits?|reagent|crossbows?|spear\s?gun|patio\s+chair|lift\s+chairs?|off[\s-]?road\s+utility|recliner\s+(?:sofa|chair)|av\s+cart|audiovisual\s+cart
 | motorcycle|moped|\be[\s-]?bike|electric\s+bicycle|hoverboard|e[\s-]?scooter
 | lawn\s?mower|lawn\s?tractor|riding\s?mower|\bengine\b|zero\s?turn\s?mower| dehumidifier|treadmill|exercise\s+bike|rowing\s+machine
)\b""", re.X | re.I)


def curate(product='', heading='', description='', hazard='', extra=''):
    """
    Returns (in_scope: bool, signals: list[str], excluded_by: str|None).

    Signals are reported so the borderline pile can be reviewed by eye and the
    rule tuned against real decisions rather than intuition.
    """
    name_blob = key(f'{product} {heading} {description}')
    haz_blob = clean(f'{hazard} {description} {heading} {extra}')
    all_blob = f'{name_blob} {key(haz_blob)}'

    sig = []
    if S1_PRODUCT.search(name_blob):
        sig.append('product')
    if S2_VICTIM.search(haz_blob):
        sig.append('victim')
    if S3_STANDARD.search(all_blob):
        sig.append('standard')

    if not sig:
        return False, [], None

    # exclusions checked last, and never override an explicit child standard
    if 'standard' not in sig:
        m = EXCLUDE.search(f'{name_blob} {key(hazard)}')
        if m:
            return False, sig, m.group(0)

    return True, sig, None
