import os, json, time, threading, requests
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from twilio.rest import Client

# Cached anonymous JWT for Ontopo API (refreshed on expiry)
_ONTOPO_JWT = {"token": None, "expires_at": 0}

def _get_ontopo_jwt():
    """Return a valid anonymous JWT for Ontopo, refreshing when needed."""
    if _ONTOPO_JWT["token"] and time.time() < _ONTOPO_JWT["expires_at"] - 30:
        return _ONTOPO_JWT["token"]
    r = requests.post(
        "https://ontopo.com/api/loginAnonymously",
        json={},
        headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"},
        timeout=10,
    )
    data = r.json()
    _ONTOPO_JWT["token"] = data["jwt_token"]
    # JWT exp is ~15 min; keep for 14 min to be safe
    _ONTOPO_JWT["expires_at"] = time.time() + 14 * 60
    return _ONTOPO_JWT["token"]

app = Flask(__name__)
DATA_FILE = "restaurants.json"

# --- Twilio WhatsApp config (filled from .env) ---
TWILIO_SID    = os.environ.get("TWILIO_SID", "")
TWILIO_TOKEN  = os.environ.get("TWILIO_TOKEN", "")
TWILIO_FROM   = os.environ.get("TWILIO_FROM", "whatsapp:+14155238886")
WHATSAPP_TO   = os.environ.get("WHATSAPP_TO", "whatsapp:+972507557559")

# ---------- Data helpers ----------
def load():
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE) as f:
        return json.load(f)

