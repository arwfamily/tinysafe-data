#!/usr/bin/env python3
"""
TinySafe recall sync — datatables (FDA timely) + CPSC API.
- No OpenFDA (per decision: misses too much).
- FDA enforcement (recall_id / Class I) is added MANUALLY via promote step.
- Preserves existing recalls_unified.json envelope; never overwrites blindly.
- Exclusion-first baby filter (never drop a real child recall silently).
- Conservative promotion: only auto-upgrade on high confidence; else needs_review.

Sources confirmed reachable via plain GET (no bot wall):
  FDA datatables xlsx, CPSC saferproducts.gov API.
"""
import os, io, json, re, hashlib, datetime, sys, time

import requests
import pandas as pd

DB_PATH = os.environ.get("DB_PATH", "recalls_unified.json")
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
HAZARD_SET = ["botulism", "bacteria", "contamination", "lead", "mold",
              "battery", "magnet", "choking", "suffocation", "strangulation",
              "flammable", "fire", "fall", "laceration", "asbestos", "chemical"]
HAZARD_PATTERNS = [
    ("botulism", r"botulism|clostridium botulinum"),
    ("bacteria", r"salmonella|listeria|e\.? ?coli|cronobacter|bacteria|microbial"),
    ("contamination", r"contaminat|foreign material|undeclared|metal fragment"),
    ("lead", r"\blead\b|lead paint|lead level"),
    ("mold", r"\bmold\b|mould"),
    ("battery", r"button (cell|battery)|coin (cell|battery)|lithium|battery ingestion"),
    ("magnet", r"\bmagnet"),
    ("choking", r"chok|small part|detach"),
    ("suffocation", r"suffocat|entrap"),
    ("strangulation", r"strangulat|cord around"),
    ("flammable", r"flammab"),
    ("fire", r"\bfire\b|burn hazard|overheat|igni"),
    ("fall", r"\bfall\b|tip ?over|tip-over|topple"),
    ("laceration", r"lacerat|sharp|cut hazard"),
    ("asbestos", r"asbestos"),
    ("chemical", r"chemical|toxic|formaldehyde|phthalate|benzene"),
]

def derive_hazard(text):
    t = (text or "").lower()
    for name, pat in HAZARD_PATTERNS:
        if re.search(pat, t):
            return name
    return ""

# Lethal (death-associated) hazards drive the "Most urgent" section.
# Rank: lower = more lethal, surfaced first.
LETHAL_HAZARDS = {"suffocation", "strangulation", "botulism", "bacteria",
                  "contamination", "battery", "choking", "magnet", "mold"}
LETHAL_RANK = {"botulism": 1, "suffocation": 2, "strangulation": 2,
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
    # Otherwise: a death-associated hazard -> urgent (app caps at 90d window).
    if haz in LETHAL_HAZARDS:
        return True, LETHAL_RANK.get(haz, 6)
    return False, 99

def _days_since(ds):
    try:
        return (datetime.datetime.now() - datetime.datetime.strptime(str(ds), "%Y%m%d")).days
    except Exception:
        return 99999

CATEGORY_PATTERNS = [
    # Ingestibles first so "purified water"/"probiotic" never fall through to Toys.
    ("Medications", r"\bdrops\b|medication|acetaminophen|ibuprofen|gripe water|"
                    r"\bsupplement|multivitamin|\bvitamin|probiotic|electrolyte|"
                    r"gas relief|colic|teething gel|ointment|\biron\b"),
    ("Food & Formula", r"formula|baby food|puree|pouch|cereal|snack|yogurt|milk|"
                       r"juice|puff|purified water|drinking water|water with fluoride|"
                       r"infant water|nursery water|beverage|electrolyte drink"),
    ("Wipes", r"\bwipe"),
    ("Baby Sunscreen", r"sunscreen|spf\b|sun ?block|mineral sun|uv protect"),
    ("Skincare", r"lotion|baby oil|diaper cream|balm|shampoo|baby wash|"
                 r"\bpowder|moisturiz"),
    ("Oral Care", r"toothpaste|teether|teething|toothbrush|pacifier"),
    # Water TOYS (balloons, water tables, squirters) stay in Toys, distinct from
    # drinking water above which already matched Food.
    ("Toys & Gear", r"\btoy|stroller|car seat|crib|bassinet|lounger|nursing pillow|"
                    r"\bbottle|high ?chair|playpen|play yard|rattle|walker|bouncer|"
                    r"swing|carrier|changing table|dresser|bed rail|harness|stool|"
                    r"tent|chair|water balloon|water table|squirt|magnet|chess|fidget|"
                    r"doll|game|block|puzzle|ride-?on|tricycle|scooter|thermos"),
]

def derive_category(text):
    t = (text or "").lower()
    for name, pat in CATEGORY_PATTERNS:
        if re.search(pat, t):
            return name
    return "Other"

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

def default_action(hazard):
    return {
        "suffocation": "Stop using it immediately and do not put your baby in or near it. "
                       "Move your baby to a firm, flat, empty sleep surface.",
        "choking": "Take it away from your child immediately and stop using it.",
        "battery": "Keep it away from children and stop using it immediately.",
        "fire": "Stop using it immediately and unplug or power it off.",
    }.get(hazard, "Stop using it and follow the recall instructions for a refund or repair.")

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

def is_child_product(text, name=None):
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
            if not is_child_product(blob, name=f"{brand} {desc}"):
                continue
            rid = "dt-" + hashlib.md5(squash(f"{date_raw}{company}{desc}").encode()).hexdigest()[:12]
            if rid in out:
                continue
            recall_date = normalize_date(date_raw)
            hazard = derive_hazard(reason or desc)
            product_name = desc or brand
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
        if not is_child_product(blob, name=f"{title} {product}"):
            continue
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
        reason = _first(it.get("Hazards", [])) or desc
        hazard = derive_hazard(f"{title} {reason} {desc}")
        product_name = title
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
    # (Re)compute urgent flags for every record so the app can read them directly.
    for r in db["recalls"]:
        u, rank = compute_urgent(r)
        r["is_urgent"] = u
        r["urgent_rank"] = rank
    db["total"] = len(db["recalls"])
    db["updated"] = datetime.datetime.utcnow().isoformat() + "Z"
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
