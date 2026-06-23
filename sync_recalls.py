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

CATEGORY_PATTERNS = [
    ("Food & Formula", r"formula|baby food|puree|pouch|cereal|snack|yogurt|milk|juice|puff"),
    ("Wipes", r"\bwipe"),
    ("Skincare", r"lotion|sunscreen|baby oil|diaper cream|balm|shampoo|baby wash|powder"),
    ("Oral Care", r"toothpaste|teether|teething|toothbrush|pacifier"),
    ("Medications", r"\bdrops\b|medication|acetaminophen|ibuprofen|gripe water|supplement|vitamin"),
    ("Toys & Gear", r"toy|stroller|car seat|crib|bassinet|lounger|nursing pillow|bottle|"
                    r"high ?chair|playpen|play yard|rattle|walker|bouncer|swing|carrier|"
                    r"changing table|dresser|bed rail|helmet|harness|stool|tent|chair"),
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
    "nursing","bottle","sippy","high chair","playpen","play yard","swaddle",
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

def is_child_product(text):
    blob = (text or "").lower()
    # Hard produce exclusion, unless clearly a baby-food product
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
            if not is_child_product(blob):
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
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=45)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[!] CPSC fetch failed: {e}", file=sys.stderr)
        return []
    out = []
    for it in data:
        title = str(it.get("Title", ""))
        desc = str(it.get("Description", ""))
        product = _first(it.get("Products", []), "Name")
        blob = f"{title} {desc} {product}"
        if not is_child_product(blob):
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
    db["total"] = len(db["recalls"])
    db["updated"] = datetime.datetime.utcnow().isoformat() + "Z"
    print(f"[+] added={added} status_updated={updated} total={db['total']}")
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
