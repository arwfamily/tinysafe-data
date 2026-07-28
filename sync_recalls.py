#!/usr/bin/env python3
"""
TinySafe recall sync — datatables (FDA timely) + CPSC API.
- No OpenFDA (per decision: misses too much).
- FDA enforcement (recall_id / Class I) is added MANUALLY via promote step.
- Writes recalls_pipeline.json; merge_sources.py builds recalls_unified.json from it.
- Exclusion-first baby filter (never drop a real child recall silently).
- Conservative promotion: only auto-upgrade on high confidence; else needs_review.

Sources confirmed reachable via plain GET (no bot wall):
  FDA datatables xlsx, CPSC saferproducts.gov API.
"""
import os, io, json, re, glob, hashlib, datetime, sys, time

import requests
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))

import tinysafe_curate as tcur
import tinysafe_hazard as thaz
import tinysafe_categories as tcat
import fda_enforcement as fenf
import fda_enforcement_import as fimp

# The pipeline writes its OWN file, not the one the app reads.
#
# The daily action fetches CPSC and the FDA datatables. Everything else — the
# 1973-2026 CPSC archive, the FDA Enforcement Report exports, the NHTSA child
# restraints — is downloaded by hand and cannot be re-fetched on a schedule.
# When both wrote to recalls_unified.json, every automatic run silently deleted
# the manual half.
#
# So: sync writes recalls_pipeline.json, merge_sources.py combines that with the
# static archives, and recalls_unified.json is a build output nobody edits.
DB_PATH = os.environ.get("DB_PATH", "recalls_pipeline.json")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
# Full browser-like header set — bot detectors check more than User-Agent.
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts",
    "Sec-Ch-Ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "X-Requested-With": "XMLHttpRequest",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}
FDA_KEYWORDS = ["baby", "infant", "toddler", "newborn"]
CPSC_LOOKBACK_DAYS = 120  # CPSC status never flips; 3-year window handled downstream

# ----------------------------------------------------------------------------
# Hazard / category derivation (shared)
# ----------------------------------------------------------------------------
# Hazard derivation now lives in tinysafe_hazard. Two changes from the table
# that used to sit here: every matching pattern is recorded rather than the
# first one in list order (37% of records match two or more, so the ordering
# was silently deciding severity), and the severity tier is a function of the
# match set rather than a frozen field.
#
# The single-string `hazard` key is still written for backward compatibility —
# the app reads it — and carries the most severe match.

def derive_hazards(text, product="", category=None):
    """Returns (hazards list, tier int, primary hazard string)."""
    hazards, tier = thaz.derive(text or "", product or "", category)
    return hazards, tier, thaz.primary_of(hazards)

def derive_hazard(text):
    """Legacy shim — returns the single most severe hazard name."""
    return derive_hazards(text)[2]

# Lethal (death-associated) hazards drive the "Most urgent" section.
# Rank: lower = more lethal, surfaced first.
LETHAL_HAZARDS = {"suffocation", "entrapment", "strangulation", "botulism", "bacteria",
                  "contamination", "battery", "choking", "magnet", "mold"}
LETHAL_RANK = {"botulism": 1, "suffocation": 2, "entrapment": 2, "strangulation": 2,
               "battery": 3, "bacteria": 3, "contamination": 3,
               "choking": 4, "magnet": 4, "mold": 5}

def compute_urgent(rec):
    """is_urgent = this recall is death-associated and still active.
    Severity only — the APP decides section placement by age
    (New <=7d, Most urgent 7-90d + Class I to 730d, Ongoing after).
    Returns (is_urgent, urgent_rank).
    - Medical is excluded (handled by the Medical chip).
    - Terminated / no-longer-active recalls are NOT urgent, so when the
      monthly enforcement cross-check flips a status to Terminated, the
      recall drops out of Most urgent automatically on the next sync.
    """
    if rec.get("display_category") == "Medical":
        return False, 99
    status = (rec.get("status") or "").strip().lower()
    if status in ("terminated", "completed", "closed"):
        return False, 99
    cls = (rec.get("classification") or "").strip().lower()
    haz = rec.get("hazard", "")
    is_class_i = cls == "class i" or (cls.startswith("class i") and "ii" not in cls and "iii" not in cls)
    # FDA Class I = death-probability grade -> urgent regardless of age (app caps at 730d).
    if is_class_i:
        return True, LETHAL_RANK.get(haz, 6)
    # Otherwise the severity tier decides. Tiers 1-3 are the ACT NOW band.
    tier = rec.get("tier")
    if tier is None:
        tier = thaz.TIER.get(haz, 4)
    if tier <= 3:
        return True, tier
    return False, 99

def _days_since(ds):
    try:
        return (datetime.datetime.now() - datetime.datetime.strptime(str(ds), "%Y%m%d")).days
    except Exception:
        return 99999

# Display categories now come from tinysafe_categories (20 product families).
# The previous 9-value list put 37% of the feed in "Toys & Gear"; after the
# full-archive merge that single bucket would hold over 2,000 records.
# `display_category` keeps the old value so nothing downstream breaks until the
# app ships the new model; `category_family` carries the new one.

