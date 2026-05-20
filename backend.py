import time
import requests
import csv
import re
from typing import List, Tuple, Set, Dict

from bs4 import BeautifulSoup
from google.transit import gtfs_realtime_pb2
from google.protobuf import text_format

# LCD library
from RPLCD.i2c import CharLCD

# ================= LCD SETUP =================
# Common I2C addresses: 0x27 or 0x3F
lcd = CharLCD(
    i2c_expander="PCF8574",
    address=0x27,
    port=1,
    cols=16,
    rows=2,
)

# ================= TTC CONFIG =================
ELLESMERE_CODES = ["7704"]
NEILSON_CODES   = ["1379"]
STOP_CODES = set(ELLESMERE_CODES + NEILSON_CODES)

ROUTE_PAIRS = [
    ("38",  "7704"),
    ("938", "7704"),
    ("95",  "7704"),
    ("995", "7704"),
    ("154", "7704"),
    ("133", "1379"),
]

POLL_SECS     = 10
ROTATE_SECS   = 1
TOP_N         = 4
ALERT_MINUTES = 5
PAUSE         = 2
ALERT_DISPLAY_SECS = 2

TRIP_UPDATES_URL  = "https://bustime.ttc.ca/gtfsrt/trips"
SUBWAY_STATUS_URL = "https://www.ttc.ca/"

ROUTES_TXT = "routes.txt"
STOPS_TXT  = "stops.txt"

# ================= DISPLAY HELPERS =================
def shorten(s: str, n=16) -> str:
    return s if len(s) <= n else s[:n-3] + "..."

def clear():
    lcd.clear()

def draw(l1: str, l2: str = ""):
    lcd.clear()
    lcd.write_string(l1[:16])
    lcd.crlf()
    lcd.write_string(l2[:16])

