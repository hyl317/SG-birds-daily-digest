"""Tests for ebird.py. Network calls are mocked via unittest.mock."""

from unittest.mock import patch, MagicMock

import pytest
import requests

import ebird


@pytest.fixture(autouse=True)
def clear_caches():
    """LRU caches on geocode/reverse_geocode persist across tests — clear them."""
    ebird.geocode_candidates.cache_clear()
    ebird.reverse_geocode.cache_clear()
    yield


# ---------------------------------------------------------------------------
# is_in_sg
# ---------------------------------------------------------------------------

def test_is_in_sg_inside():
    # Sungei Buloh Wetland Reserve
    assert ebird.is_in_sg(1.4460, 103.7270) is True


def test_is_in_sg_city_center():
    assert ebird.is_in_sg(1.2900, 103.8500) is True


def test_is_in_sg_outside_north():
    # Johor Bahru
    assert ebird.is_in_sg(1.4927, 103.7414) is False


def test_is_in_sg_outside_far():
    # Taipei
    assert ebird.is_in_sg(25.0330, 121.5654) is False


def test_is_in_sg_bbox_edges():
    assert ebird.is_in_sg(1.15, 103.6) is True     # SW corner
    assert ebird.is_in_sg(1.48, 104.1) is True     # NE corner
    assert ebird.is_in_sg(1.149, 103.85) is False  # just south
    assert ebird.is_in_sg(1.3, 104.101) is False   # just east


# ---------------------------------------------------------------------------
# group_by_species
# ---------------------------------------------------------------------------

def test_group_by_species_aggregates_counts():
    rows = [
        {"species": "Fairy Pitta", "date": "2026-04-14"},
        {"species": "Fairy Pitta", "date": "2026-04-10"},
        {"species": "Brown Shrike", "date": "2026-04-12"},
    ]
    grouped = ebird.group_by_species(rows)
    by_sp = {r["species"]: r for r in grouped}
    assert by_sp["Fairy Pitta"]["_count"] == 2
    assert by_sp["Brown Shrike"]["_count"] == 1


def test_group_by_species_keeps_most_recent():
    # The function trusts the caller's ordering (recent_near sorts date desc),
    # so we pass rows in desc order and expect the first occurrence to win.
    rows = [
        {"species": "Fairy Pitta", "date": "2026-04-14", "location": "A"},
        {"species": "Fairy Pitta", "date": "2026-04-10", "location": "B"},
    ]
    grouped = ebird.group_by_species(rows)
    assert len(grouped) == 1
    assert grouped[0]["date"] == "2026-04-14"
    assert grouped[0]["location"] == "A"
    assert grouped[0]["_count"] == 2


def test_group_by_species_empty():
    assert ebird.group_by_species([]) == []


def test_group_by_species_does_not_mutate_input():
    rows = [{"species": "Fairy Pitta", "date": "2026-04-14"}]
    ebird.group_by_species(rows)
    assert "_count" not in rows[0]


# ---------------------------------------------------------------------------
# geocode_candidates
# ---------------------------------------------------------------------------

def _mock_response(json_data, status=200):
    m = MagicMock()
    m.json.return_value = json_data
    m.status_code = status
    m.raise_for_status = MagicMock()
    return m


def _photon_feature(name, lat, lng, key, value, country=None, state=None):
    """Shape a Photon feature dict. All real Photon responses have this layout."""
    props = {"name": name, "osm_key": key, "osm_value": value}
    if country: props["country"] = country
    if state: props["state"] = state
    return {"properties": props, "geometry": {"coordinates": [lng, lat]}}


def _photon_payload(*features):
    return {"features": list(features)}


def test_geocode_candidates_happy_path():
    payload = _photon_payload(
        _photon_feature("Foster City", 37.5585, -122.2711, "place", "town",
                        country="United States", state="California"),
    )
    with patch("ebird.requests.get", return_value=_mock_response(payload)) as mock_get:
        result = ebird.geocode_candidates("foster city")
    assert result == ((37.5585, -122.2711, "Foster City, California, United States"),)
    # Verify we sent a User-Agent (not strictly required by Photon, but good hygiene)
    _, kwargs = mock_get.call_args
    assert "User-Agent" in kwargs["headers"]