LEGACY_CATEGORY = [
    ("Medications", r"\bdrops\b|medication|acetaminophen|ibuprofen|gripe water|"
                    r"\bsupplement|multivitamin|\bvitamin|probiotic|electrolyte|"
                    r"gas relief|colic|teething gel|ointment|\biron\b"),
    ("Food & Formula", r"formula|baby food|puree|pouch|cereal|snack|yogurt|milk|"
                       r"juice|puff|purified water|drinking water|water with fluoride|"
                       r"infant water|nursery water|beverage|electrolyte drink"),
    ("Wipes", r"\bwipe"),
    ("Baby Sunscreen", r"sunscreen|spf\b|sun ?block|mineral sun|uv protect"),
    ("Skincare", r"lotion|baby oil|diaper cream|balm|shampoo|baby wash|\bpowder|moisturiz"),
    ("Oral Care", r"toothpaste|teether|teething|toothbrush|pacifier"),
    ("Toys & Gear", r"\btoy|stroller|car seat|crib|bassinet|lounger|nursing pillow|"
                    r"\bbottle|high ?chair|playpen|play yard|rattle|walker|bouncer|"
                    r"swing|carrier|changing table|dresser|bed rail|harness|stool|"
                    r"tent|chair|water balloon|water table|squirt|magnet|chess|fidget|"
                    r"doll|game|block|puzzle|ride-?on|tricycle|scooter|thermos"),
]

def derive_category(text):
    t = (text or "").lower()
    for name, pat in LEGACY_CATEGORY:
        if re.search(pat, t):
            return name
    return "Other"

def derive_family(text):
    """New 25-family product category."""
    return tcat.family(tcur.key(text or "")) or "Other"

def derive_group(family_name):
    """Browse group (7 sections) that the family sits in."""
    return tcat.group(family_name)

def plain_reason(hazard):
    return {
        "suffocation": "Poses a suffocation or entrapment risk during sleep or play.",
        "choking": "Contains small parts that can detach and pose a choking risk.",
        "fall": "Can tip over or let a child fall, posing an injury risk.",
        "fire": "Can overheat, ignite, or burn, posing a fire risk.",
        "battery": "Button or lithium battery can be accessed or ingested.",
        "lead": "Contains lead above the allowed limit.",
        "bacteria": "May be contaminated with harmful bacteria.",
        "botulism": "May be contaminated and pose a botulism risk.",
        "contamination": "May be contaminated or contain undeclared material.",
        "magnet": "Contains magnets that can be swallowed.",
        "strangulation": "Cords or parts pose a strangulation risk.",
        "mold": "May contain mold.",
        "chemical": "Contains a harmful chemical.",
        "laceration": "Has sharp parts that can cut.",
        "flammable": "Is flammable.",
        "asbestos": "May contain asbestos.",
    }.get(hazard, "Has been recalled for a safety risk.")

