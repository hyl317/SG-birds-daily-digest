"""
Geocoder + eBird helpers for out-of-SG location queries.

Forward geocoding uses Photon (OSM-backed, no API key) because Nominatim
lacks full-text indexing and misses most POIs — Photon finds the
Rainforest Discovery Centre, Gunung Panti, Kaeng Krachan, etc. that
Nominatim returns nothing for. Reverse geocoding (used only to label
GPS pins) stays on Nominatim; it's fine for that.

The bot uses these to answer "what birds near <place>?" for anywhere in
the world by (1) geocoding the place via Photon, (2) hitting eBird's
/obs/geo/recent endpoint with the resulting lat/lng. SG queries still go
to the local SQLite archive — see bot.on_message for routing.
"""

import functools

import requests

PHOTON_BASE = "https://photon.komoot.io/api"
NOMINATIM_BASE = "https://nominatim.openstreetmap.org"
EBIRD_BASE = "https://api.ebird.org/v2"
USER_AGENT = "sg-birds-bot/1.0 (https://github.com/hyl317/sg-birds-summary)"

# Rough Singapore bounding box
SG_LAT_MIN, SG_LAT_MAX = 1.15, 1.48
SG_LNG_MIN, SG_LNG_MAX = 103.6, 104.1

# osm_key values that are almost always false positives for a "place"
# lookup: shops, roads, individual buildings, railways, offices, crafts,
# linear waterways (a river isn't a bird spot; a wetland is).
_BLOCKED_OSM_KEYS = {
    "shop", "highway", "building", "railway", "office", "craft",
    "waterway", "barrier", "power", "man_made",
}

# For osm_key=amenity, almost all values are food/retail/civic noise.
# Keep only the handful that overlap with nature/visitor centres.
_ALLOWED_AMENITY_VALUES = {
    "exhibition_centre", "community_centre", "townhall", "marketplace",
    "place_of_worship",  # temples/shrines are sometimes the nearest named feature
}


def is_in_sg(lat, lng):
    return SG_LAT_MIN <= lat <= SG_LAT_MAX and SG_LNG_MIN <= lng <= SG_LNG_MAX


def _photon_display_name(props, fallback):
    """Build a human-readable label from a Photon feature's properties."""
    name = props.get("name") or fallback
    # Prefer state+country; fall back to country alone; avoid clutter
    # from district/city when state is present.
    parts = [p for p in (props.get("state"), props.get("country")) if p]
    return f"{name}, {', '.join(parts)}" if parts else name


def _photon_allowed(props):
    """Apply the osm_key denylist / amenity allowlist."""
    key = props.get("osm_key")
    val = props.get("osm_value")
    if key in _BLOCKED_OSM_KEYS:
        return False
    if key == "amenity" and val not in _ALLOWED_AMENITY_VALUES:
        return False
    if key == "tourism" and val in {"hotel", "motel", "guest_house", "hostel",
                                     "apartment", "chalet", "camp_site"}:
        return False
    return True


# Common US→UK spelling normalizations. OSM is dominantly British, and
# Photon's fuzzy matcher does NOT treat "center" and "centre" as synonyms,
# so an American-spelling query for "rainforest discovery center" returns
# zero despite "Rainforest Discovery Centre" being in OSM. Rewrite
# word-by-word before sending to Photon.
_US_UK_MAP = {
    "center": "centre", "centers": "centres",
    "harbor": "harbour", "harbors": "harbours",
    "theater": "theatre", "theaters": "theatres",
    "color": "colour", "colors": "colours",
    "favorite": "favourite", "favorites": "favourites",
}


def _normalize_spelling(query):
    """Replace US spellings with UK equivalents token-wise, preserving case."""
    def sub(match):
        w = match.group(0)
        uk = _US_UK_MAP.get(w.lower())
        if not uk:
            return w
        # Preserve title case; lowercase everything else (OSM is lowercase-ish).
        return uk.capitalize() if w[:1].isupper() else uk
    import re as _re
    return _re.sub(r"[A-Za-z]+", sub, query)


@functools.lru_cache(maxsize=256)
def geocode_candidates(query, limit=5):
    """
    Resolve a free-text place name to up to `limit` (lat, lng, display_name)
    candidates via Photon, ordered by Photon's internal ranking.

    Filters out POI noise using a denylist (shop, highway, building, etc.)
    and a small amenity allowlist, so random restaurants/roads don't
    drown out actual birding sites. Dedupes by (name, state).

    Returns an empty tuple on HTTP failure, empty results, or if no
    candidate passes the filter.
    """
    normalized = _normalize_spelling(query)
    try:
        r = requests.get(
            PHOTON_BASE,
            params={"q": normalized, "limit": 10},
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        r.raise_for_status()
        features = r.json().get("features", [])
    except Exception:
        return ()

    out = []
    seen = set()
    for feat in features:
        props = feat.get("properties") or {}
        if not _photon_allowed(props):
            continue
        coords = (feat.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        lng, lat = float(coords[0]), float(coords[1])
        display_name = _photon_display_name(props, query)
        dedupe_key = (display_name, round(lat, 3), round(lng, 3))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        out.append((lat, lng, display_name))
        if len(out) >= limit:
            break
    # Return a tuple so the lru_cache can hash it
    return tuple(out)


@functools.lru_cache(maxsize=256)
def reverse_geocode(lat, lng):
    """Lat/lng → human-readable place name. Used for the GPS-pin reply header."""
    try:
        r = requests.get(
            f"{NOMINATIM_BASE}/reverse",
            params={"lat": lat, "lon": lng, "format": "json", "zoom": 12},
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("display_name")
    except Exception:
        return None


def recent_near(lat, lng, api_key, dist_km=10, back_days=30):
    """
    Fetch recent sightings from eBird near a point. Returns a list of row
    dicts sorted by date desc. Empty list on empty result or failure.
    """
    if not api_key:
        return None  # sentinel: caller should show "set EBIRD_API_KEY" message
    try:
        r = requests.get(
            f"{EBIRD_BASE}/data/obs/geo/recent",
            params={"lat": lat, "lng": lng, "dist": dist_km, "back": back_days},
            headers={"X-eBirdApiToken": api_key},
            timeout=15,
        )
        r.raise_for_status()
        raw = r.json()
    except Exception as e:
        print(f"eBird request failed: {e!r}", flush=True)
        return []

    rows = []
    for obs in raw:
        rows.append({
            "species": obs.get("comName") or "Unknown",
            "sci_name": obs.get("sciName"),
            "location": obs.get("locName"),
            "lat": obs.get("lat"),
            "lng": obs.get("lng"),
            "date": obs.get("obsDt", "").split(" ")[0],  # strip time component
            "count": obs.get("howMany"),
            "notable": obs.get("obsReviewed", False),
        })
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


def group_by_species(rows):
    """
    Collapse to one row per species (keeping the most recent), annotating
    each with `_count` = total sightings seen. Mirrors
    bot.maybe_dedupe_by_species for the local-DB path.
    """
    by_species = {}
    counts = {}
    for r in rows:
        sp = r["species"]
        counts[sp] = counts.get(sp, 0) + 1
        if sp not in by_species:
            by_species[sp] = dict(r)
    out = []
    for sp, r in by_species.items():
        r["_count"] = counts[sp]
        out.append(r)
    return out
