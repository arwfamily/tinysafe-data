"""
TinySafe — FDA enforcement enrichment.

The datatables feed the pipeline runs on is the press-release listing: seven
columns, no lot numbers, no distribution, no termination date. It is good at
being timely and bad at being specific.

The Enforcement Report carries the rest, and openFDA serves it publicly keyed on
`recall_number` — which is exactly the id our FDA records already use. So this
is a join, not a new source.

Two things it adds:

  1. `code_info` — the lot numbers, UPCs and best-by dates. This is the field
     that lets a parent check whether *their* can of formula is the recalled
     one. Without it we can only say "a2 Platinum was recalled", and a parent
     either throws out a safe tin or keeps an unsafe one.

  2. Records the press-release feed never had. Most recalls never get a press
     release — FDA says so on the recalls index — so enforcement-only baby
     recalls were simply invisible. A keyword pull of the Enforcement Report
     surfaced 25 we did not hold, four of them Class I, including Hyland's Baby
     Teething Tablets (belladonna) and Dr. King's Baby Colic Relief.

The earlier decision not to use openFDA as the *primary* source still stands —
it only carries classified recalls, so it lags. As an enrichment keyed on ids we
already hold, that objection doesn't apply.
"""
import json
import re
import time
import urllib.parse
import urllib.request

ENDPOINTS = {
    "F": "food", "H": "drug", "D": "drug", "Z": "device", "C": "food",
}
BASE = "https://api.fda.gov/{cat}/enforcement.json"

# openFDA field -> our field. Names we already carry are deliberately absent:
# the enrichment never overwrites what the primary feed established.
FIELD_MAP = {
    "code_info": "code_info",
    "more_code_info": "more_code_info",
    "product_quantity": "units",
    "distribution_pattern": "distribution",
    "recall_initiation_date": "initiated_date",
    "center_classification_date": "classified_date",
    "termination_date": "terminated_date",
    "voluntary_mandated": "recall_initiated_by",
    "initial_firm_notification": "firm_notification",
    "event_id": "event_id",
    "city": "firm_city",
    "state": "firm_state",
    "country": "firm_country",
}


def _endpoint(recall_number):
    m = re.match(r"^([A-Z])-", str(recall_number or "").strip())
    return ENDPOINTS.get(m.group(1)) if m else None


def fetch_one(recall_number, timeout=20):
    """One enforcement record, or None. Never raises — enrichment is optional
    and must not be able to take the sync down."""
    cat = _endpoint(recall_number)
    if not cat:
        return None
    q = urllib.parse.quote(f'recall_number:"{recall_number}"')
    url = BASE.format(cat=cat) + f"?search={q}&limit=1"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        results = data.get("results") or []
        return results[0] if results else None
    except Exception:
        return None


def apply_enrichment(rec, src):
    """Copy the enforcement fields onto a record. Only fills blanks — the
    primary feed and any hand-curated value always win."""
    added = []
    for k, dest in FIELD_MAP.items():
        v = (src.get(k) or "").strip() if isinstance(src.get(k), str) else src.get(k)
        if not v:
            continue
        if not (rec.get(dest) or ""):
            rec[dest] = v
            added.append(dest)
    # status is the one field where enforcement is authoritative — it is the
    # only source with a lifecycle, and a Terminated recall should stop being
    # presented as live.
    st = (src.get("status") or "").strip()
    if st and not rec.get("action_curated"):
        rec["status"] = st
    rec["enriched"] = True
    return added


def enrich_all(recalls, sleep=0.15, limit=None, log=print):
    """Enrich every FDA record that has an enforcement-style recall number."""
    targets = [r for r in recalls
               if "FDA" in str(r.get("source", ""))
               and _endpoint(r.get("recall_id"))
               and not r.get("enriched")]
    if limit:
        targets = targets[:limit]
    hit = miss = 0
    fields = {}
    for r in targets:
        src = fetch_one(r["recall_id"])
        if src:
            for f in apply_enrichment(r, src):
                fields[f] = fields.get(f, 0) + 1
            hit += 1
        else:
            miss += 1
        time.sleep(sleep)
    log(f"[+] enforcement enrichment: {hit} matched, {miss} not found "
        f"(of {len(targets)} attempted)")
    if fields:
        log("    fields filled: " + ", ".join(f"{k}={v}" for k, v in sorted(fields.items())))
    return hit, miss


# ---------------------------------------------------------------------------
# CSV path — for a manual Enforcement Report export, which is how the gap was
# found. Same mapping, no network.
# ---------------------------------------------------------------------------
CSV_MAP = {
    "Code Info": "code_info", "Product Quantity": "units",
    "Distribution Pattern": "distribution", "Recall Initiation Date": "initiated_date",
    "Center Classification Date": "classified_date", "Termination Date": "terminated_date",
    "Voluntary/Mandated": "recall_initiated_by", "Event ID": "event_id",
    "City": "firm_city", "State/Province": "firm_state", "Country": "firm_country",
    "Initial Firm Notification of Consignee or Public": "firm_notification",
}


def enrich_from_csv(recalls, path, log=print):
    """Enrichment from a manual Enforcement Report export.

    One trap in these exports: a long lot list overflows `Code Info` into
    `More Code Info`, `More Code Info.1` ... up to `.57` in the files seen so
    far, and the longest single record ran to 2.6 million characters. Reading
    only the first column silently truncates the lot list — which is the one
    field a parent uses to check their own tin, so a partial list is worse than
    an obviously absent one.
    """
    import csv
    by_id = {str(r.get("recall_id", "")).strip(): r for r in recalls}
    hit = 0
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        spill = [c for c in (reader.fieldnames or []) if c.startswith("More Code Info")]
        for row in reader:
            rec = by_id.get((row.get("Recall Number") or "").strip())
            if not rec:
                continue
            for col, dest in CSV_MAP.items():
                v = (row.get(col) or "").strip()
                if dest == "code_info" and spill:
                    v = " ".join(x for x in [v] + [(row.get(c) or "").strip() for c in spill] if x)
                if v and not (rec.get(dest) or ""):
                    rec[dest] = v
            st = (row.get("Status") or "").strip()
            if st and not rec.get("action_curated"):
                rec["status"] = st
            rec["enriched"] = True
            hit += 1
    log(f"[+] enforcement CSV: enriched {hit} records from {path}")
    return hit
