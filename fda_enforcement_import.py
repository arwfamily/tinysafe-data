"""
TinySafe — import FDA Enforcement Report records.

The pipeline's FDA source is the datatables feed, which is the press-release
listing. Most recalls never get a press release — FDA says so on its own recalls
index — so enforcement-only recalls were invisible to us. A keyword pull of the
Enforcement Report surfaced ~1,500 we did not hold.

Keyword choice matters more than it looks. Two lessons from the pull:

  * `formula` alone is unusable. It matches Jarrow Formulas, Herbalife Formula 1,
    "Maximum Strength Formula" cold remedies, Formula Shaver surgical handpieces
    and "Lamb & Rice Formula for Adult Dogs". 740 of 1,195 candidates entered on
    that one word. Use `infant formula` as a phrase.

  * FDA labels infant medical products `neonatal` and `pediatric`, never `baby`.
    A search on baby/infant/toddler misses NEONATAL INTUBATION TRAY, NEWBORN
    ADMISSION KIT and Gentamicin (PEDIATRIC) entirely — 30 records in the first
    pull, and the same blind spot existed in our own curation patterns.

Recommended keyword set:
    baby · infant · toddler · newborn · neonatal · pediatric · child · children
    · nursery · "infant formula"

Export in year slices. The web export caps at 1,000 rows — one pull came back
at exactly 1000, which is how the cap was found.
"""
import csv
import re

import tinysafe_curate as tcur
import tinysafe_hazard as thaz
import tinysafe_categories as tcat

# Long lot lists overflow `Code Info` into `More Code Info`, `.1` ... `.57`.
# The longest single record seen ran to 68,434 characters after rejoining.
SPILL = re.compile(r'^More Code Info')

FIELDS = {
    'recall_id': 'Recall Number', 'product_name': 'Product Description',
    'brand': 'Recalling Firm', 'reason': 'Reason for Recall',
    'classification': 'Classification', 'status': 'Status',
    'units': 'Product Quantity', 'distribution': 'Distribution Pattern',
    'event_id': 'Event ID', 'firm_city': 'City', 'firm_state': 'State/Province',
    'firm_country': 'Country', 'initiated_date': 'Recall Initiation Date',
    'classified_date': 'Center Classification Date',
    'terminated_date': 'Termination Date',
    'recall_initiated_by': 'Voluntary/Mandated',
    'firm_notification': 'Initial Firm Notification of Consignee or Public',
}


def _date(s):
    s = (s or '').strip()
    m = re.match(r'(\d{1,2})/(\d{1,2})/(\d{4})', s)
    if m:
        return f"{m.group(3)}{int(m.group(1)):02d}{int(m.group(2)):02d}"
    return ''


def read_jsonl(path):
    """Compact enforcement records, one JSON object per line.

    The nine raw CSV exports total 40.5 MB — too large for GitHub's web
    uploader, and 62% of that was `code_info` spillover across up to 58 columns.
    Reduced to the fields this importer actually reads, with lot lists capped,
    the same 2,945 records fit in 4.5 MB.
    """
    import json as _json
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = _json.loads(line)
            yield {
                'Recall Number': r.get('recall_id', ''),
                'Product Description': r.get('product_name', ''),
                'Recalling Firm': r.get('brand', ''),
                'Reason for Recall': r.get('reason', ''),
                'Classification': r.get('classification', ''),
                'Status': r.get('status', ''),
                'Product Quantity': r.get('units', ''),
                'Distribution Pattern': r.get('distribution', ''),
                'Event ID': r.get('event_id', ''),
                'City': r.get('firm_city', ''),
                'State/Province': r.get('firm_state', ''),
                'Country': r.get('firm_country', ''),
                'Recall Initiation Date': r.get('initiated_date', ''),
                'Center Classification Date': r.get('classified_date', ''),
                'Termination Date': r.get('terminated_date', ''),
                'Voluntary/Mandated': r.get('recall_initiated_by', ''),
                'Initial Firm Notification of Consignee or Public': r.get('firm_notification', ''),
                'Product Type': r.get('product_type', ''),
                'Report Date': r.get('report_date', ''),
                '_code_info': r.get('code_info', ''),
            }


def read_any(path):
    return read_jsonl(path) if path.endswith('.jsonl') else read_csv(path)


def read_csv(path):
    """Yield raw rows with the code-info spillover rejoined."""
    with open(path, encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        spill = [c for c in (reader.fieldnames or []) if SPILL.match(c)]
        for row in reader:
            code = (row.get('Code Info') or '').strip()
            if spill:
                code = ' '.join(x for x in [code] + [(row.get(c) or '').strip() for c in spill] if x)
            row['_code_info'] = code
            yield row


def build(row):
    """One unified record, or None if it isn't a children's product."""
    desc = (row.get('Product Description') or '').strip()
    firm = (row.get('Recalling Firm') or '').strip()
    reason = (row.get('Reason for Recall') or '').strip()

    ok, signals, _ = tcur.curate(product=desc, heading=firm, description=desc, hazard=reason)
    if not ok:
        return None

    rec = {'source': 'FDA', 'record_type': 'recall', 'in_feed_scope': True,
           'action_curated': False, 'curation_signals': signals}
    for dest, col in FIELDS.items():
        v = (row.get(col) or '').strip()
        if v:
            rec[dest] = v
    for k in ('initiated_date', 'classified_date', 'terminated_date'):
        if rec.get(k):
            rec[k] = _date(rec[k])
    rec['recall_date'] = _date(row.get('Report Date'))
    rec['code_info'] = row.get('_code_info') or ''
    rec['product_name'] = tcur.clean(desc)
    rec['brand'] = tcur.clean(firm)
    rec['reason'] = tcur.clean(reason)

    fam = tcat.family_for_fda(row.get('Product Type'), tcur.key(f"{desc} {firm}"))
    rec['category_family'] = fam
    rec['category_group'] = tcat.group(fam)
    hz, tier = thaz.derive(rec['reason'], rec['product_name'], fam)
    rec['hazards'] = hz
    rec['hazard'] = thaz.primary_of(hz)
    rec['tier'] = tier
    rec['band'] = thaz.BAND.get(tier, 'CHECK')
    return rec


def import_files(paths, existing_ids, log=print):
    """Returns (new records, enrichment updates keyed by recall_id)."""
    new, enrich, seen = [], {}, set()
    for p in paths:
        for row in read_any(p):
            rid = (row.get('Recall Number') or '').strip()
            if not rid or rid in seen:
                continue
            seen.add(rid)
            rec = build(row)
            if not rec:
                continue
            if rid in existing_ids:
                enrich[rid] = {k: v for k, v in rec.items()
                               if k in ('code_info', 'units', 'distribution', 'event_id',
                                        'initiated_date', 'classified_date', 'terminated_date',
                                        'recall_initiated_by', 'firm_notification',
                                        'firm_city', 'firm_state', 'firm_country', 'status')
                               and v}
            else:
                new.append(rec)
    log(f"[+] enforcement import: {len(new)} new, {len(enrich)} existing enriched, "
        f"{len(seen)} rows read")
    return new, enrich
