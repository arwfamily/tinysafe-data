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


# UTF-8 bytes read as Latin-1, and combining accents left with nothing to
# combine with. "LullaBear\u00e2\u201e\u00a2" is a trademark sign that went through the
# wrong decoder; "LDLXLHTE' \u0304Crib Bumpers" is a stray macron. Legitimate
# accented text \u2014 S\u00e9fralls, Gr\u00e3o de Gente, Jan\u00e9 \u2014 must survive untouched, so
# this repairs known sequences rather than stripping non-ASCII.
_MOJIBAKE = [
    ('\u00e2\u201e\u00a2', '\u2122'),
    ('\u00e2\u20ac\u2122', '\u2019'),
    ('\u00e2\u20ac\u0153', '\u201c'),
    ('\u00e2\u20ac\u009d', '\u201d'),
    ('\u00e2\u20ac\u201c', '\u2013'),
    ('\u00e2\u20ac\u201d', '\u2014'),
    ('\u00c2\u00ae', '\u00ae'),
    ('\u00c2\u00a9', '\u00a9'),
    ('\u00c2\u00a0', ' '),
]
# a combining mark following a space or quote has nothing to sit on
# The mark can also follow a letter it never belonged to; requiring a space
# or quote before it missed "LDLXLHTE' \u0304Crib". Any combining mark not
# preceded by a letter has nothing to modify.
_ORPHAN_MARK = re.compile("(?<![A-Za-z\u00c0-\u024f])[\u0300-\u036f]+")


def repair_text(s):
    # NFC first. "LullaBear\u00e2\u201e\u00a2" arrives DECOMPOSED - `a` + U+0302 + U+201E +
    # U+00A2 - so the table's precomposed key (U+00E2) never matched, and
    # _ORPHAN_MARK could not strip the mark either, because a letter precedes
    # it. Composing first turns `a`+U+0302 into U+00E2 and the existing
    # ('\u00e2\u201e\u00a2', '\u2122') entry then fires. One line, no new table entry.
    t = unicodedata.normalize('NFC', str(s or ''))
    for bad, good in _MOJIBAKE:
        if bad in t:
            t = t.replace(bad, good)
    return _ORPHAN_MARK.sub('', t)


# CPSC's notices are scraped from HTML and the anchor tags did not survive: the
# href stayed, truncated, and the link TEXT was pushed out past a leftover
# attribute. 1,908 field values across hazard_text, action and sold_at carry the
# debris, and the sentence a parent reads comes out as
#
#   "violate the mandatory safety standard for
#    https://www.cpsc.gov/Business--Manufacturing/Busines... lang="EN-US">children's
#    sleepwear, posing a risk of burn injury or death to children."
#
# Nothing is lost - the link text is still there, on the far side of the junk.
# So this removes the href and the orphaned attributes and KEEPS the text,
# recovering "children's sleepwear", "Virginia Graeme Baker Pool and Spa Safety
# Act (VGBA)" and "toys". A rule that deleted the whole span would throw away
# the only words that say which standard was violated.
# The scraped href can contain whitespace where the source HTML wrapped a long
# URL - "https://www.cpsc.gov/Business-- Manufacturing/Business-Education/...".
# Matching \S+ stopped at that space and left the second half of the URL sitting
# in the sentence as though it were a word: "the mandatory safety standard for
# Manufacturing/Business-Education/Busines...". The debris always terminates at
# the orphaned `>`, so consume up to it rather than up to the first space.
_LINK_DEBRIS = re.compile(
    r'\s*https?://[^>]{0,400}?'
    r'(?:&gt;|>)\s*', re.I)
# A url with nothing after it. Allow the same wrapped-whitespace shape, but stop
# at a sentence boundary so ordinary prose after a bare link is never eaten.
_BARE_URL = re.compile(
    r'\s*\(?\s*https?://(?:\S|\s(?=\S*[/.]))*\.{0,3}\s*\)?', re.I)


