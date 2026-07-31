"""TinySafe — product family (display category) classification.

20 families derived from the actual product distribution across the full CPSC
archive. Replaces a 9-value list in which "Toys & Gear" held 37% of the feed and
would have exceeded 2,000 records after the full-archive merge.

family() is the only entry point the sync needs; the loaders below are used by
the offline build.
"""
import re, csv, io, json, pandas as pd
from collections import Counter
import tinysafe_curate as tc

# ---------- product families (display categories) ----------
FAM = [
 ('Sleep — cribs, bassinets, loungers', r'crib|bassinet|cradle|lounger|co.?sleeper|bedside sleeper|play ?yard|playard|pack and play|playpen|infant sleep|sleep ?nest|swaddle|travel bassinet|infant support cushion|inclined sleeper|\bsleepers?\b|rocking sleeper|rock ?n ?play|napper|baby nest|moses basket|hammock|baby bed|toddler bed|motion bed|ottoman|nap ?mat|sleeping bag'),
 ('Sleep — mattresses & bedding',       r'crib mattress|mattress pad|mattress topper|fitted sheet|sleep sack|infant pillow|nursing pillow|head support|neck support|baby blanket|weighted blanket|\bblankets?\b|\bquilts?\b|bumper'),
 ('Car seats & travel',                 r'car seat|child restraint|booster seat|travel system|infant carrier|carrier base'),
 ('Strollers & carriers',               r'stroller|pushchair|pram|buggy|sling carrier|soft carrier|baby carrier|toddler carrier|wrap carrier|baby wrap|backpack carrier|child carrier|kid comfort|hiking carrier'),
 ('Feeding & high chairs',              r'high ?chair|booster.{0,12}chair|hook.?on chair|splash seat|sassy seat|clip.?on chair|feeding pillow|self.?feeding|sippy|pacifier|soother|teether|bib|breast pump|baby bottle|nursing bottle|feeding bottle|bottle warmer|bottle brush|bottle steriliz|highchair|spoon|fork|cups?\b|plates?\b|bowls?\b|tableware|dish|utensil|placemat|straw|tumbler|thermos|lunch ?box|water bottle|drinking'),
 ('Bath & water safety',                r'bath seat|bath tub|infant bath|swim float|flotation|swim ring|puddle jumper|pool drain|life jacket|swim vest|bath ring|bath support|above.?ground pool|drain covers?|\bbuckets?\b|\bpails?\b|bath foam|bath bomb|bath toy|bath mat'),
 ('Walkers, swings & bouncers',         r'infant walker|baby walker|walker|swing|bouncer|jumper|rocker|motions? seat|infant seat|activity cent|exersaucer|jumperoo'),
 ('Outdoor & play equipment',       r'\btents?\b|playhouse|play ?house|teepee|tunnel|trampoline|slides?\b|dive stick|pool toy|wheelbarrow|garden set|swing ?set|playground|sandbox|\bpools?\b|inflatable|climbing|harness|bounce house|water table|sprinkler|kite|lawn dart|play ?set|sky wheel|merry.?go.?round'),
 ('Toys',                               r'\btoy|magnet|building (?:block|set|stick)|stacking|plush|doll|rattle|musical instrument|puzzle|ride.?on|water bead|slime|figure|game|squeeze|teddy|bear|animal|figure|play set|tummy time|play ?mat|activity gym|baby gym|fingerpaint|finger ?painting|paint set|craft kit|sticker|play kitchen|toy kitchen|music set|stuffed (?:animal|toy)|yarn|balloon|marble|craft|books?\b|resin|slime|putty|activity kit|science kit|kinetic sand'),
 ('Nursery furniture & tip-over',       r'dresser|clothing storage|chest of drawers|\bchests?\b|cabinet|curio|armoire|hutch|bookcase|book ?shelf|shelving|shelf unit|changing table|tip restraint|furniture strap|anchor|wardrobe|nightstand|step ?stool|toddler tower|learning tower|tower stool|kitchen helper|bunk bed|toy (?:box|chest)|bean ?bag|murphy bed|coffee table|glider|\bfurnitures?\b|\bcribs?\b furniture|televis|\btvs?\b|media console|entertainment cent|av cart|a v cart|audiovisual cart'),
 ('Sleepwear & apparel',                r'pajama|pyjama|sleepwear|nightgown|robe|onesie|romper|bodysuit|jacket|hoodie|drawstring|sweatshirt|tutu|costume|shoe|sandal|boot|dress|legging|swimsuit|slumber suit|sleepsuit|hat|glove|mitten|bib overall|sock|outfit|coverall|kovarall|leg warmer|snowsuit|apparel|clothing|garment|skirt|pajama set|sweater|pullover|fleece|loungewear|slumber suits?|sleepsuits?|snowsuits?|swimsuits?|bathing suits?|coats?\b|jean|shorts?\b|sleeve|jumpsuit|overall'),
 ('Gates, rails & childproofing',       r'bed rail|bed guard|safety gate|baby gate|window guard|corner guard|outlet cover|cabinet (?:lock|latch)|door lock|latch|hinge.?clos|self.?clos|pool gate|childproof|baby.?proof'),
 ('Helmets & wheeled toys',             r'helmet|bicycle|\bbikes?\b|tricycle|scooter|skateboard|training wheel|wagon|go.?kart|sled|roller skate|inline skate|all terrain vehicle|\batvs?\b|youth model'),
 ('Button cells & batteries',           r'button cells?|coin batter|cr20\d\d|lr\d\d|reese|\bbutton batter'),
 ('Nursery electricals & monitors',     r'monitor|night ?light|humidifier|sound machine|wipe warmer|bottle warmer|sterilizer|nursery lamp|projector|speaker|audio player|walkie'),
 ('Jewelry & accessories',             r'jewel|necklace|bracelet|earring|charm|pendant|rings?\b|tiara|hair ?(?:clip|pin|bow|accessor)|sunglass|watch|purse|wallet|keychain|zipper pull'),
 ('Lighters & fire hazards',            r'lighter|matches|candle|torch|fire ?pit|fireplace|lamp oil|torch fuel|flame'),
 ('Window coverings & cords',           r'blind|\bshades?\b|window (?:covering|treatment)|curtain|drapery|cord (?:stop|cleat)|corded|cordless'),
 ('Household chemicals',                r'hydroxide|drain cleaner|paint thinner|solvent|antifreeze|pesticide|cleaner|detergent|bleach|acids?\b|chemical|fuel container|de.?icer|antifreez|countertop|glue|adhesive|serum|minoxidil|hair growth|coating|methanol|gasoline|faucet'),
 ('Food & formula',                    r'infant formulas?|baby formulas?|follow.?on formulas?|toddler formulas?|baby food|infant food|puree|food pouch|cereal|snack|yogurt|\bmilk\b|juice|puff|infant water|nursery water|beverage|probiotic|jelly bean|candy|cinnamon|produce|spinach|bok ?choy|lettuce|fruit|vegetable|applesauce|apple sauce'),
 ('Medications & supplements',         r'medication|acetaminophen|ibuprofen|gripe water|supplement|multivitamin|vitamin|electrolyte|gas relief|colic|nyquil|cough|cold remedy|nasal|antihistamine|allergy|syrup|drops\b|tablet|capsule|zinc oxide|ointment'),
 ('Skincare, bath & diapering',        r'lotion|baby oil|diaper|cream|balm|shampoo|baby wash|bubble bath|powder|moisturiz|eczema|wipe|sunscreen|spf\b|sun ?block'),
 ('Oral care & teething',              r'toothpaste|toothbrush|teething|teether|oral gel|orajel|mouthwash|dental'),
 ('Medical devices',                   r'ventilat\w*|respirator\w*|resuscitat|forceps|blood pressure(?: cuffs?)?|catheter|intubation|neonatal|\bnicu\b|'
                                       r'airvo|humidifier system|oxygen (?:therapy|delivery)|cpap|bipap|'
                                       r'syringe|thermometer|nebuliz|oximet|monitor kit|convenience kit|admission kit|'
                                       r'drainage|tracheal|manometers?\b|stethoscope|infusion|feeding tube|apnea|'
                                       r'surgical|surgery|resection|transection|fixation|femoral|thoracic|abdominal|'
                                       r'endoscop|laparoscop|imaging system|angiograph|electrode|implant|prosthe|'
                                       r'suture|scalpel|retractor|dilator|cannula|stent|shunt|guidewire|'
                                       r'\bartis\b|axiom|sterilization|autoclave|defibrillat|incubator|phototherapy'),
 ('Personal care & medicine',           r'wipe|lotion|shampoo|diaper cream|sunscreen|ointment|syrup|drops|supplement|vitamin|tablet|capsule|toothpaste|essential oil|lidocaine|minoxidil|numbing|anesthetic|topical|balm|sanitizer'),
]
FAM = [(n, re.compile(p, re.I)) for n, p in FAM]


