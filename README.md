# TTC Bus Tracker

A Raspberry Pi / Python TTC bus arrival tracker with I2C LCD display support.  
Designed for a small LCD monitor and expandable to sequential LEDs and an optional buzzer/speaker alert when a bus is close.

---

## Overview

This project fetches TTC GTFS-realtime bus predictions and displays them on a 16x2 I2C LCD screen.  
It is built to support:

- 16x2 I2C LCD display
- sequential LED proximity indicator
- optional speaker/buzzer that buzzes every minute when a bus is very close

---

## Features

- Polls TTC GTFS-RT bus trip updates
- Displays filtered routes and stops
- Shows next arrival times on LCD
- Alerts when a bus is within a configured close range
- LEDs that light up progressively depending on how close the bus is
- Audio alert that buzzes every minute when close to the stop

---

## Files

- `backend.py` — main tracker application
- `routes.txt` — GTFS route lookup data
- `stops.txt` — GTFS stop lookup data
- `logs/` — optional logs directory

---

## Requirements

- Python 3
- `requests`
- `beautifulsoup4`
- `protobuf`
- `google-transit`
- `RPLCD`
- I2C LCD display hardware

---

## Installation

```bash
git clone https://github.com/shawnn101/ttc_bus_tracker.git
cd ttc_bus_tracker
pip install requests beautifulsoup4 protobuf RPLCD google-transit
```



## How It Works

* `backend.py` loads static GTFS route and stop files.
* It polls TTC's real-time trip updates.
* It filters predictions for configured routes and stop codes.
* It shows the nearest upcoming bus arrivals on the LCD.
* If a prediction is within the alert window, it can trigger a close alert.
* Optional LED and buzzer logic can be added around the alert code.

---

## Configuration

Edit `backend.py` to configure:

* `ROUTE_PAIRS` — route and stop code pairs to track
* `POLL_SECS` — how often to update TTC data
* `ROTATE_SECS` — how long each LCD entry displays before rotating
* `ALERT_MINUTES` — how early to trigger close alerts
* `TOP_N` — number of predictions to cycle through

### Example

```python
ROUTE_PAIRS = [
    ("38", "7704"),
    ("938", "7704"),
    ("95", "7704"),
    ("995", "7704"),
]
```

---

## Run

```bash
python backend.py
```