ACTION_TEMPLATES = {
    "suffocation": "Stop using it now and take your baby out of it. Put them to sleep on a "
                   "firm, flat, empty surface — a crib, bassinet or play yard with nothing else in it.",
    "strangulation": "Stop using it now and keep it away from where your child sleeps or plays. "
                     "Cut any cords or straps before you throw it out.",
    "entrapment": "Stop using it now. Check that your child can be lifted out freely from "
                  "anything similar you still use.",
    "drowning": "Stop using it now and never leave your child near water unattended, even for a moment.",
    "botulism": "Stop feeding it immediately. If your baby has eaten it and seems weak, floppy, "
                "is feeding poorly or has a weak cry, call your pediatrician or go to the ER now.",
    "bacteria": "Stop feeding it immediately. If your baby has fever, vomiting or diarrhoea, "
                "call your pediatrician.",
    "magnet": "Take it away from your child now. If you think a magnet was swallowed, go to the "
              "ER immediately — two magnets can pinch the gut and this is a surgical emergency.",
    "battery": "Take it away from your child now. If you think a button battery was swallowed, "
               "go to the ER immediately — do not wait for symptoms and do not induce vomiting.",
    "tipover": "Stop using it or anchor it to the wall right now. Keep your child away from it "
               "until it is anchored or gone.",
    "choking": "Take it away from your child now and check for any small parts that have come loose.",
    "fire": "Stop using it now and unplug it. Keep it away from anywhere your child sleeps.",
    "burn": "Stop using it now and keep it out of your child's reach.",
    "flammable": "Stop dressing your child in it and take it out of the drawer now.",
    "fall": "Stop using it now. Do not leave your child in it, even briefly.",
    "laceration": "Take it away from your child now and check for sharp or broken edges.",
    "crash": "Stop using this seat now. Do not drive with your child in it until it is replaced or repaired.",
    "electrical": "Unplug it and stop using it now. Keep it away from water and out of reach.",
    "lead": "Take it away from your child now. If they have mouthed or swallowed any part of it, "
            "ask your pediatrician about a blood lead test.",
    "chemical": "Keep it out of sight and reach of children now. If swallowed, call Poison Control "
                "at 1-800-222-1222.",
    "entanglement": "Stop using it now and keep it away from your child's neck and hands.",
    "overheat": "Stop using it and unplug it now.",
    "mold": "Stop using it now.",

    # --- the rest of the vocabulary. Written because 290 records were carrying
    # "follow the manufacturer's recall instructions for a refund or replacement",
    # and for an FDA enforcement report there are no instructions a parent can
    # find — no per-recall page exists. A directive that can't be followed is
    # not a directive.
    "contamination": "Stop using or feeding it now and throw it out. If your baby consumed it and seems "
                     "unwell, call your pediatrician.",
    "sanitation": "Stop feeding or using it now and throw it out. If your baby consumed it and has "
                  "fever, vomiting or diarrhoea, call your pediatrician.",
    "undeclared": "Stop feeding it now if your baby has any allergy. An ingredient that can cause a "
                  "reaction is not on the label. If they've eaten it and are having trouble breathing "
                  "or swelling, call 911.",
    "subpotent": "Stop using it and speak to your pediatrician before the next dose — it may be weaker "
                 "or stronger than the label says.",
    "asbestos": "Stop using it now, seal it in a bag before throwing it out, and don't shake or brush it.",
    "carbonmonoxide": "Stop using it now and get fresh air. If anyone has a headache, dizziness or "
                      "nausea, leave the building and call 911.",
    "crash_protection": "Stop using this seat now. Don't drive with your child in it until it's been "
                        "replaced or repaired.",
    "label_instruction": "Check the label and instructions against the recall notice before you use it "
                         "again — an incorrectly fitted seat may not protect your child in a crash.",

    # These three describe a process failure rather than a hazard to the child.
    # Say that plainly instead of inventing an urgency the record doesn't support.
    "quality": "Stop using it. It was made outside required quality standards, so what's in the package "
               "may not match the label.",
    "manufacturing": "Stop using it. It was made outside required quality standards — the specific "
                     "problem isn't stated in the recall.",
    "labeling": "Check the label against the recall notice. Something on it is wrong or missing, which "
                "may matter if your child has an allergy or takes a measured dose.",

    # Deliberately no "stop using". This is hospital and NICU equipment, where
    # stopping could be more dangerous than the defect. Route to the clinician.
    "device_failure": "If this is in use for your child, speak to their care team before changing "
                      "anything — do not stop or swap equipment on your own.",
    "sterility": "If this is in use for your child, tell their care team about this recall. Don't use "
                 "any unopened units.",
    "unapproved": "Speak to your pediatrician or the clinician who supplied it before using it again.",

    # No classified hazard. Say so rather than implying we know.
    "general": "Stop using it for now. The recall notice doesn't say what the specific risk is — check "
               "the official notice, and if your baby used it and has any symptoms, contact your "
               "pediatrician.",
}
SAFE_FALLBACK = ("Stop using it now. Check the official recall notice for what to do next. "
                 "If your baby used it and has any symptoms, contact your pediatrician.")

# Openings that identify a line as generated by us rather than written by an
# agency. Only these get replaced when the templates change — anything else is
# CPSC's own Remedy text or a hand-curated line and must survive untouched.
#
# The first entry is why this list exists: 290 records carried it, and for an
# FDA enforcement report there are no "manufacturer's recall instructions" a
# parent can reach. It slipped past the previous rule because that rule looked
# for "refund or repair" and this says "refund or replacement".
REPLACEABLE_OPENINGS = (
    "Stop using this product and follow the manufacturer's recall instructions",
    "Stop using it and follow the recall instructions",
    "Stop using and follow the recall instructions",
)


def is_generated_action(text):
    t = (text or "").strip()
    if not t:
        return True
    if t in ACTION_TEMPLATES.values() or t == SAFE_FALLBACK:
        return True
    return any(t.startswith(p) for p in REPLACEABLE_OPENINGS)

def default_action(hazard):
    """Never says 'refund or repair' — most records have no such remedy, and for
    a CPSC safety warning there is no remedy at all."""
    return ACTION_TEMPLATES.get(hazard, SAFE_FALLBACK)

# ----------------------------------------------------------------------------
# Baby / child product filter (exclusion-first)
# ----------------------------------------------------------------------------
CHILD_KEYWORDS = [
    "baby","infant","toddler","newborn","nursery","crib","cradle","bassinet",
    "stroller","car seat","booster seat","diaper","pacifier","teeth","teether",
    "nursing","baby bottle","nursing bottle","bottle warmer","sippy",
    "human milk","donor milk","milk bank","breast milk","breastmilk",
    "high chair","playpen","play yard","swaddle",
    "onesie","children","child","kids","kid","youth","pajama","sleepwear","rattle",
    "potty","formula","wipe","changing table","bed rail","play mat","walker",
    "bouncer","jumper","swing","carrier","toy",
]
PRODUCE_RE = re.compile(
    r"baby (spinach|arugula|kale|bok ?choy|carrots?|greens?|romaine|broccoli|"
    r"corn|peas?|bhindi|okra|bella|mushroom|lettuce|spring mix)", re.I)
SALAD_RE = re.compile(
    r"\b(vegetable tray|veggie tray|salad (kit|mix|blend|with)|spring mix|"
    r"mixed greens|deluxe salad|clamshell|stir fry)\b", re.I)
BABYFOOD_RESCUE = re.compile(
    r"(pouch|puree|baby food|baby cereal|\d+\s?(oz|ounce)\s?cups?|yobaby|"
    r"plum organics|gerber|beech-?nut|earth.?s best|happy ?baby|good & gather baby|"
    r"heb baby|h-e-b baby|sprout organic|once upon a farm|cerebelly|tippy toes)", re.I)

