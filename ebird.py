"""
eBird + Nominatim helpers for out-of-SG location queries.

The bot uses these to answer "what birds near <place>?" for anywhere in the
world by (1) geocoding the place via Nominatim, (2) hitting eBird's
/obs/geo/recent endpoint with the resulting lat/lng. SG queries still go to
the local SQLite archive — see bot.on_message for routing.
"""

import functools

import requests

NOMINATIM_BASE = "https://nominatim.openstreetmap.org"
EBIRD_BASE = "https://api.ebird.org/v2"
USER_AGENT = "sg-birds-bot/1.0 (https://github.com/hyl317/sg-birds-summary)"

# Rough Singapore bounding box
SG_LAT_MIN, SG_LAT_MAX = 1.15, 1.48
SG_LNG_MIN, SG_LNG_MAX = 103.6, 104.1


def is_in_sg(lat, lng):
    return SG_LAT_MIN <= lat <= SG_LAT_MAX and SG_LNG_MIN <= lng <= SG_LNG_MAX


@functools.lru_cache(maxsize=256)
def geocode(query):
    """
    Resolve a free-text place name to (lat, lng, display_name) via Nominatim.
    Returns None if no confident match. Filters out POIs to avoid random
    amenity hits for species-like queries.
    """
    try:
        r = requests.get(
            f"{NOMINATIM_BASE}/search",
            params={"q": query, "format": "json", "limit": 5, "addressdetails": 0},
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        r.raise_for_status()
        results = r.json()
    except Exception:
        return None

    for item in results:
        cls = item.get("class")
        if cls in ("place", "boundary"):
            return (float(item["lat"]), float(item["lon"]), item.get("display_name") or query)
    return None


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
