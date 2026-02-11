# app.py — YiThume Flask API (MongoDB-only; no local storage)
from urllib.parse import quote
import os
import re
import io
import uuid
import hashlib
from datetime import datetime, timedelta
from math import radians, cos, sin, asin, sqrt
from flask import Flask, request, jsonify, send_file, abort, send_from_directory, render_template, redirect, url_for
from flask_cors import CORS
from pymongo import MongoClient, ASCENDING, DESCENDING, errors as mongo_errors
from bson.objectid import ObjectId
from gridfs import GridFS
from werkzeug.utils import secure_filename

# -------------------------------------------------
# ENV + MONGO
# -------------------------------------------------
MONGO_URI = os.environ.get(
    "MONGO_URI",
    "mongodb+srv://username:password@cluster0.mongodb.net/yithume?retryWrites=true&w=majority"
)
DB_NAME = os.environ.get("MONGO_DB", "yithume")

# Admin secret for privileged endpoints (used by admin panel)
# Default PIN set to 1234 for demo; override via ADMIN_SECRET env in real deployments.
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "1234")
# Accept admin PIN via body/query too (so the web admin panel can pass it)
ALLOW_PIN_PARAM = os.environ.get("ALLOW_PIN_PARAM", "true").lower() == "true"
# Expose debug PINs for driver login (off in prod)
PIN_DEBUG_EXPOSE = os.environ.get("PIN_DEBUG_EXPOSE", "false").lower() == "true"

# ---------------- USSD / SECURITY CONFIG ----------------
USSD_ENABLE = os.environ.get("USSD_ENABLE", "true").lower() == "true"

# If you use Africa's Talking or Infobip, set their webhook source IPs (comma-separated)
WEBHOOK_IP_ALLOWLIST = set(
    filter(None, [i.strip() for i in os.environ.get("WEBHOOK_IP_ALLOWLIST", "").split(",")])
)

# Optional HMAC secret for providers that send signatures (some do for payments/voice; USSD often doesn't)
WEBHOOK_HMAC_SECRET = os.environ.get("WEBHOOK_HMAC_SECRET", "").strip()

# Basic rate limits (tune as needed)
RATE_LIMIT_PER_PHONE_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_PHONE_PER_MIN", "12"))   # 12 requests/min
RATE_LIMIT_PER_IP_PER_MIN    = int(os.environ.get("RATE_LIMIT_PER_IP_PER_MIN", "60"))      # 60 requests/min

# Idempotency TTL for POST writes (seconds)
IDEMPOTENCY_TTL_SEC = int(os.environ.get("IDEMPOTENCY_TTL_SEC", "3600"))

# Optional: Your shared USSD code label for logs
USSD_SERVICE_LABEL = os.environ.get("USSD_SERVICE_LABEL", "YiThume-USSD")

mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)


def get_db():
    mongo_client.admin.command("ping")
    return mongo_client[DB_NAME]


def get_fs(db=None) -> GridFS:
    db = db or get_db()
    return GridFS(db)


# -------------------------------------------------
# CONFIG / CONSTANTS
# -------------------------------------------------
ITEM_MARGIN_RATE = 0.12
PLATFORM_FEE_RATE = 0.10
BATCH_BONUS_PER_EXTRA = 0.25
BATCH_BONUS_CAP = 0.60
CLUSTER_WINDOW_MIN = 120
AUTO_ASSIGN_RADIUS_KM = 12

# loose SA-ish box; tweak as needed
SERVICE_BBOX = {"min_lat": -35.5, "max_lat": -22.0, "min_lng": 16.0, "max_lng": 33.5}

DRIVER_TOKEN_TTL_MIN = 7 * 24 * 60  # 7 days
DRIVER_PIN_TTL_MIN = 10             # 10 minutes

# Build info (so /health shows when this file was last baked)
BUILD_TS = datetime.utcnow().isoformat() + "Z"

# -------------------------------------------------
# FLASK
# -------------------------------------------------
app = Flask(__name__)
CORS(app)

# -----------------------------
# BYPASS PATCH (serverless-safe)
# Prevent NameError from legacy manager snippet that calls:
#   _ensure_indexes_for_manager(db)
# -----------------------------
db = None  # <-- ensures "db" name exists at import time

def _ensure_indexes_for_manager(_db):
    """
    Legacy compatibility: older snippet calls _ensure_indexes_for_manager(db).
    On Vercel/serverless we avoid doing DB work at import time.
    If a db is passed and it's usable, we can try to create indexes safely.
    """
    try:
        if _db is None:
            return
        # If you defined ensure_manager_indexes() elsewhere, call it.
        if "ensure_manager_indexes" in globals():
            ensure_manager_indexes(_db)
            return
        # Otherwise: do nothing (safe no-op)
        return
    except Exception:
        return
# -------------------------------------------------
# HELPERS
# -------------------------------------------------
def _now_dt():
    return datetime.utcnow()


def _now_iso():
    return _now_dt().isoformat() + "Z"


def make_order_public_id():
    ts = datetime.utcnow().strftime("%Y%m%d")
    return f"YI-{ts}-{str(uuid.uuid4())[:6].upper()}"


def safe_doc(doc):
    if not doc:
        return None
    out = dict(doc)
    out.pop("_id", None)
    # datetime -> iso
    for k in ("created_at", "assigned_at", "delivered_at", "pin_expiry"):
        if isinstance(out.get(k), datetime):
            out[k] = out[k].isoformat() + "Z"
    loc = out.get("current_location")
    if isinstance(loc, dict) and isinstance(loc.get("updated_at"), datetime):
        loc["updated_at"] = loc["updated_at"].isoformat() + "Z"
    # redact auth
    if "auth" in out and isinstance(out["auth"], dict):
        red = {}
        for k, v in out["auth"].items():
            if k == "pin_hash":
                continue
            if k == "sessions" and isinstance(v, list):
                red["sessions"] = [
                    {
                        "expires_at": (
                            s.get("expires_at").isoformat() + "Z"
                        ) if isinstance(s.get("expires_at"), datetime) else s.get("expires_at")
                    }
                    for s in v
                ]
            else:
                red[k] = v
        out["auth"] = red
    return out


def phone_ok(p):
    return bool(re.fullmatch(r"\d{10,15}", str(p or "").strip()))


def inside_service_area(lat, lng):
    if lat is None or lng is None:
        return True
    bb = SERVICE_BBOX
    return (bb["min_lat"] <= lat <= bb["max_lat"] and bb["min_lng"] <= lng <= bb["max_lng"])


def haversine_km(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    r = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(a))


def ensure_indexes(db):
    db.orders.create_index([("created_at", DESCENDING)])
    db.orders.create_index([("_internal_id", ASCENDING)], unique=True)
    db.orders.create_index([("customer.phone", ASCENDING), ("created_at", DESCENDING)])
    db.orders.create_index([("status", ASCENDING), ("created_at", DESCENDING)])
    db.orders.create_index([("cluster_key", ASCENDING)])
    db.orders.create_index([("assigned_driver_id", ASCENDING), ("delivered_at", DESCENDING)])
    db.orders.create_index([("order_id", ASCENDING)], unique=True)

    db.drivers.create_index([("_internal_id", ASCENDING)], unique=True)
    db.drivers.create_index([("active", ASCENDING), ("available", ASCENDING), ("meta.zone", ASCENDING)])
    db.drivers.create_index([("current_location.lat", ASCENDING), ("current_location.lng", ASCENDING)])
    db.drivers.create_index([("phone", ASCENDING)], unique=False)
    db.drivers.create_index([("auth.sessions.token", ASCENDING)], sparse=True)

    db.zone_demand.create_index([("zone", ASCENDING), ("ts", DESCENDING)])
    db.payouts.create_index([("driver_id", ASCENDING), ("created_at", DESCENDING)])
    db.stores.create_index([("_internal_id", ASCENDING)], unique=True)
    db.store_items.create_index([("store_id", ASCENDING)])
    db.whatsapp_log.create_index([("created_at", DESCENDING)])

    # --- NEW: anti-fraud / infra
    db.rate_limiter.create_index([("key", ASCENDING)], unique=True)
    db.rate_limiter.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
    db.idempotency.create_index([("key", ASCENDING)], unique=True)
    db.idempotency.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)

    # --- NEW: catalog
    db.catalog.create_index([("active", ASCENDING), ("name", ASCENDING)])
    db.catalog.create_index([("category", ASCENDING), ("active", ASCENDING)])


# --------- CATALOG SEEDER HELPERS (inline) ----------
AUTO_SEED_CATALOG_ON_START = os.environ.get("AUTO_SEED_CATALOG_ON_START", "false").lower() == "true"


def _catalog_seed_payload():
    # Minimal sensible OTC mix for USSD menus
    return [
        # Pain Relief
        {"name": "Panado Tablets 500mg (24)", "category": "Pain Relief", "price": 34.99, "sku": "PAN500-24"},
        {"name": "Grand-Pa Powders (6)", "category": "Pain Relief", "price": 39.99, "sku": "GPA-P6"},
        {"name": "Nurofen 200mg (24)", "category": "Pain Relief", "price": 79.99, "sku": "NUR-200-24"},
        {"name": "Voltarin Gel 50g", "category": "Pain Relief", "price": 109.99, "sku": "VLT-G50"},
        # Cold & Flu
        {"name": "Med-Lemon (6)", "category": "Cold & Flu", "price": 39.99, "sku": "MDL-6"},
        {"name": "ACC 200 Effervescent (10)", "category": "Cold & Flu", "price": 89.99, "sku": "ACC-200-10"},
        {"name": "Strepsils Honey & Lemon (16)", "category": "Cold & Flu", "price": 54.99, "sku": "STR-HL-16"},
        {"name": "Sinutab (10)", "category": "Cold & Flu", "price": 94.99, "sku": "SNT-10"},
        # Digestive
        {"name": "Gaviscon Liquid 150ml", "category": "Digestive", "price": 84.99, "sku": "GAV-L150"},
        {"name": "Buscopan (20)", "category": "Digestive", "price": 79.99, "sku": "BUS-20"},
        {"name": "Imodium (6)", "category": "Digestive", "price": 64.99, "sku": "IMO-6"},
        {"name": "Eno Sachets (6)", "category": "Digestive", "price": 29.99, "sku": "ENO-6"},
        # Vitamins
        {"name": "Vitamin C 1000mg (30)", "category": "Vitamins", "price": 79.99, "sku": "VTC1000-30"},
        {"name": "Multivitamin Adult (30)", "category": "Vitamins", "price": 99.99, "sku": "MVA-30"},
        {"name": "Zinc 15mg (30)", "category": "Vitamins", "price": 69.99, "sku": "ZN15-30"},
        # Hygiene
        {"name": "Lifebuoy Soap 175g", "category": "Hygiene", "price": 16.99, "sku": "LFB-175"},
        {"name": "Colgate 100ml", "category": "Hygiene", "price": 24.99, "sku": "COL-100"},
        {"name": "Always Pads (8)", "category": "Hygiene", "price": 29.99, "sku": "ALW-8"},
        {"name": "Dove Roll-On 50ml", "category": "Hygiene", "price": 29.99, "sku": "DOV-RO50"},
        # Baby Care
        {"name": "Pampers Size 3 (21)", "category": "Baby Care", "price": 129.99, "sku": "PMP-S3-21"},
        {"name": "Baby Wipes (80)", "category": "Baby Care", "price": 34.99, "sku": "BWP-80"},
        {"name": "Barrier Cream 100g", "category": "Baby Care", "price": 39.99, "sku": "BAR-100"},
        {"name": "Panado Syrup 100ml", "category": "Baby Care", "price": 39.99, "sku": "PAN-SYR-100"},
        # Women’s Health
        {"name": "Canesten Cream 20g", "category": "Women’s Health", "price": 119.99, "sku": "CAN-20"},
        {"name": "UTI Test Strips (3)", "category": "Women’s Health", "price": 59.99, "sku": "UTI-3"},
        {"name": "Ibusor 400mg (20)", "category": "Women’s Health", "price": 84.99, "sku": "IBU400-20"},
    ]


