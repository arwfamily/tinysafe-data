"""
TinySafe — import FDA Enforcement Report exports dropped into inbox/fda/.

How to use (no terminal needed):
  1. On the FDA Enforcement Report site (iRES), search by REPORT DATE only —
     one or two weeks at a time, every product type, NO keyword. Export.
  2. On GitHub, open inbox/fda/ → Add file → Upload files → drop the export
     (.csv or .xlsx). Commit.
  3. The daily sync picks it up. Nothing else to do.

Why no keyword: the export is a text search, so every keyword is a blind spot
(Gerber Arrowroot Biscuits, Pumpkin Tree pouches and SNOO bassinets were all
missed that way). A full week is a few hundred rows, well under the 1,000-row
export cap, and tinysafe_curate decides membership on every row — the same
gate every other FDA record goes through.

What this does with each file:
  * new recall numbers that pass curation  -> appended to fda_enforcement.jsonl
  * recall numbers we already hold         -> status / termination refreshed
  * everything else                        -> not stored (adult products)
  * the raw file                           -> gzipped into inbox/fda/processed/
                                              so a future rule change can
                                              re-read it, then removed
  * a line per file                        -> inbox/fda/IMPORT_LOG.md

A file that has no "Recall Number" column is left in place and reported, never
deleted. A file with exactly 1,000 rows is flagged: the export was truncated
and the date range must be split.

Run:  python import_fda_inbox.py [repo_dir]
"""
import csv
import datetime
import glob
import gzip
import io
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fda_enforcement_import as fimp  # noqa: E402

CODE_CAP = 2000

# CSV column -> compact jsonl key (the shape fda_enforcement.jsonl already uses)
COLMAP = [
    ("recall_id", "Recall Number"), ("product_name", "Product Description"),
    ("brand", "Recalling Firm"), ("reason", "Reason for Recall"),
    ("classification", "Classification"), ("status", "Status"),
    ("units", "Product Quantity"), ("distribution", "Distribution Pattern"),
    ("event_id", "Event ID"), ("firm_city", "City"), ("firm_state", "State/Province"),
    ("firm_country", "Country"), ("initiated_date", "Recall Initiation Date"),
    ("classified_date", "Center Classification Date"),
    ("terminated_date", "Termination Date"), ("recall_initiated_by", "Voluntary/Mandated"),
    ("firm_notification", "Initial Firm Notification of Consignee or Public"),
    ("product_type", "Product Type"), ("report_date", "Report Date"),
]
# fields refreshed on records we already hold (lifecycle can move; text can't)
REFRESH = ("status", "terminated_date", "classification")

# Header aliases seen across iRES export versions
ALIASES = {
    "Recall Event ID": "Event ID", "Event Id": "Event ID",
    "Recall Status": "Status", "Product Classification": "Classification",
    "Firm Name": "Recalling Firm", "Recalling Firm Name": "Recalling Firm",
    "Center Classification Date ": "Center Classification Date",
}


def _norm_header(h):
    h = (h or "").strip().lstrip("﻿")
    return ALIASES.get(h, h)


def read_file(path):
    """Yield CSV-shaped dict rows (code-info spillover rejoined)."""
    low = path.lower()
    if low.endswith((".xlsx", ".xls")):
        import pandas as pd
        df = pd.read_excel(path, dtype=str).fillna("")
        df.columns = [_norm_header(c) for c in df.columns]
        spill = [c for c in df.columns if fimp.SPILL.match(c)]
        for rec in df.to_dict("records"):
            code = (rec.get("Code Info") or "").strip()
            if spill:
                code = " ".join(x for x in [code] + [(rec.get(c) or "").strip() for c in spill] if x)
            rec["_code_info"] = code
            yield rec
        return
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    reader = csv.DictReader(io.StringIO(text))
    reader.fieldnames = [_norm_header(h) for h in (reader.fieldnames or [])]
    spill = [c for c in reader.fieldnames if fimp.SPILL.match(c)]
    for row in reader:
        code = (row.get("Code Info") or "").strip()
        if spill:
            code = " ".join(x for x in [code] + [(row.get(c) or "").strip() for c in spill] if x)
        row["_code_info"] = code
        yield row


def compact(row):
    out = {k: (row.get(col) or "").strip() for k, col in COLMAP}
    out["code_info"] = (row.get("_code_info") or "")[:CODE_CAP]
    return out


def _date_key(s):
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s or "")
    return f"{m.group(3)}{int(m.group(1)):02d}{int(m.group(2)):02d}" if m else ""


