# FDA Enforcement Report inbox

Drop FDA Enforcement Report exports here. The daily sync imports them and moves
the raw file into `processed/` (gzipped). Results are listed in `IMPORT_LOG.md`.

**How to export (once a week, after FDA posts the report on Wednesday):**

1. Open the FDA Enforcement Report site: https://www.accessdata.fda.gov/scripts/ires/index.cfm
2. Advanced search → set **Report Date** from / to (one or two weeks).
   Leave product type = all and **do not type any keyword**.
3. Export (CSV or Excel).
4. On GitHub: open this `inbox/fda` folder → **Add file → Upload files** → drop the
   export → **Commit changes**.

Why no keyword: the export is a text search, so every keyword hides recalls whose
names don't contain it (Gerber Arrowroot Biscuits, Pumpkin Tree pouches). A full
week is a few hundred rows, under the 1,000-row export cap. If an export comes
back with exactly 1,000 rows it was cut off — split the date range.

TinySafe's curation keeps only children's products; adult products are not stored.