def mins_until(eta_epoch: int) -> int:
    now = time.time()
    return max(0, int((eta_epoch - now + 59) // 60))

def show_bus_alerts(alerts: List[Tuple[int, str]]):
    for m, r in alerts:
        draw("BUS ALERT", f"R{r} in {m} min")
        time.sleep(ALERT_DISPLAY_SECS)

def show_subway_status(alerts: List[str]):
    if not alerts:
        draw("SUBWAY STATUS", "No alerts")
        time.sleep(ALERT_DISPLAY_SECS)
        return

    for alert in alerts:
        draw("SYSTEM ALERT", shorten(alert, 16))
        time.sleep(ALERT_DISPLAY_SECS)

# ================= GTFS STATIC FILES =================
def load_route_id_to_short_name(path: str) -> Dict[str, str]:
    m: Dict[str, str] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rid = (row.get("route_id") or "").strip()
            rsn = (row.get("route_short_name") or "").strip()
            if rid and rsn:
                m[rid] = rsn
    return m

def load_stop_id_to_code(path: str) -> Dict[str, str]:
    m: Dict[str, str] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sid = (row.get("stop_id") or "").strip()
            sc  = (row.get("stop_code") or "").strip()
            if sid and sc:
                m[sid] = sc
    return m

def allowed_routes_from_pairs(pairs: List[Tuple[str, str]]) -> Set[str]:
    return set(r for r, _ in pairs)

def allowed_codes_from_pairs(pairs: List[Tuple[str, str]]) -> Set[str]:
    return set(c for _, c in pairs)

# ================= GTFS-RT FETCH =================
def fetch_tripupdates(url: str) -> gtfs_realtime_pb2.FeedMessage:
    r = requests.get(
        url,
        timeout=15,
        headers={
            "User-Agent": "TTC-Tracker/1.0",
            "Accept": "application/x-protobuf, application/octet-stream, */*",
        },
        allow_redirects=True,
    )
    r.raise_for_status()

    data = r.content

    if b"<html" in data[:200].lower():
        snippet = r.text[:220].replace("\n", " ")
        raise RuntimeError(f"Got HTML instead of GTFS data: {snippet}")

    feed = gtfs_realtime_pb2.FeedMessage()
    stripped = data.lstrip()

    if stripped.startswith(b"header {") or stripped.startswith(b"entity {"):
        text_format.Merge(stripped.decode("utf-8", errors="ignore"), feed)
    else:
        feed.ParseFromString(data)

    return feed

# ================= BUS PREDICTIONS =================
def extract_predictions(
    feed: gtfs_realtime_pb2.FeedMessage,
    route_allow: Set[str],
    code_allow: Set[str],
    route_id_to_short: Dict[str, str],
    stop_id_to_code: Dict[str, str],
) -> List[Tuple[int, str, str]]:
    out: List[Tuple[int, str, str]] = []

    for ent in feed.entity:
        if not ent.HasField("trip_update"):
            continue

        tu = ent.trip_update
        raw_route_id = (tu.trip.route_id or "").strip()
        route = route_id_to_short.get(raw_route_id, raw_route_id)

        if route_allow and route not in route_allow:
            continue

        for stu in tu.stop_time_update:
            raw_stop_id = (stu.stop_id or "").strip()
            code = stop_id_to_code.get(raw_stop_id)

            if not code or code not in code_allow:
                continue

            eta = 0
            if stu.HasField("arrival") and stu.arrival.time:
                eta = stu.arrival.time
            elif stu.HasField("departure") and stu.departure.time:
                eta = stu.departure.time

            if eta:
                out.append((int(eta), route, code))

    return out

def dedupe_soonest_per_route(rows: List[Tuple[int, str, str]]) -> List[Tuple[int, str, str]]:
    best: Dict[str, Tuple[int, str]] = {}

    for eta, route, code in rows:
        if route not in best or eta < best[route][0]:
            best[route] = (eta, code)

    out = [(eta, route, code) for route, (eta, code) in best.items()]
    out.sort(key=lambda x: x[0])
    return out

# ================= SUBWAY ALERTS =================
def fetch_subway_status_lines() -> List[str]:
    r = requests.get(
        SUBWAY_STATUS_URL,
        timeout=15,
        headers={"User-Agent": "TTC-Tracker/1.0"},
    )
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text("\n", strip=True)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    in_block = False
    out: List[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        if "Subway and light rail" in line:
            in_block = True
            i += 1
            continue

        if in_block:
            if "Surface routes with active alerts" in line:
                break

            if re.fullmatch(r"\d+", line):
                line_num = line
                status = lines[i + 1] if i + 1 < len(lines) else ""
                out.append(f"Line {line_num}: {status}")
                i += 2
                continue

        i += 1

    return out

def subway_alert_lines(status_lines: List[str]) -> List[str]:
    return [s for s in status_lines if "Normal service" not in s]

# ================= MAIN LOOP =================
def main():
    draw("TTC Tracker", "Kennedy-bound")

    try:
        route_id_to_short = load_route_id_to_short_name(ROUTES_TXT)
        stop_id_to_code = load_stop_id_to_code(STOPS_TXT)
    except FileNotFoundError as e:
        draw("Missing file", e.filename[:16])
        time.sleep(5)
        return

    route_allow = allowed_routes_from_pairs(ROUTE_PAIRS)
    code_allow = allowed_codes_from_pairs(ROUTE_PAIRS)

    last_poll = 0.0
    idx = 0

    merged: List[Tuple[int, str, str]] = []
    pending_bus_alerts: List[Tuple[int, str]] = []
    pending_subway_alerts: List[str] = []

    while True:
        try:
            now = time.time()

            if now - last_poll >= POLL_SECS:
                last_poll = now

                feed = fetch_tripupdates(TRIP_UPDATES_URL)
                merged = extract_predictions(
                    feed,
                    route_allow,
                    code_allow,
                    route_id_to_short,
                    stop_id_to_code,
                )

                merged = dedupe_soonest_per_route(merged)[:8]

                bus_alerts = [
                    (mins_until(eta), route)
                    for eta, route, _ in merged
                    if mins_until(eta) <= ALERT_MINUTES
                ]

                pending_bus_alerts = sorted(bus_alerts, key=lambda x: (x[0], x[1]))

                try:
                    subway_status = fetch_subway_status_lines()
                    pending_subway_alerts = subway_alert_lines(subway_status)
                except Exception:
                    pending_subway_alerts = []

                if merged:
                    idx %= min(TOP_N, len(merged))
                else:
                    idx = 0

            if merged:
                top = merged[:min(TOP_N, len(merged))]
                cycle_len = len(top)

                if idx >= cycle_len:
                    idx = 0

                if idx == 0:
                    show_subway_status(pending_subway_alerts)

                    if pending_bus_alerts:
                        show_bus_alerts(pending_bus_alerts)

                eta, route, _code = top[idx]
                minutes = mins_until(eta)

                draw(
                    f"{idx + 1}/{cycle_len}",
                    f"{minutes:>2}m Route {route}"
                )

                idx += 1

                if idx >= cycle_len:
                    time.sleep(PAUSE)
                    idx = 0
            else:
                draw("No arrivals", "Check filters")

            time.sleep(ROTATE_SECS)

        except KeyboardInterrupt:
            break
        except Exception as e:
            draw("ERROR", shorten(str(e), 16))
            time.sleep(3)

    clear()

if __name__ == "__main__":
    main()