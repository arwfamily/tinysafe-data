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
 ('Feeding & high chairs',              r'high ?chair|booster.{0,12}chair|hook.?on chair|feeding pillow|self.?feeding|bottle|sippy|pacifier|soother|teether|bib|breast pump|formula|baby food|highchair|spoon|fork|cups?\b|plates?\b|bowls?\b|tableware|dish|utensil|placemat|snack|\bcand(?:y|ies)\b|straw|tumbler|thermos|lunch ?box|water bottle|drinking'),
 ('Bath & water safety',                r'bath seat|bath tub|infant bath|swim float|flotation|swim ring|puddle jumper|pool drain|life jacket|swim vest|bath ring|bath support|above.?ground pool|\bbuckets?\b|\bpails?\b|bath foam|bath bomb|bath toy|bath mat'),
 ('Walkers, swings & bouncers',         r'infant walker|baby walker|walker|swing|bouncer|jumper|rocker|motions? seat|infant seat|activity cent|exersaucer|jumperoo'),
 ('Outdoor & play equipment',       r'tent|playhouse|play ?house|teepee|tunnel|trampoline|slides?\b|dive stick|pool toy|wheelbarrow|garden set|swing ?set|playground|sandbox|\bpools?\b|inflatable|climbing|harness|bounce house|water table|sprinkler|kite|lawn dart|play ?set|sky wheel'),
 ('Toys',                               r'\btoy|magnet|building (?:block|set|stick)|stacking|plush|doll|rattle|musical instrument|puzzle|ride.?on|water bead|slime|figure|game|squeeze|teddy|bear|animal|figure|play set|tummy time|play ?mat|activity gym|fingerpaint|finger ?painting|paint set|craft kit|sticker|play kitchen|toy kitchen|music set|stuffed (?:animal|toy)|yarn|balloon|marble|craft|books?\b|resin|slime|putty|activity kit|science kit|kinetic sand'),
 ('Nursery furniture & tip-over',       r'dresser|clothing storage|chest of drawers|\bchests?\b|cabinet|curio|armoire|hutch|bookcase|book ?shelf|shelving|shelf unit|changing table|tip restraint|furniture strap|anchor|wardrobe|nightstand|step ?stool|toddler tower|learning tower|tower stool|kitchen helper|bunk bed|toy (?:box|chest)|bean ?bag|murphy bed|coffee table|glider|\bfurnitures?\b|\bcribs?\b furniture|televis|\btvs?\b|media console|entertainment cent|av cart|a v cart|audiovisual cart'),
 ('Sleepwear & apparel',                r'pajama|pyjama|sleepwear|nightgown|robe|onesie|romper|bodysuit|jacket|hoodie|drawstring|sweatshirt|tutu|costume|shoe|sandal|boot|dress|legging|swimsuit|slumber suit|sleepsuit|hat|glove|mitten|bib overall|sock|outfit|coverall|kovarall|leg warmer|snowsuit|apparel|clothing|garment|skirt|pajama set|sweater|pullover|fleece|loungewear|slumber suits?|sleepsuits?|snowsuits?|swimsuits?|bathing suits?|coats?\b|jean|shorts?\b|sleeve|jumpsuit|overall'),
 ('Gates, rails & childproofing',       r'bed rail|bed guard|safety gate|baby gate|window guard|corner guard|outlet cover|cabinet (?:lock|latch)|door lock|latch|hinge.?clos|self.?clos|pool gate|childproof|baby.?proof'),
 ('Helmets & wheeled toys',             r'helmet|bicycle|\bbikes?\b|tricycle|scooter|skateboard|training wheel|wagon|go.?kart|sled|roller skate|inline skate|all terrain vehicle|\batvs?\b|youth model'),
 ('Button cells & batteries',           r'button cell|coin batter|cr20\d\d|lr\d\d|reese|battery pack|batteries'),
 ('Nursery electricals & monitors',     r'monitor|night ?light|humidifier|sound machine|wipe warmer|bottle warmer|sterilizer|nursery lamp|projector|speaker|audio player|walkie'),
 ('Jewelry & accessories',             r'jewel|necklace|bracelet|earring|charm|pendant|rings?\b|tiara|hair ?(?:clip|pin|bow|accessor)|sunglass|watch|purse|wallet|keychain|zipper pull'),
 ('Lighters & fire hazards',            r'lighter|matches|candle|torch|fire ?pit|fireplace|lamp oil|torch fuel|flame'),
 ('Window coverings & cords',           r'blind|roman shade|roller shade|window (?:covering|treatment|shade)|curtain|cord (?:stop|cleat)|drapery'),
 ('Household chemicals',                r'hydroxide|drain cleaner|paint thinner|solvent|antifreeze|pesticide|cleaner|detergent|bleach|acids?\b|chemical|fuel container|de.?icer|antifreez|countertop|glue|adhesive|serum|minoxidil|hair growth|coating|methanol|gasoline|faucet'),
 ('Food & formula',                    r'formula|baby food|puree|pouch|cereal|snack|yogurt|\bmilk\b|juice|puff|infant water|nursery water|beverage|probiotic|jelly bean|candy|cinnamon|produce|spinach|bok ?choy|lettuce|fruit|vegetable|applesauce|apple sauce'),
 ('Medications & supplements',         r'medication|acetaminophen|ibuprofen|gripe water|supplement|multivitamin|vitamin|electrolyte|gas relief|colic|nyquil|cough|cold remedy|nasal|antihistamine|allergy|syrup|drops\b|tablet|capsule|zinc oxide|ointment'),
 ('Skincare, bath & diapering',        r'lotion|baby oil|diaper|cream|balm|shampoo|baby wash|bubble bath|powder|moisturiz|eczema|wipe|sunscreen|spf\b|sun ?block'),
 ('Oral care & teething',              r'toothpaste|toothbrush|teething|teether|oral gel|orajel|mouthwash|dental'),
 ('Medical devices',                   r'ventilator|resuscitat|forceps|blood pressure|cuff\b|catheter|intubation|neonatal|nicu|syringe|thermometer|nebuliz|oximet|monitor kit|convenience kit|admission kit|drainage|tracheal|manometer|stethoscope|infusion|feeding tube|apnea'),
 ('Personal care & medicine',           r'wipe|lotion|shampoo|diaper cream|sunscreen|ointment|syrup|drops|supplement|vitamin|tablet|capsule|toothpaste|essential oil|lidocaine|minoxidil|numbing|anesthetic|topical|balm|sanitizer'),
]
FAM = [(n, re.compile(p, re.I)) for n, p in FAM]


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
LABELS = {
    'Sleep — cribs, bassinets, loungers': 'Cribs & bassinets',
    'Sleep — mattresses & bedding': 'Mattresses & bedding',
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
    'Jewelry & accessories': 'Jewellery & accessories',
    'Sleepwear & apparel': 'Clothing & sleepwear',
}


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
    "Food": None,        # too broad — fall through to the text classifier
    "Veterinary": None,  # excluded upstream anyway
}


def family_for_fda(product_type, text):
    """Family for an FDA enforcement record. Product type wins where it is
    specific; food falls through because it spans formula, purees and snacks."""
    forced = FDA_TYPE_FAMILY.get((product_type or "").strip())
    if forced:
        return forced
    return family(text) or "Other"
