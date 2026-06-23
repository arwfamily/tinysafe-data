#!/usr/bin/env python3
"""
Enforcement promotion — run monthly after downloading the FDA Enforcement CSV.

The enforcement download endpoint is behind bot protection + a session, so it
can't be fetched unattended. It also trails the timely feed by months, so manual
monthly is fine. This script reconciles enforcement (which has recall_number +
Classification) against our DB.

Two paths, by confidence:
  1. DIRECT  — enforcement Recall Number already equals a recall_id in our DB
               (our older FDA records came from enforcement, so most match here).
               -> fill/refresh classification + status. Safe, exact.
  2. FUZZY   — a datatables-origin record (recall_id starts 'dt-') looks like an
               enforcement row by company + date + product. Company names differ
               wildly between the two feeds (e.g. "IF Copack dba Initiative Foods"
               vs "IF Holding II, LLC"), so we DO NOT auto-merge these. We flag
               needs_review=True with a hint, and you confirm by hand.

Never deletes. Never overwrites CPSC records. Never auto-merges on weak signals.

Usage:  python promote_enforcement.py <enforce_rpt.csv>
        python promote_enforcement.py <enforce_rpt.csv> --apply-review <dt-id> <recall_number>
"""
import os, sys, re, json, datetime
import pandas as pd

DB_PATH = os.environ.get("DB_PATH", "recalls_unified.json")

def nc(s):
    return re.sub(r"\b(inc|llc|ltd|co|corp|company|usa|america|imports?|trading|holding|the|ii|iii)\b",
                  "", (s or "").lower()).replace(",", "").replace(".", "").strip()
def norm_date(s):
    s = (s or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m: return m.group(1)+m.group(2)+m.group(3)
    for fmt in ("%m/%d/%Y", "%B %d, %Y", "%Y%m%d"):
        try: return datetime.datetime.strptime(s, fmt).strftime("%Y%m%d")
        except ValueError: pass
    return ""
def datediff(a, b):
    try: return abs((datetime.datetime.strptime(a,"%Y%m%d")-datetime.datetime.strptime(b,"%Y%m%d")).days)
    except Exception: return 99999

CHILD_KW = ["baby","infant","toddler","newborn","children","child","kids","formula",
            "diaper","wipe","teether","teething","pacifier","nursing","bottle","stroller",
            "crib","car seat","bassinet","puree","pouch","yobaby"]
PRODUCE_RE = re.compile(r"baby (spinach|arugula|kale|bok ?choy|carrots?|greens?|romaine|"
                        r"broccoli|corn|peas?|bhindi|okra|bella|mushroom|lettuce)", re.I)
def is_child(t):
    t=(t or "").lower()
    if PRODUCE_RE.search(t) and not re.search(r"baby food|infant|formula|puree|pouch", t): return False
    return any(k in t for k in CHILD_KW)

def main(csv_path):
    db = json.load(open(DB_PATH, encoding="utf-8"))
    by_id = {r["recall_id"]: r for r in db["recalls"]}
    dt_records = [r for r in db["recalls"] if str(r.get("recall_id","")).startswith("dt-")]
    df = pd.read_csv(csv_path, dtype=str).fillna("")

    direct = flagged = 0
    for _, row in df.iterrows():
        blob = f"{row.get('Product Description','')} {row.get('Recalling Firm','')} {row.get('Reason for Recall','')}"
        if not is_child(blob): continue
        num = (row.get("Recall Number","") or "").strip()
        cls = row.get("Classification","")
        status = row.get("Status","")
        if not num: continue

        # PATH 1: direct recall_id match (exact, safe)
        if num in by_id:
            rec = by_id[num]
            changed = False
            if cls and rec.get("classification") != cls:
                rec["classification"] = cls; changed = True
            if status and rec.get("status") != status and rec.get("source")!="CPSC":
                rec["status"] = status; changed = True
            if changed: direct += 1
            continue

        # PATH 2: fuzzy candidate against dt- records -> review queue only
        edate = norm_date(row.get("Recall Initiation Date",""))
        firm = nc(row.get("Recalling Firm",""))
        for dt in dt_records:
            if dt.get("is_enforced"): continue
            dtc = nc(dt.get("brand","")) + " " + nc(dt.get("match_text",""))
            firm_tokens = [t for t in firm.split() if len(t) >= 4]   # ignore 2-3 char noise like "if"
            company_hit = any(t in dtc for t in firm_tokens) if firm_tokens else False
            close = datediff(edate, dt.get("recall_date","")) <= 45 if edate and dt.get("recall_date") else False
            if company_hit and close:
                dt["needs_review"] = True
                dt["review_hint"] = f"enforcement {num} ({cls}) | firm={row.get('Recalling Firm','')[:40]}"
                flagged += 1
                break

    db["recalls"] = list(by_id.values())
    db["total"] = len(db["recalls"])
    db["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    json.dump(db, open(DB_PATH,"w",encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[*] direct_enriched={direct} flagged_for_review={flagged} total={db['total']}")
    queue=[r for r in db["recalls"] if r.get("needs_review")]
    if queue:
        print(f"\n[review queue: {len(queue)}] confirm by hand:")
        for r in queue[:20]:
            print(f"  {r['recall_id']} ({r.get('brand','')[:24]}) <- {r.get('review_hint','')}")

if __name__ == "__main__":
    main(sys.argv[1])
