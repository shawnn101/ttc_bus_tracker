import time
import requests
import csv
from typing import List, Tuple, Set, Dict
from google.transit import gtfs_realtime_pb2
from google.protobuf import text_format

# ====== Kennedy-bound TTC STOP CODES (keep these unchanged) ======
ELLESMERE_CODES = ["7704"]
NEILSON_CODES   = ["1379"]
STOP_CODES = set(ELLESMERE_CODES + NEILSON_CODES)

ROUTE_PAIRS = [
    ("38",  "7704"),
    ("95",  "7704"),
    ("995", "7704"),
    ("133", "1379"),
]

# ---- Config ----
POLL_SECS     = 20
ROTATE_SECS   = 2
TOP_N         = 4
ALERT_MINUTES = 5
PAUSE         = 10
ALERT_DISPLAY_SECS = 3

TRIP_UPDATES_URL = "https://bustime.ttc.ca/gtfsrt/trips"

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

def show_alerts(alerts: List[Tuple[int, str]]):
    clear()
    print("\n🚨 ALERTS:", flush=True)
    for m, r in alerts:
        print(f"ALERT: Route {r} in {m} min", flush=True)

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
    best: Dict[str, Tuple[int, str]] = {}  # route -> (eta, stop_code)
    for eta, r, code in rows:
        if (r not in best) or (eta < best[r][0]):
            best[r] = (eta, code)

    out = [(eta, route, code) for route, (eta, code) in best.items()]
    out.sort(key=lambda x: x[0])
    return out

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

    # ✅ alerts updated on poll, displayed EVERY cycle start (idx==0)
    pending_alerts: List[Tuple[int, str]] = []

    while True:
        try:
            now = time.time()

            # ---- Poll GTFS-RT (update merged + pending alerts) ----
            if now - last_poll >= POLL_SECS:
                last_poll = now

                feed = fetch_tripupdates(TRIP_UPDATES_URL)
                merged = extract_predictions(
                    feed, route_allow, code_allow, route_id_to_short, stop_id_to_code
                )
                merged = dedupe_soonest_per_route(merged)[:8]

                # compute alerts using LIVE mins, keep stable order
                alerts = [(mins_until(eta), r) for eta, r, _ in merged if mins_until(eta) <= ALERT_MINUTES]
                pending_alerts = sorted(alerts, key=lambda x: (x[0], x[1]))

                # keep idx safe if list shrank
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

                if idx == 0 and pending_alerts:
                    show_alerts(pending_alerts)
                    time.sleep(ALERT_DISPLAY_SECS)

                eta, route, _code = top[idx]
                m = mins_until(eta)
                draw(f"{idx + 1}/{cycle_len}", f"{m:>2}m  Route {shorten(route)}")

                idx += 1
                if idx >= cycle_len:
                    print()
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