# Words that name the product outright. Length is a decent proxy for specificity
# until it isn't: `vegetable` (9) beat `toy` (3) on "Play With Your Veggies
# toys", and `fruit` (5) beat it on a "Fruit Drink toy gun". A product calling
# itself a toy is a toy, whatever else its name mentions.
DEFINITIVE = [
    # Definitive terms, checked before the longest-match pass. Each of these
    # names the product outright, and each was added because a longer or
    # earlier-listed word was taking the record somewhere wrong:
    #   infant walkers were filing as Toys, window blinds as Jewellery,
    #   nursing pillows as Sleep, board books as Lighters.
    # A cradle swing is a swing, a nursery heater is an appliance, a travel
    # mobile is a toy. All seven were sitting in the crib family because the
    # crib pattern matched first on `cradle`, `nursery` or `crib-side`.
    ('Nursery electricals & monitors', re.compile(r'\bspace heaters?\b|\bheaters?\b(?!\s?proof)', re.I)),
    ('Nursery furniture & tip-over', re.compile(r'\bottomans?\b|\bpoufs?\b|\bdressers?\b|\bchests?\s+of\s+drawers\b', re.I)),
    ('Outdoor & play equipment', re.compile(r'\btents?\b|\bplayhouses?\b|\bteepees?\b', re.I)),
    ('Walkers, swings & bouncers', re.compile(
        r'\b(?:infant|baby)\s+walkers?\b|\bbouncers?\b|\bexersaucers?\b|'
        r'\bcradle\s?[\u2019\']?n?\s?swings?\b|\bhammock\s?swings?\b|\bswings?\b|'
        r'\b(?:baby|infant)\s+swings?\b|\bjumperoos?\b|\bactivity\s+cent(?:er|re)s?\b',
        re.I)),
    ('Window coverings & cords', re.compile(
        r'\bblinds?\b|roman shades?|roller shades?|cellular shades?|'
        r'window (?:covering|treatment|shade)s?', re.I)),
    ('Feeding & high chairs', re.compile(r'nursing pillows?|feeding pillows?', re.I)),
    # What goes IN the crib is not the crib. "Bubble Bear Crib Mattresses" and
    # "SARO Braided Crib Bumpers" both contain `crib`, which was matching first
    # and putting 42 mattresses and bumpers in with the furniture. The bedding
    # word is the specific one; the crib word only says where it goes.
    ('Sleep \u2014 mattresses & bedding', re.compile(
        r'\bmattress(?:es)?\b|\bbumpers?\b|\bbedding\b|\bquilts?\b|\bcomforters?\b|'
        r'\bblankets?\b|\bsheets?\b|\bmattress\s?pads?\b|\bcrib\s?liners?\b|'
        r'\bsleep\s?(?:sacks?|bags?)\b|\bswaddles?\b|\bnappers?\b|\bsnuggle\s?pods?\b',
        re.I)),
    # A sleep surface is what the product IS; plush is what it is made of. The
    # Leachco Podster - "Podster, Podster Plush, Bummzie and Podster Playtime
    # ... Infant Loungers", two infant deaths, tier 1 - filed as Toys because
    # `plush` and `playtime` matched before `lounger` was reached. Everything
    # keyed on family was then wrong for it: contact band, the retirement rule,
    # and the fatal-hazard backfill, which refused to name suffocation because
    # the family said Toys.
    # "Soother" is a pacifier to CPSC's feeding vocabulary and an inclined
    # sleeper to Fisher-Price. 21-147 - Rock 'n Glide Soothers, four infant
    # deaths - filed as Feeding & high chairs because `soother` matched before
    # anything read the rest of the name. Same shape as `tent` inside "Lead
    # Content Ban" and `nicu` inside "manicure": the word is right, the product
    # is not. A glider that rocks is a sleep surface whatever it is called.
    ('Sleep — cribs, bassinets, loungers', re.compile(
        r"\brock\s?'?n?\s?glide\b|\bglide\s?soothers?\b|\bsoothe\s?'?n\s?play\b|"
        r'\bgliders?\b(?=[^.]{0,40}\b(?:soother|sleep|nap|infant|baby)\b)', re.I)),
    ('Sleep — cribs, bassinets, loungers', re.compile(
        r'\bcribs?\b|\bbassinets?\b|\bloungers?\b|\bco.?sleepers?\b|'
        r'\binclined sleepers?\b|\brocking sleepers?\b|\bbaby nests?\b|'
        r'\bnappers?\b|\bmoses baskets?\b|\bplay ?yards?\b|\bplayards?\b', re.I)),
    ('Toys', re.compile(r'\btoys?\b|\bdolls?\b|\bplush\b|stuffed animals?|\bmobiles?\b|'
                        r'\bboard books?\b|fidget|building (?:blocks?|sets?)', re.I)),
    # Appliances before food: "baby food processor" is gear. Order inside this
    # list is the tiebreak, so the narrower rule has to come first.
    ('Feeding & high chairs', re.compile(r'food\s+(?:processor|maker|mill|warmer)|'
                                         r'\bblenders?\b|steriliz|bottle\s+warmers?', re.I)),
    # Feeding & high chairs is gear; Food & formula is what goes in the child.
    # `formula` and `baby food` used to sit in the gear pattern, which is why
    # the two categories read as overlapping — they were.
    ('Food & formula', re.compile(r'infant\s+formulas?|baby\s+formulas?|baby\s+food|'
                                  r'infant\s+(?:food|cereal)|baby\s+cereal', re.I)),
    ('Feeding & high chairs', re.compile(r'\b(?:baby|nursing|feeding)\s+bottles?\b|'
                                         r'\bhigh\s?chairs?\b|\bpacifiers?\b', re.I)),
    ('Car seats & travel', re.compile(r'\bcar\s?seats?\b|child\s+restraints?', re.I)),
    ('Strollers & carriers', re.compile(r'\bstrollers?\b', re.I)),
    ('Lighters & fire hazards', re.compile(r'\blighters?\b', re.I)),
    ('Medications & supplements', re.compile(r'\btablets?\b|\bcapsules?\b|\bsyrup\b', re.I)),
]