def main(repo="."):
    inbox = os.path.join(repo, "inbox", "fda")
    processed = os.path.join(inbox, "processed")
    status_dir = os.path.join(repo, ".pipeline_status")
    os.makedirs(status_dir, exist_ok=True)
    jsonl = os.path.join(repo, "fda_enforcement.jsonl")

    rows_by_id, order = {}, []
    with open(jsonl, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                r = json.loads(line)
                rows_by_id[r["recall_id"]] = r
                order.append(r["recall_id"])

    files = sorted(p for p in glob.glob(os.path.join(inbox, "*"))
                   if p.lower().endswith((".csv", ".xlsx", ".xls")))
    status = {"source": "FDA enforcement inbox",
              "ran_at": datetime.datetime.utcnow().isoformat() + "Z",
              "files": [], "warnings": []}
    log_lines = []
    total_new = total_upd = 0

    for path in files:
        name = os.path.basename(path)
        info = {"file": name, "rows": 0, "new": 0, "refreshed": 0, "not_child": 0}
        try:
            rows = list(read_file(path))
        except Exception as e:
            info["error"] = f"could not read: {e}"
            status["files"].append(info)
            status["warnings"].append(f"{name}: could not read ({e}) — left in inbox")
            continue
        if not rows or "Recall Number" not in rows[0]:
            info["error"] = "no 'Recall Number' column — not an Enforcement Report export"
            status["files"].append(info)
            status["warnings"].append(f"{name}: no 'Recall Number' column — left in inbox")
            continue
        info["rows"] = len(rows)
        if len(rows) >= 1000:
            status["warnings"].append(
                f"{name}: exactly {len(rows)} rows — the iRES export cap. Rows past 1,000 "
                f"were cut off; re-export this range in smaller date windows.")
        dates = sorted(d for d in (_date_key(r.get("Report Date")) for r in rows) if d)
        if dates:
            info["report_dates"] = f"{dates[0]}–{dates[-1]}"
        seen = set()
        for row in rows:
            rid = (row.get("Recall Number") or "").strip()
            if not rid or rid in seen:
                continue
            seen.add(rid)
            c = compact(row)
            if rid in rows_by_id:
                old = rows_by_id[rid]
                changed = False
                for k in REFRESH:
                    if c.get(k) and c[k] != old.get(k):
                        old[k] = c[k]
                        changed = True
                if changed:
                    info["refreshed"] += 1
                continue
            if fimp.build(row) is None:
                info["not_child"] += 1
                continue
            rows_by_id[rid] = c
            order.append(rid)
            info["new"] += 1
        total_new += info["new"]
        total_upd += info["refreshed"]

        os.makedirs(processed, exist_ok=True)
        stamp = datetime.date.today().isoformat()
        with open(path, "rb") as src, gzip.open(
                os.path.join(processed, f"{stamp}_{name}.gz"), "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.remove(path)
        status["files"].append(info)
        log_lines.append(f"| {stamp} | {name} | {info.get('report_dates', '?')} | {info['rows']} | "
                         f"{info['new']} | {info['refreshed']} | {info['not_child']} |")

    if total_new or total_upd:
        with open(jsonl, "w", encoding="utf-8") as f:
            for rid in order:
                f.write(json.dumps(rows_by_id[rid], ensure_ascii=False) + "\n")

    if log_lines:
        logp = os.path.join(inbox, "IMPORT_LOG.md")
        if not os.path.exists(logp):
            with open(logp, "w", encoding="utf-8") as f:
                f.write("# FDA Enforcement Report imports\n\n"
                        "| imported | file | report dates | rows | new child recalls | "
                        "status refreshed | not child products |\n|---|---|---|---|---|---|---|\n")
        with open(logp, "a", encoding="utf-8") as f:
            f.write("\n".join(log_lines) + "\n")

    # Freshness: how old is the newest report we hold?
    newest = max((_date_key(r.get("report_date")) for r in rows_by_id.values()), default="")
    status["newest_report_date"] = newest
    if newest:
        age = (datetime.date.today() - datetime.datetime.strptime(newest, "%Y%m%d").date()).days
        status["newest_report_age_days"] = age
        if age > 14:
            status["warnings"].append(
                f"FDA Enforcement Report data is {age} days old (newest report {newest}). "
                f"Upload the latest weeks to inbox/fda/.")
    status["new"], status["refreshed"] = total_new, total_upd
    json.dump(status, open(os.path.join(status_dir, "fda_inbox.json"), "w"), indent=1)
    print(f"[fda-inbox] {len(files)} file(s): {total_new} new child recalls, "
          f"{total_upd} status refreshed; newest report {newest}")
    for w in status["warnings"]:
        print(f"[fda-inbox] ! {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