# A byte that did not survive the scrape, left as U+00BF. NFC cannot reach it -
# nothing was decomposed, the character is simply gone. Context says what it was:
#   attached to a word   Similac\u00bf Advance, Neocate\u00bf, Panda\u00bf iRes  -> \u00ae or \u2122
#   standing alone       "\u00bf fl. oz", "\u00bf in. Tubing"                    -> a fraction
#
# The trademark case is REMOVED rather than guessed at. \u00ae and \u2122 are
# indistinguishable here, they carry nothing a parent needs, and the app types
# display_name straight into an fda.gov search - where "Similac\u00bf" matches
# nothing and "Similac" matches the recall. Removing is both safer and the fix.
#
# The fraction case is LEFT ALONE: \u00bd, \u00bc and \u00be are equally likely and
# dropping it would delete a quantity. 6 records, flagged rather than damaged.
_TM_LOST = re.compile(r'(?<=[A-Za-z0-9])\u00bf(?=[\s,.;:)\]/-]|$)')


def strip_lost_symbol(s):
    return _TM_LOST.sub('', str(s or ''))


def strip_markup(s):
    """Drop scraped-HTML debris without dropping the words it swallowed."""
    t = str(s or '')
    t = strip_lost_symbol(t.replace('&nbsp;', ' '))
    t = _LINK_DEBRIS.sub(' ', t)      # href + orphaned attributes, link text kept
    t = _BARE_URL.sub(' ', t)         # a url that had no text after it
    t = re.sub(r'\s+([,.;:])', r'\1', t)
    return re.sub(r'\s{2,}', ' ', t).strip()


# CPSC's remedy sentence names the company when the heading does not:
# "Consumers should ... contact Beestech for a full refund". 155 records were
# left with an empty brand after the heading rule because the heading names no
# firm; 66 of them name one here. The remaining 89 genuinely name nobody -
# "the firm has been uncooperative", "contact the retailer" - and stay empty.
_FIRM_WORD = r"[A-Za-z0-9][\w&.'\u2019/-]*"
_FIRM_STOP = (r'(?:the|them|us|your|customer|consumers?|a|an|any|all|retailer|'
              r'retailers|store|stores|place|point)\b')
_FIRM_PATS = [
    re.compile(r'\bcontact\s+(?!' + _FIRM_STOP + r')((?:' + _FIRM_WORD + r')(?:\s+' + _FIRM_WORD + r'){0,3})'),
    re.compile(r'\breturn\s+(?:it|them|the\s+\w+)\s+to\s+(?!' + _FIRM_STOP + r')'
               r'((?:' + _FIRM_WORD + r')(?:\s+' + _FIRM_WORD + r'){0,3})', re.I),
    re.compile(r'\bCPSC\s+and\s+((?:' + _FIRM_WORD + r')(?:\s+' + _FIRM_WORD + r'){0,3}?)\s+(?:urge|are|have)', re.I),
    re.compile(r'^((?:' + _FIRM_WORD + r')(?:\s+' + _FIRM_WORD + r'){0,3}?)\s+is\s+(?:unable|no longer|not)', re.I),
    re.compile(r'\bcall\s+(?!' + _FIRM_STOP + r')((?:' + _FIRM_WORD + r')(?:\s+' + _FIRM_WORD + r'){0,3})'),
]
_FIRM_TAIL = re.compile(
    r'\s+(?:for|at|toll|by|to|online|via|through|between|Monday|from|or|and|'
    r'regarding|about|customer|collect|immediately|of|on|with|free)\b.*$', re.I)
_FIRM_BAD = re.compile(r'^(?:the|a|an|this|that|it|they|we|you|cpsc|fda|consumers?|retailers?)$', re.I)


def firm_from_action(action):
    """Company named in the remedy sentence, or ''. Case-insensitive on the
    first letter: iMOONZZZ and phil&teds are real brands and an [A-Z] anchor
    silently dropped them."""
    a = str(action or '').replace('&nbsp;', ' ').replace('&amp;', '&')
    for pat in _FIRM_PATS:
        m = pat.search(a)
        if not m:
            continue
        f = _FIRM_TAIL.sub('', m.group(1)).strip(' ,.;:')
        f = ' '.join(f.split()[:4])
        if f and len(f) > 1 and not _FIRM_BAD.match(f):
            return f
    return ''