# P34 - a parent opened the Teething & oral care chip and found 3 records
# while ~40 teething products sat under Toys (pull-string teething toys),
# Feeding (pacifiers - the word list at the top catches them first),
# Medications (Orajel swabs, Hyland's tablets - the FDA drug centre wins
# over the name) and Food (Gerber teething snacks). The proof the split is
# wrong: the same Gerber Soothe'n'Chew sat in Teething via its press-release
# record and in Food via its enforcement record. Usage beats regulation for
# a parent-facing shelf, with two exceptions that stay put:
#   - button-battery hazard outranks everything (a battery chip that misses
#     a battery product is a safety miss, not a taxonomy miss)
#   - doll/figure accessories are toys - Calico Critters ships "pacifier
#     accessories" for dolls, and a blanket pacifier rule would shelve a
#     doll set with real pacifiers.
_ORAL_SIGNAL = re.compile(
    r'teething|teethers?\b|toothbrush|toothpast|mouthwash|oral care|'
    r'orajel|\bpacifiers?\b|dental floss', re.I)
_ORAL_NOT_DOLL = re.compile(r'\bdolls?\b|figur|playset|animal figures?', re.I)
_ORAL_NOT_BATTERY = re.compile(
    r'button cells?|coin batter|cr20\d\d|lr\d\d|\bbutton batter', re.I)


