"""
TinySafe — deaths and injuries already reported.

The severity tier says how bad the hazard could be. This says whether it
already happened, and that turned out to be missing from the ranking entirely.

Rock 'n Play Sleepers is the case that found it. Around a hundred infants died
in it — the deadliest infant product recall in US history — and the store had it
at `general`, tier 4, ranked below crib bumpers with no reported incidents. The
hazard text was empty and the reason field said "infant fatalities have occurred
... after the infants rolled from their back to their stomach", which matched no
pattern in the hazard table because `fatalities` was never a hazard word.

Two rules come out of that:

  1. A recall reporting a death is tier 1. Not "probably severe" — the hazard
     has already killed a child, whatever vocabulary the notice used to say so.
  2. Among equally-tiered records, the ones with deaths come first. A crib
     bumper with five deaths and a crib bumper with none are not the same
     record to a parent.

Counting is deliberately conservative. Year digits read as counts is the known
failure — "In 2019, 30 fatalities" must not become thirty-plus-2019 — so only
one- to three-digit numbers and spelled-out numerals count, and the largest
single mention wins rather than a sum, because notices restate totals.
"""
import re

WORDS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7,
    'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12,
    'thirteen': 13, 'fourteen': 14, 'fifteen': 15, 'sixteen': 16,
    'seventeen': 17, 'eighteen': 18, 'nineteen': 19, 'twenty': 20,
    'thirty': 30, 'forty': 40, 'fifty': 50, 'a': 1, 'an': 1,
}
_NUM = r'(\d{1,3}|' + '|'.join(sorted(WORDS, key=len, reverse=True)) + r')'

DEATHS = re.compile(
    _NUM + r'\s+(?:additional\s+|reported\s+|confirmed\s+|further\s+)?'
    r'(?:infant|child|children|baby|babies|toddler|adult|pediatric)?\s*'
    r'(?:deaths?|fatalities|fatality)\b', re.I)

# "an infant died", "a child was killed" — a count of one without a numeral.
DIED = re.compile(
    r'\b(?:infants?|child(?:ren)?|bab(?:y|ies)|toddlers?)\s+(?:has |have |was |were )?'
    r'(?:died|been killed|suffocated|strangled|drowned)\b|'
    r'\bdeath\s+(?:of|prompt)|\bfatal(?:ity|ities)\b|\bdied\b', re.I)

INJURIES = re.compile(
    _NUM + r'\s+(?:additional\s+|reported\s+|confirmed\s+)?'
    r'(?:infant|child|children|baby|toddler)?\s*'
    r'(?:injur(?:y|ies)|hospitaliz\w+|near\s+strangulations?|'
    r'emergency\s+room\s+visits?)\b', re.I)


def _largest(pattern, text):
    """Largest single count mentioned. Not a sum — notices restate totals, and
    adding "two additional deaths" to "a total of five" gives seven."""
    best = 0
    for m in pattern.finditer(text or ''):
        v = m.group(1).lower()
        if v.isdigit():
            n = int(v)
            if n > 500:          # implausible for a recall; almost certainly a year
                continue
        else:
            n = WORDS.get(v, 0)
        best = max(best, n)
    return best


def deaths(*texts):
    """Reported deaths, or 0. Returns 1 for an unnumbered fatality mention."""
    joined = ' '.join(str(t or '') for t in texts)
    n = _largest(DEATHS, joined)
    if n:
        return n
    return 1 if DIED.search(joined) else 0


def injuries(*texts):
    return _largest(INJURIES, ' '.join(str(t or '') for t in texts))


def tier_floor(tier, death_count):
    """A recall that has already killed a child is tier 1.

    Not a heuristic about severity — the hazard materialised. Rock 'n Play sat
    at tier 4 with a hundred infant deaths because the notice described them in
    words the hazard table didn't carry, and no amount of vocabulary work makes
    that class of miss impossible.
    """
    return 1 if death_count else tier


def rank_bonus(death_count, injury_count):
    """Where a record sits inside its tier's decade. Lower is more urgent.

    Deaths lead, then injuries, then nothing reported. Capped at 40 so the
    invariant the app sorts on — rank // 100 == tier — survives alongside the
    age (max 30) and contact (max 5) terms.
    """
    if death_count >= 3:
        return 0
    if death_count:
        return 10
    if injury_count >= 5:
        return 20
    if injury_count:
        return 30
    return 40