def upsert_catalog_items(db, items):
    inserted = 0
    updated = 0
    for it in items:
        base = {"name": it["name"], "category": it["category"]}
        existing = db.catalog.find_one(base)
        if existing:
            updates = {
                "price": float(it["price"]),
                "sku": it.get("sku"),
                "active": True
            }
            db.catalog.update_one({"_id": existing["_id"]}, {"$set": updates})
            updated += 1
        else:
            doc = {
                "_internal_id": str(uuid.uuid4()),
                "name": it["name"],
                "category": it["category"],
                "price": float(it["price"]),
                "sku": it.get("sku"),
                "active": True,
                "created_at": _now_dt()
            }
            db.catalog.insert_one(doc)
            inserted += 1
    return inserted, updated


def wa_order_text(order):
    items_list = ", ".join([f"{i.get('name')} x{i.get('qty')}" for i in order.get("items", [])])
    addr = order.get("customer", {}).get("address", {})
    eta = order.get("route", {}).get("eta_text", "TBC")
    total = order.get("total", 0)
    collection = order.get("meta", {}).get("collection_name", "")
    pay_m = order.get("payment", {}).get("method", "card")
    lines = [
        "YiThume Order Confirmation",
        f"Order ID: {order.get('order_id')}",
        f"Items: {items_list}",
        f"Total: R{total}",
        f"Pickup: {collection}",
        f"Address: {addr.get('line1', '')}",
        "Status: Awaiting driver pickup.",
        f"Payment: {pay_m}",
        f"ETA: {eta}"
    ]
    return "\n".join(lines)


def rule_based_fraud_score(db, order_doc):
    score = 0.0
    flags = {}

    phone = (order_doc.get("customer") or {}).get("phone")
    if not phone_ok(phone):
        flags["bad_phone"] = True
        score += 0.2

    recent_count = db.orders.count_documents({
        "customer.phone": phone,
        "created_at": {"$gte": _now_dt() - timedelta(minutes=60)}
    }) if phone else 0
    if recent_count >= 3:
        flags["phone_velocity"] = True
        score += 0.4

    if phone:
        dup = db.orders.find_one({
            "customer.phone": phone,
            "subtotal": order_doc.get("subtotal", 0),
            "created_at": {"$gte": _now_dt() - timedelta(minutes=10)}
        })
        if dup:
            flags["duplicate_like"] = True
            score += 0.3

    coords = (((order_doc.get("customer") or {}).get("address") or {}).get("coords") or {})
    if not inside_service_area(coords.get("lat"), coords.get("lng")):
        flags["out_of_area"] = True
        score += 0.5

    pipeline = [{"$group": {"_id": None, "avg": {"$avg": "$total"}}}]
    agg = list(db.orders.aggregate(pipeline))
    avg_total = agg[0]["avg"] if agg else 50
    if order_doc.get("total", 0) > avg_total * 3:
        flags["high_value"] = True
        score += 0.2

    return min(score, 1.0), flags


def find_available_driver(db, zone, drop_lat=None, drop_lng=None):
    q = {"active": True, "available": True}
    if zone:
        q["meta.zone"] = zone
    candidates = list(db.drivers.find(q))
    if not candidates:
        return None

    if drop_lat is None or drop_lng is None:
        return candidates[0]

    best = None
    best_d = 1e9
    for d in candidates:
        loc = (d.get("current_location") or {})
        km = haversine_km(drop_lat, drop_lng, loc.get("lat"), loc.get("lng"))
        if km is None:
            continue
        if km <= AUTO_ASSIGN_RADIUS_KM and km < best_d:
            best = d
            best_d = km
    return best or candidates[0]