def _oral_override(text):
    t = str(text or '')
    return (_ORAL_SIGNAL.search(t)
            and not _ORAL_NOT_DOLL.search(t)
            and not _ORAL_NOT_BATTERY.search(t))


def family(text):
    """Most specific match, not the first in list order.

    FAM was scanned top to bottom and the first hit won, so a family's position
    in the list decided the answer — the same first-match-wins failure the
    hazard table was rebuilt to remove, sitting one module over. "Liizousuda
    Paint Thinner" landed in Feeding because Feeding is listed fifth and matched
    `bottle` somewhere in the title, while Household chemicals is fifteenth and
    matched `paint thinner`.

    Longest matched substring wins: a 13-character product term is a stronger
    signal than a 6-character generic one. Ties fall back to list order, which
    is where the deliberate ordering (Bath before Outdoor for pool drains) still
    does its job.
    """
    if _oral_override(text):
        return 'Oral care & teething'
    # A definitive term wins outright — it names the product rather than
    # describing something the product mentions.
    for name, pat in DEFINITIVE:
        if pat.search(text):
            return name

    best, best_len = None, 0
    for n, p in FAM:
        m = p.search(text)
        if m and len(m.group(0)) > best_len:
            best, best_len = n, len(m.group(0))
    return best


def load_cpsc(path='cpsc_clean.csv'):
    df = pd.read_csv(path, low_memory=False)
    df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
    out = [tc.curate(r.get('Name of product'), r.get('Recall Heading'), r.get('Description'),
                     r.get('Hazard Description'), r.get('Remedy')) for _, r in df.iterrows()]
    df['_in'] = [o[0] for o in out]
    df['_sig'] = ['+'.join(o[1]) for o in out]
    df['_exc'] = [o[2] for o in out]
    df['_blob'] = (df['Name of product'].fillna('') + ' ' + df['Recall Heading'].fillna('')).map(tc.key)
    df['_blob2'] = df['Description'].fillna('').str[:400].map(tc.key)
    df['_fam'] = [family(a) or family(b) for a, b in zip(df['_blob'], df['_blob2'])]
    return df