# Noise: explicit adult products + general consumer goods that are not baby items.
# No trailing \b on pluralizing terms so "coolers"/"rails"/"bottles" still match.
# These are checked against display_name (the product itself) so a stray word in
# the reason text (e.g. "violates children's sleepwear standard" on a cooler)
# does not rescue a non-baby product.
STRONG_NOISE_RE = re.compile(
    r"(coffee ?maker|coffeemaker|espresso|kettle|toaster|blender|microwave|"
    r"air ?fryer|pressure washer|power washer|vacuum cleaner|\bgenerator|"
    r"space heater|chainsaw|lawn ?mower|treadmill|\bcooler|water bottle|"
    r"above-?ground pool|bicycle helmet|\bhelmet|bed rail|portable bed rail|"
    r"patio door|sliding patio|turpentine|gum spirits|kerosene|sodium hydroxide|"
    r"caustic|heater fluid|\b1-k\b|"
    r"lithium coin batter|coin batter|button cell|"
    r"bunk bed|utility bunk|loft bed|youth clothing|youth sweatshirt|"
    r"hemorrhoidal|medicated wipe|cleansing washcloth|food jar|stainless king|"
    r"power strip|extension cord|surge protector|denture|"
    r"\bbadminton\b|mario kart|tonka)",
    re.I)
NOISE_NAME_RE = re.compile(
    r"(\badult\b|vaporizer|firework|pool drain|spa drain|\bpatio\b|"
    r"woven (sofa|chair|patio)|\bladder\b)", re.I)
# Protects genuine baby gear from the noise filter.
BABY_PROTECT_RE = re.compile(
    r"\b(bassinet|crib|cradle|baby|babies|infant|newborn|toddler|nursery|nursing|"
    r"childcare|child care|pacifier|diaper|stroller|car seat|teeth|teether|teething|"
    r"swaddle|onesie|sippy|high ?chair|playpen|rattle|\bbib\b|formula|"
    r"tricycle|magnet|chess|\btoy|game|pajama|sleepwear)\b", re.I)

# Age noise: items aimed at older kids / adults that a 0-4 child does not use.
# Curated for the 0-4 (esp. newborn) parent audience.
AGE_NOISE_RE = re.compile(
    r"\b(fidget|spinner|racket|badminton|tennis|golf|skateboard|hoverboard|"
    r"promotional|desk toy|executive|office|\bzen\b|stress ball|"
    r"tip restraint|furniture anchor|anti-?tip|"
    r"bicycle|\bbike\b|scooter|\bATV\b|all-?terrain|go-?kart|"
    r"bunk bed|loft bed|"
    r"bowling|basketball|soccer|football|baseball|hockey|"
    r"slap bracelet|light-?up ring|light-?up bracelet|jelly ring|"
    r"tumbler|sport bottle|sipper|"
    r"backpack|lunch box|water gun|nerf|"
    r"trampoline|pogo|ride-?on racer|\bdrone\b|"
    r"craft kit|science kit|chemistry|assay|"
    r"ski boot|ski boots|snowboard|firearm|firearms|gun sight|dot sight|"
    r"red dot|rifle|pistol|holster|ammunition|"
    r"youth|teen|tween)\b", re.I)
# But keep genuine infant magnet toys (ingestion is a top infant hazard):
# magnet blocks, chess, building sticks, stackers — a 0-4 child can reach these.
INFANT_MAGNET_SAVE = re.compile(
    r"\b(magnet|magnetic)\b.*\b(block|chess|building|stick|stack|tile|set|toy)\b|"
    r"\b(block|chess|building|stacker|tile)\b.*\bmagnet", re.I)

# Child-HAZARD language describes WHO is at risk, not WHAT the product is.
# CPSC headlines for adult products say "Child-Resistant Packaging", "Child
# Poisoning", "swallowed by young children" — without stripping these, paint
# thinner / fuel bottles / Benadryl all pass the child filter.
CHILD_HAZARD_PHRASE_RE = re.compile(
    r"child[-\s]?resistant|child[-\s]?proof|poison prevention packaging|"
    r"child(ren)?\s+poisoning|poisoning[^.]{0,40}child(ren)?|"
    r"swallowed by (young )?child(ren)?|ingested by (young )?child(ren)?|"
    r"out of the (sight and )?reach of child(ren)?|keep (it )?away from child(ren)?|"
    r"accessible to (young )?child(ren)?|if a (young )?child|small child(ren)?|"
    r"harmful if swallowed", re.I)

# Adult / general categories that only ever appear via child-resistant-packaging
# violations. Rescued when the item is clearly the kids'/baby version.
EXTRA_NOISE_RE = re.compile(
    r"(paint thinner|fuel bottle|liquid fuel|minoxidil|lidocaine|essential oil|"
    r"nasal spray|dietary supplement|multivitamin|mouthwash|kitchen scale|"
    r"\blighter|hair (growth|serum)|beard|waxing kit|reagent|test kit|"
    r"iron supplement|hydrogen peroxide|silicone glue|numbing cream|"
    r"battery pack|battery pouch|bottled water)", re.I)
