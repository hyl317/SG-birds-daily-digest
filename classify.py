"""
Query classifier: species | location | ambiguous.

Uses the lookup sets built by taxonomy.py from the eBird taxonomy. No
network calls, no LLM — a pure function over three sets. The rule set
was tuned on a 34-query battery covering SG species, SG places,
international hotspots, and adversarial place-with-bird-word names like
"hawk mountain" and "eagle lake". See tests/test_classify.py.

Routing meaning (the bot's on_message uses these):

  species   → the query is an exact species/sci-name/alpha-code match
              OR every non-stopword token is a known bird-family word.
              Search the local SG archive only; no geocoder fallback.

  location  → nothing in the query looks like a bird. Send to the Photon
              geocoder; route to eBird (or local DB, if top candidate is
              inside the SG bounding box).

  ambiguous → some tokens look bird-y, some don't ("eagle lake",
              "hawk mountain", "chinese garden"). Try the local archive
              first; if it's empty, fall back to the geocoder.
"""

import re

import taxonomy

_STOPWORDS = {"and", "or", "the", "of", "a", "an", "sp", "spp", "cf"}

# Keyword separators for "SPECIES <sep> LOCATION" queries. First match wins,
# so "fairy pitta at kaeng krachan near bangkok" splits on " at " and the
# rest is treated as the location string.
_SPLIT_RE = re.compile(r"\s+(?:near|in|at|around)\s+", re.I)


def parse_species_location(text: str):
    """
    Try to parse a "SPECIES <near|in|at|around> LOCATION" query.

    Returns (species_code, species_name, location_string) if the LHS is
    an exact match for a species comName / sciName / alpha code, else
    None. Single-word generic queries like "eagle" deliberately don't
    match — the LHS must be a full taxonomy entry so we know which
    speciesCode to use. species_name is the user-typed LHS (title-cased
    for display).
    """
    m = _SPLIT_RE.search(text)
    if not m:
        return None
    lhs = text[: m.start()].strip()
    rhs = text[m.end():].strip()
    if not lhs or not rhs:
        return None
    lhs_norm = re.sub(r"[^\w\s]", " ", lhs.lower()).strip()
    lhs_norm = re.sub(r"\s+", " ", lhs_norm)
    code = taxonomy.SPECIES_CODES.get(lhs_norm)
    if not code:
        return None
    return code, lhs.title(), rhs


def classify(text: str) -> str:
    """
    Return "species", "location", or "ambiguous".

    Rule order (first match wins):
      1. The full query (normalized) exactly matches a comName/sciName.
      2. The full query exactly matches an alpha code.
      3. Any single token of the query is itself a full species name
         (catches "shikra sp", "fairy pitta male").
      4. Every non-stopword token is in the bird lexicon.
      5. At least one non-stopword token is in the bird lexicon.
      6. Otherwise.
    """
    qlow = re.sub(r"[^\w\s]", " ", text.strip().lower()).strip()
    qlow = re.sub(r"\s+", " ", qlow)
    if not qlow:
        return "location"

    if qlow in taxonomy.FULL_NAMES:
        return "species"
    if qlow in taxonomy.ALPHA_CODES:
        return "species"

    tokens = [t for t in re.findall(r"[a-z]+", qlow) if t not in _STOPWORDS]
    if not tokens:
        return "location"

    if any(t in taxonomy.FULL_NAMES for t in tokens):
        return "species"

    lexicon_hits = [t for t in tokens if t in taxonomy.BIRD_LEXICON]
    if len(lexicon_hits) == len(tokens):
        return "species"
    if lexicon_hits:
        return "ambiguous"
    return "location"
