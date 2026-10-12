"""
TinySafe — append NEW NHTSA child-restraint (car seat) recalls automatically.

nhtsa_child_restraints.json was built once by hand (251 campaigns, every hazard
hand-assigned) and never grew: the newest record is the campaign that existed
on the day it was built. NHTSA publishes the same flat file daily, so this
script downloads it, keeps child restraints (RCLTYPECD == "C"), collapses the
one-row-per-model layout on CAMPNO, and APPENDS campaigns we do not hold.

It never edits an existing record. The hand-assigned hazards, display names and
rationales stay exactly as they are.

New campaigns get a rule-based hazard with hazard_hand_assigned=false and
needs_review=true, so they are visible for a human pass. When the rules are
unsure they choose crash_protection (tier 2) — for a car seat the safe
direction is to over-warn, never to file a restraint failure as a label issue.

Safety:
  * Field order is checked against records we already hold before anything is
    appended. The flat file has no header; if NHTSA ever reorders columns the
    dates and unit counts of known campaigns stop matching, and the run aborts
    instead of writing garbage.
  * Network failure is not fatal. nhtsa.gov is fronted by a bot filter; if the
    download is refused, a file dropped into inbox/nhtsa/ (the same zip, or the
    .txt inside it) is used instead, and a status line says which path ran.

Run:  python fetch_nhtsa.py [repo_dir]
Writes nhtsa_child_restraints.json (only if something was added) and
.pipeline_status/nhtsa.json (always).
"""
import csv
import datetime
import glob
import io
import json
import os
import re
import sys
import zipfile

URLS = [
    "https://static.nhtsa.gov/odi/ffdd/rcl/FLAT_RCL_POST_2010.zip",
    "https://static.nhtsa.gov/odi/ffdd/rcl/FLAT_RCL.zip",
]

# Field order of the ODI recalls flat file (RCL.txt). Tab-delimited, no header.
FIELDS = ["RECORD_ID", "CAMPNO", "MAKETXT", "MODELTXT", "YEARTXT", "MFGCAMPNO",
          "COMPNAME", "MFGNAME", "BGMAN", "ENDMAN", "RCLTYPECD", "POTAFF",
          "ODATE", "INFLUENCED_BY", "MFGTXT", "RCDATE", "DATEA", "RPNO", "FMVSS",
          "DESC_DEFECT", "CONEQUENCE_DEFECT", "CORRECTIVE_ACTION", "NOTES",
          "RCL_CMPT_ID", "MFR_COMP_NAME", "MFR_COMP_DESC", "MFR_COMP_PTNO",
          "DO_NOT_DRIVE", "PARK_OUTSIDE"]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "*/*",
}

# Known car-seat makes, cased the way the brands write them. Anything not
# listed is title-cased; the first run also learns casing from records we hold.
MAKE_CASE = {
    "UPPABABY": "UPPAbaby", "MAXI-COSI": "Maxi-Cosi", "MAXI COSI": "Maxi-Cosi",
    "SAFETY 1ST": "Safety 1st", "BABY TREND": "Baby Trend", "PEG PEREGO": "Peg Perego",
    "PEG-PEREGO": "Peg Perego", "ORBIT BABY": "Orbit Baby", "SILVER CROSS": "Silver Cross",
    "GB": "gb", "BOB": "BOB", "WAYB": "WAYB", "IMMI": "IMMI", "4MOMS": "4moms",
    "EVENFLO": "Evenflo", "GRACO": "Graco", "BRITAX": "Britax", "CHICCO": "Chicco",
    "NUNA": "Nuna", "DOONA": "Doona", "CYBEX": "Cybex", "CLEK": "Clek", "DIONO": "Diono",
    "COSCO": "Cosco", "JOOVY": "Joovy", "KOLCRAFT": "Kolcraft", "BUGABOO": "Bugaboo",
    "JOIE": "Joie", "MIFOLD": "mifold", "RECARO": "Recaro", "HARMONY": "Harmony",
}

