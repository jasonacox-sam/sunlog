#!/usr/bin/env python3
"""
sunlog.py — Solar + Powerwall Energy Diary
Polls pypowerwall data every N minutes, tracks the day's energy story,
and writes a human-readable narrative summary at end of day.

Author: Sam Cox <sam@jasonacox.com>
Repo:   https://github.com/jasonacox-sam/sunlog
"""

import csv
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path
import urllib.request

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "sunlog.conf")

DEFAULTS = {
    "powerwall_url": "http://10.0.1.26:8675/csv/v2?headers",
    "poll_interval_day":    300,    # seconds between polls during daylight
    "poll_interval_night":  600,    # seconds between polls at night
    "log_dir":              "logs",
    "summary_hour":         23,     # hour to write daily narrative (23 = 11pm)
    "solar_threshold_w":    50,     # watts — below this, solar is "off"
    "timezone":             "America/Los_Angeles",
}

def load_config():
    config = DEFAULTS.copy()
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            config.update(json.load(f))
    # resolve log_dir relative to script location
    if not os.path.isabs(config["log_dir"]):
        config["log_dir"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), config["log_dir"])
    Path(config["log_dir"]).mkdir(parents=True, exist_ok=True)
    return config

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sunlog")

# ── Data fetch ────────────────────────────────────────────────────────────────

