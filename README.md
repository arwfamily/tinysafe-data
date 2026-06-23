# TinySafe Recall Automation

Keeps `recalls_unified.json` current from FDA and CPSC, with a manual monthly
step for FDA enforcement classification.

## Design in one line

Daily, fully automated: **FDA datatables** (timely) + **CPSC API** (stable).
Monthly, by hand: **FDA enforcement CSV** to fill `recall_id` / `Classification`.
No OpenFDA (it misses too much). Every automated source is a plain GET with no
bot wall, so unattended runs don't silently die.

## Why this split

| Source | What it gives | Automatable? |
|---|---|---|
| FDA datatables (xlsx) | newest recalls within days | yes — clean GET, confirmed 200 |
| CPSC API (json) | full child-product recalls, per-recall URL | yes — clean GET, confirmed 200 |
| FDA enforcement (CSV) | `recall_number`, `Classification`, `Status` | no — behind bot protection + session |

The enforcement feed also trails the others by ~4 months, so it is never the
thing standing between a parent and a fresh recall. Doing it monthly by hand is
fine and avoids a fragile scraper.

## Files

- `sync_recalls.py` — daily job. Fetches datatables + CPSC, applies the
  baby/child filter (exclusion-first: it would rather keep a borderline item
  than drop a real recall), normalizes to the app schema, and merges into
  `recalls_unified.json` by `recall_id`. Preserves the existing envelope and
  every existing record. CPSC is left untouched by the FDA path and vice versa.
- `promote_enforcement.py` — monthly job. Reconciles the enforcement CSV:
  - **Direct match** (enforcement `Recall Number` == an existing `recall_id`):
    fills/refreshes `classification` and `status`. Exact and safe — most of our
    older FDA records match here because they originally came from enforcement.
  - **Fuzzy candidate** (a `dt-` datatables record that looks like an
    enforcement row by company + date): NOT auto-merged. The two feeds use
    different company names for the same recall (e.g. "IF Copack dba Initiative
    Foods" vs "IF Holding II, LLC") and different product wording, so auto-merge
    would risk fusing different recalls. These are flagged `needs_review` with a
    hint, for you to confirm by hand.
- `.github/workflows/sync.yml` — runs `sync_recalls.py` daily at 08:00 ET and
  commits any changes.

## Schema notes

Records match the existing `recalls_unified.json` shape (24 fields incl.
`hazard`, `action`, `plain_reason`, `display_category`, `display_name`, and the
`match_*` fields used for My Brands matching). Two fields support the lifecycle:

- `is_enforced` — `false` for datatables-origin records awaiting enforcement
  classification; `true` once enforced (or for CPSC, which carries its own id).
- `legacy_id` / `needs_review` / `review_hint` — set during promotion so already
  sent deep-links keep resolving and ambiguous matches surface for review.

## Running

Daily sync runs itself. To run on demand: Actions tab → this workflow → Run.

Monthly enforcement step (by hand):

1. Download the FDA Enforcement Report CSV (search "baby", export). The download
   needs a real browser session, which is why this step isn't automated.
2. Put the CSV next to the scripts and run:
   ```
   python promote_enforcement.py 06-23-2026_enforce_rpt.csv
   ```
3. Review anything printed under `[review queue]` and confirm real matches.

## What this does NOT do

- It does not scrape the enforcement download endpoint (bot-protected).
- It does not auto-merge weak fuzzy matches (data-loss / contamination risk).
- It does not touch CPSC records from the FDA path.
- It does not use OpenFDA.

## Current state (verified)

At setup time, cross-checking the enforcement CSV against the 781-record DB found
143 direct `recall_id` matches, all already classified — i.e. the DB was already
current and there was nothing to back-fill. The value of the cross-check is
forward-looking: today's datatables-only recalls (e.g. the June infant formula
and baby-wipe recalls) get their classification filled once enforcement catches
up in the following months.