# --- hazard rules -----------------------------------------------------------
# Checked in order; first match wins. Text = defect + consequence.
RULES = [
    ("entrapment", r"(?:difficult|unable|cannot|can not|hard)\s+(?:to\s+)?(?:unlatch|release|unbuckle|open|remove\s+the\s+child)"
                   r"|buckle\W+(?:may\s+|can\s+)?(?:stick|jam|not\s+release|fail\s+to\s+release)"
                   r"|buckle\s+release\s+force|release\s+force|stick\w*\s+latched|egress"
                   r"|entrap|remove\s+the\s+child\s+in\s+an\s+emergency|finger\w*\s+(?:can\s+)?(?:become\s+)?(?:stuck|trapped)"),
    ("strangulation", r"strangulat|loop\s+around"),
    ("choking", r"chok|small\s+parts?|swallow|pick\s+(?:off|at)\s|ingest|in\s+(?:his|her|his/her|their)\s+mouth|mouth\w*"),
    ("flammable", r"\b302\b|flammab|burn\w*\s+(?:at\s+a\s+)?rate|burns?\s+faster"),
    ("fire", r"overheat|melt|ignit|catch\s+fire|fire\s+hazard|smok"),
    ("fall", r"carry(?:ing)?\s+handle|handle\w*\s+(?:locks?\s+)?(?:\w+\s+){0,4}(?:release|unlock|move)"
             r"|(?:child|infant)\s+(?:inside\s+the\s+carrier\s+)?(?:may|could|can)\s+fall|fall\s+(?:out|from|to\s+the\s+ground)|flip\s+forward"),
    ("laceration", r"lacerat|pinch|exposing\s+springs"),
]
LABEL = re.compile(r"\blabel|instruction|manual|registration\s+card|owner.?s\s+guide|diagram"
                   r"|marking|printed|stated?\s+(?:an\s+)?incorrect|incorrectly\s+state|missing\s+(?:the\s+)?(?:label|card|warning)"
                   r"|without\s+the\s+(?:separate\s+)?(?:seat\s+)?base", re.I)
# Physical failure language. FMVSS / "fail to comply" is NOT physical: label
# defects are written as FMVSS 213 noncompliance too.
PHYSICAL = re.compile(r"webbing|harness\s+(?:may|can|could)?\s*(?:break|tear|fray|detach|loosen|fail)"
                      r"|anchor\w*\s+(?:may|can|could)?\s*(?:break|detach|fail)|shell|crack|fractur"
                      r"|detach|separat|\bbreak|frays?|weld|latch\s+(?:may|can|could)?\s*(?:fail|disengage)"
                      r"|corro|foam|crash\s+test|excessive\s+(?:head|chest)|shift", re.I)


# A design that cannot be installed correctly is a restraint failure even when
# the notice also mentions labels (22C007/22C008 Joybuy/Marainbow).
DESIGN = re.compile(r"design|prevent\w*\s+(?:proper|correct)|cannot\s+be\s+(?:properly\s+)?install", re.I)


def classify(defect, consequence):
    text = f"{defect} {consequence}".lower()
    if not text.strip():
        return "general", "auto: no defect text at source"
    for hz, pat in RULES:
        m = re.search(pat, text, re.I)
        if m:
            return hz, f"auto: '{m.group(0).strip()}'"
    # label/instruction only when the DEFECT is about paperwork and nothing
    # physical is described as failing. Consequence text mentions "crash" for
    # nearly every car-seat recall, label defects included, so it cannot decide.
    if (LABEL.search(defect or "") and not PHYSICAL.search(defect or "")
            and not DESIGN.search(defect or "")):
        return "label_instruction", f"auto: '{LABEL.search(defect).group(0)}'"
    return "crash_protection", "auto: default for a child restraint defect"


# --- io ---------------------------------------------------------------------
def download():
    try:
        import requests
    except ImportError:
        return None, "requests not installed"
    last = ""
    for url in URLS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=180)
            if r.status_code == 200 and r.content[:2] == b"PK":
                return r.content, url
            last = f"{url} -> HTTP {r.status_code}"
        except Exception as e:  # network, TLS, timeout
            last = f"{url} -> {e}"
    return None, last


def read_rows(blob_or_path):
    """Yield dict rows from a zip (bytes or path) or a .txt path."""
    if isinstance(blob_or_path, (bytes, bytearray)):
        zf = zipfile.ZipFile(io.BytesIO(blob_or_path))
    elif str(blob_or_path).lower().endswith(".zip"):
        zf = zipfile.ZipFile(blob_or_path)
    else:
        zf = None
    if zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
        if not names:
            return
        raw = zf.read(names[0])
    else:
        raw = open(blob_or_path, "rb").read()
    text = raw.decode("latin-1")
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 22:
            continue
        parts += [""] * (len(FIELDS) - len(parts))
        yield {k: (parts[i] or "").strip() for i, k in enumerate(FIELDS)}


