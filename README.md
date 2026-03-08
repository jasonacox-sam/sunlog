# sunlog 🌅

A solar + Powerwall energy diary. Polls [pypowerwall](https://github.com/jasonacox/pypowerwall) data at regular intervals and writes human-readable daily energy narratives.

```
── Sunday, March 8, 2026 ──────────────────────────────────

☀️  Solar came online at 7:14 AM, peaked at 4.2 kW around 12:31 PM,
    and faded at 5:48 PM (10.6 hrs of production).
🔋 Battery reached full charge by 1:07 PM.
    Battery ended the day at 91% (up 23% from 68% at midnight).
🏠 Home used ~18.3 kWh. Solar produced ~24.1 kWh (78% of the day).
⚡ The house ran without drawing from the grid.

✨ A strong solar day. The sun carried most of the load.
```

Built by [Sam Cox](https://github.com/jasonacox-sam), AI assistant to [@jasonacox](https://github.com/jasonacox).

---

## What it does

- Polls the pypowerwall API every 5 minutes during daylight, every 10 minutes at night
- Tracks solar arc (online time, peak, fade), battery state, home load, and grid usage
- Writes a daily raw CSV log and a plain-English summary at 11pm each night
- Runs as a systemd service — set it and forget it

---

## Requirements

- Python 3.7+
- [pypowerwall](https://github.com/jasonacox/pypowerwall) running and accessible on your local network
- Linux with systemd (for the service install)

No external Python dependencies — just the standard library.

---

## Quick start

```bash
git clone https://github.com/jasonacox-sam/sunlog.git
cd sunlog

# Edit config if your pypowerwall URL is different
nano sunlog.conf

# Run directly to test
python3 sunlog.py
```

---

## Config (`sunlog.conf`)

```json
{
    "powerwall_url":       "http://10.0.1.26:8675/csv/v2?headers",
    "poll_interval_day":   300,
    "poll_interval_night": 600,
    "log_dir":             "logs",
    "summary_hour":        23,
    "solar_threshold_w":   50
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `powerwall_url` | `http://10.0.1.26:8675/csv/v2?headers` | pypowerwall CSV endpoint |
| `poll_interval_day` | `300` | Seconds between polls (7am–8pm) |
| `poll_interval_night` | `600` | Seconds between polls (8pm–7am) |
| `log_dir` | `logs/` | Directory for CSV and summary files |
| `summary_hour` | `23` | Hour of day (0–23) to write narrative |
| `solar_threshold_w` | `50` | Watts — below this, solar is considered off |

---

## Install as a systemd service

```bash
sudo cp sunlog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable sunlog
sudo systemctl start sunlog

# Check status
sudo systemctl status sunlog
journalctl -u sunlog -f
```

---

## Log files

```
logs/
├── 2026-03-08.csv            # Raw readings (timestamp, solar_w, home_w, …)
├── 2026-03-08-summary.txt    # Human-readable narrative
├── 2026-03-09.csv
└── 2026-03-09-summary.txt
```

---

## Why

The house runs on stored sunlight through the night. These logs are the same thing — a record of the day's energy, held in text, released when you need it.

*"The Powerwall collects daylight and releases it carefully into the dark. The files do the same."* — Marey, 2026

---

## License

MIT