def fetch(url, timeout=10):
    """Fetch the pypowerwall CSV endpoint. Returns dict or None on error."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            lines = r.read().decode().strip().splitlines()
        if len(lines) < 2:
            return None
        reader = csv.DictReader(lines)
        row = next(reader, None)
        if not row:
            return None
        return {k.strip(): v.strip() for k, v in row.items()}
    except Exception as e:
        log.warning("Fetch error: %s", e)
        return None

def parse(row):
    """Parse a CSV row into typed values. Returns dict with float fields."""
    def f(key):
        try:
            return float(row.get(key, 0))
        except (ValueError, TypeError):
            return 0.0

    return {
        "grid_w":       f("Grid"),
        "home_w":       f("Home"),
        "solar_w":      f("Solar"),
        "battery_w":    f("Battery"),   # positive = charging, negative = discharging
        "battery_pct":  f("BatteryLevel"),
        "grid_status":  row.get("GridStatus", "").strip(),
        "ts":           datetime.now(),
    }

# ── Day tracker ───────────────────────────────────────────────────────────────

class DayTracker:
    """Accumulates readings throughout a day and produces a narrative."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.reset()

    def reset(self):
        self.date          = date.today()
        self.readings      = []          # list of parsed dicts
        self.solar_on_ts   = None        # first moment solar > threshold
        self.solar_off_ts  = None        # last moment solar > threshold
        self.solar_peak_w  = 0.0
        self.solar_peak_ts = None
        self.battery_full_ts = None      # first time battery >= 99%
        self.grid_events   = 0           # times grid_status changed to/from On
        self.summary_written = False

    def load_from_csv(self):
        """Reload today's readings from CSV on startup (survives process restarts)."""
        path = os.path.join(self.cfg["log_dir"], f"{self.date}.csv")
        if not os.path.exists(path):
            return
        try:
            with open(path, newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    d = {
                        "ts":          datetime.fromisoformat(row["timestamp"]),
                        "solar_w":     float(row["solar_w"]),
                        "home_w":      float(row["home_w"]),
                        "battery_w":   float(row["battery_w"]),
                        "battery_pct": float(row["battery_pct"]),
                        "grid_w":      float(row["grid_w"]),
                        "grid_status": row.get("grid_status", ""),
                    }
                    self.ingest(d)
            self._rows_saved = len(self.readings)
            log.info("♻️  Reloaded %d readings from %s", len(self.readings), path)
        except Exception as e:
            log.warning("Could not reload CSV: %s", e)

    def ingest(self, d):
        """Feed one parsed reading into the tracker."""
        self.readings.append(d)
        threshold = self.cfg["solar_threshold_w"]

        if d["solar_w"] > threshold:
            if self.solar_on_ts is None:
                self.solar_on_ts = d["ts"]
                log.info("☀️  Solar online — %.0f W", d["solar_w"])
            self.solar_off_ts = d["ts"]

            if d["solar_w"] > self.solar_peak_w:
                self.solar_peak_w  = d["solar_w"]
                self.solar_peak_ts = d["ts"]

        if d["battery_pct"] >= 99 and self.battery_full_ts is None:
            self.battery_full_ts = d["ts"]
            log.info("🔋 Battery full at %s", d["ts"].strftime("%H:%M"))

    def _kwh(self, key, sign=None):
        """Rough kWh estimate from watt readings (trapezoidal, ~5min intervals)."""
        vals = [abs(r[key]) for r in self.readings
                if (sign is None or (sign > 0 and r[key] > 0) or (sign < 0 and r[key] < 0))]
        if len(vals) < 2:
            return 0.0
        # average watts * hours (assume uniform poll spacing)
        hours = (self.readings[-1]["ts"] - self.readings[0]["ts"]).total_seconds() / 3600
        avg_w = sum(vals) / len(vals)
        return round(avg_w * hours / 1000, 2)

    def _pct_solar(self):
        """Fraction of home load intervals where solar was producing."""
        if not self.readings:
            return 0.0
        threshold = self.cfg["solar_threshold_w"]
        solar_intervals = sum(1 for r in self.readings if r["solar_w"] > threshold)
        return solar_intervals / len(self.readings)

    def narrative(self):
        """Build a human-readable energy story for the day."""
        d    = self.date
        r    = self.readings
        cfg  = self.cfg

        if not r:
            return f"{d} — No data recorded."

        first = r[0]
        last  = r[-1]

        lines = []
        lines.append(f"── {d.strftime('%A, %B %-d, %Y')} ──────────────────────────")
        lines.append("")

        # Solar arc
        if self.solar_on_ts:
            on_str   = self.solar_on_ts.strftime("%-I:%M %p")
            off_str  = self.solar_off_ts.strftime("%-I:%M %p") if self.solar_off_ts else "?"
            peak_str = self.solar_peak_ts.strftime("%-I:%M %p") if self.solar_peak_ts else "?"
            dur_h    = (self.solar_off_ts - self.solar_on_ts).total_seconds() / 3600 if self.solar_off_ts else 0
            lines.append(f"☀️  Solar came online at {on_str}, peaked at "
                         f"{self.solar_peak_w/1000:.1f} kW around {peak_str}, "
                         f"and faded at {off_str} ({dur_h:.1f} hrs of production).")
        else:
            lines.append("☀️  No solar production recorded today.")

        # Battery
        if self.battery_full_ts:
            lines.append(f"🔋 Battery reached full charge by {self.battery_full_ts.strftime('%-I:%M %p')}.")
        end_pct = last["battery_pct"]
        start_pct = first["battery_pct"]
        delta_pct = end_pct - start_pct
        direction = "up" if delta_pct > 0 else "down"
        lines.append(f"    Battery ended the day at {end_pct:.0f}% "
                     f"({direction} {abs(delta_pct):.0f}% from {start_pct:.0f}% at midnight).")

        # Home load & grid
        home_kwh   = self._kwh("home_w")
        solar_kwh  = self._kwh("solar_w")
        grid_kwh   = self._kwh("grid_w", sign=1)   # positive = importing
        export_kwh = self._kwh("grid_w", sign=-1)  # negative = exporting
        solar_frac = self._pct_solar()

        lines.append(f"🏠 Home used ~{home_kwh:.1f} kWh. "
                     f"Solar produced ~{solar_kwh:.1f} kWh ({solar_frac*100:.0f}% of the day).")

        if grid_kwh > 0.1:
            lines.append(f"⚡ Grid supplied ~{grid_kwh:.1f} kWh.")
        else:
            lines.append(f"⚡ The house ran without drawing from the grid.")

        if export_kwh > 0.1:
            lines.append(f"    ~{export_kwh:.1f} kWh exported back to the grid.")

        # One-line story
        lines.append("")
        if solar_frac >= 0.95 and not grid_kwh > 0.5:
            story = "A nearly perfect solar day — the house ran entirely on stored sunlight."
        elif solar_frac >= 0.7:
            story = "A strong solar day. The sun carried most of the load."
        elif solar_frac >= 0.4:
            story = "A mixed day — solar and storage shared the work with the grid."
        elif self.solar_on_ts:
            story = "A thin solar day. The grid did most of the heavy lifting."
        else:
            story = "No sun today. The Powerwall and grid kept the lights on."
        lines.append(f"✨ {story}")
        lines.append("")

        return "\n".join(lines)

    def save_raw_csv(self):
        """Append only new (unsaved) readings to today's CSV file."""
        path = os.path.join(self.cfg["log_dir"], f"{self.date}.csv")
        write_header = not os.path.exists(path)

        # Track how many rows are already on disk (set during load_from_csv or prior saves)
        rows_on_disk = getattr(self, "_rows_saved", 0)
        new_readings = self.readings[rows_on_disk:]
        if not new_readings:
            return

        with open(path, "a", newline="") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(["timestamp", "solar_w", "home_w",
                                 "battery_w", "battery_pct", "grid_w", "grid_status"])
            for r in new_readings:
                writer.writerow([
                    r["ts"].isoformat(timespec="seconds"),
                    round(r["solar_w"], 1), round(r["home_w"], 1),
                    round(r["battery_w"], 1), round(r["battery_pct"], 1),
                    round(r["grid_w"], 1), r["grid_status"],
                ])
        self._rows_saved = len(self.readings)
        log.debug("💾 Saved %d new rows to CSV", len(new_readings))

    def save_narrative(self):
        """Write the narrative summary to a .txt file."""
        text = self.narrative()
        path = os.path.join(self.cfg["log_dir"], f"{self.date}-summary.txt")
        with open(path, "w") as f:
            f.write(text)
        log.info("📓 Summary written → %s", path)
        print("\n" + text)
        return path

# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    cfg     = load_config()
    tracker = DayTracker(cfg)
    running = True

    def shutdown(sig, frame):
        nonlocal running
        log.info("Signal received — shutting down gracefully.")
        running = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT,  shutdown)

    log.info("🌅 sunlog starting — polling %s", cfg["powerwall_url"])
    log.info("   Day poll: %ds | Night poll: %ds | Log dir: %s",
             cfg["poll_interval_day"], cfg["poll_interval_night"], cfg["log_dir"])

    # Reload today's data from CSV if we're restarting mid-day
    tracker.load_from_csv()

    summary_hour    = cfg["summary_hour"]
    last_summary_d  = None

    while running:
        now  = datetime.now()
        today = now.date()

        # Roll over at midnight
        if tracker.date != today:
            log.info("🌙 Midnight rollover — saving %s data", tracker.date)
            tracker.save_raw_csv()
            if not tracker.summary_written:
                tracker.save_narrative()
            tracker = DayTracker(cfg)

        # Fetch + ingest
        row = fetch(cfg["powerwall_url"])
        if row:
            d = parse(row)
            tracker.ingest(d)
            log.debug("☀ %.0fW  🏠 %.0fW  🔋 %.0f%%  ⚡ %.0fW",
                      d["solar_w"], d["home_w"], d["battery_pct"], d["grid_w"])

        # Write summary at summary_hour each day
        if now.hour >= summary_hour and last_summary_d != today and not tracker.summary_written:
            tracker.save_raw_csv()
            tracker.save_narrative()
            tracker.summary_written = True
            last_summary_d = today

        # Dynamic sleep — shorter during daylight hours
        solar_active = tracker.solar_on_ts is not None and tracker.solar_off_ts == tracker.readings[-1]["ts"] if tracker.readings else False
        interval = cfg["poll_interval_day"] if (7 <= now.hour <= 20) else cfg["poll_interval_night"]

        # Sleep in 1s chunks so we can respond to signals
        for _ in range(interval):
            if not running:
                break
            time.sleep(1)

    # Final flush on shutdown
    log.info("💾 Flushing data before exit…")
    if tracker.readings:
        tracker.save_raw_csv()
        if not tracker.summary_written:
            tracker.save_narrative()
    log.info("👋 sunlog stopped.")

if __name__ == "__main__":
    main()