def cluster_key(order_doc):
    addr = ((order_doc.get("customer") or {}).get("address") or {})
    zone = (order_doc.get("meta") or {}).get("zone", "")
    line1 = (addr.get("line1") or "").strip().lower()
    coarse = re.split(r"[,\s]+", line1)[0] if line1 else "unknown"
    now = _now_dt()
    block_hours = (now.hour // (CLUSTER_WINDOW_MIN // 60)) * (CLUSTER_WINDOW_MIN // 60)
    window_start = now.replace(hour=block_hours, minute=0, second=0, microsecond=0)
    bucket_str = window_start.strftime("%Y%m%d%H%M")
    return f"{zone}:{coarse}:{bucket_str}"


def compute_earnings(order_doc, prior_in_cluster=0):
    fee = float(order_doc.get("delivery_fee", 0))
    platform_cut = fee * PLATFORM_FEE_RATE
    driver_cut = fee - platform_cut

    if prior_in_cluster > 0:
        bonus_pct = min(prior_in_cluster * BATCH_BONUS_PER_EXTRA, BATCH_BONUS_CAP)
        bonus_amt = bonus_pct * fee
        driver_cut += bonus_amt
        platform_cut = max(0.0, fee - driver_cut)

    items = order_doc.get("items", [])
    margin = 0.0
    for it in items:
        price = float(it.get("price", 0))
        qty = int(it.get("qty", 1))
        cost = it.get("cost")
        if cost is not None:
            margin += max(0.0, price - float(cost)) * qty
        else:
            margin += (price * ITEM_MARGIN_RATE) * qty

    platform_total = platform_cut + margin
    return round(driver_cut, 2), round(platform_total, 2)


def accrue_driver_earning(db, driver_internal_id, amount, reason, order_id):
    db.drivers.update_one(
        {"_internal_id": driver_internal_id},
        {
            "$inc": {"weekly_payout_due": amount},
            "$push": {"earnings_history": {
                "amount": amount,
                "reason": reason,
                "order_id": order_id,
                "ts": _now_dt()
            }}
        }
    )


def log_zone_demand(db, zone, coords, phone):
    db.zone_demand.insert_one({
        "zone": zone,
        "ts": _now_dt(),
        "phone": phone,
        "coords": coords
    })


def recent_zone_demand_snapshot(db):
    since = _now_dt() - timedelta(hours=24)
    pipe = [
        {"$match": {"ts": {"$gte": since}}},
        {"$group": {"_id": "$zone", "count": {"$sum": 1}}},
    ]
    out = {}
    for row in db.zone_demand.aggregate(pipe):
        z = row["_id"] or "?"
        out[z] = {"misses": row["count"]}
    return out


def hash_pin(pin: str) -> str:
    return hashlib.sha256(str(pin).encode("utf-8")).hexdigest()


def _pin_or_header_ok():
    """
    Admin auth helper.

    Priority:
      1) X-Admin-Secret header == ADMIN_SECRET
      2) admin_pin in query string or JSON body (if ALLOW_PIN_PARAM is true)

    Uses request.get_json(silent=True) so we NEVER trigger a Flask 400 just by
    sending an empty / invalid JSON body from JS.
    """
    # 1) Header takes precedence
    if request.headers.get("X-Admin-Secret") == ADMIN_SECRET:
        return True

    # 2) Optional PIN via query/body
    if not ALLOW_PIN_PARAM:
        return False

    data = {}
    try:
        data = request.get_json(silent=True) or {}
    except Exception:
        data = {}

    pin = request.args.get("admin_pin") or data.get("admin_pin")
    return bool(pin) and (pin == ADMIN_SECRET)

def require_admin():
    return _pin_or_header_ok()


# -------- NEW: Security helpers (IP/HMAC, rate limit, idempotency) --------
def client_ip():
    # Works behind most proxies
    return (request.headers.get("X-Forwarded-For", request.remote_addr or "")).split(",")[0].strip()


def ip_allowed():
    if not WEBHOOK_IP_ALLOWLIST:
        return True
    return client_ip() in WEBHOOK_IP_ALLOWLIST


def verify_hmac_signature(raw_body: bytes) -> bool:
    if not WEBHOOK_HMAC_SECRET:
        return True
    sig = request.headers.get("X-Signature") or request.headers.get("X-AT-Signature") or ""
    try:
        import hmac as _hmac, hashlib as _hashlib
        mac = _hmac.new(WEBHOOK_HMAC_SECRET.encode("utf-8"), raw_body, _hashlib.sha256).hexdigest()
        return _hmac.compare_digest(mac, sig)
    except Exception:
        return False


def rate_limit_touch(db, key: str, limit_per_min: int):
    # rolling 60s window
    now = _now_dt()
    rec = db.rate_limiter.find_one({"key": key})
    if not rec:
        db.rate_limiter.insert_one({
            "key": key,
            "count": 1,
            "window_start": now,
            "expires_at": now + timedelta(minutes=1)
        })
        return True
    # if window expired, reset
    if rec.get("window_start") and rec["window_start"] < now - timedelta(minutes=1):
        db.rate_limiter.update_one(
            {"key": key},
            {"$set": {
                "count": 1,
                "window_start": now,
                "expires_at": now + timedelta(minutes=1)
            }}
        )
        return True
    # otherwise increment
    new_count = int(rec.get("count", 0)) + 1
    if new_count > limit_per_min:
        return False
    db.rate_limiter.update_one(
        {"key": key},
        {"$set": {
            "count": new_count,
            "expires_at": now + timedelta(minutes=1)
        }}
    )
    return True


def idempotency_guard(db):
    """
    Prevents duplicate writes when the client retries.
    Client should send: X-Idempotency-Key (UUID).
    If absent, we generate one per-request (less protection but avoids 400s).
    """
    key = request.headers.get("X-Idempotency-Key") or str(uuid.uuid4())
    now = _now_dt()
    try:
        db.idempotency.insert_one({
            "key": key,
            "seen": True,
            "created_at": now,
            "expires_at": now + timedelta(seconds=IDEMPOTENCY_TTL_SEC)
        })
        return key, False  # inserted now, not a replay
    except mongo_errors.DuplicateKeyError:
        return key, True   # replay


# -------- DASHBOARD STATS HELPER (for JS) -------------
def compute_stats_overview(db, days: int = 90):
    since = _now_dt() - timedelta(days=max(1, days))

    orders = list(db.orders.find({"created_at": {"$gte": since}}))
    total_orders = len(orders)
    revenue = sum(float(o.get("total", 0)) for o in orders)

    # top products
    prod = {}
    for o in orders:
        for it in (o.get("items") or []):
            name = it.get("name")
            if not name:
                continue
            qty = int(it.get("qty", 1))
            prod[name] = prod.get(name, 0) + qty
    top_products = sorted(prod.items(), key=lambda x: x[1], reverse=True)[:5]

    # top areas by collection_name
    areas = {}
    for o in orders:
        k = (o.get("meta") or {}).get("collection_name") or "—"
        areas[k] = areas.get(k, 0) + 1
    top_areas = sorted(areas.items(), key=lambda x: x[1], reverse=True)[:5]

    # driver aggregates
    active_drivers = db.drivers.count_documents({"active": True})
    total_drivers = db.drivers.estimated_document_count()

    # delivered per driver
    pipeline = [
        {"$match": {"status": "delivered", "assigned_driver_id": {"$ne": None}}},
        {"$group": {
            "_id": "$assigned_driver_id",
            "deliveries": {"$sum": 1},
            "total_driver_pay": {"$sum": {"$ifNull": ["$settlement.driver", 0]}}
        }},
        {"$sort": {"deliveries": -1}}
    ]
    per_driver = list(db.orders.aggregate(pipeline))

    return {
        "total_orders": total_orders,
        "revenue": round(revenue, 2),
        "top_products": [{"name": k, "count": v} for k, v in top_products],
        "top_areas": [{"name": k, "count": v} for k, v in top_areas],
        "drivers_active": active_drivers,
        "drivers_total": total_drivers,
        "drivers_ranked": [
            {
                "driver_internal_id": row["_id"],
                "deliveries": row["deliveries"],
                "total_driver_pay": round(row["total_driver_pay"], 2)
            }
            for row in per_driver
        ]
    }


# init indexes once per cold start (and optional auto-seed)
try:
    _db_boot = get_db()
    ensure_indexes(_db_boot)
    if AUTO_SEED_CATALOG_ON_START:
        if _db_boot.catalog.estimated_document_count() == 0:
            upsert_catalog_items(_db_boot, _catalog_seed_payload())
except Exception:
    pass

# -------------------------------------------------
# ROUTES
# -------------------------------------------------



@app.route("/", methods=["GET"])
def home():
    # Serves: api/static/index.html
    return send_from_directory(app.static_folder, "index.html")

# -----------------------------
# Manager Setup Wizard (proper onboarding)
# -----------------------------

@app.post("/manager/api/setup")
def manager_setup_submit():
    """
    Receives JSON from dashboard wizard, creates:
      workspace -> manager -> employees -> initial jobs
    Returns:
      { ok: true, workspace_id: "...", redirect_url: "/dashboard?ws=..." }
    """

    if not require_admin():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    db = manager_db()
    data = request.get_json(silent=True) or {}

    store_name = str(data.get("store_name") or "New Store").strip()
    template = str(data.get("template") or "general").strip()

    survey = {
        "channel_web": bool(data.get("channel_web", True)),
        "needs_support": bool(data.get("needs_support", True)),
        "needs_returns": bool(data.get("needs_returns", False)),
        "needs_delivery": bool(data.get("needs_delivery", True)),
        "delivery_mode": str(data.get("delivery_mode") or "manual").strip(),

        "currency": str(data.get("currency") or "ZAR").strip(),
        "theme": str(data.get("theme") or "clean").strip(),
        "products_mode": str(data.get("products_mode") or "catalog_seed").strip(),
        "autopilot_level": str(data.get("autopilot_level") or "assist").strip(),
        "plan": str(data.get("plan") or "starter").strip(),
    }

    ws = m_create_workspace(db, store_name, template=template)
    m_log_event(db, ws["_id"], "workspace.created", f"Workspace created: {ws['name']}")

    manager_create_manager(db, ws["_id"], survey)
    manager_create_employees(db, ws["_id"], survey)

    # Send user back to /dashboard with the workspace selected
    redirect_url = f"/dashboard?ws={quote(str(ws['_id']))}"

    return jsonify({"ok": True, "workspace_id": ws["_id"], "redirect_url": redirect_url})
@app.post("/manager/api/setup")
def manager_setup_submit():
    """
    Receives JSON from the wizard, creates:
      workspace -> manager -> employees -> initial jobs
    Returns: { ok: true, workspace_id, redirect_url }
    """
    # require_admin() reads header OR admin_pin in query OR JSON body
    if not require_admin():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    db = manager_db()

    data = request.get_json(silent=True) or {}

    store_name = (data.get("store_name") or "New Store").strip()
    template = (data.get("template") or "general").strip()

    survey = {
        "channel_web": bool(data.get("channel_web", True)),
        "needs_support": bool(data.get("needs_support", True)),
        "needs_returns": bool(data.get("needs_returns", False)),
        "needs_delivery": bool(data.get("needs_delivery", True)),
        "delivery_mode": (data.get("delivery_mode") or "manual").strip(),

        # optional fields captured by wizard (safe defaults)
        "currency": (data.get("currency") or "ZAR").strip(),
        "theme": (data.get("theme") or "clean").strip(),
        "products_mode": (data.get("products_mode") or "csv_or_shopify_or_whatsapp").strip(),
        "autopilot_level": (data.get("autopilot_level") or "assist").strip(),
        "plan": (data.get("plan") or "starter").strip(),
    }

    ws = m_create_workspace(db, store_name, template=template)
    m_log_event(db, ws["_id"], "workspace.created", f"Workspace created: {ws['name']}")

    manager_create_manager(db, ws["_id"], survey)
    manager_create_employees(db, ws["_id"], survey)

    admin_pin = request.args.get("admin_pin") or (data.get("admin_pin") if isinstance(data, dict) else "")
    redirect_url = f"/manager/dashboard/{ws['_id']}"
    if admin_pin:
        redirect_url += f"?admin_pin={admin_pin}"

    return jsonify({"ok": True, "workspace_id": ws["_id"], "redirect_url": redirect_url})
  
@app.get("/manager/api/workspaces")
def manager_list_workspaces():
    if not require_admin():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    db = manager_db()
    wss = list(db.workspaces.find({}).sort("created_at", -1).limit(50))

    out = []
    for w in wss:
        out.append({
            "_id": w.get("_id"),
            "name": w.get("name"),
            "template": w.get("template"),
            "created_at": (w.get("created_at").isoformat() + "Z") if isinstance(w.get("created_at"), datetime) else w.get("created_at"),
        })
    return jsonify({"ok": True, "workspaces": out})


@app.get("/manager/api/workspace/<workspace_id>/state")
def manager_workspace_state(workspace_id):
    if not require_admin():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    db = manager_db()

    ws = m_get_workspace(db, workspace_id)
    if not ws:
        return jsonify({"ok": False, "error": "Workspace not found"}), 404

    mgr = m_get_manager(db, workspace_id) or {}
    employees = m_get_employees(db, workspace_id)
    jobs = m_get_jobs(db, workspace_id, limit=50)
    events = m_get_events(db, workspace_id, limit=20)

    def fmt_dt(x):
        return (x.isoformat() + "Z") if isinstance(x, datetime) else x

    return jsonify({
        "ok": True,
        "workspace": {
            "_id": ws.get("_id"),
            "name": ws.get("name"),
            "template": ws.get("template"),
            "created_at": fmt_dt(ws.get("created_at"))
        },
        "manager": {
            "autopilot_level": mgr.get("autopilot_level", "assist"),
            "plan": mgr.get("plan", "starter"),
            "employees": mgr.get("employees", {})
        },
        "employees": [
            {
                "employee_type": e.get("employee_type"),
                "name": e.get("name"),
                "description": e.get("description"),
                "capabilities": e.get("capabilities", []),
                "enabled": bool(e.get("enabled", True)),
                "config": e.get("config", {})
            } for e in employees
        ],
        "jobs": [
            {
                "_id": j.get("_id"),
                "employee_type": j.get("employee_type"),
                "job_type": j.get("job_type"),
                "status": j.get("status"),
                "progress": int(j.get("progress", 0)),
                "created_at": fmt_dt(j.get("created_at")),
                "logs": j.get("logs", [])[-3:]
            } for j in jobs
        ],
        "events": [
            {
                "kind": ev.get("kind"),
                "msg": ev.get("msg"),
                "created_at": fmt_dt(ev.get("created_at"))
            } for ev in events
        ]
    })
  
@app.route("/health", methods=["GET"])     # <-- move health off "/"
@app.route("/api/app", methods=["GET"])
def health():
    try:
        db = get_db()
        return jsonify({
            "ok": True,
            "service": "YiThume (mongo)",
            "db": "up",
            "build_info": {"built_at": BUILD_TS},
            "now_utc": _now_iso(),
            "orders_count": db.orders.estimated_document_count(),
            "drivers_count": db.drivers.count_documents({"active": True}),
            "stores_count": db.stores.estimated_document_count()
        }), 200
    except Exception as e:
        return jsonify({
            "ok": True,
            "service": "YiThume (mongo)",
            "db": "down",
            "build_info": {"built_at": BUILD_TS},
            "error": str(e)
        }), 200


# ---------------- CREATE ORDER -------------------
@app.route("/orders", methods=["POST"])
@app.route("/api/app/orders", methods=["POST"])
def create_order():
    data = request.json or {}

    internal_id = str(uuid.uuid4())
    public_id = make_order_public_id()

    order_doc = {
        "_internal_id": internal_id,
        "order_id": public_id,
        "created_at": _now_dt(),
        "created_at_iso": _now_iso(),

        "customer": data.get("customer", {}),
        "items": data.get("items", []),

        "subtotal": float(data.get("subtotal", 0)),
        "delivery_fee": float(data.get("delivery_fee", 0)),
        "total": float(data.get("total", 0)),

        "payment": {
            "method": (data.get("payment") or {}).get("method", "card"),
            "status": "pending",
            "provider_ref": None,
            "fake_checkout_url": f"https://pay.yithume.example/checkout/{internal_id}"
        },

        "status": "pending",
        "assigned_driver_id": None,
        "assigned_at": None,
        "delivered_at": None,

        "route": data.get("route", {}),
        "created_by": data.get("created_by", "web"),
        "meta": data.get("meta", {}),

        "fraud_score": 0.0,
        "fraud_flags": {},
        "cluster_key": None,

        "delivery_photo_file_id": None,
        "delivery_photo_url": None,

        # payout tracking expected by admin UI
        "driver_pay_status": "pending",
        "driver_pay_pending": 0.0,
        "driver_pay_approved": 0.0,

        "settlement": {
            "driver": 0.0,
            "platform": 0.0,
            "settled": False
        }
    }

    try:
        db = get_db()
        zone = (order_doc["meta"] or {}).get("zone")
        coords = (((order_doc.get("customer") or {}).get("address") or {}).get("coords") or {})
        candidate_driver = find_available_driver(
            db, zone, coords.get("lat"), coords.get("lng")
        )

        if not candidate_driver:
            log_zone_demand(db, zone, coords, (order_doc.get("customer") or {}).get("phone"))
            return jsonify({
                "ok": False,
                "error": "no_driver_available",
                "zone": zone,
                "message": "No driver currently available in your area."
            }), 409

        fs, ff = rule_based_fraud_score(db, order_doc)
        order_doc["fraud_score"], order_doc["fraud_flags"] = fs, ff
        if fs >= 0.75:
            order_doc["status"] = "review_required"

        order_doc["cluster_key"] = cluster_key(order_doc)

        # pre-compute initial driver payout baseline (will be finalized on delivery)
        order_doc["driver_pay_pending"] = round(min(max(order_doc["delivery_fee"], 25), 45), 2)
        order_doc["driver_pay_status"] = "pending"

        db.orders.insert_one(order_doc)

        wa_msg = wa_order_text(order_doc)
        zd_snapshot = recent_zone_demand_snapshot(db)

        return jsonify({
            "ok": True,
            "order_db_id": internal_id,
            "order_public_id": public_id,
            "status": order_doc["status"],
            "fraud_score": fs,
            "fraud_flags": ff,
            "wa_message": wa_msg,
            "zone_demand_snapshot": zd_snapshot,
            "payment_portal_url": order_doc["payment"]["fake_checkout_url"]
        }), 201

    except mongo_errors.PyMongoError as e:
        return jsonify({"ok": False, "error": "db_write_failed", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500


# ---------------- LIST ORDERS (ADMIN/OP) -------
@app.route("/orders", methods=["GET"])
@app.route("/api/app/orders", methods=["GET"])
def list_orders():
    status = request.args.get("status")
    limit = max(1, min(int(request.args.get("limit", "100")), 500))
    q = {"status": status} if status else {}

    try:
        db = get_db()
        cur = db.orders.find(q).sort("created_at", DESCENDING).limit(limit)
        orders_out = [safe_doc(o) for o in cur]
        zd_snapshot = recent_zone_demand_snapshot(db)
        return jsonify({"ok": True, "orders": orders_out, "zone_demand_snapshot": zd_snapshot}), 200
    except mongo_errors.PyMongoError as e:
        return jsonify({"ok": False, "error": "db_read_failed", "details": str(e), "orders": []}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e), "orders": []}), 500


# ---------------- ADMIN STATS (for dashboard) ---
@app.route("/stats/overview", methods=["GET"])
@app.route("/api/app/stats/overview", methods=["GET"])
@app.route("/api/stats", methods=["GET"])  # alias for JS frontends
def stats_overview():
    try:
        db = get_db()
        days = int(request.args.get("days", "90"))
        stats = compute_stats_overview(db, days)
        return jsonify({"ok": True, **stats}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500


# ---------------- DASHBOARD COMBINED (stats + drivers) ---
@app.route("/dashboard", methods=["GET"])
@app.route("/api/app/dashboard", methods=["GET"])
@app.route("/api/dashboard", methods=["GET"])
def dashboard():
    """
    Combined endpoint intended for your JS admin dashboard.
    Returns stats plus active driver list and per-driver metrics.
    """
    try:
        db = get_db()
        days = int(request.args.get("days", "90"))
        stats = compute_stats_overview(db, days)

        # active drivers list
        drivers_cur = db.drivers.find({"active": True})
        drivers = [safe_doc(d) for d in drivers_cur]

        # attach simple aggregate metrics to each driver (deliveries + earnings)
        aggregates = {d["driver_internal_id"]: d for d in stats["drivers_ranked"]}
        # but drivers_ranked uses driver_internal_id = assigned_driver_id (_internal_id)
        agg_map = {row["driver_internal_id"]: row for row in stats["drivers_ranked"]}

        for d in drivers:
            did = d.get("_internal_id") or d.get("driver_id") or d.get("id")
            row = agg_map.get(did, {})
            d["deliveries"] = row.get("deliveries", 0)
            d["total_driver_pay"] = row.get("total_driver_pay", 0.0)

        return jsonify({
            "ok": True,
            "stats": stats,
            "drivers": drivers
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500


# ---------------- MARK PAID (ADMIN) ------------
@app.route("/orders/<oid>/mark-paid", methods=["POST"])
@app.route("/api/app/orders/<oid>/mark-paid", methods=["POST"])
def mark_paid(oid):
    if not require_admin():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    try:
        db = get_db()
        o = db.orders.find_one({"_internal_id": oid})
        if not o:
            return jsonify({"ok": False, "error": "order_not_found"}), 404
        payment = o.get("payment", {})
        payment["status"] = "paid"
        db.orders.update_one({"_internal_id": oid}, {"$set": {"payment": payment}})
        return jsonify({"ok": True}), 200
    except mongo_errors.PyMongoError as e:
        return jsonify({"ok": False, "error": "db_write_failed", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500


# ---------------- SIMULATE PAYMENT -------------
@app.route("/simulate_payment", methods=["POST"])
@app.route("/api/app/simulate_payment", methods=["POST"])
def simulate_payment():
    body = request.json or {}
    order_db_id = body.get("order_db_id")
    order_public_id = body.get("order_public_id")
    if not order_db_id and not order_public_id:
        return jsonify({"ok": False, "error": "order identifier required"}), 400

    try:
        db = get_db()
        q = {"_internal_id": order_db_id} if order_db_id else {"order_id": order_public_id}
        o = db.orders.find_one(q)
        if not o:
            return jsonify({"ok": False, "error": "order_not_found"}), 404

        o["payment"] = o.get("payment", {})
        o["payment"]["status"] = "paid"
        db.orders.update_one(q, {"$set": {"payment": o["payment"]}})

        msg = wa_order_text(o)
        db.whatsapp_log.insert_one({
            "direction": "outbound",
            "to": "CENTRAL_NUMBER",
            "order_id": o.get("order_id"),
            "body": msg,
            "created_at": _now_dt()
        })

        return jsonify({"ok": True, "status": "paid", "order_id": o.get("order_id")}), 200
    except mongo_errors.PyMongoError as e:
        return jsonify({"ok": False, "error": "db_write_failed", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500


# ---------------- AUTO-ASSIGN DRIVER ----------
@app.route("/orders/<oid>/auto-assign", methods=["POST"])
@app.route("/api/app/orders/<oid>/auto-assign", methods=["POST"])
def auto_assign(oid):
    """
    Auto-assign a driver to a single order.

    NOTE: Admin check removed so that the web admin panel can call this
    endpoint without needing X-Admin-Secret/admin_pin. If you want to
    re-lock this down later, re-introduce `require_admin()` here.
    """
    try:
        db = get_db()
        o = db.orders.find_one({"_internal_id": oid})
        if not o:
            return jsonify({"ok": False, "error": "order_not_found"}), 404

        zone = (o.get("meta") or {}).get("zone")
        coords = (((o.get("customer") or {}).get("address") or {}).get("coords") or {})
        d = find_available_driver(db, zone, coords.get("lat"), coords.get("lng"))
        if not d:
            return jsonify({"ok": False, "error": "no_driver_available"}), 409

        db.orders.update_one(
            {"_internal_id": oid},
            {"$set": {
                "assigned_driver_id": d["_internal_id"],
                "assigned_at": _now_dt(),
                "status": "assigned"
            }}
        )
        return jsonify({"ok": True, "driver_id": d["_internal_id"]}), 200
    except mongo_errors.PyMongoError as e:
        return jsonify({"ok": False, "error": "db_write_failed", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500

# ---------------- MANUAL ASSIGN DRIVER --------
@app.route("/orders/<oid>/assign", methods=["POST"])
@app.route("/api/app/orders/<oid>/assign", methods=["POST"])
def assign_driver(oid):
    if not require_admin():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    body = request.json or {}
    driver_id = body.get("driver_id")
    if not driver_id:
        return jsonify({"ok": False, "error": "driver_id required"}), 400
    try:
        db = get_db()
        if not db.orders.find_one({"_internal_id": oid}):
            return jsonify({"ok": False, "error": "order_not_found"}), 404
        if not db.drivers.find_one({"_internal_id": driver_id, "active": True}):
            return jsonify({"ok": False, "error": "driver_not_found"}), 404

        db.orders.update_one(
            {"_internal_id": oid},
            {"$set": {
                "assigned_driver_id": driver_id,
                "assigned_at": _now_dt(),
                "status": "assigned"
            }}
        )
        return jsonify({"ok": True}), 200
    except mongo_errors.PyMongoError as e:
        return jsonify({"ok": False, "error": "db_write_failed", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500


# ---------------- UPDATE ORDER STATUS ---------
@app.route("/orders/<oid>/status", methods=["POST"])
@app.route("/api/app/orders/<oid>/status", methods=["POST"])
def update_status(oid):
    body = request.json or {}
    new_status = body.get("status")
    allowed = {
        "pending", "assigned", "in_transit",
        "delivered", "cancelled", "failed", "review_required"
    }
    if new_status not in allowed:
        return jsonify({"ok": False, "error": "invalid status"}), 400

    try:
        db = get_db()
        o = db.orders.find_one({"_internal_id": oid})
        if not o:
            return jsonify({"ok": False, "error": "order_not_found"}), 404

        update_set = {"status": new_status}

        if new_status == "delivered":
            update_set["delivered_at"] = _now_dt()

            ck = o.get("cluster_key")
            since = _now_dt() - timedelta(minutes=CLUSTER_WINDOW_MIN)
            prior = db.orders.count_documents({
                "cluster_key": ck,
                "delivered_at": {"$gte": since},
                "assigned_driver_id": o.get("assigned_driver_id")
            })

            driver_cut, platform_cut = compute_earnings(
                o,
                prior_in_cluster=max(0, prior - 1)
            )
            update_set["settlement"] = {
                "driver": driver_cut,
                "platform": platform_cut,
                "settled": False
            }

            # Flip driver payout to approved for this order
            update_set["driver_pay_status"] = "approved"
            update_set["driver_pay_approved"] = max(
                float(o.get("driver_pay_approved") or 0.0),
                driver_cut
            )
            update_set["driver_pay_pending"] = 0.0

            if o.get("assigned_driver_id"):
                accrue_driver_earning(
                    db,
                    o["assigned_driver_id"],
                    driver_cut,
                    "delivery",
                    o.get("order_id")
                )

        db.orders.update_one({"_internal_id": oid}, {"$set": update_set})
        return jsonify({"ok": True}), 200

    except mongo_errors.PyMongoError as e:
        return jsonify({"ok": False, "error": "db_write_failed", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500


# ---------------- BULK AUTO-ASSIGN (ADMIN) ----
@app.route("/orders/auto-assign-all", methods=["POST"])
@app.route("/api/app/orders/auto-assign-all", methods=["POST"])
def auto_assign_all():
    """
    Bulk auto-assign all pending orders.

    NOTE: Admin check removed so that the web admin panel can call this
    endpoint without needing X-Admin-Secret/admin_pin. If you want to
    re-lock this down later, re-introduce `require_admin()` here.
    """
    try:
        db = get_db()
        pend = list(db.orders.find({"status": "pending"}).limit(500))
        results = []
        for o in pend:
            zone = (o.get("meta") or {}).get("zone")
            coords = (((o.get("customer") or {}).get("address") or {}).get("coords") or {})
            d = find_available_driver(db, zone, coords.get("lat"), coords.get("lng"))
            if not d:
                results.append({"id": o["_internal_id"], "ok": False, "reason": "no_driver"})
                continue
            db.orders.update_one(
                {"_internal_id": o["_internal_id"]},
                {"$set": {
                    "assigned_driver_id": d["_internal_id"],
                    "assigned_at": _now_dt(),
                    "status": "assigned"
                }}
            )
            results.append({
                "id": o["_internal_id"],
                "ok": True,
                "driver_id": d["_internal_id"]
            })
        return jsonify({"ok": True, "results": results}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500

# ---------------- DRIVERS ----------------------
@app.route("/drivers", methods=["POST"])
@app.route("/api/app/drivers", methods=["POST"])
def create_driver():
    data = request.json or {}
    internal_id = str(uuid.uuid4())
    doc = {
        "_internal_id": internal_id,
        "driver_id": data.get("driver_id") or f"DRV-{internal_id[:6].upper()}",
        "name": data.get("name"),
        "phone": data.get("phone"),
        "vehicle": data.get("vehicle", "car"),
        "active": True,
        "available": data.get("available", True),
        "current_location": {
            "lat": (data.get("current_location") or {}).get("lat"),
            "lng": (data.get("current_location") or {}).get("lng"),
            "updated_at": _now_dt()
        },
        "weekly_payout_due": 0.0,
        "earnings_history": [],
        "ratings": {"count": 0, "avg": None},
        "docs": {
            "id_doc_ref": None,
            "licence_ref": None,
            "vehicle_reg_ref": None
        },
        "auth": {
            "pin_hash": None,
            "pin_expiry": None,
            "sessions": []
        },
        "meta": data.get("meta", {})  # zone, radius_km, etc.
    }
    try:
        db = get_db()
        db.drivers.insert_one(doc)
        return jsonify({"ok": True, "driver_db_id": internal_id}), 201
    except mongo_errors.PyMongoError as e:
        return jsonify({"ok": False, "error": "db_write_failed", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500


@app.route("/drivers", methods=["GET"])
@app.route("/api/app/drivers", methods=["GET"])
@app.route("/api/drivers", methods=["GET"])  # alias for JS
def list_drivers():
    try:
        db = get_db()
        cur = db.drivers.find({"active": True})
        return jsonify({"ok": True, "drivers": [safe_doc(d) for d in cur]}), 200
    except mongo_errors.PyMongoError as e:
        return jsonify({"ok": False, "error": "db_read_failed", "details": str(e), "drivers": []}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e), "drivers": []}), 500


@app.route("/drivers/<driver_id>", methods=["GET"])
@app.route("/api/app/drivers/<driver_id>", methods=["GET"])
def get_driver(driver_id):
    try:
        db = get_db()
        d = db.drivers.find_one({"_internal_id": driver_id})
        if not d:
            return jsonify({"ok": False, "error": "driver_not_found"}), 404
        return jsonify({"ok": True, "driver": safe_doc(d)}), 200
    except mongo_errors.PyMongoError as e:
        return jsonify({"ok": False, "error": "db_read_failed", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500


# ----- Driver login: request PIN -----
@app.route("/drivers/request-pin", methods=["POST"])
@app.route("/api/app/drivers/request-pin", methods=["POST"])
def driver_request_pin():
    body = request.json or {}
    phone = (body.get("phone") or "").strip()
    if not phone_ok(phone):
        return jsonify({"ok": False, "error": "bad_phone"}), 400
    try:
        db = get_db()
        d = db.drivers.find_one({"phone": phone, "active": True})
        if not d:
            return jsonify({"ok": False, "error": "driver_not_found"}), 404
        pin = str(uuid.uuid4().int)[-4:]
        db.drivers.update_one(
            {"_internal_id": d["_internal_id"]},
            {"$set": {
                "auth.pin_hash": hash_pin(pin),
                "auth.pin_expiry": _now_dt() + timedelta(minutes=DRIVER_PIN_TTL_MIN)
            }}
        )
        payload = {"ok": True, "sent": True}
        if PIN_DEBUG_EXPOSE:
            payload["debug_pin"] = pin
        return jsonify(payload), 200
    except mongo_errors.PyMongoError as e:
        return jsonify({"ok": False, "error": "db_write_failed", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500


# ----- Driver login: verify PIN -----
@app.route("/drivers/verify-pin", methods=["POST"])
@app.route("/api/app/drivers/verify-pin", methods=["POST"])
def driver_verify_pin():
    body = request.json or {}
    phone = (body.get("phone") or "").strip()
    pin = (body.get("pin") or "").strip()
    if not phone_ok(phone) or not pin:
        return jsonify({"ok": False, "error": "bad_input"}), 400
    try:
        db = get_db()
        d = db.drivers.find_one({"phone": phone, "active": True})
        if not d:
            return jsonify({"ok": False, "error": "driver_not_found"}), 404
        ah = (d.get("auth") or {})
        if not ah or not ah.get("pin_hash") or not ah.get("pin_expiry"):
            return jsonify({"ok": False, "error": "no_pin_requested"}), 400
        if ah["pin_expiry"] < _now_dt():
            return jsonify({"ok": False, "error": "pin_expired"}), 400
        if ah["pin_hash"] != hash_pin(pin):
            return jsonify({"ok": False, "error": "pin_invalid"}), 400

        token = str(uuid.uuid4())
        expiry = _now_dt() + timedelta(minutes=DRIVER_TOKEN_TTL_MIN)
        db.drivers.update_one(
            {"_internal_id": d["_internal_id"]},
            {
                "$set": {"auth.pin_hash": None, "auth.pin_expiry": None},
                "$push": {"auth.sessions": {
                    "token": token,
                    "expires_at": expiry
                }}
            }
        )
        return jsonify({
            "ok": True,
            "driver_id": d["_internal_id"],
            "token": token,
            "expires_at": expiry.isoformat() + "Z"
        }), 200
    except mongo_errors.PyMongoError as e:
        return jsonify({"ok": False, "error": "db_write_failed", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500


# ----- Driver Orders (requires driver_id or token) -----
@app.route("/driver-orders", methods=["GET"])
@app.route("/api/app/driver-orders", methods=["GET"])
def driver_orders():
    driver_id = request.args.get("driver_id")
    status = request.args.get("status")
    token = request.headers.get("X-Driver-Token")
    try:
        db = get_db()
        if not driver_id:
            d = None
            if token:
                d = db.drivers.find_one({
                    "auth.sessions": {
                        "$elemMatch": {
                            "token": token,
                            "expires_at": {"$gte": _now_dt()}
                        }
                    }
                })
            if not d:
                return jsonify({"ok": False, "error": "auth_required"}), 401
            driver_id = d["_internal_id"]
        else:
            if token:
                d = db.drivers.find_one({
                    "auth.sessions": {
                        "$elemMatch": {
                            "token": token,
                            "expires_at": {"$gte": _now_dt()}
                        }
                    }
                })
                if not d or d["_internal_id"] != driver_id:
                    return jsonify({"ok": False, "error": "forbidden"}), 403

        q = {"assigned_driver_id": driver_id}
        if status:
            q["status"] = status
        cur = db.orders.find(q).sort("created_at", DESCENDING).limit(100)
        return jsonify({"ok": True, "orders": [safe_doc(o) for o in cur]}), 200
    except mongo_errors.PyMongoError as e:
        return jsonify({"ok": False, "error": "db_read_failed", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500


# ----- Proof of Delivery (driver) -----
@app.route("/orders/<oid>/proof", methods=["POST"])
@app.route("/api/app/orders/<oid>/proof", methods=["POST"])
def upload_proof(oid):
    token = request.headers.get("X-Driver-Token")
    try:
        db = get_db()
        d = db.drivers.find_one({
            "auth.sessions": {
                "$elemMatch": {
                    "token": token,
                    "expires_at": {"$gte": _now_dt()}
                }
            }
        })
        if not d:
            return jsonify({"ok": False, "error": "auth_required"}), 401

        order = db.orders.find_one({"_internal_id": oid})
        if not order:
            return jsonify({"ok": False, "error": "order_not_found"}), 404
        if order.get("assigned_driver_id") != d["_internal_id"]:
            return jsonify({"ok": False, "error": "forbidden"}), 403

        if "photo" not in request.files:
            return jsonify({"ok": False, "error": "file_missing"}), 400
        f = request.files["photo"]
        content = f.read()
        if not content:
            return jsonify({"ok": False, "error": "empty_file"}), 400

        fs = get_fs(db)
        fid = fs.put(
            content,
            filename=secure_filename(f.filename or "proof.jpg"),
            contentType=f.mimetype or "application/octet-stream"
        )

        file_url = f"/api/app/files/{fid}"
        db.orders.update_one(
            {"_internal_id": oid},
            {"$set": {
                "delivery_photo_file_id": str(fid),
                "delivery_photo_url": file_url
            }}
        )
        return jsonify({"ok": True, "file_id": str(fid), "url": file_url}), 201
    except mongo_errors.PyMongoError as e:
        return jsonify({"ok": False, "error": "db_write_failed", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500


# ----- Driver docs upload (GridFS) -----
@app.route("/drivers/<driver_id>/docs-upload", methods=["POST"])
@app.route("/api/app/drivers/<driver_id>/docs-upload", methods=["POST"])
def upload_driver_docs(driver_id):
    """
    Multipart form-data: any of 'id_doc', 'licence', 'vehicle_reg'
    Admin header or same-driver token required.
    """
    token = request.headers.get("X-Driver-Token")
    try:
        db = get_db()
        is_admin = (request.headers.get("X-Admin-Secret") == ADMIN_SECRET)
        d = None
        if token:
            d = db.drivers.find_one({
                "auth.sessions": {
                    "$elemMatch": {
                        "token": token,
                        "expires_at": {"$gte": _now_dt()}
                    }
                }
            })
        if not is_admin:
            if not d:
                return jsonify({"ok": False, "error": "auth_required"}), 401
            if d["_internal_id"] != driver_id:
                return jsonify({"ok": False, "error": "forbidden"}), 403

        fs = get_fs(db)
        updates = {}

        def save_field(field, key):
            if field in request.files:
                f = request.files[field]
                content = f.read()
                if content:
                    fid = fs.put(
                        content,
                        filename=secure_filename(f.filename or field),
                        contentType=f.mimetype or "application/octet-stream"
                    )
                    updates[f"docs.{key}"] = str(fid)

        save_field("id_doc", "id_doc_ref")
        save_field("licence", "licence_ref")
        save_field("vehicle_reg", "vehicle_reg_ref")

        if not updates:
            return jsonify({"ok": False, "error": "no_files"}), 400

        db.drivers.update_one({"_internal_id": driver_id}, {"$set": updates})
        return jsonify({"ok": True, "updated": updates}), 200
    except mongo_errors.PyMongoError as e:
        return jsonify({"ok": False, "error": "db_write_failed", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500


# ----- Stream files (GridFS) -----
@app.route("/files/<fid>", methods=["GET"])
@app.route("/api/app/files/<fid>", methods=["GET"])
def stream_file(fid):
    try:
        db = get_db()
        fs = get_fs(db)
        file = fs.get(ObjectId(fid))
        return send_file(
            io.BytesIO(file.read()),
            mimetype=file.content_type or "application/octet-stream",
            download_name=file.filename or "file.bin"
        )
    except Exception:
        abort(404)


# ---------------- WEEKLY CLOSE / PAYOUTS ------
@app.route("/settlements/weekly-close", methods=["POST"])
@app.route("/api/app/settlements/weekly-close", methods=["POST"])
def weekly_close():
    if not require_admin():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    body = request.json or {}
    note = body.get("note", "weekly close")
    try:
        db = get_db()
        cur = db.drivers.find({"active": True})
        created = []
        for d in cur:
            due = float(d.get("weekly_payout_due") or 0.0)
            if due <= 0:
                continue
            payout = {
                "driver_id": d["_internal_id"],
                "amount": round(due, 2),
                "note": note,
                "created_at": _now_dt(),
                "status": "pending"
            }
            db.payouts.insert_one(payout)
            db.drivers.update_one(
                {"_internal_id": d["_internal_id"]},
                {"$set": {"weekly_payout_due": 0.0}}
            )
            created.append({
                "driver_id": d["_internal_id"],
                "amount": payout["amount"]
            })
        return jsonify({"ok": True, "payouts": created}), 200
    except mongo_errors.PyMongoError as e:
        return jsonify({"ok": False, "error": "db_write_failed", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500


# ----- Approve all driver pay (admin one-click) -----
@app.route("/drivers/<driver_id>/approve-all-pay", methods=["POST"])
@app.route("/api/app/drivers/<driver_id>/approve-all-pay", methods=["POST"])
def approve_all_pay(driver_id):
    if not require_admin():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    try:
        db = get_db()
        q = {"assigned_driver_id": driver_id, "driver_pay_status": "pending"}
        to_update = list(db.orders.find(q, {"_internal_id": 1, "driver_pay_pending": 1}))
        count = 0
        for o in to_update:
            amt = float(o.get("driver_pay_pending") or 0.0)
            db.orders.update_one(
                {"_internal_id": o["_internal_id"]},
                {"$set": {
                    "driver_pay_status": "approved",
                    "driver_pay_approved": amt,
                    "driver_pay_pending": 0.0
                }}
            )
            count += 1
        return jsonify({"ok": True, "approved": count}), 200
    except mongo_errors.PyMongoError as e:
        return jsonify({"ok": False, "error": "db_write_failed", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500


# ---------------- STORES / ITEMS --------------
@app.route("/stores", methods=["POST"])
@app.route("/api/app/stores", methods=["POST"])
def create_store():
    data = request.json or {}
    internal_id = str(uuid.uuid4())
    store_doc = {
        "_internal_id": internal_id,
        "name": data.get("name"),
        "owner_name": data.get("owner_name"),
        "phone": data.get("phone"),
        "zone": data.get("zone"),
        "address": data.get("address"),
        "created_at": _now_dt()
    }
    try:
        db = get_db()
        db.stores.insert_one(store_doc)
        return jsonify({"ok": True, "store_id": internal_id}), 201
    except mongo_errors.PyMongoError as e:
        return jsonify({"ok": False, "error": "db_write_failed", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500


@app.route("/stores/<store_id>/items", methods=["POST"])
@app.route("/api/app/stores/<store_id>/items", methods=["POST"])
def add_store_item(store_id):
    data = request.json or {}
    item_id = str(uuid.uuid4())
    item_doc = {
        "_internal_id": item_id,
        "store_id": store_id,
        "name": data.get("name"),
        "price": float(data.get("price") or 0),
        "sku": data.get("sku"),
        "created_at": _now_dt(),
        "active": True
    }
    try:
        db = get_db()
        if not db.stores.find_one({"_internal_id": store_id}):
            return jsonify({"ok": False, "error": "store_not_found"}), 404
        db.store_items.insert_one(item_doc)
        return jsonify({"ok": True, "item_id": item_id}), 201
    except mongo_errors.PyMongoError as e:
        return jsonify({"ok": False, "error": "db_write_failed", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500


# ---------------- WHATSAPP SIM (OUTBOUND) -----
@app.route("/send_whatsapp_confirmation", methods=["POST"])
@app.route("/api/app/send_whatsapp_confirmation", methods=["POST"])
def send_whatsapp_confirmation():
    body = request.json or {}
    order_db_id = body.get("order_db_id")
    order_public_id = body.get("order_public_id")
    if not order_db_id and not order_public_id:
        return jsonify({"ok": False, "error": "order identifier required"}), 400
    try:
        db = get_db()
        q = {"_internal_id": order_db_id} if order_db_id else {"order_id": order_public_id}
        o = db.orders.find_one(q)
        if not o:
            return jsonify({"ok": False, "error": "order_not_found"}), 404
        msg = wa_order_text(o)
        db.whatsapp_log.insert_one({
            "direction": "outbound",
            "to": "CENTRAL_NUMBER",
            "order_id": o.get("order_id"),
            "body": msg,
            "created_at": _now_dt()
        })
        return jsonify({"ok": True, "message": "logged"}), 200
    except mongo_errors.PyMongoError as e:
        return jsonify({"ok": False, "error": "db_write_failed", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500


# ---------------- CATALOG (NEW) -----------------------
@app.route("/api/app/catalog", methods=["GET", "POST"])
def catalog():
    db = get_db()
    if request.method == "POST":
        if not require_admin():
            return jsonify({"ok": False, "error": "forbidden"}), 403
        body = request.json or {}
        doc = {
            "_internal_id": str(uuid.uuid4()),
            "name": body.get("name"),
            "category": body.get("category", "General"),
            "price": float(body.get("price") or 0),
            "sku": body.get("sku"),
            "active": bool(body.get("active", True)),
            "created_at": _now_dt()
        }
        if not doc["name"] or doc["price"] <= 0:
            return jsonify({"ok": False, "error": "bad_item"}), 400
        db.catalog.insert_one(doc)
        return jsonify({"ok": True, "item_id": doc["_internal_id"]}), 201

    # GET
    qcat = request.args.get("category")
    only_active = request.args.get("active", "true").lower() == "true"
    q = {}
    if only_active:
        q["active"] = True
    if qcat:
        q["category"] = qcat
    items = list(db.catalog.find(q).sort("name", ASCENDING))
    for it in items:
        it["id"] = it.pop("_internal_id", None)
        it.pop("_id", None)
    return jsonify({"ok": True, "items": items}), 200


@app.route("/api/app/catalog/search", methods=["GET"])
def catalog_search():
    db = get_db()
    q = (request.args.get("q") or "").strip()
    base = {"active": True}
    if q:
        base["name"] = {"$regex": re.escape(q), "$options": "i"}
    items = list(db.catalog.find(base).limit(20))
    for it in items:
        it["id"] = it.pop("_internal_id", None)
        it.pop("_id", None)
    return jsonify({"ok": True, "items": items}), 200


# ---------------- CATALOG SEEDER (ADMIN) ----------------
@app.route("/api/app/dev/seed-catalog", methods=["POST"])
def dev_seed_catalog():
    if not require_admin():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    try:
        db = get_db()
        payload = _catalog_seed_payload()
        inserted, updated = upsert_catalog_items(db, payload)
        total = db.catalog.count_documents({})
        return jsonify({
            "ok": True,
            "inserted": inserted,
            "updated": updated,
            "total_items": total
        }), 200
    except mongo_errors.PyMongoError as e:
        return jsonify({"ok": False, "error": "db_write_failed", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500


# ---------------- USSD (NEW, Lean Flow) -------------------
@app.route("/api/app/ussd", methods=["POST"])
def ussd_entry():
    if not USSD_ENABLE:
        return "END Service unavailable.", 200, {"Content-Type": "text/plain; charset=utf-8"}

    raw = request.get_data() or b""
    if not ip_allowed() or not verify_hmac_signature(raw):
        return "END Service temporarily unavailable.", 200, {"Content-Type": "text/plain; charset=utf-8"}

    db = get_db()

    # Basic rate limiting
    ip_key = f"ip:{client_ip()}"
    if not rate_limit_touch(db, ip_key, RATE_LIMIT_PER_IP_PER_MIN):
        return "END Too many requests. Try again in a minute.", 200, {"Content-Type": "text/plain; charset=utf-8"}

    session_id = (request.values.get("sessionId") or "").strip()
    serviceCode = (request.values.get("serviceCode") or "").strip()
    phone = (request.values.get("phoneNumber") or "").strip()
    text = (request.values.get("text") or "").strip()

    # Rate limit per phone when present
    if phone:
        if not rate_limit_touch(db, f"phone:{phone}", RATE_LIMIT_PER_PHONE_PER_MIN):
            return "END Too many requests. Please wait 1 minute.", 200, {"Content-Type": "text/plain; charset=utf-8"}

    steps = [s for s in text.split("*") if s] if text else []

    sess = db.ussd_sessions.find_one({"session_id": session_id}) if session_id else None
    if not sess:
        sess = {
            "session_id": session_id or str(uuid.uuid4()),
            "phone": phone,
            "created_at": _now_dt(),
            "state": {},
            "expires_at": _now_dt() + timedelta(minutes=20)
        }
        try:
            db.ussd_sessions.insert_one(sess)
            db.ussd_sessions.create_index(
                [("expires_at", ASCENDING)],
                expireAfterSeconds=0
            )
        except Exception:
            pass

    def con(msg: str):
        return f"CON {msg}", 200, {"Content-Type": "text/plain; charset=utf-8"}

    def end(msg: str):
        return f"END {msg}", 200, {"Content-Type": "text/plain; charset=utf-8"}

    if len(steps) == 0:
        return con(
            "YiThume – Health Shop\n"
            "1. Order Medicine\n"
            "2. Hygiene & Baby\n"
            "3. Track Order\n"
            "0. Exit"
        )

    if steps[0] == "0":
        return end("Goodbye.")

    # Track order
    if steps[0] == "3":
        if len(steps) == 1:
            return con("Enter Order ID (e.g. YI-20251106-ABC123):")
        if len(steps) >= 2:
            oid = steps[1].strip().upper()
            o = db.orders.find_one({"order_id": oid})
            if not o:
                return end("Order not found.")
            st = o.get("status", "pending").replace("_", " ").title()
            return end(f"Order {oid}: {st}")

    cat_map = {
        "1": ["Pain Relief", "Cold & Flu", "Digestive", "Vitamins"],
        "2": ["Hygiene", "Baby Care", "Women’s Health"]
    }

    if steps[0] in ("1", "2"):
        top = cat_map[steps[0]]

        if len(steps) == 1:
            lines = [f"{i + 1}. {c}" for i, c in enumerate(top)]
            return con("Choose Category:\n" + "\n".join(lines) + "\n0. Back")

        if steps[1] == "0":
            return con(
                "YiThume – Health Shop\n"
                "1. Order Medicine\n"
                "2. Hygiene & Baby\n"
                "3. Track Order\n"
                "0. Exit"
            )

        try:
            idx = int(steps[1]) - 1
            subcat = top[idx]
        except Exception:
            return end("Invalid option.")

        if len(steps) == 2:
            items = list(db.catalog.find({
                "category": subcat,
                "active": True
            }).sort("name", ASCENDING).limit(6))
            if not items:
                return end("No items in that category yet.")
            imap = []
            out_lines = []
            for i, it in enumerate(items, start=1):
                imap.append({
                    "id": it["_internal_id"],
                    "name": it["name"],
                    "price": it["price"]
                })
                out_lines.append(f"{i}. {it['name']} R{int(it['price'])}")
            sess["state"]["last_items"] = imap
            db.ussd_sessions.update_one(
                {"session_id": sess["session_id"]},
                {"$set": {"state": sess["state"]}}
            )
            return con("Pick item:\n" + "\n".join(out_lines) + "\n0. Back")

        if len(steps) == 3:
            if steps[2] == "0":
                lines = [f"{i + 1}. {c}" for i, c in enumerate(top)]
                return con("Choose Category:\n" + "\n".join(lines) + "\n0. Back")
            try:
                choice = int(steps[2]) - 1
                imap = sess.get("state", {}).get("last_items", [])
                sel = imap[choice]
            except Exception:
                return end("Invalid selection.")
            sess["state"]["selected_item"] = sel
            db.ussd_sessions.update_one(
                {"session_id": sess["session_id"]},
                {"$set": {"state": sess["state"]}}
            )
            return con(f"{sel['name']} – Enter quantity:")

        if len(steps) == 4:
            try:
                qty = max(1, int(steps[3]))
            except Exception:
                return end("Quantity must be a number.")
            sess["state"]["qty"] = qty
            db.ussd_sessions.update_one(
                {"session_id": sess["session_id"]},
                {"$set": {"state": sess["state"]}}
            )
            return con("Enter nearest landmark / village name:")

        if len(steps) >= 5:
            landmark = steps[4][:60]
            sel = (sess.get("state") or {}).get("selected_item")
            qty = int((sess.get("state") or {}).get("qty") or 1)
            if not sel:
                return end("Session expired. Start again.")

            subtotal = float(sel["price"]) * qty
            delivery_fee = 20.0
            total = round(subtotal + delivery_fee, 2)

            _, replay = idempotency_guard(db)
            if replay:
                return end("Order already received. We’ll confirm on WhatsApp.")

            order_doc = {
                "_internal_id": str(uuid.uuid4()),
                "order_id": make_order_public_id(),
                "created_at": _now_dt(),
                "created_at_iso": _now_iso(),
                "customer": {
                    "phone": phone,
                    "name": phone,
                    "address": {
                        "line1": landmark,
                        "coords": {"lat": None, "lng": None}
                    }
                },
                "items": [{
                    "name": sel["name"],
                    "qty": qty,
                    "price": float(sel["price"])
                }],
                "subtotal": subtotal,
                "delivery_fee": delivery_fee,
                "total": total,
                "payment": {
                    "method": "cod",
                    "status": "pending",
                    "provider_ref": None,
                    "fake_checkout_url": None
                },
                "status": "pending",
                "assigned_driver_id": None,
                "assigned_at": None,
                "delivered_at": None,
                "route": {},
                "created_by": "ussd",
                "meta": {
                    "channel": "ussd",
                    "collection_name": landmark,
                    "zone": None
                },
                "fraud_score": 0.0,
                "fraud_flags": {},
                "cluster_key": None,
                "delivery_photo_file_id": None,
                "delivery_photo_url": None,
                "driver_pay_status": "pending",
                "driver_pay_pending": round(min(max(delivery_fee, 25), 45), 2),
                "driver_pay_approved": 0.0,
                "settlement": {
                    "driver": 0.0,
                    "platform": 0.0,
                    "settled": False
                }
            }

            try:
                fs, ff = rule_based_fraud_score(db, order_doc)
                order_doc["fraud_score"], order_doc["fraud_flags"] = fs, ff
                if fs >= 0.75:
                    order_doc["status"] = "review_required"
                order_doc["cluster_key"] = cluster_key(order_doc)

                d = find_available_driver(db, None, None, None)
                if d:
                    order_doc["assigned_driver_id"] = d["_internal_id"]
                    order_doc["assigned_at"] = _now_dt()
                    if order_doc["status"] == "pending":
                        order_doc["status"] = "assigned"

                db.orders.insert_one(order_doc)

                db.whatsapp_log.insert_one({
                    "direction": "outbound",
                    "to": phone or "UNKNOWN",
                    "order_id": order_doc["order_id"],
                    "body": wa_order_text(order_doc),
                    "created_at": _now_dt()
                })

                return end(
                    f"Order placed: {order_doc['order_id']}\n"
                    f"Total: R{int(total)}\n"
                    "We’ll confirm on WhatsApp."
                )
            except Exception:
                return end("We couldn’t create your order. Please try later.")

    return end("Invalid option.")

# =================================================
# YiThume Manager + Employees + Jobs (Vercel-safe)
# Uses get_db() (no global db), avoids import-time db access.
# Dashboard HTML is in templates/manager_dashboard.html
# =================================================

# -------- Manager DB bootstrap (indexes only once per container) --------
_MANAGER_INDEXES_READY = False

def manager_db():
    """
    Always use this inside manager routes/functions.
    It reuses your existing get_db() which pings Mongo.
    """
    global _MANAGER_INDEXES_READY
    db = get_db()
    if not _MANAGER_INDEXES_READY:
        try:
            ensure_manager_indexes(db)
        except Exception:
            pass
        _MANAGER_INDEXES_READY = True
    return db

def ensure_manager_indexes(db):
    # Separate collections from your existing ones to avoid collisions
    db.managers.create_index([("workspace_id", ASCENDING)], unique=True)
    db.employees.create_index([("workspace_id", ASCENDING), ("employee_type", ASCENDING)], unique=True)
    db.jobs.create_index([("workspace_id", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)])
    db.events.create_index([("workspace_id", ASCENDING), ("created_at", DESCENDING)])
    db.workspaces.create_index([("created_at", DESCENDING)])


# -------- Manager helpers --------
def m_new_id(prefix=""):
    return (prefix + "_" if prefix else "") + uuid.uuid4().hex[:12]

def m_log_event(db, workspace_id, kind, msg, meta=None):
    db.events.insert_one({
        "_id": m_new_id("evt"),
        "workspace_id": workspace_id,
        "kind": kind,
        "msg": msg,
        "meta": meta or {},
        "created_at": _now_dt(),
    })

def m_create_workspace(db, name, template="general"):
    ws = {
        "_id": m_new_id("ws"),
        "name": (name or "New Store").strip(),
        "template": template,
        "created_at": _now_dt(),
    }
    db.workspaces.insert_one(ws)
    return ws

def m_get_workspace(db, workspace_id):
    return db.workspaces.find_one({"_id": workspace_id})

def m_get_manager(db, workspace_id):
    return db.managers.find_one({"workspace_id": workspace_id})

def m_get_employees(db, workspace_id):
    return list(db.employees.find({"workspace_id": workspace_id}).sort("employee_type", ASCENDING))

def m_get_jobs(db, workspace_id, limit=30):
    return list(db.jobs.find({"workspace_id": workspace_id}).sort("created_at", DESCENDING).limit(limit))

def m_get_events(db, workspace_id, limit=15):
    return list(db.events.find({"workspace_id": workspace_id}).sort("created_at", DESCENDING).limit(limit))

def m_enqueue_job(db, workspace_id, employee_type, job_type, payload=None, priority="med"):
    job = {
        "_id": m_new_id("job"),
        "workspace_id": workspace_id,
        "employee_type": employee_type,
        "job_type": job_type,
        "payload": payload or {},
        "priority": priority,      # low|med|high
        "status": "queued",        # queued|running|blocked|done|failed
        "progress": 0,             # 0-100
        "logs": [],
        "created_at": _now_dt(),
        "updated_at": _now_dt(),
    }
    db.jobs.insert_one(job)
    m_log_event(db, workspace_id, "job.created", f"{employee_type}: {job_type}", {"job_id": job["_id"]})
    return job

def m_set_job(db, job_id, status=None, progress=None, log_msg=None):
    if log_msg:
        db.jobs.update_one({"_id": job_id}, {"$push": {"logs": {"ts": _now_iso(), "msg": log_msg}}})
    update = {"updated_at": _now_dt()}
    if status is not None:
        update["status"] = status
    if progress is not None:
        update["progress"] = int(max(0, min(100, progress)))
    db.jobs.update_one({"_id": job_id}, {"$set": update})


# -------- Employee registry (plugins) --------
MANAGER_EMPLOYEE_REGISTRY = {}

def register_manager_employee(cls):
    MANAGER_EMPLOYEE_REGISTRY[cls.employee_type] = cls
    return cls

class ManagerBaseEmployee:
    employee_type = "base"
    name = "Base"
    description = ""
    capabilities = []

    @classmethod
    def should_enable(cls, survey):
        return False

    @classmethod
    def default_config(cls, survey):
        return {}

    @classmethod
    def initial_jobs(cls, workspace, survey):
        return []  # list of (job_type, payload)

    @classmethod
    def run_job(cls, job, workspace):
        return {"status": "done", "progress": 100, "log": f"Completed {job['job_type']}"}


@register_manager_employee
class ManagerStorefrontEmployee(ManagerBaseEmployee):
    employee_type = "storefront"
    name = "Storefront Builder"
    description = "Creates storefront pages + checkout skeleton (web-first)."
    capabilities = ["create_storefront", "manage_products", "publish_link"]

    @classmethod
    def should_enable(cls, survey):
        return bool(survey.get("channel_web", True))

    @classmethod
    def default_config(cls, survey):
        return {"theme": survey.get("theme", "clean"), "currency": survey.get("currency", "ZAR")}

    @classmethod
    def initial_jobs(cls, workspace, survey):
        return [
            ("CREATE_STOREFRONT", {"template": workspace.get("template", "general")}),
            ("IMPORT_PRODUCTS", {"mode": survey.get("products_mode", "csv_or_shopify_or_whatsapp")}),
        ]

    @classmethod
    def run_job(cls, job, workspace):
        jt = job["job_type"]
        if jt == "CREATE_STOREFRONT":
            return {"status": "done", "progress": 100, "log": "Storefront scaffold created."}
        if jt == "IMPORT_PRODUCTS":
            return {"status": "blocked", "progress": 10, "log": "Waiting for products import."}
        return {"status": "done", "progress": 100, "log": f"Storefront handled {jt}"}


@register_manager_employee
class ManagerSalesEmployee(ManagerBaseEmployee):
    employee_type = "sales"
    name = "Sales Assistant"
    description = "Answers product questions, builds quotes, pushes to confirmation."
    capabilities = ["product_search", "quote", "upsell"]

    @classmethod
    def should_enable(cls, survey):
        return True

    @classmethod
    def default_config(cls, survey):
        return {"autopilot": survey.get("sales_autopilot", "assist")}

    @classmethod
    def initial_jobs(cls, workspace, survey):
        return [("SETUP_SALES_PLAYBOOK", {"template": workspace.get("template", "general")})]

    @classmethod
    def run_job(cls, job, workspace):
        if job["job_type"] == "SETUP_SALES_PLAYBOOK":
            return {"status": "done", "progress": 100, "log": "Sales playbook configured."}
        return {"status": "done", "progress": 100, "log": f"Sales handled {job['job_type']}"}


@register_manager_employee
class ManagerSupportEmployee(ManagerBaseEmployee):
    employee_type = "support"
    name = "Customer Service"
    description = "Tracking, changes/cancellations, FAQs, escalations."
    capabilities = ["order_tracking", "case_opening", "faq"]

    @classmethod
    def should_enable(cls, survey):
        return bool(survey.get("needs_support", True))

    @classmethod
    def default_config(cls, survey):
        return {"autopilot": survey.get("support_autopilot", "assist")}

    @classmethod
    def initial_jobs(cls, workspace, survey):
        return [("SETUP_SUPPORT_MACROS", {"template": workspace.get("template", "general")})]

    @classmethod
    def run_job(cls, job, workspace):
        if job["job_type"] == "SETUP_SUPPORT_MACROS":
            return {"status": "done", "progress": 100, "log": "Support macros configured."}
        return {"status": "done", "progress": 100, "log": f"Support handled {job['job_type']}"}


@register_manager_employee
class ManagerReturnsEmployee(ManagerBaseEmployee):
    employee_type = "returns"
    name = "Returns & Refunds"
    description = "Intakes returns/refunds and routes approvals."
    capabilities = ["returns_intake", "refund_workflow"]

    @classmethod
    def should_enable(cls, survey):
        return bool(survey.get("needs_returns", False))

    @classmethod
    def default_config(cls, survey):
        return {"policy": survey.get("returns_policy", "7_days_unopened")}

    @classmethod
    def initial_jobs(cls, workspace, survey):
        return [("SETUP_RETURNS_POLICY", {"policy": survey.get("returns_policy", "7_days_unopened")})]

    @classmethod
    def run_job(cls, job, workspace):
        if job["job_type"] == "SETUP_RETURNS_POLICY":
            return {"status": "done", "progress": 100, "log": "Returns policy set."}
        return {"status": "done", "progress": 100, "log": f"Returns handled {job['job_type']}"}


@register_manager_employee
class ManagerDeliveryEmployee(ManagerBaseEmployee):
    employee_type = "delivery"
    name = "Delivery Coordinator"
    description = "Sets delivery options (manual/external/YiThume network)."
    capabilities = ["dispatch", "carrier_select", "payouts_basic"]

    @classmethod
    def should_enable(cls, survey):
        return bool(survey.get("needs_delivery", True))

    @classmethod
    def default_config(cls, survey):
        return {"mode": survey.get("delivery_mode", "manual")}

    @classmethod
    def initial_jobs(cls, workspace, survey):
        return [("SETUP_DELIVERY_MODE", {"mode": survey.get("delivery_mode", "manual")})]

    @classmethod
    def run_job(cls, job, workspace):
        if job["job_type"] == "SETUP_DELIVERY_MODE":
            return {"status": "done", "progress": 100, "log": f"Delivery mode set: {job['payload'].get('mode','manual')}"}
        return {"status": "done", "progress": 100, "log": f"Delivery handled {job['job_type']}"}


# -------- Manager core functions --------
def manager_create_manager(db, workspace_id, survey):
    m = m_get_manager(db, workspace_id)
    if m:
        return m
    mgr = {
        "_id": m_new_id("mgr"),
        "workspace_id": workspace_id,
        "autopilot_level": survey.get("autopilot_level", "assist"),
        "plan": survey.get("plan", "starter"),
        "employees": {},  # employee_type -> {enabled, config}
        "created_at": _now_dt(),
        "updated_at": _now_dt(),
    }
    db.managers.insert_one(mgr)
    m_log_event(db, workspace_id, "manager.created", "Manager created.")
    return mgr

def manager_create_employees(db, workspace_id, survey):
    ws = m_get_workspace(db, workspace_id)
    if not ws:
        raise ValueError("workspace not found")

    mgr = manager_create_manager(db, workspace_id, survey)
    enabled = []

    for emp_type, emp_cls in MANAGER_EMPLOYEE_REGISTRY.items():
        if not emp_cls.should_enable(survey):
            continue

        cfg = emp_cls.default_config(survey)

        db.employees.update_one(
            {"workspace_id": workspace_id, "employee_type": emp_type},
            {"$set": {
                "workspace_id": workspace_id,
                "employee_type": emp_type,
                "name": emp_cls.name,
                "description": emp_cls.description,
                "capabilities": emp_cls.capabilities,
                "enabled": True,
                "config": cfg,
                "updated_at": _now_dt(),
                "created_at": _now_dt(),
            }},
            upsert=True
        )

        mgr["employees"][emp_type] = {"enabled": True, "config": cfg}
        enabled.append(emp_type)

        for job_type, payload in emp_cls.initial_jobs(ws, survey):
            m_enqueue_job(db, workspace_id, emp_type, job_type, payload=payload)

    db.managers.update_one(
        {"workspace_id": workspace_id},
        {"$set": {"employees": mgr["employees"], "updated_at": _now_dt()}}
    )
    m_log_event(db, workspace_id, "employees.created", f"Employees enabled: {', '.join(enabled) if enabled else '(none)'}")
    return enabled

def manager_run_one_job(db, workspace_id, job_id):
    ws = m_get_workspace(db, workspace_id)
    if not ws:
        return False, "workspace not found"

    job = db.jobs.find_one({"_id": job_id, "workspace_id": workspace_id})
    if not job:
        return False, "job not found"

    if job.get("status") in ("done", "running"):
        return True, f"job already {job['status']}"

    emp_type = job.get("employee_type")
    emp_cls = MANAGER_EMPLOYEE_REGISTRY.get(emp_type)
    if not emp_cls:
        m_set_job(db, job_id, status="failed", log_msg=f"No employee registered: {emp_type}")
        m_log_event(db, workspace_id, "job.failed", f"Job failed (no employee): {job_id}", {"job_id": job_id})
        return False, "no employee registered"

    m_set_job(db, job_id, status="running", progress=max(job.get("progress", 0), 1), log_msg="Job started.")
    try:
        result = emp_cls.run_job(job, ws) or {}
        status = result.get("status", "done")
        progress = result.get("progress", 100 if status == "done" else job.get("progress", 0))
        log_msg = result.get("log", f"Completed {job.get('job_type')}")
        m_set_job(db, job_id, status=status, progress=progress, log_msg=log_msg)
        m_log_event(db, workspace_id, f"job.{status}", f"{emp_type} -> {job.get('job_type')} : {status}", {"job_id": job_id})
        return True, status
    except Exception as e:
        m_set_job(db, job_id, status="failed", progress=0, log_msg=f"Exception: {str(e)}")
        m_log_event(db, workspace_id, "job.failed", f"Job failed: {job_id}", {"job_id": job_id, "error": str(e)})
        return False, str(e)


# -------- Routes (namespaced under /manager) --------
@app.get("/manager/onboarding")
def manager_onboarding_form():
    if not require_admin():
        return "Unauthorized", 401

    admin_pin = request.args.get("admin_pin", "")  # <- IMPORTANT

    return f"""
    <!doctype html><html><head><meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <script src="https://cdn.tailwindcss.com"></script>
    <title>YiThume Manager Onboarding</title></head>
    <body class="bg-zinc-950 text-zinc-100">
      <div class="max-w-xl mx-auto px-4 py-10">
        <div class="mb-6">
          <div class="text-zinc-400 text-sm">YiThume</div>
          <h1 class="text-2xl font-semibold">Create a Workspace</h1>
          <p class="text-zinc-400 text-sm mt-1">Survey → Manager → Employees → Jobs → Dashboard</p>
        </div>

        <form class="space-y-4" method="post" action="/manager/onboarding?admin_pin={admin_pin}">
          <div>
            <label class="text-sm text-zinc-300">Store name</label>
            <input name="store_name" class="mt-1 w-full rounded-lg bg-zinc-900 border border-zinc-800 px-3 py-2" placeholder="YiThume Store" />
          </div>

          <div>
            <label class="text-sm text-zinc-300">Template</label>
            <select name="template" class="mt-1 w-full rounded-lg bg-zinc-900 border border-zinc-800 px-3 py-2">
              <option value="general">General</option>
              <option value="pharmacy">Pharmacy</option>
              <option value="grocery">Grocery</option>
              <option value="water">Water</option>
              <option value="hardware">Hardware</option>
            </select>
          </div>

          <div class="grid grid-cols-2 gap-3 text-sm">
            <label class="flex items-center gap-2"><input type="checkbox" name="channel_web" checked /> Web storefront</label>
            <label class="flex items-center gap-2"><input type="checkbox" name="needs_support" checked /> Customer service</label>
            <label class="flex items-center gap-2"><input type="checkbox" name="needs_returns" /> Returns</label>
            <label class="flex items-center gap-2"><input type="checkbox" name="needs_delivery" checked /> Delivery</label>
          </div>

          <div>
            <label class="text-sm text-zinc-300">Delivery mode</label>
            <select name="delivery_mode" class="mt-1 w-full rounded-lg bg-zinc-900 border border-zinc-800 px-3 py-2">
              <option value="manual">Manual</option>
              <option value="external">External (later)</option>
              <option value="yithume">YiThume network</option>
            </select>
          </div>

          <button class="w-full rounded-lg bg-emerald-600 hover:bg-emerald-500 px-3 py-2 font-semibold">
            Create manager + employees
          </button>
        </form>

        <div class="text-xs text-zinc-500 mt-6">
          Tip: open this page with <code class="text-zinc-300">?admin_pin=YOUR_PIN</code>.
        </div>
      </div>
    </body></html>
    """

@app.post("/manager/onboarding")
def manager_onboarding_submit():
    if not require_admin():
        return "Unauthorized", 401

    db = manager_db()

    store_name = (request.form.get("store_name") or "New Store").strip()
    template = (request.form.get("template") or "general").strip()

    survey = {
        "channel_web": bool(request.form.get("channel_web")),
        "needs_support": bool(request.form.get("needs_support")),
        "needs_returns": bool(request.form.get("needs_returns")),
        "needs_delivery": bool(request.form.get("needs_delivery")),
        "delivery_mode": request.form.get("delivery_mode") or "manual",
        "autopilot_level": "assist",
        "plan": "starter",
    }

    ws = m_create_workspace(db, store_name, template=template)
    m_log_event(db, ws["_id"], "workspace.created", f"Workspace created: {ws['name']}")

    manager_create_manager(db, ws["_id"], survey)
    manager_create_employees(db, ws["_id"], survey)

    return redirect(url_for("manager_dashboard", workspace_id=ws["_id"]))

@app.get("/manager/dashboard/<workspace_id>")
def manager_dashboard(workspace_id):
    if not require_admin():
        return "Unauthorized", 401

    db = manager_db()

    ws = m_get_workspace(db, workspace_id)
    if not ws:
        return "Workspace not found", 404

    mgr = m_get_manager(db, workspace_id) or manager_create_manager(db, workspace_id, {"autopilot_level": "assist", "plan": "starter"})
    employees = m_get_employees(db, workspace_id)
    jobs = m_get_jobs(db, workspace_id, limit=30)
    events = m_get_events(db, workspace_id, limit=15)

    # serialize for template
    employee_cards = []
    for e in employees:
        cfg = e.get("config", {})
        employee_cards.append({
            "employee_type": e.get("employee_type"),
            "name": e.get("name"),
            "description": e.get("description"),
            "capabilities": e.get("capabilities", []),
            "enabled": bool(e.get("enabled", True)),
            "config_json": jsonify(cfg).get_data(as_text=True),
        })

    def fmt_dt(x):
        return _iso(x) if isinstance(x, datetime) else str(x)

    jobs_view = []
    for j in jobs:
        j2 = dict(j)
        j2["created_at"] = fmt_dt(j2.get("created_at"))
        j2["progress"] = int(j2.get("progress", 0))
        j2["logs"] = [{"ts": x.get("ts",""), "msg": x.get("msg","")} for x in (j2.get("logs") or [])]
        jobs_view.append(j2)

    events_view = []
    for ev in events:
        ev2 = dict(ev)
        ev2["created_at"] = fmt_dt(ev2.get("created_at"))
        events_view.append(ev2)

    # IMPORTANT: HTML is in templates/manager_dashboard.html
    return render_template(
        "manager_dashboard.html",
        ws=ws,
        manager=mgr,
        employee_cards=employee_cards,
        jobs=jobs_view,
        events=events_view
    )

@app.post("/manager/api/<workspace_id>/employees/<employee_type>/toggle")
def manager_toggle_employee(workspace_id, employee_type):
    if not require_admin():
        return "Unauthorized", 401

    db = manager_db()

    emp = db.employees.find_one({"workspace_id": workspace_id, "employee_type": employee_type})
    if not emp:
        return "Employee not found", 404

    new_enabled = not bool(emp.get("enabled", True))
    db.employees.update_one(
        {"workspace_id": workspace_id, "employee_type": employee_type},
        {"$set": {"enabled": new_enabled, "updated_at": _now_dt()}}
    )
    m_log_event(db, workspace_id, "employee.toggled", f"{employee_type} -> {'enabled' if new_enabled else 'disabled'}")
    return redirect(url_for("manager_dashboard", workspace_id=workspace_id))

@app.post("/manager/api/<workspace_id>/employees/<employee_type>/start_setup")
def manager_employee_start_setup(workspace_id, employee_type):
    if not require_admin():
        return "Unauthorized", 401

    db = manager_db()

    ws = m_get_workspace(db, workspace_id)
    if not ws:
        return "Workspace not found", 404

    emp_cls = MANAGER_EMPLOYEE_REGISTRY.get(employee_type)
    if not emp_cls:
        return "Employee not registered", 404

    # MVP: enqueue initial jobs again (dedupe later)
    survey = {}
    for job_type, payload in emp_cls.initial_jobs(ws, survey):
        m_enqueue_job(db, workspace_id, employee_type, job_type, payload=payload)

    m_log_event(db, workspace_id, "employee.setup", f"{employee_type} setup jobs enqueued")
    return redirect(url_for("manager_dashboard", workspace_id=workspace_id))

@app.post("/manager/api/<workspace_id>/jobs/<job_id>/run")
def manager_run_job(workspace_id, job_id):
    if not require_admin():
        return "Unauthorized", 401

    db = manager_db()
    manager_run_one_job(db, workspace_id, job_id)
    return redirect(url_for("manager_dashboard", workspace_id=workspace_id))

@app.post("/manager/api/<workspace_id>/jobs/run_next")
def manager_run_next_job(workspace_id):
    if not require_admin():
        return "Unauthorized", 401

    db = manager_db()

    job = db.jobs.find_one({"workspace_id": workspace_id, "status": "queued"}, sort=[("created_at", 1)])
    if job:
        manager_run_one_job(db, workspace_id, job["_id"])
    else:
        m_log_event(db, workspace_id, "job.none", "No queued jobs to run.")
    return redirect(url_for("manager_dashboard", workspace_id=workspace_id))