#!/usr/bin/env python3
"""Overlay the verified US release on the feed build_v2_feed.py makes.

build_v2_feed.py reads the FDA structured ingredient table only and matches
recalls on the first words of a recall's product name. Both were wrong in a
way that matters (2026-10-22 review): fragrance printed on the label was missed
on most products that print it, and recalls were shown on products that were
never recalled (Coppertone Pure & Simple lotions, Badger Kids sticks, Banana
Boat mineral lotions).

The verified US release (arwfamily/tinysafe-dailymed-scraper-v2,
data/releases/us/latest/us_all.jsonl) reads the printed Drug Facts as well as
the filing, and every recall on it is matched on setid/NDC or reviewed against
the recall's own product description. For every product the release has:

  _x_fragrance       = the release's fragrance flag (printed label + filing)
  _x_eu_allergen     = the release's EU fragrance-allergen flag
  _recall_history    = a recall matched to this exact product
  _recall_reasons    = that recall's hazards
  inactives_label_verified, label_check, us_release_id  (added; nothing removed)

Products the release does not list as an in-scope sunscreen keep their values and
get "_in_us_release": false.

Usage: python3 scripts/overlay_v4.py feed.json us_all.jsonl
"""
import json
import sys


def main():
    feed_p, rel_p = sys.argv[1], sys.argv[2]
    feed = json.load(open(feed_p, encoding="utf-8"))
    rel = {}
    for line in open(rel_p, encoding="utf-8"):
        if line.strip():
            r = json.loads(line)
            rel[r["setid"]] = r
    P = feed["products"] if isinstance(feed, dict) else feed
    n = changed_fr = changed_rec = 0
    for p in P:
        r = rel.get(p.get("setid"))
        if not r:
            p["_in_us_release"] = False
            continue
        n += 1
        flags = {f[0] for f in r.get("flags") or []}
        fr = "fragrance" in flags
        changed_fr += fr != bool(p.get("_x_fragrance"))
        p["_x_fragrance"] = fr
        p["_x_eu_allergen"] = "eu_fragrance_allergen" in flags
        matched = [x for x in r.get("recalls") or [] if x.get("status") == "matched"]
        hist = bool(matched)
        changed_rec += hist != bool(p.get("_recall_history"))
        p["_recall_history"] = hist
        p["_recall_reasons"] = sorted({h for x in matched for h in x.get("hazards") or []})
        p["inactives_label_verified"] = r.get("inactives") or []
        p["label_check"] = r.get("label_check")
        p["_in_us_release"] = True
    if isinstance(feed, dict):
        rid = None
        try:
            rid = json.load(open(rel_p.rsplit("/", 1)[0] + "/manifest.json")).get("release_id")
        except Exception:  # noqa: BLE001
            pass
        feed["us_release_id"] = rid
        feed["notes"] = (str(feed.get("notes") or "") + " | fragrance, EU allergen and recall fields from the "
                         "verified US release (scripts/overlay_v4.py)").strip(" |")
    json.dump(feed, open(feed_p, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"overlay: {n}/{len(P)} products in the US release; fragrance changed on {changed_fr}, "
          f"recall flag changed on {changed_rec}")


if __name__ == "__main__":
    main()