def test_geocode_candidates_filters_out_restaurant():
    payload = _photon_payload(
        _photon_feature("Fairy Pitta Restaurant", 1.3, 103.8, "amenity", "restaurant",
                        country="Singapore"),
    )
    with patch("ebird.requests.get", return_value=_mock_response(payload)):
        assert ebird.geocode_candidates("fairy pitta") == ()


def test_geocode_candidates_filters_out_shops_and_roads():
    payload = _photon_payload(
        _photon_feature("Birdwatcher Bookshop", 1.3, 103.8, "shop", "books"),
        _photon_feature("Eagle Drive", 40.0, -120.0, "highway", "residential"),
        _photon_feature("Heron Building", 1.3, 103.8, "building", "yes"),
    )
    with patch("ebird.requests.get", return_value=_mock_response(payload)):
        assert ebird.geocode_candidates("eagle") == ()


def test_geocode_candidates_keeps_tourism_attraction():
    # Real regression: Rainforest Discovery Centre, Sabah is tagged
    # tourism=attraction. Must not be filtered out.
    payload = _photon_payload(
        _photon_feature("Rainforest Discovery Centre", 5.8765, 117.9445,
                        "tourism", "attraction", country="Malaysia", state="Sabah"),
    )
    with patch("ebird.requests.get", return_value=_mock_response(payload)):
        result = ebird.geocode_candidates("rainforest discovery centre")
    assert len(result) == 1
    assert "Rainforest Discovery Centre" in result[0][2]
    assert "Sabah" in result[0][2]
    assert "Malaysia" in result[0][2]


def test_geocode_candidates_keeps_natural_peak():
    # Regression from the Nominatim-era fix: mountains are class=natural.
    payload = _photon_payload(
        _photon_feature("Gunung Panti Timur", 1.8833, 103.9167, "natural", "peak",
                        country="Malaysia", state="Johor"),
    )
    with patch("ebird.requests.get", return_value=_mock_response(payload)):
        result = ebird.geocode_candidates("gunung panti")
    assert len(result) == 1
    assert "Gunung Panti Timur" in result[0][2]


def test_geocode_candidates_keeps_boundary_and_leisure():
    payload = _photon_payload(
        _photon_feature("Kaeng Krachan National Park", 12.82, 99.62,
                        "boundary", "national_park", country="Thailand"),
        _photon_feature("Point Pelee National Park", 41.95, -82.51,
                        "leisure", "park", country="Canada"),
    )
    with patch("ebird.requests.get", return_value=_mock_response(payload)):
        result = ebird.geocode_candidates("parks")
    assert len(result) == 2


def test_geocode_candidates_returns_multiple_for_ambiguous():
    payload = _photon_payload(
        _photon_feature("Cambridge", 52.2053, 0.1218, "place", "city", country="United Kingdom"),
        _photon_feature("Cambridge", 42.3736, -71.1097, "place", "city",
                        country="United States", state="Massachusetts"),
        _photon_feature("Cambridge", -37.8793, 175.4791, "place", "town", country="New Zealand"),
    )
    with patch("ebird.requests.get", return_value=_mock_response(payload)):
        result = ebird.geocode_candidates("cambridge")
    assert len(result) == 3
    assert "United Kingdom" in result[0][2]
    assert "United States" in result[1][2]
    assert "New Zealand" in result[2][2]


def test_geocode_candidates_dedupes_by_name_and_location():
    payload = _photon_payload(
        _photon_feature("Foo", 1.0, 2.0, "place", "village", country="Vanuatu"),
        _photon_feature("Foo", 1.00001, 2.00001, "place", "village", country="Vanuatu"),  # near-dup (same at 3dp)
        _photon_feature("Bar", 3.0, 4.0, "place", "village", country="Vanuatu"),
    )
    with patch("ebird.requests.get", return_value=_mock_response(payload)):
        result = ebird.geocode_candidates("foo")
    assert len(result) == 2
    names = [r[2] for r in result]
    assert any("Foo" in n for n in names)
    assert any("Bar" in n for n in names)


def test_geocode_candidates_respects_limit():
    payload = _photon_payload(*[
        _photon_feature(f"Place {i}", float(i), float(i), "place", "village")
        for i in range(10)
    ])
    with patch("ebird.requests.get", return_value=_mock_response(payload)):
        result = ebird.geocode_candidates("many", limit=3)
    assert len(result) == 3


def test_geocode_candidates_empty_results():
    with patch("ebird.requests.get", return_value=_mock_response(_photon_payload())):
        assert ebird.geocode_candidates("asdfghjkl") == ()


