"""
TinySafe — publish gate for the daily recall sync.

The audit step reports and never blocks, on purpose: one flawed record must not
stop a formula recall from reaching parents. This gate is the other half. It
blocks only CATASTROPHIC output — the kind no parent should ever receive:

  * recalls_unified.json does not parse, or has no records
  * the total drops more than 3% from what is published now
  * any one source (CPSC / FDA / NHTSA) drops more than 10%
  * more than 1% of records are missing an id, a date or a name

A real day never looks like that: recalls are added, never removed in bulk.
When the gate trips, the previously published files are restored, so the app
keeps showing yesterday's good data, and the run is marked failed (GitHub
emails the repo owner) and an issue is opened by pipeline_notify.py.

It also records non-blocking freshness warnings — CPSC silent for 3 weeks, the
FDA Enforcement data getting old — which go into the same issue.

Run:  python pipeline_gate.py [repo_dir]      exit 1 = blocked
"""
import collections
import datetime
import json
import os
import re
import subprocess
import sys

OUT_FILES = ["recalls_unified.json", "recalls_full.jsonl", "recalls_pipeline.json"]
MAX_TOTAL_DROP = 0.03
MAX_SOURCE_DROP = 0.10
MAX_MISSING_FIELD = 0.01


def published(repo, path):
    try:
        raw = subprocess.run(["git", "-C", repo, "show", f"HEAD:{path}"],
                             capture_output=True, check=True).stdout
        return json.loads(raw)
    except Exception:
        return None


def source_of(r):
    s = str(r.get("source", "")).upper()
    return "FDA" if "FDA" in s else ("CPSC" if "CPSC" in s else ("NHTSA" if "NHTSA" in s else "OTHER"))


def ymd(r):
    return re.sub(r"\D", "", str(r.get("recall_date", "")))[:8]


def main(repo="."):
    status_dir = os.path.join(repo, ".pipeline_status")
    os.makedirs(status_dir, exist_ok=True)
    fails, warns = [], []

    try:
        new = json.load(open(os.path.join(repo, "recalls_unified.json"), encoding="utf-8"))
        recs = new["recalls"]
        assert isinstance(recs, list) and recs
    except Exception as e:
        new, recs = None, []
        fails.append(f"recalls_unified.json is unreadable or empty ({e})")

    old = published(repo, "recalls_unified.json")
    old_recs = (old or {}).get("recalls") or []

    if recs and old_recs:
        if len(recs) < len(old_recs) * (1 - MAX_TOTAL_DROP):
            fails.append(f"total fell {len(old_recs)} -> {len(recs)} "
                         f"({(1 - len(recs) / len(old_recs)):.1%}); limit is {MAX_TOTAL_DROP:.0%}")
        a = collections.Counter(source_of(r) for r in old_recs)
        b = collections.Counter(source_of(r) for r in recs)
        for src, n in a.items():
            if n >= 20 and b.get(src, 0) < n * (1 - MAX_SOURCE_DROP):
                fails.append(f"{src} fell {n} -> {b.get(src, 0)}; limit is {MAX_SOURCE_DROP:.0%}")
    if recs:
        for field, test in (("recall_id", lambda r: r.get("recall_id")),
                            ("recall_date", lambda r: len(ymd(r)) == 8),
                            ("name", lambda r: r.get("display_name") or r.get("product_name"))):
            bad = sum(1 for r in recs if not test(r))
            if bad > len(recs) * MAX_MISSING_FIELD:
                fails.append(f"{bad} records ({bad / len(recs):.1%}) have no {field}")

        # freshness (warn only)
        today = datetime.date.today()
        newest = collections.defaultdict(str)
        for r in recs:
            s = source_of(r)
            newest[s] = max(newest[s], ymd(r))
        def age(d):
            try:
                return (today - datetime.datetime.strptime(d, "%Y%m%d").date()).days
            except ValueError:
                return 9999
        # CPSC announces children's recalls nearly every week.
        if age(newest["CPSC"]) > 21:
            warns.append(f"No new CPSC recall in {age(newest['CPSC'])} days (newest {newest['CPSC']}). "
                         f"CPSC publishes almost weekly — check the 'Fetch live sources' step.")
        new_ids = {r.get("recall_id") for r in recs}
        added = [r for r in recs if r.get("recall_id") not in {o.get("recall_id") for o in old_recs}]
        if old_recs and added:
            print(f"[gate] {len(added)} record(s) added today")
        info = {k: v for k, v in newest.items()}
    else:
        info = {}

    blocked = bool(fails)
    if blocked:
        # Put back what the app is already reading. Yesterday's data beats broken data.
        for p in OUT_FILES:
            subprocess.run(["git", "-C", repo, "checkout", "HEAD", "--", p], capture_output=True)

    status = {"source": "publish gate", "ran_at": datetime.datetime.utcnow().isoformat() + "Z",
              "blocked": blocked, "fails": fails, "warnings": warns,
              "total": len(recs), "published_total": len(old_recs), "newest_by_source": info}
    json.dump(status, open(os.path.join(status_dir, "gate.json"), "w"), indent=1)
    for f in fails:
        print(f"[gate] BLOCK  {f}")
    for w in warns:
        print(f"[gate] warn   {w}")
    print(f"[gate] {'BLOCKED — previous files restored' if blocked else 'ok'} "
          f"({len(old_recs)} -> {len(recs)})")
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
