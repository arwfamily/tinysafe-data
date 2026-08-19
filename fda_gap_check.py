#!/usr/bin/env python3
"""
fda_gap_check.py — compare TinySafe's DB against FDA's live Enforcement Report.

READ-ONLY: fetches from openFDA, writes report files, never commits anything.

What it does
  1. Pulls food/drug/device enforcement records from api.fda.gov for a date
     window (default: 2026-06-01 → today, overlapping the archive's 07-22 end
     so late-classified stragglers are caught).
  2. Runs each record through the repo's OWN curation gate
     (fda_enforcement_import.build → tinysafe_curate.curate) — not a keyword
     filter, per the CPSC lesson.
  3. Diffs the curated survivors against recalls_unified.json:
       IN DB          — recall_id already present
       NAME MATCH?    — id absent, but a record with overlapping product-name
                        tokens exists within ±45 days (likely the same event
                        via the datatables track; judge by eye)
       MISSING        — no id, no plausible name match
  4. Writes:
       fda_gap_report.csv          — every curated record with its verdict
       fda_enforcement_new.jsonl   — MISSING records in fda_enforcement.jsonl's
                                     compact schema, ready to review and append
     and prints every MISSING record in full (print-everything method).

Env:  SINCE=YYYYMMDD  TO=YYYYMMDD  UNIFIED=path/to/recalls_unified.json
"""
import os, sys, json, csv, time, datetime, re

import requests

import tinysafe_curate as tcur
import fda_enforcement_import as fimp

SINCE = os.environ.get("SINCE", "20260601")
TO = os.environ.get("TO", datetime.date.today().strftime("%Y%m%d"))
UNIFIED = os.environ.get("UNIFIED", "recalls_unified.json")
CATS = ("food", "drug", "device")
UA = {"User-Agent": "tinysafe-gap-check/1.0"}


def ymd_to_mdy(s):
    """openFDA dates are YYYYMMDD; the importer's _date() expects M/D/YYYY."""
    s = (s or "").strip()
    if re.fullmatch(r"\d{8}", s):
        return f"{int(s[4:6])}/{int(s[6:8])}/{s[:4]}"
    return s


def fetch(cat):
    """All enforcement records for one commodity in the window, paginated."""
    out, skip = [], 0
    base = f"https://api.fda.gov/{cat}/enforcement.json"
    query = f"report_date:[{SINCE} TO {TO}]"
    while True:
        r = requests.get(base, params={"search": query, "limit": 1000, "skip": skip},
                         headers=UA, timeout=60)
        if r.status_code == 404:          # openFDA returns 404 for zero matches
            break
        r.raise_for_status()
        batch = r.json().get("results", [])
        out.extend(batch)
        if len(batch) < 1000:
            break
        skip += 1000
        time.sleep(0.5)
    print(f"[*] openFDA {cat}: {len(out)} records in {SINCE}..{TO}")
    return out


def to_row(rec, cat):
    """Map an openFDA enforcement record onto the CSV-export row shape that
    fda_enforcement_import.build() already knows how to curate and convert."""
    return {
        "Recall Number": rec.get("recall_number", ""),
        "Product Description": rec.get("product_description", ""),
        "Recalling Firm": rec.get("recalling_firm", ""),
        "Reason for Recall": rec.get("reason_for_recall", ""),
        "Classification": rec.get("classification", ""),
        "Status": rec.get("status", ""),
        "Product Quantity": rec.get("product_quantity", ""),
        "Distribution Pattern": rec.get("distribution_pattern", ""),
        "Event ID": rec.get("event_id", ""),
        "City": rec.get("city", ""),
        "State/Province": rec.get("state", ""),
        "Country": rec.get("country", ""),
        "Recall Initiation Date": ymd_to_mdy(rec.get("recall_initiation_date", "")),
        "Center Classification Date": ymd_to_mdy(rec.get("center_classification_date", "")),
        "Termination Date": ymd_to_mdy(rec.get("termination_date", "")),
        "Voluntary/Mandated": rec.get("voluntary_mandated", ""),
        "Initial Firm Notification of Consignee or Public": rec.get("initial_firm_notification", ""),
        "Product Type": {"food": "Food", "drug": "Drugs", "device": "Devices"}[cat],
        "Report Date": ymd_to_mdy(rec.get("report_date", "")),
        "_code_info": rec.get("code_info", ""),
    }


def name_tokens(s):
    stop = {"the", "and", "with", "for", "of", "in", "oz", "count", "pack", "brand"}
    return {t for t in re.split(r"[^a-z0-9]+", tcur.key(s or "")) if len(t) > 3 and t not in stop}