BABY_NAME_SIGNAL = re.compile(
    r"\b(kid|kids|children|children's|child's|pediatric)\b", re.I)
BABY_STRONG = re.compile(
    r"(baby|babies|infant|newborn|toddler|nursery|breast ?milk|"
    r"donor (human )?milk|human milk|milk bank|formula)", re.I)

def in_feed_scope(text, name=None):
    """The narrow filter: does a 0-4 parent need this in their FEED.

    Deliberately excludes bicycles, scooters, ATVs, bunk beds, tip restraints,
    youth/teen gear and adult products that only surface via child-resistant
    packaging violations. These exclusions are product decisions, not bugs —
    they are what keeps the feed about the 0-4 age band.

    This runs AFTER curation. Anything curated but out of feed scope is still
    stored and still searchable; it just does not appear in the feed sections.
    """
    return _is_child_product(text, name)

def _is_child_product(text, name=None):
    # Strip child-HAZARD wording first so it cannot qualify an adult product.
    blob = CHILD_HAZARD_PHRASE_RE.sub(" ", (text or "").lower())
    name_blob = CHILD_HAZARD_PHRASE_RE.sub(" ", (name or text or "").lower())
    rescued = BABY_NAME_SIGNAL.search(name_blob) or BABY_STRONG.search(blob)
    if EXTRA_NOISE_RE.search(name_blob) and not rescued:
        return False
    if STRONG_NOISE_RE.search(name_blob):
        return False
    if AGE_NOISE_RE.search(name_blob) and not INFANT_MAGNET_SAVE.search(name_blob):
        return False
    if NOISE_NAME_RE.search(blob) and not BABY_PROTECT_RE.search(blob):
        return False
    if (PRODUCE_RE.search(blob) or SALAD_RE.search(blob)) and not BABYFOOD_RESCUE.search(blob):
        if not re.search(r"\b(baby food|infant|formula)\b", blob):
            return False
    return any(kw in blob for kw in CHILD_KEYWORDS)