def load_warnings(path='warnings_raw.txt'):
    raw = open(path, encoding='utf-8').read()
    recs = [p.strip() for p in re.split(r'(?m)^(?=\d\d-\d{3},")', raw) if re.match(r'^\d\d-\d{3},"', p)]
    rows = []
    for rec in recs:
        try:
            f = next(csv.reader(io.StringIO(rec.replace('\n', ' '))))
        except Exception:
            f = []
        f = (f + [''] * 20)[:20]
        rows.append(dict(id=f[0], date=f[1], title=f[2], name=f[3], desc=f[4],
                         hazard=f[5], action=f[6], units=f[8], incidents=f[9]))
    w = pd.DataFrame(rows)
    out = [tc.curate(r['name'], r['title'], r['desc'], r['hazard'], r['action']) for _, r in w.iterrows()]
    w['_in'] = [o[0] for o in out]
    w['_sig'] = ['+'.join(o[1]) for o in out]
    w['_exc'] = [o[2] for o in out]
    w['_blob'] = (w['name'] + ' ' + w['title']).map(tc.key)
    w['_blob2'] = w['desc'].str[:400].map(tc.key)
    w['_fam'] = [family(a) or family(b) for a, b in zip(w['_blob'], w['_blob2'])]
    return w


# --------------------------------------------------------------------------
# Groups. 25 families is not browsable; collapsing back to 9 loses the
# distinctions that matter. Groups solve navigation without flattening data.
# --------------------------------------------------------------------------
# Display labels. The stored value stays stable so nothing downstream breaks;
# only what the chip reads changes. "Sleep — cribs, bassinets, loungers" carried
# a prefix that existed to disambiguate it from its group, and with no group
# there is nothing to disambiguate from.
# Product lifetime, not calendar age, decides whether a recall still describes
# something in a house. A 2005 nightgown is gone - the child outgrew it. A 2019
# inclined sleeper is on Facebook Marketplace today, which is the whole reason
# this database carries CPSC history at all. These families circulate secondhand
# for years, so a flat time cut retires exactly the records the app exists for.
DURABLE_FAMILIES = {
    'Sleep \u2014 cribs, bassinets, loungers', 'Sleep \u2014 mattresses & bedding',
    'Car seats & travel', 'Strollers & carriers', 'Nursery furniture & tip-over',
    'Walkers, swings & bouncers', 'Gates, rails & childproofing',
    'Feeding & high chairs', 'Outdoor & play equipment', 'Window coverings & cords',
    'Bath & water safety', 'Nursery electricals & monitors', 'Toys',
    'Helmets & wheeled toys', 'Button cells & batteries',
}