def main():
    tcur.load_brands()

    db = json.load(open(UNIFIED, encoding="utf-8"))
    recalls = db["recalls"] if isinstance(db, dict) else db
    ids = {str(r.get("recall_id", "")).strip().casefold() for r in recalls}
    # name index for the fuzzy pass: (tokens, yyyymmdd int, id, display)
    idx = []
    for r in recalls:
        d = re.sub(r"\D", "", str(r.get("recall_date", "")))[:8]
        idx.append((name_tokens(f"{r.get('product_name','')} {r.get('display_name','')}"),
                    int(d) if len(d) == 8 else 0,
                    r.get("recall_id", ""), (r.get("display_name") or "")[:60]))

    rows_out, new_jsonl, verdicts = [], [], {"IN DB": 0, "NAME MATCH?": 0, "MISSING": 0}
    for cat in CATS:
        for rec in fetch(cat):
            row = to_row(rec, cat)
            built = fimp.build(row)          # repo's own curation gate
            if built is None:
                continue
            rid = row["Recall Number"].strip()
            rdate = int(re.sub(r"\D", "", rec.get("report_date", "0") or "0") or 0)
            if rid.casefold() in ids:
                verdict, match = "IN DB", rid
            else:
                cands = []
                toks = name_tokens(f"{row['Product Description']} {row['Recalling Firm']}")
                for etoks, edate, eid, edisp in idx:
                    if not toks or not etoks:
                        continue
                    overlap = len(toks & etoks)
                    if overlap >= max(2, min(len(toks), len(etoks)) // 2) and \
                       (edate == 0 or abs(edate - rdate) <= 45):
                        cands.append(f"{eid} {edisp}")
                if cands:
                    verdict, match = "NAME MATCH?", " | ".join(cands[:3])
                else:
                    verdict, match = "MISSING", ""
            verdicts[verdict] += 1
            rows_out.append({
                "verdict": verdict, "recall_number": rid,
                "report_date": rec.get("report_date", ""),
                "classification": row["Classification"], "product_type": row["Product Type"],
                "firm": row["Recalling Firm"][:60],
                "product": row["Product Description"][:120],
                "reason": row["Reason for Recall"][:160],
                "signals": ",".join(built.get("curation_signals", [])),
                "matched": match,
            })
            if verdict == "MISSING":
                new_jsonl.append({
                    "recall_id": rid,
                    "product_name": row["Product Description"],
                    "brand": row["Recalling Firm"],
                    "reason": row["Reason for Recall"],
                    "classification": row["Classification"],
                    "status": row["Status"],
                    "units": row["Product Quantity"],
                    "distribution": row["Distribution Pattern"],
                    "event_id": row["Event ID"],
                    "firm_city": row["City"], "firm_state": row["State/Province"],
                    "firm_country": row["Country"],
                    "initiated_date": row["Recall Initiation Date"],
                    "classified_date": row["Center Classification Date"],
                    "terminated_date": row["Termination Date"],
                    "recall_initiated_by": row["Voluntary/Mandated"],
                    "firm_notification": row["Initial Firm Notification of Consignee or Public"],
                    "product_type": row["Product Type"],
                    "report_date": row["Report Date"],
                    "code_info": (row["_code_info"] or "")[:2000],
                })

    rows_out.sort(key=lambda r: (r["verdict"], r["report_date"]))
    with open("fda_gap_report.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()) if rows_out else
                           ["verdict"])
        w.writeheader()
        w.writerows(rows_out)
    with open("fda_enforcement_new.jsonl", "w", encoding="utf-8") as f:
        for r in new_jsonl:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\n[*] window {SINCE}..{TO} | curated survivors: {len(rows_out)}")
    for k, v in verdicts.items():
        print(f"    {k}: {v}")
    print("\n===== MISSING (full text, judge by eye) =====")
    for r in rows_out:
        if r["verdict"] == "MISSING":
            print(f"\n{r['recall_number']} | {r['report_date']} | {r['classification']}"
                  f" | {r['product_type']}\n  firm: {r['firm']}\n  product: {r['product']}"
                  f"\n  reason: {r['reason']}\n  signals: {r['signals']}")
    print("\n===== NAME MATCH? (verify these are truly the same event) =====")
    for r in rows_out:
        if r["verdict"] == "NAME MATCH?":
            print(f"{r['recall_number']} | {r['product'][:70]} -> {r['matched']}")
    if not rows_out:
        print("no curated records in window — widen SINCE or check the query")
    return 0


if __name__ == "__main__":
    sys.exit(main())
