"""
eBird taxonomy loader — downloads and caches the ~18k-entry species list
once, then exposes three lookup sets used by classify.py:

  - FULL_NAMES   : lowercased common + scientific names ("fairy pitta",
                   "pitta nympha") — exact-match check for species queries
  - ALPHA_CODES  : lowercased 4-letter banding / alpha codes ("coos",
                   "bamk") — exact-match check for shorthand queries
  - BIRD_LEXICON : a ~1k word set built from (a) tokens of every family
                   name ("pittas", "hawks") and (b) tokens appearing in
                   ≥5 species common names ("eagle", "shrike", "ground").
                   Used by the classifier to spot bird-y words inside
                   multi-word queries.
  - SPECIES_CODES: dict mapping lowercased common name / scientific name
                   / alpha code → eBird speciesCode. Used by the
                   "species near location" parser to resolve the species
                   half into the code that eBird's species-scoped
                   /obs/geo/recent/{speciesCode} endpoint requires.

Cached at TAXONOMY_CACHE (per-host, gitignored). Re-fetched if the cache
is older than REFRESH_DAYS. Downloaded via the already-present
EBIRD_API_KEY. Fail-open: if the download fails and no cache exists, the
sets are empty — classify.py treats every query as "location" and the bot
still functions.
"""

import json
import os
import re
import time
from collections import Counter

import requests

TAXONOMY_URL = "https://api.ebird.org/v2/ref/taxonomy/ebird?fmt=json&locale=en"
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
TAXONOMY_CACHE = os.path.join(PROJECT_DIR, "taxonomy.json")
REFRESH_DAYS = 30

# Words to ignore when tokenizing family names / common names.
_STOPWORDS = {"and", "allies", "or", "the", "of", "a", "an", "sp", "spp", "cf"}

# Categories to include. "species" covers ~11k true species; "issf"/"form"
# add subspecies groups that eBird users search by; the rest are
# intentionally narrower but still valid matches for a query string.
_INCLUDE_CATEGORIES = {
    "species", "issf", "form", "hybrid", "slash", "domestic",
    "spuh", "intergrade",
}

FULL_NAMES: set[str] = set()
ALPHA_CODES: set[str] = set()
BIRD_LEXICON: set[str] = set()
SPECIES_CODES: dict[str, str] = {}


def _fetch_taxonomy(api_key):
    """Download fresh taxonomy from eBird. Returns parsed list or raises."""
    r = requests.get(
        TAXONOMY_URL,
        headers={"X-eBirdApiToken": api_key},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def _load_cache():
    with open(TAXONOMY_CACHE) as f:
        return json.load(f)


def _save_cache(data):
    tmp = TAXONOMY_CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, TAXONOMY_CACHE)


def _cache_fresh():
    try:
        age = time.time() - os.path.getmtime(TAXONOMY_CACHE)
        return age < REFRESH_DAYS * 86400
    except OSError:
        return False


def _build_sets(tax):
    """Populate FULL_NAMES, ALPHA_CODES, BIRD_LEXICON from a parsed taxonomy."""
    full_names = set()
    alpha_codes = set()
    species_codes = {}
    family_tokens = set()
    word_counts = Counter()

    for e in tax:
        if e.get("category") not in _INCLUDE_CATEGORIES:
            continue
        code = e.get("speciesCode")
        cn = (e.get("comName") or "").strip().lower()
        sn = (e.get("sciName") or "").strip().lower()
        if cn:
            full_names.add(cn)
            if code:
                species_codes[cn] = code
        if sn:
            full_names.add(sn)
            if code:
                species_codes[sn] = code
        for c in (e.get("comNameCodes") or []) + (e.get("sciNameCodes") or []):
            cl = c.lower()
            alpha_codes.add(cl)
            if code:
                species_codes.setdefault(cl, code)
        fam = (e.get("familyComName") or "").lower()
        for tok in re.findall(r"[a-z]+", fam):
            if tok not in _STOPWORDS and len(tok) >= 3:
                family_tokens.add(tok)
        if e.get("category") == "species" and cn:
            for tok in re.findall(r"[a-z]+", cn):
                if len(tok) >= 4 and tok not in _STOPWORDS:
                    word_counts[tok] += 1

    # Tokens appearing in ≥5 distinct species names are probably
    # bird-family words ("eagle", "hawk") rather than one-off modifiers
    # ("sabah", "bornean"). The cutoff is tuned on the 34-query battery
    # in tests/test_classify.py.
    common_words = {w for w, c in word_counts.items() if c >= 5}

    FULL_NAMES.clear(); FULL_NAMES.update(full_names)
    ALPHA_CODES.clear(); ALPHA_CODES.update(alpha_codes)
    BIRD_LEXICON.clear(); BIRD_LEXICON.update(family_tokens | common_words)
    SPECIES_CODES.clear(); SPECIES_CODES.update(species_codes)


def load(api_key=None, force_refresh=False):
    """
    Load the taxonomy into the module-level sets. Uses the on-disk cache
    if fresh; otherwise tries to fetch from eBird (requires api_key).
    Falls back to a stale cache on network failure.
    """
    tax = None
    if not force_refresh and _cache_fresh():
        try:
            tax = _load_cache()
        except (OSError, json.JSONDecodeError):
            tax = None

    if tax is None and api_key:
        try:
            tax = _fetch_taxonomy(api_key)
            _save_cache(tax)
        except Exception as e:
            print(f"taxonomy fetch failed: {e!r}", flush=True)

    if tax is None:
        # Fall back to any existing cache even if stale.
        try:
            tax = _load_cache()
        except (OSError, json.JSONDecodeError):
            tax = []

    _build_sets(tax)
    return len(tax)
