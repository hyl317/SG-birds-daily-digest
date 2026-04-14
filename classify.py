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