def is_durable(family_name):
    return family_name in DURABLE_FAMILIES


LABELS = {
    # Both members are named on each side so a parent can place a borderline
    # item without guessing: a crib mattress reads as bedding because bedding
    # says "mattresses", and a play yard reads as furniture because furniture
    # says "play yards". The hazard data supports the split - the structures
    # carry entrapment 103 and fall 82, the soft goods carry suffocation 67 -
    # and the stored family strings are unchanged, so contract 3 holds.
    'Sleep — cribs, bassinets, loungers': 'Cribs, bassinets & play yards',
    'Sleep — mattresses & bedding': 'Mattresses, bumpers & bedding',
    'Feeding & high chairs': 'Feeding & high chairs',
    'Outdoor & play equipment': 'Outdoor play',
    'Nursery furniture & tip-over': 'Furniture & tip-over',
    'Nursery electricals & monitors': 'Monitors & electricals',
    'Gates, rails & childproofing': 'Gates & childproofing',
    'Skincare, bath & diapering': 'Skincare & diapering',
    'Medications & supplements': 'Medications',
    'Personal care & medicine': 'Personal care',
    'Window coverings & cords': 'Blinds & cords',
    'Lighters & fire hazards': 'Lighters & fire',
    'Button cells & batteries': 'Button batteries',
    'Helmets & wheeled toys': 'Helmets & wheels',
    'Car seats & travel': 'Car seats',
    'Oral care & teething': 'Teething & oral care',
    'Walkers, swings & bouncers': 'Walkers & swings',
    'Jewelry & accessories': 'Jewelry & accessories',
    'Sleepwear & apparel': 'Clothing & sleepwear',
}


# Grouping stays in the data even though the chips could be flat. The app has a
# working two-row model — row 2 hides unless a group is selected, the second
# "All" reads "All Sleep", and the count invariant holds — so the reasons for
# flattening (a wasted row, a duplicated word) were fixed in the UI rather than
# in the taxonomy. Carrying both `category_family` and `category_group` lets the
# app choose; deleting the group field would have forced the decision through a
# data change, which is the wrong lever.
GROUPS = {
    'Sleep': ['Sleep — cribs, bassinets, loungers', 'Sleep — mattresses & bedding'],
    'Feeding': ['Feeding & high chairs', 'Food & formula'],
    'Out & about': ['Strollers & carriers', 'Car seats & travel', 'Helmets & wheeled toys'],
    'Play': ['Toys', 'Outdoor & play equipment', 'Walkers, swings & bouncers'],
    'Wear': ['Sleepwear & apparel', 'Jewelry & accessories'],
    'Around the home': ['Nursery furniture & tip-over', 'Gates, rails & childproofing',
                        'Window coverings & cords', 'Lighters & fire hazards',
                        'Household chemicals', 'Button cells & batteries',
                        'Nursery electricals & monitors', 'Bath & water safety'],
    'Health': ['Medications & supplements', 'Personal care & medicine',
               'Skincare, bath & diapering', 'Oral care & teething', 'Medical devices'],
}
GROUP_OF = {f: g for g, fams in GROUPS.items() for f in fams}


