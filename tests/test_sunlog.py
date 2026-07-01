"""
Tests for sunlog.py — the daily-narrative / solar-arc logic.

sunlog polls a pypowerwall CSV endpoint and, once a day, turns the day's
readings into a human-readable energy story: when solar came online, peaked
and faded; the battery delta; grid import/export; and a one-line summary.

These tests exercise that pure CSV-parsing / narrative logic directly by
feeding synthetic readings into DayTracker — no network and no real Powerwall
required. The only mocked boundary is urllib for the fetch() resilience tests.

Test categories covered:
  Security, Performance, Retry, Unit, Integration, Functional, Frame
"""

import csv
from datetime import datetime, date
from unittest.mock import patch

import pytest

import sunlog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DAY = date(2026, 6, 15)


def _cfg(tmp_path=None, threshold=50):
    return {
        "solar_threshold_w": threshold,
        "log_dir": str(tmp_path) if tmp_path is not None else "logs",
    }


def _reading(hour, minute, solar, home, battery_w, battery_pct, grid, status="SystemGridConnected"):
    """Build one parsed reading dict, as parse()/load_from_csv would produce."""
    return {
        "ts": datetime(DAY.year, DAY.month, DAY.day, hour, minute),
        "solar_w": float(solar),
        "home_w": float(home),
        "battery_w": float(battery_w),
        "battery_pct": float(battery_pct),
        "grid_w": float(grid),
        "grid_status": status,
    }


def _tracker(tmp_path=None, threshold=50):
    t = sunlog.DayTracker(_cfg(tmp_path, threshold))
    t.date = DAY
    return t


def _sunny_day():
    """A full solar arc: online ~6am, peak at noon, fades ~7pm, exports midday."""
    return [
        _reading(0, 0, 0, 400, -300, 45, 300),  # midnight: no sun, importing
        _reading(6, 0, 120, 500, 200, 46, 0),  # solar online
        _reading(9, 0, 3000, 800, 2000, 70, -1200),  # ramping, exporting
        _reading(12, 0, 7200, 900, 3000, 99, -4000),  # peak + battery full
        _reading(15, 0, 4000, 850, 500, 95, -2000),  # afternoon, exporting
        _reading(19, 0, 90, 700, -400, 92, 0),  # fading
        _reading(23, 0, 0, 600, -500, 88, 200),  # night
    ]


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------


def test_security_fetch_swallows_network_errors():
    """fetch() must never propagate an exception from the (remote, untrusted)
    Powerwall endpoint — a failed poll returns None so the daemon survives."""
    with patch("sunlog.urllib.request.urlopen", side_effect=OSError("boom")):
        assert sunlog.fetch("http://10.0.1.26:8675/csv") is None


def test_security_parse_coerces_hostile_values():
    """parse() must tolerate malformed / injected field values from the remote
    CSV, coercing non-numeric junk to 0.0 rather than crashing."""
    row = {
        "Grid": "'; DROP TABLE readings; --",
        "Home": "NaNaN",
        "Solar": "",
        "Battery": None,
        "BatteryLevel": "42.5",
        "GridStatus": "  SystemGridConnected  ",
    }
    parsed = sunlog.parse(row)
    assert parsed["grid_w"] == 0.0
    assert parsed["home_w"] == 0.0
    assert parsed["solar_w"] == 0.0
    assert parsed["battery_w"] == 0.0
    assert parsed["battery_pct"] == 42.5
    # GridStatus is stripped of surrounding whitespace.
    assert parsed["grid_status"] == "SystemGridConnected"


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------


def test_performance_save_raw_csv_is_incremental(tmp_path):
    """save_raw_csv appends only new readings; a second call with nothing new
    must not rewrite or duplicate rows already on disk."""
    t = _tracker(tmp_path)
    for r in _sunny_day()[:3]:
        t.ingest(r)
    t.save_raw_csv()

    path = tmp_path / f"{DAY}.csv"
    first_size = path.stat().st_size
    with open(path) as f:
        assert sum(1 for _ in f) == 4  # header + 3 rows

    # No new readings -> no write, file unchanged.
    t.save_raw_csv()
    assert path.stat().st_size == first_size

    # Two more readings -> only those two are appended.
    for r in _sunny_day()[3:5]:
        t.ingest(r)
    t.save_raw_csv()
    with open(path) as f:
        assert sum(1 for _ in f) == 6  # header + 5 rows


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="N/A: fetch() performs a single request and returns None on failure; "
    "recovery is handled by the next scheduled poll in the main loop, not by "
    "any in-call retry/backoff logic to exercise here."
)
def test_retry_not_applicable():
    """Placeholder — sunlog has no in-call retry logic."""
    pass


# ---------------------------------------------------------------------------
# Unit
# ---------------------------------------------------------------------------


def test_unit_parse_typing():
    """parse() converts the expected CSV columns to floats."""
    row = {
        "Grid": "150.5",
        "Home": "900",
        "Solar": "3200.2",
        "Battery": "-450",
        "BatteryLevel": "88",
        "GridStatus": "SystemGridConnected",
    }
    p = sunlog.parse(row)
    assert p["grid_w"] == 150.5
    assert p["home_w"] == 900.0
    assert p["solar_w"] == 3200.2
    assert p["battery_w"] == -450.0
    assert p["battery_pct"] == 88.0
    assert isinstance(p["ts"], datetime)


def test_unit_pct_solar_fraction():
    """_pct_solar is the fraction of intervals with solar above threshold."""
    t = _tracker()
    t.ingest(_reading(6, 0, 0, 500, 0, 50, 0))  # below threshold
    t.ingest(_reading(7, 0, 100, 500, 0, 50, 0))  # above
    t.ingest(_reading(8, 0, 100, 500, 0, 50, 0))  # above
    t.ingest(_reading(9, 0, 100, 500, 0, 50, 0))  # above
    assert t._pct_solar() == pytest.approx(0.75)


