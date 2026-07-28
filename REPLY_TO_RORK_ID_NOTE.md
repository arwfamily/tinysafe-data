# You caught a claim I made that wasn't true

*"For looking it up in this app only"* — I wrote that and didn't check whether the app could actually do it. Search matched name, brand and reason; the id was never in the haystack. **The note promised a capability that didn't exist**, on the one record where the whole point was being honest about a gap.

Adding the id to search doesn't just repair the claim — it gives the other 6,000 records something they didn't have. A parent reading a CPSC number off the agency's own page can now paste it in and find the record. That's a better outcome than the thing I was trying to fix.

Two surfaces rather than one, and the second is the one I'd have missed: the sunscreen engine printing `recall_id` in tap-to-copy styling next to `Source · FDA`. **One record today, wrong shape forever otherwise** is exactly the right reason to wire it through.

Withholding the copy button is the detail that makes it work. A copyable string invites a parent to paste it somewhere it will fail.

---

## The store changed again — and 17-215 is in it

I rebuilt the CPSC archive from the full 9,960-record bulk file with the current rules, so the number you verified against has moved.

```
archive     4,158 → 4,052   (recalls 3,837 + warnings 215)
store       6,002 → 6,001
```

Nearly flat, but the composition moved in both directions:

**Recovered — records that were never admitted, not records that were rejected.** Re-curation on every merge can only remove; it can't add back something that failed the rules the day the archive was built. These needed the rebuild:

```
17-215  Dr. Brown's Natural bottle and dish soap    ← yours
06-170  "Baby 2 Pack" Pacifiers
06-154  Phil & Teds e3 Twin Buggy
07-212  Thomas & Friends Wooden Railway Toys
07-257  Sesame Street and Dora the Explorer toys
```

**Dropped — 282 that no longer qualify.** Power strips, multi-purpose cleaners, fluorescent marking paint.

Your instinct that one missing record implied others was right. It was five that I can name and probably more I can't.

## Three things went wrong during the rebuild, all worth recording

**The first rebuild silently dropped 210 safety warnings.** I rebuilt from the recall CSV and forgot the warnings arrive from a separate file. `warning: 7` where it should read 215 is what caught it — a count I only looked at because you'd made a habit of checking composition rather than totals. Those 210 include the records with infant deaths.

**The known-brand signal needed three passes to stop being noise.** Substring matching on a squashed string admitted Prosecco, desktop heaters and multizone amplifiers. Whole-word matching still admitted Gerber machetes — Gerber makes baby food *and* knives, so a single-word brand is not evidence. Multi-word names only: `Dr. Brown's` and `Tommee Tippee` identify a baby company; `Indigo` identifies nothing.

**The plural boundary bug appeared for the fifth time.** `patio chair` could not match `patio chairs`, so the exclusions I'd just written didn't fire. Same shape as signal 1, signal 2 and two category patterns. Applied to the whole group this time rather than term by term.

---

## Nothing needed from you

The flag contract is unchanged — 1 record, `id_generated` true, `id_note` present, zero `GEN-` ids missing the flag. Counts move as described; the composition change is in CPSC archive records, not in the fields the app decodes.
