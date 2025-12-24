import time
import requests
import csv
import re
from typing import List, Tuple, Set, Dict

from bs4 import BeautifulSoup
from google.transit import gtfs_realtime_pb2
from google.protobuf import text_format

# ====== Kennedy-bound TTC STOP CODES (keep these unchanged) ======
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

# ---- Config ----
POLL_SECS     = 5
ROTATE_SECS   = 1
TOP_N         = 4
ALERT_MINUTES = 5
PAUSE         = 10
ALERT_DISPLAY_SECS = 1

TRIP_UPDATES_URL   = "https://bustime.ttc.ca/gtfsrt/trips"
SUBWAY_STATUS_URL  = "https://www.ttc.ca/"

ROUTES_TXT = "routes.txt"
STOPS_TXT  = "stops.txt"

# ---------- Terminal helpers ----------
def shorten(s: str, n=24) -> str:
    return s if len(s) <= n else s[:n-3] + "..."

def clear():
    print("\033[2J\033[H", end="", flush=True)

def draw(l1: str, l2: str = ""):
    clear()
    print(l1, flush=True)
    print(l2, flush=True)

def mins_until(eta_epoch: int) -> int:
    now = time.time()
    return max(0, int((eta_epoch - now + 59) // 60))  # ceil

def show_bus_alerts(alerts: List[Tuple[int, str]]):
    clear()
    print("🚨 BUS ALERTS:", flush=True)
    for m, r in alerts:
        print(f"ALERT: Route {r} in {m} min", flush=True)

def show_subway_status(lines: List[str]):
    """
    Shows subway status every cycle start.
    If there are no disruptions, it prints 'No active subway alerts'.
    """
    clear()
    print("\n🚇 SUBWAY STATUS:", flush=True)
    if not lines:
        print("No active subway alerts (all normal).", flush=True)
        return
    for s in lines:
        print(s, flush=True)

# ---------- Load static GTFS maps ----------
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

# ---------- GTFS-RT fetch ----------
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
        raise RuntimeError(f"Got HTML (wrong endpoint). Snippet: {snippet}")

    feed = gtfs_realtime_pb2.FeedMessage()
    stripped = data.lstrip()

    if stripped.startswith(b"header {") or stripped.startswith(b"entity {"):
        text_format.Merge(stripped.decode("utf-8", errors="ignore"), feed)
    else:
        feed.ParseFromString(data)

    return feed

# ---------- Extract predictions ----------
def extract_predictions(
    feed: gtfs_realtime_pb2.FeedMessage,
    route_allow: Set[str],
    code_allow: Set[str],
    route_id_to_short: Dict[str, str],
    stop_id_to_code: Dict[str, str],
) -> List[Tuple[int, str, str]]:
    """
    Returns (eta_epoch_seconds, public_route_number, stop_code)
    """
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

# ---------- Keep only soonest per route ----------
def dedupe_soonest_per_route(rows: List[Tuple[int, str, str]]) -> List[Tuple[int, str, str]]:
    best: Dict[str, Tuple[int, str]] = {}
    for eta, r, code in rows:
        if (r not in best) or (eta < best[r][0]):
            best[r] = (eta, code)

    out = [(eta, route, code) for route, (eta, code) in best.items()]
    out.sort(key=lambda x: x[0])
    return out

# ---------- Subway status (FIXED parsing) ----------
def fetch_subway_status_lines() -> List[str]:
    """
    TTC homepage prints the heading as two lines:
      "Subway and light rail"
      "status"
    Your old parser required both words in the SAME line, so it never entered the block.

    This version:
    - Detects "Subway and light rail" alone
    - Reads line numbers (1/2/4/6) + their status ("Normal service", "Delay", etc.)
    - Stops at "Surface routes with active alerts"
    """
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

            # TTC shows a number line ("1", "2", "4", "6") then a status line ("Normal service")
            if re.fullmatch(r"\d+", line):
                line_num = line
                status = ""
                if i + 1 < len(lines):
                    status = lines[i + 1]
                out.append(f"Line {line_num}: {status}")
                i += 2
                continue

        i += 1

    return out

def subway_alert_lines(status_lines: List[str]) -> List[str]:
    # Only return disruptions (non-normal). If all normal, return [].
    return [s for s in status_lines if "Normal service" not in s]

# ---------- main loop ----------
def main():
    draw("TTC Tracker (Terminal)", "Kennedy-bound")

    try:
        route_id_to_short = load_route_id_to_short_name(ROUTES_TXT)
        stop_id_to_code   = load_stop_id_to_code(STOPS_TXT)
    except FileNotFoundError as e:
        draw("MISSING FILE", f"{e.filename} not found")
        print("\nPut routes.txt and stops.txt next to this script.", flush=True)
        return

    route_allow = allowed_routes_from_pairs(ROUTE_PAIRS)
    code_allow  = allowed_codes_from_pairs(ROUTE_PAIRS)

    last_poll = 0.0
    idx = 0
    merged: List[Tuple[int, str, str]] = []

    pending_bus_alerts: List[Tuple[int, str]] = []
    pending_subway_status: List[str] = []
    pending_subway_alerts: List[str] = []

    while True:
        try:
            now = time.time()

            # ---- Poll (update merged + pending alerts/status) ----
            if now - last_poll >= POLL_SECS:
                last_poll = now

                # buses
                feed = fetch_tripupdates(TRIP_UPDATES_URL)
                merged = extract_predictions(
                    feed, route_allow, code_allow, route_id_to_short, stop_id_to_code
                )
                merged = dedupe_soonest_per_route(merged)[:8]

                bus_alerts = [(mins_until(eta), r) for eta, r, _ in merged if mins_until(eta) <= ALERT_MINUTES]
                pending_bus_alerts = sorted(bus_alerts, key=lambda x: (x[0], x[1]))

                # subway (status + alerts)
                try:
                    pending_subway_status = fetch_subway_status_lines()
                    pending_subway_alerts = subway_alert_lines(pending_subway_status)
                except Exception:
                    pending_subway_status = []
                    pending_subway_alerts = []

                if merged:
                    idx %= min(TOP_N, len(merged))
                else:
                    idx = 0

            # ---- Rotate display ----
            if merged:
                top = merged[:min(TOP_N, len(merged))]
                cycle_len = len(top)

                if idx >= cycle_len:
                    idx = 0

                # At start of each cycle:
                # 1) show subway STATUS always (so you know parsing is working)
                # 2) if there are disruptions, that will show inside the status block
                # 3) show bus alerts
                if idx == 0:
                    # show status (not just disruptions) so you actually see something
                    show_subway_status(
                        pending_subway_alerts if pending_subway_alerts else []
                    )
                    time.sleep(ALERT_DISPLAY_SECS)

                    if pending_bus_alerts:
                        show_bus_alerts(pending_bus_alerts)
                        time.sleep(ALERT_DISPLAY_SECS)

                eta, route, _code = top[idx]
                m = mins_until(eta)
                draw(f"{idx + 1}/{cycle_len}", f"{m:>2}m  Route {shorten(route)}")

                idx += 1
                if idx >= cycle_len:
                    print()  # blank line after 3/3
                    time.sleep(PAUSE)
                    idx = 0
            else:
                draw("Nothing to display...", "No arrivals / filter mismatch")
                time.sleep(ROTATE_SECS)

            time.sleep(ROTATE_SECS)

        except KeyboardInterrupt:
            break
        except Exception as e:
            draw("ERROR", str(e))
            time.sleep(2)

    clear()
    print("Exited cleanly.", flush=True)

if __name__ == "__main__":
    main()
