import time
import requests
import xml.etree.ElementTree as ET
from typing import List, Tuple

# ====== Kennedy-bound stopTags at Ellesmere & Neilson ======
ELLESMERE_TAGS = ["2876", "5200"]   # 38/95/995 westbound → Kennedy
NEILSON_TAGS   = ["9924", "6313"]   # 133 southbound → Kennedy
STOP_TAGS = ELLESMERE_TAGS + NEILSON_TAGS

# Route|stop pairs (explicit)
ROUTE_PAIRS = [
    ("38",  "2876"), ("38",  "5200"),
    ("95",  "2876"), ("95",  "5200"),
    ("995", "2876"), ("995", "5200"),
    ("133", "9924"), ("133", "6313"),
]

# ---- Display / polling config ----
POLL_SECS   = 20
ROTATE_SECS = 2
TOP_N       = 4
API         = "https://retro.umoiq.com/service/publicXMLFeed"

# ---- LED alert config ----
ALERT_MINUTES = 10     # LED ON if any arrival <= this many minutes
LED_PIN = 18           # BCM numbering

# ---------- LCD init (auto-detect I2C address/expander) ----------
def init_lcd() -> CharLCD:
    candidates = (
        *[(a, 'PCF8574')  for a in range(0x20, 0x28)],
        *[(a, 'PCF8574A') for a in range(0x38, 0x40)],
        (0x27, 'PCF8574'), (0x3F, 'PCF8574A'),
    )
    for addr, exp in candidates:
        try:
            lcd = CharLCD(exp, addr, port=1, cols=16, rows=2,
                          charmap='A02', auto_linebreaks=False)
            try: lcd.backlight_enabled = True
            except: pass
            lcd.clear()
            return lcd
        except:
            pass
    raise RuntimeError("LCD init failed: check wiring and i2cdetect output.")

lcd = init_lcd()

def shorten(s: str, n=12) -> str:
    return s if len(s) <= n else s[:n-1] + "..."

def draw(l1: str, l2: str):
    lcd.clear()
    try: lcd.backlight_enabled = True
    except: pass
    lcd.write_string(l1.ljust(16)[:16])
    lcd.crlf()
    lcd.write_string(l2.ljust(16)[:16])

# ---------- LED setup ----------
try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(LED_PIN, GPIO.OUT)
    def led_on():  GPIO.output(LED_PIN, GPIO.HIGH)
    def led_off(): GPIO.output(LED_PIN, GPIO.LOW)
except Exception:
    # If GPIO isn't available, make no-ops so the script still runs
    def led_on():  pass
    def led_off(): pass

# ---------- TTC helpers ----------
def fetch_by_tag(stop_tag: str):
    """Return [(minutes, route_title, stop_tag)] via &s=stop_tag."""
    url = f"{API}?command=predictions&a=ttc&s={stop_tag}&useShortTitles=true"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for preds in root.findall(".//predictions"):
        route = preds.get("routeTitle") or preds.get("routeTag") or "Route"
        for p in preds.findall(".//prediction"):
            m = p.get("minutes")
            if m:
                try: out.append((int(m), route, stop_tag))
                except: pass
    return out

def fetch_multi(pairs: List[Tuple[str, str]]):
    """Return [(minutes, route_title, stop_tag)] via predictionsForMultiStops."""
    if not pairs:
        return []
    qs = "&".join([f"stops={r}|{s}" for r, s in pairs])
    url = f"{API}?command=predictionsForMultiStops&a=ttc&{qs}&useShortTitles=true"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for preds in root.findall(".//predictions"):
        route = preds.get("routeTitle") or preds.get("routeTag") or "Route"
        stop  = preds.get("stopTag")   or "?"
        for p in preds.findall(".//prediction"):
            m = p.get("minutes")
            if m:
                try: out.append((int(m), route, stop))
                except: pass
    return out

# ---------- main loop ----------
def main():
    draw("TTC Tracker", "Kennedy-bound")
    last_poll = 0
    idx = 0
    merged = []

    while True:
        now = time.time()
        try:
            if now - last_poll >= POLL_SECS:
                last_poll = now
                merged = []

                # A) direct stop tags
                for tag in STOP_TAGS:
                    try: merged.extend(fetch_by_tag(tag))
                    except: pass

                # B) explicit route|stop
                try: merged.extend(fetch_multi(ROUTE_PAIRS))
                except: pass

                # sort + dedupe
                merged.sort(key=lambda t: t[0])
                seen, deduped = set(), []
                for m, r, s in merged:
                    key = (m, r, s)
                    if key not in seen:
                        seen.add(key)
                        deduped.append((m, r, s))
                merged = deduped[:8]

                if not merged:
                    draw("Kennedy-bound", "No predictions")
                    led_off()
                else:
                    # LED alert: any bus within ALERT_MINUTES?
                    if any(m <= ALERT_MINUTES for m, _, _ in merged):
                        led_on()
                    else:
                        led_off()

            if merged:
                top = merged[:TOP_N]
                m, route, stop = top[idx % len(top)]
                draw(f"{m:>2}m {shorten(route)}", f"[{stop}] {idx%len(top)+1}/{len(top)}")
                idx = (idx + 1) % len(top)

            time.sleep(ROTATE_SECS)

        except KeyboardInterrupt:
            break
        except Exception as e:
            draw("Error", str(e)[:16])
            led_off()
            time.sleep(2)

    led_off()
    lcd.clear()

if __name__ == "__main__":
    main()