# ----------------------------------------------------------------------------
# Match-key helpers (for dedupe + My Brands matching, mirrors existing schema)
# ----------------------------------------------------------------------------
def squash(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def build_match_fields(brand, product_name):
    b = (brand or "").lower().strip()
    text = f"{b} {(product_name or '').lower()}".strip()
    return {
        "match_brand": b,
        "match_text": text,
        "match_ndc": "",
        "match_squash": squash(text),
        "match_brand_squash": squash(b),
        "match_words": text,
    }

def display_name_from(product_name):
    # First clause before "Recalled"/"Due to"/";" — mirrors existing display_name style
    n = re.split(r"\s+(recalled|due to)\b|;", product_name, flags=re.I)[0].strip()
    return n[:80] if n else product_name[:80]

# ----------------------------------------------------------------------------
# FDA datatables (timely xlsx)
# ----------------------------------------------------------------------------
def fetch_fda_datatables():
    out = {}
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    # Warm-up: hit the human page first so the session looks like a real visit,
    # then request the data endpoint (some bot filters key off this sequence).
    try:
        session.get("https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts",
                    timeout=45)
    except Exception:
        pass
    for kw in FDA_KEYWORDS:
        url = ("https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/"
               f"datatables-data?_format=xlsx&search_api_fulltext={kw}"
               "&field_regulated_product_field=All&field_terminated_recall=All")
        df = None
        for attempt in range(3):
            try:
                r = session.get(url, timeout=45)
                if r.status_code == 200 and "abuse-detection" not in r.url:
                    df = pd.read_excel(io.BytesIO(r.content), dtype=str).fillna("")
                    break
                print(f"[!] datatables {kw} attempt {attempt+1}: status={r.status_code} url={r.url[:60]}",
                      file=sys.stderr)
            except Exception as e:
                print(f"[!] datatables {kw} attempt {attempt+1}: {e}", file=sys.stderr)
            time.sleep(3 * (attempt + 1))   # back off: 3s, 6s, 9s
        if df is None:
            print(f"[!] datatables fetch gave up ({kw})", file=sys.stderr)
            continue
        for _, row in df.iterrows():
            brand = row.get("Brand-Names", "")
            desc = row.get("Product-Description", "")
            company = row.get("Company-Name", "")
            reason = row.get("Recall-Reason-Description", "")
            date_raw = row.get("Date", "")
            blob = f"{brand} {desc} {company} {reason}"
            ok, signals, _ = tcur.curate(product=desc, heading=brand,
                                         description=desc, hazard=reason)
            if not ok:
                continue
            feed_ok = in_feed_scope(blob, name=f"{brand} {desc}")
            rid = "dt-" + hashlib.md5(squash(f"{date_raw}{company}{desc}").encode()).hexdigest()[:12]
            if rid in out:
                continue
            recall_date = normalize_date(date_raw)
            product_name = desc or brand
            fam = derive_family(blob)
            hazards, tier, hazard = derive_hazards(reason or desc, product_name, fam)
            rec = {
                "source": "FDA",
                "category": "Baby & Kids",
                "recall_id": rid,
                "product_name": product_name,
                "brand": brand,
                "recall_date": recall_date,
                "reason": reason,
                "classification": "",                      # filled by manual enforcement promote
                "status": "Terminated" if "terminat" in row.get("Terminated Recall","").lower() else "Ongoing",
                "url": "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts",
                "display_category": derive_category(blob),
                "display_name": display_name_from(product_name),
                "hazard": hazard,
                "hazards": hazards,
                "tier": tier,
                "band": thaz.BAND.get(tier, "CHECK"),
                "category_family": fam,
                "category_group": derive_group(fam),
                "record_type": "recall",
                "in_feed_scope": feed_ok,
                "curation_signals": signals,
                "action": default_action(hazard),
                "product_count": 1,
                "grouped_products": [],
                "plain_reason": plain_reason(hazard),
                "needs_review": False,
                "is_enforced": False,                      # datatables = not yet enforcement-classified
            }
            rec.update(build_match_fields(brand, product_name))
            out[rid] = rec
    return list(out.values())

# ----------------------------------------------------------------------------
# CPSC saferproducts.gov API
# ----------------------------------------------------------------------------
def _first(lst, key="Name"):
    if isinstance(lst, list) and lst:
        return str(lst[0].get(key, "") or "")
    return ""

def normalize_date(s):
    s = (s or "").strip()
    # CPSC: 2026-06-18T00:00:00
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(1) + m.group(2) + m.group(3)
    # datatables: "June 18, 2026" or MM/DD/YYYY
    for fmt in ("%B %d, %Y", "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.datetime.strptime(s, fmt).strftime("%Y%m%d")
        except ValueError:
            pass
    return ""

def fetch_cpsc():
    start = (datetime.date.today() - datetime.timedelta(days=CPSC_LOOKBACK_DAYS)).isoformat()
    url = ("https://www.saferproducts.gov/RestWebServices/Recall"
           f"?format=json&RecallDateStart={start}")
    data = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=45)
            r.raise_for_status()
            parsed = r.json()
            # Guard against a transient empty response: a 120-day window should
            # never legitimately be empty, so treat [] as a failure and retry.
            if isinstance(parsed, list) and len(parsed) > 0:
                data = parsed
                break
            print(f"[!] CPSC attempt {attempt+1}: empty response, retrying", file=sys.stderr)
        except Exception as e:
            print(f"[!] CPSC attempt {attempt+1}: {e}", file=sys.stderr)
        time.sleep(3 * (attempt + 1))
    if data is None:
        print("[!] CPSC fetch gave up after retries — keeping existing CPSC records",
              file=sys.stderr)
        return []
    out = []
    for it in data:
        title = str(it.get("Title", ""))
        desc = str(it.get("Description", ""))
        product = _first(it.get("Products", []), "Name")
        blob = f"{title} {desc} {product}"
        # `reason` carries the hazard sentence and is the strongest curation
        # signal, so it has to be read before curate() is called.
        reason = _first(it.get("Hazards", [])) or desc
        ok, signals, _ = tcur.curate(product=product, heading=title,
                                     description=desc, hazard=reason)
        if not ok:
            continue
        feed_ok = in_feed_scope(blob, name=f"{title} {product}")
        rid = str(it.get("RecallNumber", "")).strip()
        if not rid:
            continue
        # CPSC numbers in unified DB are formatted NN-NNN (e.g. 26-569 from 26569)
        if re.fullmatch(r"\d{5}", rid):
            rid = rid[:2] + "-" + rid[2:]
        brand = (_first(it.get("Manufacturers", []))
                 or _first(it.get("Importers", []))
                 or _first(it.get("Distributors", [])))
        # brand cleanup: take leading proper-noun-ish chunk
        brand = re.split(r",| of | dba ", brand)[0].strip()
        product_name = title
        fam = derive_family(blob)
        hazards, tier, hazard = derive_hazards(f"{title} {reason} {desc}", product_name, fam)
        rec = {
            "source": "CPSC",
            "category": "Baby & Kids",
            "recall_id": rid,
            "product_name": product_name,
            "brand": brand,
            "recall_date": normalize_date(it.get("RecallDate", "")),
            "reason": reason,
            "classification": "",
            "status": "Recalled",                 # CPSC status is always "Recalled"
            "url": str(it.get("URL", "")),
            "display_category": derive_category(blob),
            "display_name": display_name_from(product_name),
            "hazard": hazard,
            "hazards": hazards,
            "tier": tier,
            "band": thaz.BAND.get(tier, "CHECK"),
            "category_family": fam,
            "category_group": derive_group(fam),
            "record_type": "recall",
            "in_feed_scope": feed_ok,
            "curation_signals": signals,
            "action": _first(it.get("Remedies", [])) or default_action(hazard),
            "product_count": len(it.get("Products", []) or []),
            "grouped_products": [],
            "plain_reason": plain_reason(hazard),
            "needs_review": False,
            "is_enforced": True,                  # CPSC carries its own stable id
        }
        rec.update(build_match_fields(brand, product_name))
        out.append(rec)
    return out

# ----------------------------------------------------------------------------
# Merge: preserve envelope, append+dedupe, conservative promotion
# ----------------------------------------------------------------------------
def load_db():
    if os.path.exists(DB_PATH):
        with open(DB_PATH, encoding="utf-8") as f:
            return json.load(f)
    # First run after the pipeline/unified split: recalls_pipeline.json does not
    # exist yet. Seed from the published store rather than starting empty —
    # returning [] here would wipe the automatic half on the very first run and
    # the merge would then publish only the manual archives.
    legacy = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)) or ".",
                          "recalls_unified.json")
    if os.path.exists(legacy):
        with open(legacy, encoding="utf-8") as f:
            db = json.load(f)
        # recalls_unified.json is the merged output, so it already contains the
        # manual archives. Seeding from it wholesale would hand those records
        # back to the merge to add a second time. Keep only what this script
        # actually produces — NHTSA and the CPSC archive are re-added by
        # merge_sources.py from their own files.
        keep = [r for r in db.get("recalls", [])
                if r.get("source") != "NHTSA" and not r.get("curation_signals")]
        db["recalls"] = keep
        db["total"] = len(keep)
        print(f"[!] {DB_PATH} is missing — seeded {len(keep)} pipeline records from "
              f"recalls_unified.json. Commit recalls_pipeline.json so this "
              f"fallback is not needed.", file=sys.stderr)
        return db
    return {"version": "0", "recalls": [], "categories": [], "sources": []}