def test_unit_kwh_sign_filtering():
    """_kwh averages the sign-filtered watt readings over the elapsed hours."""
    t = _tracker()
    t.ingest(_reading(0, 0, 0, 0, 0, 50, 1000))
    t.ingest(_reading(0, 30, 0, 0, 0, 50, -500))
    t.ingest(_reading(1, 0, 0, 0, 0, 50, 2000))
    # span = 1.0 h; positive grid vals = [1000, 2000] -> avg 1500 W -> 1.5 kWh
    assert t._kwh("grid_w", sign=1) == pytest.approx(1.5)
    # only one negative value -> fewer than 2 samples -> 0.0
    assert t._kwh("grid_w", sign=-1) == 0.0


def test_unit_ingest_tracks_solar_arc_and_battery_full():
    """ingest records first/last solar-on timestamps, the peak, and the first
    moment the battery reaches full."""
    t = _tracker()
    for r in _sunny_day():
        t.ingest(r)
    assert t.solar_on_ts == datetime(2026, 6, 15, 6, 0)
    assert t.solar_off_ts == datetime(2026, 6, 15, 19, 0)
    assert t.solar_peak_w == 7200.0
    assert t.solar_peak_ts == datetime(2026, 6, 15, 12, 0)
    assert t.battery_full_ts == datetime(2026, 6, 15, 12, 0)


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------


def test_integration_csv_roundtrip_reproduces_arc(tmp_path):
    """Readings written by save_raw_csv reload via load_from_csv into a fresh
    tracker that reconstructs the same solar arc — this is how sunlog survives
    a mid-day restart."""
    writer = _tracker(tmp_path)
    for r in _sunny_day():
        writer.ingest(r)
    writer.save_raw_csv()

    reloaded = sunlog.DayTracker(_cfg(tmp_path))
    reloaded.date = DAY
    reloaded.load_from_csv()

    assert len(reloaded.readings) == len(_sunny_day())
    assert reloaded.solar_on_ts == writer.solar_on_ts
    assert reloaded.solar_off_ts == writer.solar_off_ts
    assert reloaded.solar_peak_w == writer.solar_peak_w
    assert reloaded.battery_full_ts == writer.battery_full_ts
    # Narrative regenerated from reloaded data matches the original.
    assert reloaded.narrative() == writer.narrative()


def test_integration_save_narrative_writes_summary_file(tmp_path):
    """save_narrative writes the story to <date>-summary.txt and returns path."""
    t = _tracker(tmp_path)
    for r in _sunny_day():
        t.ingest(r)
    path = t.save_narrative()
    assert path == str(tmp_path / f"{DAY}-summary.txt")
    text = open(path).read()
    assert "Solar came online" in text


# ---------------------------------------------------------------------------
# Functional
# ---------------------------------------------------------------------------


def test_functional_narrative_tells_the_full_story(tmp_path):
    """A full sunny day produces a narrative naming the online/peak/fade times,
    the battery delta, grid/export usage, and a one-line summary."""
    t = _tracker(tmp_path)
    for r in _sunny_day():
        t.ingest(r)
    text = t.narrative()

    # Solar arc — the sunny_day arc uses 6am / 12pm / 7pm boundaries.
    assert "Solar came online at 6:00 AM" in text
    assert "12:00 PM" in text  # peak
    assert "faded at 7:00 PM" in text
    assert "peaked at 7.2 kW" in text

    # Battery: started 45% at midnight, ended 88% -> up 43%.
    assert "Battery reached full charge" in text
    assert "ended the day at 88%" in text
    assert "up 43%" in text
    assert "from 45% at midnight" in text

    # Grid / export lines and a one-line story.
    assert "exported back to the grid" in text
    assert "✨" in text


def test_functional_perfect_solar_day_story():
    """When solar covers ~100% of intervals with no grid import, the summary is
    the 'nearly perfect solar day' story."""
    t = _tracker()
    t.ingest(_reading(6, 0, 2000, 500, 1000, 50, -100))
    t.ingest(_reading(12, 0, 6000, 500, 3000, 90, -2000))
    t.ingest(_reading(18, 0, 1500, 500, -200, 95, -50))
    text = t.narrative()
    assert "nearly perfect solar day" in text
    assert "without drawing from the grid" in text


# ---------------------------------------------------------------------------
# Frame (boundary / edge conditions)
# ---------------------------------------------------------------------------


def test_frame_narrative_no_data():
    """No readings at all -> a terse 'No data recorded' line."""
    t = _tracker()
    assert t.narrative() == f"{DAY} — No data recorded."


def test_frame_narrative_no_sun():
    """A day where solar never crosses the threshold reports no production and
    selects the 'No sun today' story; a falling battery reads 'down'."""
    t = _tracker()
    t.ingest(_reading(0, 0, 0, 800, -600, 90, 800))
    t.ingest(_reading(12, 0, 10, 900, -700, 70, 900))
    t.ingest(_reading(23, 0, 0, 800, -600, 55, 800))
    text = t.narrative()
    assert "No solar production recorded today." in text
    assert "down 35%" in text  # 90% -> 55%
    assert "No sun today" in text
    assert "Grid supplied" in text


def test_frame_kwh_needs_two_samples():
    """_kwh returns 0.0 when fewer than two samples survive filtering."""
    t = _tracker()
    assert t._kwh("home_w") == 0.0  # no readings
    t.ingest(_reading(6, 0, 100, 500, 0, 50, 0))
    assert t._kwh("home_w") == 0.0  # single reading