def clean(s) -> str:
    """Human-readable normalised text: entities gone, smart punctuation flattened."""
    s = strip_markup(repair_text(s))
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
  bab(?:y|ies)|infant|newborn|neonat\w*|p(?:a)?ediatric|preemie|premature\s+infant
 |toddler|preschool|nursery|juvenile
 |crib|bassinet|cradle|play\s?yard|playard|pack\s?and\s?play|playpen|moses\s?basket
 |stroller|pushchair|pram|buggy|travel\s?system
 |car\s?seat|child\s?restraint|booster\s?seat
 |high\s?chair|hook\s?on\s?chair|feeding\s?(?:pillow|seat)|bottle\s?warmer|breast\s?pump
 |pacifier|soother|teether|teething|sippy|bib
 |infant\s+formula|baby\s+formula|follow[\s-]?on\s+formula|toddler\s+formula
 |diaper|nappy|swaddle|onesie|romper|bodysuit
 |bouncer|jumper|exersaucer|activity\s?(?:center|centre|gym)|baby\s?swing|infant\s?swing
 |sling|wrap\s?carrier|soft\s?carrier|baby\s?carrier|water\s?bead|step\s?stool|learning\s?tower
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
   prosecco|champagne|\bwine\b|whisk(?:e)?y|vodka|liqueur|brewing
 | patio\s+(?:chair|furniture|set)|dining\s+set|slow\s+cooker|coffee\s?maker
 | saut[eé]\s+pan|candle\s?holder|electroplasma|entertainment\s+stand|\bmugs?\b
 | handheld\s+fan|butane|utility\s+lighter
 | pet\s+food|dog\s+food|cat\s+food|puppy|kitten|canine|feline|equine|bovine
 | veterinary|livestock|poultry\s+feed|animal\s+feed|bird\s+seed
 | breeder|foster\s+care|milk\s+replacer|colostrum\s+supplement
 | goat\s+milk\s+formula|lamb\s+milk|calf|calves|foal|piglet|swine
 | \bnursing\s+(?:puppies|kittens|animals)|baby\s+birds?|baby\s+macaws?|hand[\s-]?feeding|chew\s+toy\s+for
 | all[\s-]?terrain\s+vehicle|\batvs?\b|utvs?|off[\s-]?highway|side\s?by\s?side|snowmobile|snow\s?bike|dirt\s?bike
 | gun\s+safe|firearm|ammunition|rifle|handgun
 | pressure\s+washer|air\s+compressor|chainsaw|table\s+saw|miter\s+saw|nail\s?gun|welder|generator
 | boiler|furnace|water\s+heater|hvac|air\s+conditioner
 | elevator|escalator|forklift|scaffold|ladder\s+jack
 | work\s+boots?|steel\s+toe|power\s+strips?|surge\s+protectors?|extension\s+cords?|dissolved\s+oxygen|test\s+kits?|reagent|crossbows?|spear\s?gun|patio\s+chair|lift\s+chairs?|off[\s-]?road\s+utility|recliner\s+(?:sofa|chair)|av\s+cart|audiovisual\s+cart
 | motorcycle|moped|\be[\s-]?bike|electric\s+bicycle|hoverboard|e[\s-]?scooter
 | lawn\s?mower|lawn\s?tractor|riding\s?mower|\bengine\b|zero\s?turn\s?mower| dehumidifier|treadmill|exercise\s+bike|rowing\s+machine
)(?:s|es)?\b""", re.X | re.I)


# --------------------------------------------------------------------------
# "baby" as a size, not an audience. Baby spinach, baby carrots and baby back
# ribs are adult groceries; a produce recall is not a children's product recall
# just because the cultivar is small. This lived in the old pipeline as
# PRODUCE_RE and was lost when curation moved out of it — an FDA enforcement
# extract keyed on the word "baby" surfaced the gap immediately.
PRODUCE = re.compile(r"""
    baby[-\s]+(?:\w+[-\s]+)?(?:spinach|arugula|rocket|kale|bok\s?choy|carrots?|greens?|romaine|
              broccoli|corn|peas?|bhindi|okra|bella|mushrooms?|lettuces?|
              spring\s?mix|cut\s+vegetables?|potatoes?|beets?|turnips?|squash|
              portabella|portobello|cucumbers?|onions?|tomatoes?|
              swiss|monterey|jack|gouda|edam|brie|cheeses?)\b
  | baby\s+back\s+ribs?
  | spring\s+mix|mixed\s+greens|salad\s+(?:kit|mix|blend)|vegetable\s+tray|veggie\s+tray