def norm_company(s):
    return re.sub(r"\b(inc|llc|ltd|co|corp|company|usa|america|imports?|trading)\b", "",
                  (s or "").lower()).strip()

def datediff_days(a, b):
    try:
        da = datetime.datetime.strptime(a, "%Y%m%d")
        db = datetime.datetime.strptime(b, "%Y%m%d")
        return abs((da - db).days)
    except Exception:
        return 9999

def merge(db, fresh):
    by_id = {r["recall_id"]: r for r in db.get("recalls", [])}
    added = updated = promoted = flagged = 0

    for rec in fresh:
        rid = rec["recall_id"]
        if rid in by_id:
            # refresh status only; never clobber an enforced/manually-edited record's fields
            existing = by_id[rid]
            if existing.get("status") != rec["status"] and not existing.get("is_enforced"):
                existing["status"] = rec["status"]
                updated += 1
            continue
        by_id[rid] = rec
        added += 1

    db["recalls"] = list(by_id.values())
    # Purge a small, explicit set of adult / non-child products that were stored
    # before the current name filters existed. merge() is append-only, so these
    # would otherwise linger. This is a TARGETED removal by product name only —
    # it intentionally does NOT re-run the full is_child_product filter across the
    # DB (that would wrongly drop food, medical, and edge baby items). Enforced
    # (hand-curated) records are always kept.
    ADULT_PURGE_RE = re.compile(
        r"hemorrhoidal|stainless king|\bski boots?\b|\bfirearms?\b|"
        r"gun sight|dot sight|red dot|cleansing washcloths?|"
        r"\bpower strips?\b|extension cord|surge protector|"
        # Adult/general products stored before the child-hazard-language fix.
        # Kids/baby versions are protected by the BABY rescue in the loop below.
        r"paint thinner|fuel bottles?|liquid fuel|minoxidil|"
        r"hair and beard growth|waxing kits?|reagent bottles?|dissolved oxygen|"
        r"silicone glue|numbing cream|battery pouch|battery packs?|"
        r"bottled water|benadryl|safetussin|light up tumblers?|"
        r"toilet lighters?|kitchen scales?|hydrogen peroxide mouthwash|"
        r"essential oil bottles?|afrin|bariatric fusion|california gold nutrition|"
        r"nfh iron|ultimate multivitamin|vitaquest|firefly safe|"
        r"lidocaine ointment|relieve\W+lidocaine|loratadine|aloe vera lotion",
        re.I)
    before_purge = len(db["recalls"])
    kept = []
    for r in db["recalls"]:
        name = f"{r.get('brand','')} {r.get('display_name') or r.get('product_name') or ''}"
        # Adult / non-child products are dropped even if is_enforced, because CPSC
        # records auto-set is_enforced (stable id) — that flag does not mean a human
        # curated them. The ADULT_PURGE_RE list is deliberately narrow (hemorrhoidal,
        # firearm, ski boots, power strips, etc.) so no genuine baby item matches.
        # Kids/baby versions of these categories (Kids Multivitamin, Baby Omega 3,
        # Shakleebaby, Ferrous Sulfate for infants, donor human milk) are kept.
        if ADULT_PURGE_RE.search(name) and not (
                BABY_NAME_SIGNAL.search(name) or BABY_STRONG.search(name)):
            continue  # drop this adult / non-child product
        kept.append(r)
    purged = before_purge - len(kept)
    db["recalls"] = kept
    if purged:
        print(f"[-] purged {purged} adult/non-child product(s) by explicit name match")
    # Backfill. Every derivation change re-runs across the whole store and
    # reports what moved. The 2026-07-20 hazard correction shipped without this
    # and left the feed carrying records from two different vintages; the diff
    # print is what makes that visible instead of silent.
    moved_haz = moved_cat = 0
    for r in db["recalls"]:
        blob = f"{r.get('product_name','')} {r.get('reason','')} {r.get('display_name','')}"
        fam = derive_family(blob)
        if r.get("category_family") != fam:
            moved_cat += 1
        r["category_family"] = fam
        r["category_group"] = derive_group(fam)
        # Same text the ingest path sees — feeding only `reason` here made the
        # backfill disagree with ingest on records whose hazard is named in the
        # title (15 lead recalls lost their hazard that way).
        hz, tier, primary = derive_hazards(blob, r.get("product_name", ""), fam)
        if r.get("hazard") != primary:
            moved_haz += 1
        r["hazard"], r["hazards"], r["tier"] = primary, hz, tier
        r["band"] = thaz.BAND.get(tier, "CHECK")
        r.setdefault("record_type", "recall")
        r.setdefault("in_feed_scope", True)
        # Never overwrite a hand-curated line or an agency's own remedy text.
        if not r.get("action_curated") and is_generated_action(r.get("action")):
            r["action"] = default_action(primary)
    if moved_haz or moved_cat:
        print(f"[~] backfill: hazard changed on {moved_haz}, category on {moved_cat}")

    # (Re)compute urgent flags for every record so the app can read them directly.
    for r in db["recalls"]:
        u, rank = compute_urgent(r)
        r["is_urgent"] = u
        r["urgent_rank"] = rank
    # Enforcement Report import. The datatables feed is the press-release
    # listing, and most recalls never get a press release — FDA says so itself.
    # These exports are the enforcement side, and they carry `code_info`: the
    # lot numbers a parent needs to tell whether their own tin is the recalled
    # one. Committed to the repo because FDA's own web export caps at 1,000 rows
    # and nothing before 2012 is reachable through the API at all.
    enf_csvs = sorted(glob.glob(os.path.join(HERE, "*_enforce_rpt*.csv")))
    if enf_csvs:
        try:
            have = {str(r.get("recall_id", "")).strip() for r in db["recalls"]}
            fresh, enrich = fimp.import_files(enf_csvs, have)
            by_id = {str(r.get("recall_id", "")).strip(): r for r in db["recalls"]}
            for rid, fields in enrich.items():
                rec = by_id.get(rid)
                if not rec:
                    continue
                for k, v in fields.items():
                    # never overwrite what the primary feed or a human set
                    if v and not (rec.get(k) or ""):
                        rec[k] = v
            for r in fresh:
                # Legacy fields the app decodes. Without these the records land
                # with no category chip, no urgency flag and no link — they
                # appear in the feed but drop out of every section that matters.
                r.setdefault("action", default_action(r.get("hazard")))
                r.setdefault("display_category", derive_category(
                    f"{r.get('product_name','')} {r.get('reason','')}"))
                r.setdefault("display_name", display_name_from(r.get("product_name", "")))
                r.setdefault("plain_reason", plain_reason(r.get("hazard")))
                r.setdefault("url", "https://www.accessdata.fda.gov/scripts/ires/index.cfm#tabNav_advancedSearch")
                u, rank = compute_urgent(r)
                r["is_urgent"], r["urgent_rank"] = u, rank
                r.update(build_match_fields(r.get("brand", ""), r.get("product_name", "")))
            db["recalls"].extend(fresh)
        except Exception as e:
            print(f"[!] enforcement import skipped: {e}", file=sys.stderr)

    # Enforcement enrichment. The datatables feed carries no lot numbers, so a
    # parent can be told a product was recalled but not whether their tin is
    # the recalled one. openFDA serves the Enforcement Report keyed on the same
    # recall_number we already store, so this is a join. Optional and
    # failure-tolerant — never allowed to take the sync down.
    if os.environ.get("SKIP_ENRICH") != "1":
        try:
            fenf.enrich_all(db["recalls"])
        except Exception as e:
            print(f"[!] enforcement enrichment skipped: {e}", file=sys.stderr)

    db["total"] = len(db["recalls"])
    db["updated"] = datetime.datetime.utcnow().isoformat() + "Z"
    # Envelope. The records were being rewritten while the header still declared
    # the old schema version and category count, so a client version-check saw
    # no change at all.
    db["version"] = "5.0"
    db["schema"] = {
        "hazards": "list of all matching hazards, not first match",
        "tier": "1-8 severity computed from the hazard list",
        "band": "ACT NOW 1-3 / ACT SOON 4-5 / CHECK 6-8",
        "category_family": "25 product families",
        "category_group": "7 browse groups",
        "record_type": "recall | warning",
        "in_feed_scope": "false = searchable but outside the 0-4 feed",
        "code_info": "lot numbers / UPC / best-by, from the FDA Enforcement Report",
        "distribution": "where it was sold",
    }
    db["categories"] = sorted({r.get("category_family") for r in db["recalls"] if r.get("category_family")})
    db["groups"] = sorted({r.get("category_group") for r in db["recalls"] if r.get("category_group")})
    db["sources"] = sorted({r.get("source") for r in db["recalls"] if r.get("source")})
    print(f"[+] added={added} status_updated={updated} total={db['total']} "
          f"urgent={sum(1 for r in db['recalls'] if r.get('is_urgent'))}")
    return db

# ----------------------------------------------------------------------------
if __name__ == "__main__":
    db = load_db()
    print(f"[*] existing DB: {len(db.get('recalls', []))} records")
    fda = fetch_fda_datatables()
    cpsc = fetch_cpsc()
    print(f"[*] fetched — FDA datatables: {len(fda)} | CPSC: {len(cpsc)}")
    db = merge(db, fda + cpsc)
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    print("[*] done.")