def ymd(s):
    s = re.sub(r"\D", "", s or "")
    return s if len(s) == 8 else ""


def make_case(raw, learned):
    raw = (raw or "").strip()
    up = raw.upper()
    if up in learned:
        return learned[up]
    if up in MAKE_CASE:
        return MAKE_CASE[up]
    return " ".join(w if (len(w) <= 3 and w.isupper() and not w.isalpha()) else w.capitalize()
                    for w in raw.split())


def build(campno, rows, learned):
    rows = sorted(rows, key=lambda r: (r["MAKETXT"], r["MODELTXT"]))
    first = rows[0]
    makes = list(dict.fromkeys(make_case(r["MAKETXT"], learned) for r in rows if r["MAKETXT"]))
    models = []
    seen = set()
    for r in rows:
        key = (r["MAKETXT"], r["MODELTXT"])
        if key in seen:
            continue
        seen.add(key)
        models.append({"model": r["MODELTXT"], "manufactured_from": ymd(r["BGMAN"]),
                       "manufactured_to": ymd(r["ENDMAN"])})
    brand = " / ".join(makes) or make_case(first["MFGNAME"], learned)
    names = [m["model"] for m in models]
    if len(names) > 3:
        product = f"{brand} {'; '.join(names[:3])} (+{len(names) - 3} more models)"
        display = f"{brand} car seats ({len(names)} models)"
    else:
        product = f"{brand} {'; '.join(names)}"
        display = f"{brand} {' / '.join(n.title() for n in names)}"
    froms = [m["manufactured_from"] for m in models if m["manufactured_from"]]
    tos = [m["manufactured_to"] for m in models if m["manufactured_to"]]
    defect, conseq = first["DESC_DEFECT"], first["CONEQUENCE_DEFECT"]
    hazard, why = classify(defect, conseq)
    try:
        units = int(re.sub(r"\D", "", first["POTAFF"]) or 0)
    except ValueError:
        units = 0
    rec = {
        "source": "NHTSA",
        "record_type": "recall",
        "category": "Baby & Kids",
        "display_category": "Car Seats",
        "recall_id": f"NHTSA-{campno}",
        "campaign_no": campno,
        "mfg_campaign_no": first["MFGCAMPNO"],
        "product_name": product,
        "display_name": display[:90],
        "brand": brand,
        "brands": makes or [brand],
        "manufacturer": first["MFGNAME"],
        "recall_date": ymd(first["RCDATE"]),
        "reason": defect,
        "plain_reason": conseq,
        "classification": "",
        "status": "Recalled",
        # The 6-char prefix resolves to the recall; the 9-char form renders an
        # empty search page (verified in a real browser, 2026-07-28).
        "url": f"https://www.nhtsa.gov/recalls?nhtsaId={campno[:6]}",
        "hazard": hazard,
        "hazard_hand_assigned": False,
        "hazard_rationale": why,
        "needs_review": True,
        "action": first["CORRECTIVE_ACTION"],
        "action_curated": False,
        "units_affected": units,
        "manufactured_from": min(froms) if froms else "",
        "manufactured_to": max(tos) if tos else "",
        "models": models,
        "model_row_count": len(rows),
        "fmvss": first["FMVSS"],
        "influenced_by": first["INFLUENCED_BY"],
        "component": first["COMPNAME"],
        "auto_ingested": datetime.date.today().isoformat(),
    }
    m = re.search(r"(?:replac\w+|supersed\w+)\s+(?:by\s+)?(?:recall\s+)?(\d{2}C\d{3}(?:\d{3})?)",
                  first["CORRECTIVE_ACTION"] + " " + first["NOTES"], re.I)
    if m:
        sup = m.group(1)
        rec["superseded_by"] = "NHTSA-" + (sup if len(sup) == 9 else sup + "000")
    return rec