def test_geocode_candidates_http_error_returns_empty():
    with patch("ebird.requests.get", side_effect=requests.RequestException("boom")):
        assert ebird.geocode_candidates("foster city") == ()


def test_geocode_candidates_skips_feature_missing_coords():
    payload = _photon_payload(
        # Coordinates missing — should be skipped, not crash
        {"properties": {"name": "Nowhere", "osm_key": "place", "osm_value": "town",
                        "country": "Atlantis"}, "geometry": {}},
        _photon_feature("Real Place", 1.0, 2.0, "place", "town", country="Real"),
    )
    with patch("ebird.requests.get", return_value=_mock_response(payload)):
        result = ebird.geocode_candidates("nowhere")
    assert len(result) == 1
    assert "Real Place" in result[0][2]


# ---------------------------------------------------------------------------
# reverse_geocode
# ---------------------------------------------------------------------------

def test_reverse_geocode_happy_path():
    payload = {"display_name": "Sungei Buloh Wetland Reserve, Singapore"}
    with patch("ebird.requests.get", return_value=_mock_response(payload)):
        result = ebird.reverse_geocode(1.4460, 103.7270)
    assert result == "Sungei Buloh Wetland Reserve, Singapore"


def test_reverse_geocode_missing_display_name():
    with patch("ebird.requests.get", return_value=_mock_response({})):
        assert ebird.reverse_geocode(1.0, 2.0) is None


def test_reverse_geocode_http_error():
    with patch("ebird.requests.get", side_effect=requests.Timeout("slow")):
        assert ebird.reverse_geocode(1.0, 2.0) is None


# ---------------------------------------------------------------------------
# recent_near
# ---------------------------------------------------------------------------

def test_recent_near_no_api_key_returns_none():
    assert ebird.recent_near(1.3, 103.8, api_key=None) is None
    assert ebird.recent_near(1.3, 103.8, api_key="") is None


def test_recent_near_happy_path_parses_and_sorts():
    payload = [
        {
            "comName": "Brown Shrike",
            "sciName": "Lanius cristatus",
            "locName": "Central Park",
            "lat": 40.78,
            "lng": -73.97,
            "obsDt": "2026-04-10 08:30",
            "howMany": 2,
            "obsReviewed": False,
        },
        {
            "comName": "Fairy Pitta",
            "sciName": "Pitta nympha",
            "locName": "Daan Park",
            "lat": 25.03,
            "lng": 121.54,
            "obsDt": "2026-04-14 07:00",
            "howMany": 1,
            "obsReviewed": True,
        },
    ]
    with patch("ebird.requests.get", return_value=_mock_response(payload)) as mock_get:
        rows = ebird.recent_near(25.03, 121.54, api_key="FAKE", dist_km=10, back_days=30)

    # Verify API key header and params
    _, kwargs = mock_get.call_args
    assert kwargs["headers"]["X-eBirdApiToken"] == "FAKE"
    assert kwargs["params"]["dist"] == 10
    assert kwargs["params"]["back"] == 30

    # Verify sort (date desc) — Fairy Pitta (2026-04-14) should come first
    assert rows[0]["species"] == "Fairy Pitta"
    assert rows[0]["date"] == "2026-04-14"  # time stripped
    assert rows[0]["notable"] is True
    assert rows[1]["species"] == "Brown Shrike"
    assert rows[1]["count"] == 2


def test_recent_near_http_error_returns_empty_list():
    with patch("ebird.requests.get", side_effect=requests.ConnectionError("net down")):
        assert ebird.recent_near(1.3, 103.8, api_key="FAKE") == []


def test_recent_near_empty_results():
    with patch("ebird.requests.get", return_value=_mock_response([])):
        assert ebird.recent_near(1.3, 103.8, api_key="FAKE") == []


def test_recent_near_handles_missing_fields():
    payload = [
        {"comName": "Mystery Bird", "obsDt": "2026-04-14"},  # minimal row
    ]
    with patch("ebird.requests.get", return_value=_mock_response(payload)):
        rows = ebird.recent_near(1.3, 103.8, api_key="FAKE")
    assert rows[0]["species"] == "Mystery Bird"
    assert rows[0]["count"] is None
    assert rows[0]["location"] is None
    assert rows[0]["notable"] is False