""", re.X | re.I)

# ...but a puree, pouch or infant cereal genuinely is baby food, and several of
# those name the same vegetables. Rescue on the form of the product.
BABYFOOD = re.compile(r"""
    puree|pouch|baby\s+food|infant\s+(?:food|cereal|formula)|baby\s+cereal
  | \d+\s?(?:oz|ounce)\s?cups?
  | yobaby|plum\s+organics|gerber|beech-?nut|earth.?s\s+best|happy\s?baby
  | sprout\s+organic|once\s+upon\s+a\s+farm|cerebelly|tippy\s+toes
  | good\s?&?\s?gather\s+baby|h-?e-?b\s+baby
""", re.X | re.I)


# --------------------------------------------------------------------------
# Signal 4 — a known baby brand.
#
# "Dr. Brown's Natural Bottle & Dish Soap" carries no child word in its name and
# its hazard text is about bacteria, not children, so all three signals above
# miss it. It is a baby-bottle soap from a baby-bottle company, recalled by CPSC
# in 2017, and it was absent from the store entirely until Rork went looking.
#
# brand_list.json already holds 179 curated baby brands with normalised keys.
# Loaded lazily and optional — this module has to keep working standalone.
_BRANDS = None


def load_brands(path='brand_list.json'):
    """Populate the known-baby-brand signal. Safe to call more than once."""
    global _BRANDS
    try:
        import json
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        # Multi-word names only. A single-word brand is not evidence: Gerber
        # makes baby food and also machetes, Allure is a hair-dryer brand,
        # Kirkland is on the Prosecco. Two words is what makes a name
        # distinctive enough to stand alone as a signal — "Dr. Brown's" and
        # "Tommee Tippee" identify a baby company; "Indigo" identifies nothing.
        _BRANDS = {b['sq'] for b in data
                   if b.get('sq') and len(b['sq']) > 6
                   and len(str(b.get('name', '')).split()) >= 2}
    except Exception:
        _BRANDS = set()
    return len(_BRANDS or ())


def _brand_hit(text):
    """Whole-name match only.

    Substring matching against a squashed string was far too loose — it admitted
    Prosecco bottles, desktop heaters and multizone amplifiers. A brand has to
    appear as a contiguous run of whole words, so "Dr. Brown's" matches
    "Dr. Brown's Natural bottle and dish soap" and nothing matches by accident.
    """
    if _BRANDS is None:
        load_brands()
    if not _BRANDS:
        return False
    words = key(text).split()
    if not words:
        return False
    # squash each window of 1..4 words and test the whole thing against the set
    for n in range(1, 5):
        for i in range(len(words) - n + 1):
            if ''.join(words[i:i + n]) in _BRANDS:
                return True
    return False


# Adult mobility equipment. Not a general "adult" rule — deciding on that word
# alone dropped 653 records, 647 of them children's products whose FDA text says
# "for adult use" or "adults and children", plus an organic baby sleep drop that
# matched `elder\w*` through *elderberry*.
#
# This is one product class, and every record it removes was read: 23 branded
# "Adult Portable Bed Rails" plus a Medline bed assist bar and a Vaunn bed
# assist rail. They reach CPSC's children's feed because a child can be
# entrapped in one, but the product is adult mobility equipment and a parent
# scanning for their baby should not be reading it.
#
# "Children's and adult chests and dressers" — the IKEA recall that killed
# children — must survive, which is why the rule needs both the adult word and
# the equipment word, and why `children` vetoes it.
ADULT_EQUIPMENT = re.compile(
    # adult + a mobility term, in either order
    r'\badults?\b(?=[^.]{0,40}?\b(?:bed\s?rail|bed\s?assist|grab\s?bar|'
    r'transfer\s?(?:bench|board)|commode|rollator|mobility))|'
    r'\b(?:bed\s?assist|bed\s?rail)s?\b(?=[^.]{0,40}?\badults?\b)|'
    # and the terms that are adult equipment on their own — "LumaRail Bed
    # Assist Rails" and "Bed Assist Handles" name no adult and still are one.
    r'\bbed\s?assist\b|\bgrab\s?bars?\b|\brollators?\b|\bcommodes?\b|'
    r'\btransfer\s?(?:bench|board)e?s?\b', re.I)

# Adult consumables a child never uses directly. These are NOT curated out:
# 21-759 is recalled under the Child Nicotine Poisoning Prevention Act, so the
# record exists because of children even though the product is for adults.
# Deleting it answers the wrong question. Rank answers the right one - a product
# an infant uses directly outranks one where the child is the second-order
# victim - and the severity model already has that axis in `contact`, where an
# adult consumable is `environmental` and sinks on its own.
ADULT_CONSUMABLE = re.compile(
    r'\bliquid nicotine\b|\be.?liquid\b|\bvape\b|\be.?cigarettes?\b|'
    r'\bnicotine (?:pouch|salt)', re.I)
CHILD_VETO = re.compile(r"\bchild(?:ren)?(?:'?s)?\b|\binfants?\b|\bbab(?:y|ies)\b|\btoddlers?\b", re.I)


# CPSC states the audience in the HEADING, not the product name. Keying on the
# product name removed 24 bed rails and left 23 of the same class in, because it
# was reading the manufacturer's naming habit:
#
#   product_name  JOKOSIS Portable Bed Rails
#   heading       JOKOSIS ADULT Portable Bed Rails Recalled Due to ...
#
# The heading decides 44 of 47 unambiguously. It is also the field the pipeline
# was discarding, which is why this looked undecidable.
_HEAD_ADULT = re.compile(r"\badults?\b", re.I)
_HEAD_CHILD = re.compile(r"\bchildren'?s?\b|\btoddlers?\b|\binfants?\b|\bbab(?:y|ies)\b", re.I)


def _adult_equipment(product, heading=''):
    """Adult mobility equipment. Audience read from the heading where it exists."""
    name = str(product or '')
    head = str(heading or '')
    if not ADULT_EQUIPMENT.search(name) and not ADULT_EQUIPMENT.search(head):
        return False
    if _HEAD_CHILD.search(head) and not _HEAD_ADULT.search(head):
        return False
    if _HEAD_ADULT.search(head):
        return True
    if CHILD_VETO.search(name):
        return False
    return bool(ADULT_EQUIPMENT.search(name))


# Last resort for the handful of headings that name no audience at all. CPSC's
# own notices are consistent about this: the adult standard is written about
# "users", the children's standard about "children". Applied only after the
# heading has been read and found silent, and every record it decides is listed
# in the build output so the call can be checked rather than trusted.
_MOBILITY_CLASS = re.compile(
    r'\bbed\s?rails?\b|\bbed\s?assist\b|\bgrab\s?bars?\b|\brollators?\b|'
    r'\bcommodes?\b|\btransfer\s?(?:bench|board)', re.I)
_VICTIM_ADULT = re.compile(r'\busers?\b', re.I)
_VICTIM_CHILD = re.compile(
    r"\bchild(?:ren)?'?s?\b|\binfants?\b|\bbab(?:y|ies)\b|\btoddlers?\b", re.I)


def adult_equipment_undecided(product, heading, hazard_text):
    """True when the heading is silent and the hazard prose says `users`."""
    head = str(heading or '')
    if _HEAD_ADULT.search(head) or _HEAD_CHILD.search(head):
        return False
    # Wider gate than ADULT_EQUIPMENT: a bare "OasisSpace Bed Rails" names no
    # adult word anywhere, which is exactly the case this fallback exists for.
    if not _MOBILITY_CLASS.search(str(product or '') + ' ' + head):
        return False
    t = str(hazard_text or '')
    return bool(_VICTIM_ADULT.search(t)) and not _VICTIM_CHILD.search(t)


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
    if _adult_equipment(product, heading):
        return False, [], 'adult mobility equipment'
    if adult_equipment_undecided(product, heading, f'{description} {hazard}'):
        return False, [], 'adult mobility equipment (victim language)'
    if not sig and _brand_hit(f'{product} {heading}'):
        sig.append('known_brand')

    if not sig:
        return False, [], None

    # produce check before the exclusion list: "baby spinach" carries a real
    # child signal (the word baby) that means nothing about the audience
    name_text = clean(f'{product} {heading}')
    if PRODUCE.search(name_text) and not BABYFOOD.search(name_text):
        return False, sig, 'produce'


    # exclusions checked last, and never override an explicit child standard
    if 'standard' not in sig:
        m = EXCLUDE.search(f'{name_blob} {key(hazard)}')
        if m:
            return False, sig, m.group(0)

    return True, sig, None