def main(repo="."):
    path = os.path.join(repo, "nhtsa_child_restraints.json")
    status_dir = os.path.join(repo, ".pipeline_status")
    os.makedirs(status_dir, exist_ok=True)
    status = {"source": "NHTSA", "ran_at": datetime.datetime.utcnow().isoformat() + "Z",
              "ok": False, "added": [], "note": "", "warnings": []}

    def finish(code=0):
        json.dump(status, open(os.path.join(status_dir, "nhtsa.json"), "w"), indent=1)
        print(f"[nhtsa] {status['note']}")
        return code

    doc = json.load(open(path, encoding="utf-8"))
    recs = doc["recalls"]
    have = {r.get("campaign_no") for r in recs}

    blob, origin = download()
    if blob is None:
        manual = sorted(glob.glob(os.path.join(repo, "inbox", "nhtsa", "*.zip")) +
                        glob.glob(os.path.join(repo, "inbox", "nhtsa", "*.txt")))
        if not manual:
            status["note"] = f"download failed ({origin}) and inbox/nhtsa/ is empty — nothing added"
            # One reminder a week, not one a day: if NHTSA keeps refusing the
            # robot, the fix is a monthly manual upload, and a daily alarm
            # would only teach everyone to ignore the issue.
            if datetime.date.today().weekday() == 0:
                status["warnings"] = [
                    "NHTSA refused the automatic download. Download FLAT_RCL_POST_2010.zip from "
                    "https://www.nhtsa.gov/nhtsa-datasets-and-apis (Recalls → flat file) and upload "
                    "it to inbox/nhtsa/ on GitHub; the next run uses it."]
            return finish(0)
        src, origin = manual[-1], f"inbox file {os.path.basename(manual[-1])}"
        status["inbox_file"] = manual[-1]
    else:
        src = blob

    by_camp = {}
    total_rows = 0
    for row in read_rows(src):
        total_rows += 1
        if row["RCLTYPECD"] != "C":
            continue
        by_camp.setdefault(row["CAMPNO"], []).append(row)

    # Field-order self check against campaigns we already hold.
    known = {r["campaign_no"]: r for r in recs if r.get("campaign_no") in by_camp}
    checked = matched = 0
    learned = {}
    for camp, r in known.items():
        rows = by_camp[camp]
        checked += 1
        date_ok = ymd(rows[0]["RCDATE"]) == r.get("recall_date")
        try:
            units_ok = int(re.sub(r"\D", "", rows[0]["POTAFF"]) or -1) == int(r.get("units_affected") or -2)
        except ValueError:
            units_ok = False
        if date_ok or units_ok:
            matched += 1
        # learn brand casing from hand-built records
        if len(r.get("brands") or []) == 1:
            learned[rows[0]["MAKETXT"].upper()] = r["brands"][0]
    status.update({"origin": origin, "rows_read": total_rows,
                   "child_restraint_campaigns": len(by_camp),
                   "known_checked": checked, "known_matched": matched})
    if total_rows == 0 or len(by_camp) < 20:
        status["note"] = f"file from {origin} looks wrong ({total_rows} rows, {len(by_camp)} car-seat campaigns) — nothing added"
        return finish(1)
    if checked < 10 or matched / checked < 0.9:
        status["note"] = (f"field-order check FAILED ({matched}/{checked} known campaigns match) — "
                          f"NHTSA may have changed the file layout; nothing added")
        return finish(1)

    new = [build(c, rows, learned) for c, rows in sorted(by_camp.items()) if c not in have]
    new = [r for r in new if r["recall_date"]]
    if new:
        recs.extend(new)
        doc["recalls"] = recs
        doc["total"] = len(recs)
        tally = {}
        for r in recs:
            tally[r.get("hazard", "")] = tally.get(r.get("hazard", ""), 0) + 1
        doc["hazard_tally"] = tally
        doc["last_auto_update"] = datetime.date.today().isoformat()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
    if status.get("inbox_file"):
        # used once; the zip is ~30 MB, so delete rather than archive it
        for p in glob.glob(os.path.join(repo, "inbox", "nhtsa", "*.zip")) + \
                glob.glob(os.path.join(repo, "inbox", "nhtsa", "*.txt")):
            os.remove(p)
    status["ok"] = True
    status["added"] = [f"{r['recall_id']} {r['recall_date']} {r['hazard']} | {r['display_name']}" for r in new]
    status["note"] = (f"{origin}: {len(by_camp)} car-seat campaigns, field check {matched}/{checked}, "
                      f"{len(new)} new appended")
    if new:
        status["warnings"] = [f"{len(new)} new car-seat recall(s) added with an AUTOMATIC hazard "
                              f"label — please review hazard and display name (needs_review=true)."]
        status["items"] = status["added"]
    for line in status["added"]:
        print("  +", line)
    return finish(0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
