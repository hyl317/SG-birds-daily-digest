# Followups

Items intentionally deferred from earlier changes, kept here so they don't get lost.

## eBird integration — phase 2

The initial eBird support (April 2026) only handles "what's near this place?" queries. Worth revisiting:

1. **Species + location combo queries** — e.g. `fairy pitta in taipei`. Needs to parse the query into `(species, place)`, look up the species code via eBird's `/v2/ref/taxonomy/ebird` (cache locally — the full taxonomy is static and ~20k rows), then call `/v2/data/obs/geo/recent/{speciesCode}?lat=...&lng=...`. Probably the highest-value follow-up — it's the most natural way to ask "is X being seen near Y?".

2. **User-configurable radius / date window** — e.g. `foster city 25km 14d` suffix parsing, or inline buttons to widen/narrow after an initial reply. Currently hardcoded to 10 km / 30 days in `bot.py` (`EBIRD_DIST_KM`, `EBIRD_BACK_DAYS`).

3. **Notable-only mode** — add a `/rare <place>` command or a toggle that uses `/v2/data/obs/geo/recent/notable` instead of the regular endpoint. Good for chasing rarities on trips.

4. **Persistent geocode cache** — today `ebird.geocode` uses an in-memory `functools.lru_cache` that's fine for one long-running bot process, but a small SQLite table (`geocode_cache(query TEXT PRIMARY KEY, lat, lng, display_name, cached_at)`) would survive restarts and be kinder to Nominatim.

5. **Rate-limiting protection** — no issue at current scale, but if usage grows, add a simple token bucket in front of the Nominatim and eBird calls. Nominatim's published limit is 1 req/sec, eBird is documented as "reasonable use" with no hard number.