def group(family_name):
    """Browse group. Unclassified records group as 'Other'."""
    return GROUP_OF.get(family_name, 'Other')


def family_from_description(text):
    """Fallback for records whose name classifies to nothing.

    DEFINITIVE terms only. The full pattern set on hazard prose put
    "Walk-Behind Mowers" and a Maquet infant ECC circuit into Feeding, because
    a description mentioning candy or a bottle in passing is not the same claim
    as a product named after one. A wrong category is worse than Other — Other
    is honest about not knowing.
    """
    for name, pat in DEFINITIVE:
        if pat.search(text):
            return name
    return None


def label(family_name):
    """Chip text. Falls back to the stored name."""
    return LABELS.get(family_name, family_name)


# --------------------------------------------------------------------------
# FDA gives the product type on every enforcement record. Using it beats
# guessing from the description — surgical gowns and compression sleeves were
# landing in "Sleepwear & apparel" because the text classifier only sees words.
# --------------------------------------------------------------------------
FDA_TYPE_FAMILY = {
    "Devices": "Medical devices",
    "Drugs": "Medications & supplements",
    "Biologics": "Medications & supplements",
    "Cosmetics": "Skincare, bath & diapering",
    # Was None, "too broad — fall through to the text classifier", which was
    # wrong on its own terms: formula, purees, cereal and snacks all land in
    # Food & formula, so there was nothing for the fall-through to disambiguate.
    # What it actually did was hand FDA food records to the text matcher, which
    # filed Bright Red Food Colour Powder as a Toy and "Organic BABY bedtime
    # drops" — an ingestible — under Sleep, because "bedtime" outvoted the
    # product. Fifth appearance of a word-level rule beating what the source
    # already stated.
    "Food": "Food & formula",
    "Veterinary": None,  # excluded upstream anyway
}


def family_for_fda(product_type, text):
    """Family for an FDA enforcement record. Product type wins where it is
    specific; food falls through because it spans formula, purees and snacks."""
    if _oral_override(text):
        return 'Oral care & teething'
    forced = FDA_TYPE_FAMILY.get((product_type or "").strip())
    if forced:
        return forced
    return family(text) or "Other"


# --------------------------------------------------------------------------
# Chip order for the single-row model.
#
# Not by record count — that puts Toys (935) first and Cribs & bassinets (292)
# ninth, which is backwards for a parent of a newborn. Ordered by how central
# the category is to the 0-4 years this app is for: where the baby sleeps and
# what they eat first, then how they travel, then what they play with and wear,
# then the environmental hazards, then medical last.
#
# Within a horizontal scroll the first four or five chips are what most parents
# ever see, so those four positions are the whole decision.
# --------------------------------------------------------------------------
CHIP_ORDER = [
    # the baby's own day
    'Sleep — cribs, bassinets, loungers',
    'Sleep — mattresses & bedding',
    'Feeding & high chairs',
    'Food & formula',
    'Oral care & teething',
    # getting around
    'Car seats & travel',
    'Strollers & carriers',
    'Walkers, swings & bouncers',
    # what they wear and hold
    'Sleepwear & apparel',
    'Toys',
    'Outdoor & play equipment',
    'Bath & water safety',
    # on the body
    'Skincare, bath & diapering',
    'Medications & supplements',
    'Personal care & medicine',
    # the room around them — smaller, and deadly out of proportion to size
    'Nursery furniture & tip-over',
    'Gates, rails & childproofing',
    'Window coverings & cords',
    'Button cells & batteries',
    'Household chemicals',
    'Lighters & fire hazards',
    'Monitors & electricals',
    'Nursery electricals & monitors',
    'Helmets & wheeled toys',
    'Jewelry & accessories',
    # hospital equipment — excluded from All, last when browsed directly
    'Medical devices',
    'Other',
]
_ORDER = {name: i for i, name in enumerate(CHIP_ORDER)}


def chip_order(family_name):
    """Position in the single-row chip scroll. Unknown families sort last."""
    return _ORDER.get(family_name, len(CHIP_ORDER))