def save(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ---------- Logging helper ----------
def log_check(name, date, time_str, guests, available, detail=""):
    ts = datetime.now().strftime("%H:%M:%S")
    status = "✅ יש מקום" if available else "❌ אין מקום"
    extra = f" | {detail}" if detail else ""
    print(f"[{ts}] {name} | {date} {time_str} | {guests} אנשים | {status}{extra}")

# ---------- Availability checkers ----------
def check_ontopo(restaurant):
    """Check availability on Ontopo via their internal availability API.

    Flow (reverse-engineered from ontopo.com frontend):
      1. POST /api/loginAnonymously  → jwt_token (cached 14 min)
      2. POST /api/availability_search with header token:<jwt> and body:
           {slug, locale, criteria:{size (str), date (YYYY-MM-DD), time (HHMM)}}
      3. Response contains `page` only when availability exists:
           - page.areas[].options[].method == "seat"  → real table available
           - page.fallback.method in standby/callback → waiting-list available
           - no `page` field at all                   → nothing available
    """
    name     = restaurant.get("name", "?")
    slug     = restaurant.get("slug", "")
    guests   = str(restaurant.get("guests", 2))
    date_str = restaurant.get("next_date", datetime.now().strftime("%Y-%m-%d"))
    time_str = restaurant.get("time", "20:00").replace(":", "")
    locale   = restaurant.get("locale", "he")

    try:
        jwt = _get_ontopo_jwt()

        payload = {
            "slug": slug,
            "locale": locale,
            "criteria": {
                "size": guests,
                "date": date_str,
                "time": time_str,
            },
        }
        r = requests.post(
            "https://ontopo.com/api/availability_search",
            json=payload,
            headers={
                "token": jwt,
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
            timeout=15,
        )
        data = r.json()
        page = data.get("page")

        if not page:
            log_check(name, date_str, time_str, guests, False, "אין תגובת page מה-API")
            return False, []

        # Fallback path: waiting-list / phone only
        if page.get("fallback"):
            method = page["fallback"].get("method", "")
            if method in ("standby", "callback"):
                log_check(name, date_str, time_str, guests, True, f"רשימת המתנה ({method})")
                return True, [page["fallback"]]
            log_check(name, date_str, time_str, guests, False, f"רק fallback: {method}")
            return False, []

        # Normal path: areas with seat options
        areas = page.get("areas", [])
        seat_options = []
        for area in areas:
            for opt in area.get("options", []):
                if opt.get("method") == "seat":
                    seat_options.append({
                        "area": area.get("name", ""),
                        "time": opt.get("time", time_str),
                        "method": "seat",
                    })

        available = len(seat_options) > 0
        if available:
            areas_summary = ", ".join(
                f"{o['area']} {o['time']}" for o in seat_options[:3]
            )
            log_check(name, date_str, time_str, guests, True, f"{len(seat_options)} אופציות: {areas_summary}")
        else:
            log_check(name, date_str, time_str, guests, False, "page קיים אך אין אופציות seat")

        return available, seat_options

    except Exception as e:
        log_check(name, date_str, time_str, guests, False, f"שגיאה: {e}")
        return False, []


def check_tabit(restaurant):
    """Check availability on Tabit."""
    name     = restaurant.get("name", "?")
    org_id   = restaurant.get("slug", "")
    guests   = restaurant.get("guests", 2)
    date_str = restaurant.get("next_date", datetime.now().strftime("%Y-%m-%d"))
    time_str = restaurant.get("time", "20:00")

    try:
        r = requests.post(
            "https://app.tabit.cloud/api/reservation/availability",
            json={"orgId": org_id, "date": date_str, "time": time_str, "partySize": guests},
            headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"},
            timeout=10,
        )
        available = r.json().get("available", False)
        log_check(name, date_str, time_str, guests, available)
        return available, []
    except Exception as e:
        log_check(name, date_str, time_str, guests, False, f"שגיאה: {e}")
        return False, []

# ---------- WhatsApp sender ----------
def send_whatsapp(restaurant, booking_url, slots=None):
    try:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        name   = restaurant.get("name", "")
        date   = restaurant.get("next_date", "")
        time_r = restaurant.get("time", "")
        guests = restaurant.get("guests", 2)

        # Build seating areas block if slot details were provided
        if slots:
            # Group by area name, collect unique times per area
            areas: dict[str, list[str]] = {}
            for s in slots:
                area = s.get("area") or "כללי"
                t = s.get("time", "")
                # Convert HHMM → HH:MM for readability
                if len(t) == 4 and t.isdigit():
                    t = f"{t[:2]}:{t[2:]}"
                areas.setdefault(area, [])
                if t and t not in areas[area]:
                    areas[area].append(t)
            areas_lines = "\n".join(
                f"  • {area}: {', '.join(times) if times else ''}"
                for area, times in areas.items()
            )
            seats_block = f"\n\n🪑 *אזורי ישיבה זמינים:*\n{areas_lines}"
        else:
            seats_block = ""

        msg = (
            f"🍽️ *התפנה מקום!*\n\n"
            f"*{name}*\n"
            f"📅 {date} בשעה {time_r}\n"
            f"👥 {guests} סועדים"
            f"{seats_block}\n\n"
            f"לחץ להזמנה:\n{booking_url}"
        )
        client.messages.create(body=msg, from_=TWILIO_FROM, to=WHATSAPP_TO)
        print(f"[WhatsApp] נשלח עבור {name}")
    except Exception as e:
        print(f"[WhatsApp] שגיאה: {e}")

def get_booking_url(restaurant):
    platform = restaurant.get("platform", "ontopo")
    slug     = restaurant.get("slug", "")
    date     = restaurant.get("next_date", "")
    time_r   = restaurant.get("time", "20:00").replace(":", "")
    guests   = restaurant.get("guests", 2)

    if platform == "ontopo":
        # slug is the numeric page ID (e.g. "69127207")
        # URL format: https://ontopo.com/he/il/page/<id>?date=...&time=HHMM&partySize=N
        page_id = slug.removeprefix("page/")
        return (
            f"https://ontopo.com/he/il/page/{page_id}"
            f"?date={date}&time={time_r}&partySize={guests}"
        )
    else:
        return (
            f"https://app.tabit.cloud/site/{slug}"
            f"?date={date}&time={time_r}&partySize={guests}"
        )

# ---------- Monitor loop ----------
NOTIFIED = set()  # avoid duplicate alerts per (restaurant, date)

def get_next_dates(restaurant):
    """Return upcoming dates (up to 14 days) matching the restaurant's day preferences."""
    from datetime import timedelta
    day_map = {"א": 6, "ב": 0, "ג": 1, "ד": 2, "ה": 3, "ו": 4, "ש": 5}
    wanted = [day_map[d] for d in restaurant.get("days", []) if d in day_map]
    today = datetime.now().date()
    return [
        (today + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(14)
        if (today + timedelta(days=i)).weekday() in wanted
    ]

def monitor_loop():
    while True:
        restaurants = load()
        for r in restaurants:
            if not r.get("active", True):
                continue
            for date in get_next_dates(r):
                r["next_date"] = date
                key = f"{r['id']}_{date}"
                platform = r.get("platform", "ontopo")
                available, slots = check_ontopo(r) if platform == "ontopo" else check_tabit(r)
                if available and key not in NOTIFIED:
                    booking_url = get_booking_url(r)
                    send_whatsapp(r, booking_url, slots)
                    NOTIFIED.add(key)
                    r["last_alert"] = datetime.now().isoformat()
                    save(restaurants)
        time.sleep(300)  # check every 5 minutes

# ---------- Routes ----------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/restaurants", methods=["GET"])
def get_restaurants():
    return jsonify(load())

@app.route("/api/restaurants", methods=["POST"])
def add_restaurant():
    data = load()
    r = request.json
    r["id"] = int(time.time())
    r["active"] = True
    r["last_alert"] = None
    data.append(r)
    save(data)
    return jsonify(r)

@app.route("/api/restaurants/<int:rid>", methods=["DELETE"])
def delete_restaurant(rid):
    data = [r for r in load() if r["id"] != rid]
    save(data)
    return jsonify({"ok": True})

@app.route("/api/restaurants/<int:rid>/toggle", methods=["POST"])
def toggle_restaurant(rid):
    data = load()
    for r in data:
        if r["id"] == rid:
            r["active"] = not r.get("active", True)
    save(data)
    return jsonify({"ok": True})

# Start monitor thread on import so gunicorn workers also run it.
# Guard against double-start when Flask reloader forks a child process.
if not os.environ.get("WERKZEUG_RUN_MAIN"):
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
