# app.py — YiThume Flask API (MongoDB-only; no local storage)

from urllib.parse import quote
import os
import re
import io
import uuid
import hashlib
import secrets
import urllib.request
import urllib.error
import base64
import json
import hmac
from datetime import datetime, timedelta
from math import radians, cos, sin, asin, sqrt
from flask import Flask, request, jsonify, send_file, abort, send_from_directory, render_template, redirect, url_for
from flask_cors import CORS
from pymongo import MongoClient, ASCENDING, DESCENDING, errors as mongo_errors
from bson.objectid import ObjectId
from gridfs import GridFS
import csv
from urllib.parse import quote
from flask import Response
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
    if db is None:
        db = get_db()
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



# -------- Platform Admin Analytics (Owner / White-label KPI dashboard) --------
# These endpoints are for YOU (platform owner) — not for node operators / stores.

# -----------------------------
# Platform owner: Nodes / Stores registry (white-label control plane)
# -----------------------------
@app.route("/admin/api/nodes", methods=["GET", "POST"])
@app.route("/api/app/admin/api/nodes", methods=["GET", "POST"])
def admin_nodes():
    if not require_admin():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    db = get_db()

    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        node_id = str(uuid.uuid4())
        doc = {
            "_internal_id": node_id,
            "name": (body.get("name") or "").strip() or f"Node {node_id[:8]}",
            "country": (body.get("country") or "ZA").strip().upper(),
            "city": (body.get("city") or "").strip(),
            "currency": (body.get("currency") or "ZAR").strip().upper(),
            "commission_pct": float(body.get("commission_pct") or 0.10),  # node operator commission
            "status": (body.get("status") or "active").strip().lower(),
            "created_at": _now_dt()
        }
        db.nodes.insert_one(doc)
        return jsonify({"ok": True, "node": {"id": node_id, "name": doc["name"]}}), 201

    rows = list(db.nodes.find({}, {"_id": 0}).sort("created_at", DESCENDING).limit(500))
    return jsonify({"ok": True, "nodes": rows}), 200


@app.route("/admin/api/stores", methods=["GET"])
@app.route("/api/app/admin/api/stores", methods=["GET"])
def admin_stores_list():
    if not require_admin():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    db = get_db()
    node_id = request.args.get("node_id")
    q = {}
    if node_id:
        q["node_id"] = node_id
    rows = list(db.stores.find(q, {"_id": 0}).sort("created_at", DESCENDING).limit(1000))
    return jsonify({"ok": True, "stores": rows}), 200


@app.route("/admin/api/store/<store_id>/assign_node", methods=["POST"])
@app.route("/api/app/admin/api/store/<store_id>/assign_node", methods=["POST"])
def admin_assign_store_node(store_id):
    if not require_admin():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    db = get_db()
    s = db.stores.find_one({"_internal_id": store_id})
    if not s:
        return jsonify({"ok": False, "error": "store_not_found"}), 404
    body = request.get_json(silent=True) or {}
    node_id = (body.get("node_id") or "").strip()
    if node_id and not db.nodes.find_one({"_internal_id": node_id}):
        return jsonify({"ok": False, "error": "node_not_found"}), 404
    db.stores.update_one({"_internal_id": store_id}, {"$set": {"node_id": node_id or None}})
    return jsonify({"ok": True, "store_id": store_id, "node_id": node_id or None}), 200

# They require ADMIN_SECRET via header `X-Admin-Secret` or query `admin_pin`.

ANALYTICS_CACHE_TTL_SECONDS = int(os.environ.get("ANALYTICS_CACHE_TTL_SECONDS", "300"))

def _parse_int(v, default, lo=None, hi=None):
    try:
        n = int(v)
        if lo is not None and n < lo:
            n = lo
        if hi is not None and n > hi:
            n = hi
        return n
    except Exception:
        return default

def _now_utc():
    return datetime.utcnow()

def _dt_range_days(days: int):
    end = _now_utc()
    start = end - timedelta(days=days)
    return start, end

def _cache_col(db):
    col = db["admin_analytics_cache"]
    try:
        col.create_index("expires_at", expireAfterSeconds=0)
        col.create_index([("key", 1), ("scope", 1), ("days", 1)], unique=True)
    except Exception:
        pass
    return col

def _cache_get(db, key: str, scope: str, days: int):
    doc = _cache_col(db).find_one({"key": key, "scope": scope, "days": days})
    if not doc:
        return None
    if doc.get("expires_at") and doc["expires_at"] < _now_utc():
        return None
    return doc.get("value")

def _cache_set(db, key: str, scope: str, days: int, value):
    _cache_col(db).update_one(
        {"key": key, "scope": scope, "days": days},
        {"$set": {"value": value, "expires_at": _now_utc() + timedelta(seconds=ANALYTICS_CACHE_TTL_SECONDS)}},
        upsert=True
    )

from typing import Optional

def _scope_filter(node_id: Optional[str], store_id: Optional[str]):
    f = {}
    if node_id:
        f["node_id"] = node_id
    if store_id:
        f["store_id"] = store_id
    return f

def _orders_match(base_filter: dict, start_dt: datetime, end_dt: datetime):
    f = dict(base_filter or {})
    # Try common time fields; keep compatibility with older docs
    # We'll match by created_at OR created OR ts.
    f["$or"] = [
        {"created_at": {"$gte": start_dt, "$lt": end_dt}},
        {"created": {"$gte": start_dt, "$lt": end_dt}},
        {"ts": {"$gte": start_dt, "$lt": end_dt}},
    ]
    return f

def _sum_money_safe(vals):
    total = 0.0
    for v in vals:
        try:
            total += float(v or 0)
        except Exception:
            pass
    return total

def _money_from_order(o: dict):
    """
    Best-effort: order totals can vary by schema.
    We'll try: totals.total, quote.total, payment.amount, meta.total, etc.
    """
    for path in [
        ("totals", "total"),
        ("totals", "amount"),
        ("quote", "total"),
        ("quote", "amount"),
        ("payment", "amount"),
        ("meta", "total"),
        ("meta", "amount"),
        ("total",),
        ("amount",),
    ]:
        cur = o
        ok = True
        for k in path:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                ok = False
                break
        if ok:
            try:
                return float(cur or 0)
            except Exception:
                continue
    return 0.0

def _order_status(o: dict):
    for k in ["status", "state", "order_status"]:
        v = o.get(k)
        if v:
            return str(v).lower()
    return "unknown"

def _driver_is_online(d: dict):
    # best-effort flags
    for k in ["online", "is_online", "available", "is_available"]:
        if k in d:
            try:
                return bool(d.get(k))
            except Exception:
                pass
    # last_seen heuristic
    ls = d.get("last_seen_at") or d.get("last_seen") or d.get("last_ping")
    if isinstance(ls, datetime):
        return (ls > (_now_utc() - timedelta(minutes=10)))
    return False

def _require_admin_or_403():
    if not require_admin():
        abort(403)


def _store_payment_cfg(store_doc: dict) -> dict:
    p = (store_doc or {}).get("payments") or {}
    # defaults
    mode = (p.get("mode") or "demo").lower()  # demo | live
    provider = (p.get("provider") or ("stripe_connect" if mode == "live" else "demo")).lower()
    p.setdefault("mode", mode)
    p.setdefault("provider", provider)
    p.setdefault("stripe", p.get("stripe") or {})
    p.setdefault("yoco", p.get("yoco") or {})
    return p

def _store_is_live(store_doc: dict) -> bool:
    return (_store_payment_cfg(store_doc).get("mode") or "demo").lower() == "live"

def _stripe_platform_ready() -> bool:
    return bool(stripe) and bool(STRIPE_PLATFORM_SECRET_KEY)

def _stripe_platform_init() -> bool:
    if not stripe:
        return False
    if not STRIPE_PLATFORM_SECRET_KEY:
        return False
    try:
        stripe.api_key = STRIPE_PLATFORM_SECRET_KEY
        return True
    except Exception:
        return False

def _stripe_fee_amount_cents(total_amount: float) -> int:
    try:
        fee = float(total_amount) * float(PLATFORM_FEE_PCT)
        return max(0, int(round(fee * 100)))
    except Exception:
        return 0

def _stripe_connect_account_id(store_doc: dict) -> str | None:
    p = _store_payment_cfg(store_doc)
    acct = ((p.get("stripe") or {}).get("account_id") or "").strip()
    return acct or None

def _stripe_available():
    return bool(stripe) and bool(os.environ.get("STRIPE_SECRET_KEY") or os.environ.get("STRIPE_API_KEY"))

def _stripe_init():
    if not stripe:
        return False
    key = os.environ.get("STRIPE_SECRET_KEY") or os.environ.get("STRIPE_API_KEY")
    if not key:
        return False
    try:
        stripe.api_key = key
        return True
    except Exception:
        return False

def _stripe_mrr_from_subscriptions(subs):
    """
    Compute Monthly Recurring Revenue from active subscriptions.
    This is best-effort and depends on price recurring interval.
    """
    mrr = 0.0
    active = 0
    for s in subs:
        try:
            if (s.get("status") or "").lower() not in ("active", "trialing", "past_due"):
                continue
            active += 1
            items = (s.get("items") or {}).get("data") or []
            for it in items:
                price = (it.get("price") or {})
                unit_amount = price.get("unit_amount") or 0
                qty = it.get("quantity") or 1
                recurring = price.get("recurring") or {}
                interval = (recurring.get("interval") or "month").lower()
                interval_count = int(recurring.get("interval_count") or 1)

                amount = (float(unit_amount) / 100.0) * float(qty)

                # Normalize to monthly
                if interval == "month":
                    mrr += amount / interval_count
                elif interval == "year":
                    mrr += (amount / 12.0) / interval_count
                elif interval == "week":
                    mrr += (amount * 4.345) / interval_count
                elif interval == "day":
                    mrr += (amount * 30.437) / interval_count
                else:
                    mrr += amount
        except Exception:
            continue
    return {"mrr": round(mrr, 2), "active_subscriptions": active}

@app.route("/admin/api/analytics/overview", methods=["GET"])
def admin_analytics_overview():
    """
    Platform KPI overview for your admin admin.
    Query params:
      - days (default 30, max 365)
      - node_id (optional)
      - store_id (optional)
    """
    _require_admin_or_403()
    db = get_db()

    days = _parse_int(request.args.get("days"), 30, lo=1, hi=365)
    node_id = (request.args.get("node_id") or "").strip() or None
    store_id = (request.args.get("store_id") or "").strip() or None
    scope = f"node:{node_id or 'ALL'}|store:{store_id or 'ALL'}"

    cached = _cache_get(db, "overview", scope, days)
    if cached:
        return jsonify({"ok": True, "cached": True, **cached})

    start_dt, end_dt = _dt_range_days(days)
    base = _scope_filter(node_id, store_id)

    # Orders KPIs
    match = _orders_match(base, start_dt, end_dt)
    orders = list(db.orders.find(match, {"_id": 0}))
    orders_total = len(orders)
    by_status = {}
    gmv = 0.0
    for o in orders:
        st = _order_status(o)
        by_status[st] = by_status.get(st, 0) + 1
        gmv += _money_from_order(o)

    # Drivers KPIs
    d_filter = dict(base)
    drivers_total = db.drivers.count_documents(d_filter)
    drivers_online = 0
    drivers_active_30d = 0
    cutoff_30d = _now_utc() - timedelta(days=30)
    for d in db.drivers.find(d_filter, {"_id": 0}):
        if _driver_is_online(d):
            drivers_online += 1
        ls = d.get("last_seen_at") or d.get("last_seen") or d.get("last_ping")
        if isinstance(ls, datetime) and ls >= cutoff_30d:
            drivers_active_30d += 1

    # Stores / Nodes
    stores_total = db.stores.count_documents(_scope_filter(node_id, None))
    nodes_total = db.nodes.count_documents({}) if not node_id else (1 if db.nodes.find_one({"_internal_id": node_id}) else 0)

    # Payout ledger (best effort: if you have payouts collection)
    payouts_total = 0.0
    try:
        payout_match = dict(base)
        payout_match["created_at"] = {"$gte": start_dt, "$lt": end_dt}
        payouts = list(db.payouts.find(payout_match, {"_id": 0, "amount": 1}))
        payouts_total = _sum_money_safe([p.get("amount") for p in payouts])
    except Exception:
        payouts_total = 0.0

    stripe_summary = {"enabled": False}
    if _stripe_available() and _stripe_init():
        try:
            # Small, safe summary (avoid heavy list calls)
            subs = stripe.Subscription.list(status="all", limit=100, expand=["data.items.data.price"])
            sub_data = subs.get("data") or []
            mrr_info = _stripe_mrr_from_subscriptions(sub_data)

            # Revenue in period from paid invoices
            invoices = stripe.Invoice.list(limit=100, created={"gte": int(start_dt.timestamp()), "lt": int(end_dt.timestamp())})
            inv = invoices.get("data") or []
            revenue = 0.0
            paid = 0
            for i in inv:
                if i.get("paid"):
                    paid += 1
                    revenue += float(i.get("amount_paid") or 0) / 100.0

            stripe_summary = {
                "enabled": True,
                "mrr": mrr_info["mrr"],
                "active_subscriptions": mrr_info["active_subscriptions"],
                "paid_invoices": paid,
                "revenue_period": round(revenue, 2),
            }
        except Exception as e:
            stripe_summary = {"enabled": True, "error": str(e)}

    result = {
        "range": {"days": days, "start": start_dt.isoformat() + "Z", "end": end_dt.isoformat() + "Z"},
        "scope": {"node_id": node_id, "store_id": store_id},
        "orders": {
            "total": orders_total,
            "by_status": by_status,
            "gmv_estimate": round(gmv, 2),
        },
        "drivers": {
            "total": drivers_total,
            "online_estimate": drivers_online,
            "active_30d_estimate": drivers_active_30d,
        },
        "stores": {"total": stores_total},
        "nodes": {"total": nodes_total},
        "payouts": {"driver_payouts_estimate": round(payouts_total, 2)},
        "stripe": stripe_summary,
    }

    _cache_set(db, "overview", scope, days, result)
    return jsonify({"ok": True, "cached": False, **result})

@app.route("/admin/api/analytics/orders_timeseries", methods=["GET"])
def admin_orders_timeseries():
    """
    Daily time series for orders + GMV estimate.
    Query params:
      - days (default 30)
      - node_id (optional)
      - store_id (optional)
    """
    _require_admin_or_403()
    db = get_db()

    days = _parse_int(request.args.get("days"), 30, lo=1, hi=365)
    node_id = (request.args.get("node_id") or "").strip() or None
    store_id = (request.args.get("store_id") or "").strip() or None
    scope = f"node:{node_id or 'ALL'}|store:{store_id or 'ALL'}"

    cached = _cache_get(db, "orders_timeseries", scope, days)
    if cached:
        return jsonify({"ok": True, "cached": True, "days": days, "series": cached})

    start_dt, end_dt = _dt_range_days(days)
    base = _scope_filter(node_id, store_id)
    match = _orders_match(base, start_dt, end_dt)

    # Group by day (UTC)
    pipeline = [
        {"$match": match},
        {"$addFields": {
            "_ts": {"$ifNull": ["$created_at", {"$ifNull": ["$created", "$ts"]}]}
        }},
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_ts"}},
            "count": {"$sum": 1},
        }},
        {"$sort": {"_id": 1}}
    ]

    series = []
    try:
        rows = list(db.orders.aggregate(pipeline))
        # Build lookup for counts
        by_day = {r["_id"]: int(r.get("count") or 0) for r in rows}
        # Also compute GMV with a lightweight scan (schema varies, so avoid complex pipeline)
        orders = list(db.orders.find(match, {"_id": 0}))
        gmv_by_day = {}
        for o in orders:
            ts = o.get("created_at") or o.get("created") or o.get("ts") or _now_utc()
            if not isinstance(ts, datetime):
                ts = _now_utc()
            day = ts.strftime("%Y-%m-%d")
            gmv_by_day[day] = gmv_by_day.get(day, 0.0) + _money_from_order(o)

        # Fill every day
        cur = start_dt.date()
        end_date = end_dt.date()
        while cur <= end_date:
            dstr = cur.strftime("%Y-%m-%d")
            series.append({
                "day": dstr,
                "orders": by_day.get(dstr, 0),
                "gmv_estimate": round(gmv_by_day.get(dstr, 0.0), 2),
            })
            cur = cur + timedelta(days=1)
    except Exception:
        # Fallback: no aggregation support
        cur = start_dt.date()
        end_date = end_dt.date()
        while cur <= end_date:
            series.append({"day": cur.strftime("%Y-%m-%d"), "orders": 0, "gmv_estimate": 0.0})
            cur = cur + timedelta(days=1)

    _cache_set(db, "orders_timeseries", scope, days, series)
    return jsonify({"ok": True, "cached": False, "days": days, "series": series})

@app.route("/admin/api/analytics/drivers", methods=["GET"])
def admin_drivers_snapshot():
    """
    Driver snapshot for platform owner.
    Query params:
      - node_id (optional)
    """
    _require_admin_or_403()
    db = get_db()
    node_id = (request.args.get("node_id") or "").strip() or None
    base = _scope_filter(node_id, None)

    drivers = []
    for d in db.drivers.find(base, {"_id": 0, "name": 1, "phone": 1, "_internal_id": 1, "node_id": 1, "zone": 1, "online": 1, "available": 1, "last_seen_at": 1, "last_seen": 1, "last_ping": 1}):
        drivers.append({
            "driver_id": d.get("_internal_id"),
            "node_id": d.get("node_id"),
            "name": d.get("name"),
            "phone": d.get("phone"),
            "zone": d.get("zone"),
            "online_estimate": _driver_is_online(d),
            "last_seen": (d.get("last_seen_at") or d.get("last_seen") or d.get("last_ping")),
        })

    return jsonify({"ok": True, "count": len(drivers), "drivers": drivers})

@app.route("/admin/api/analytics/stripe", methods=["GET"])
def admin_stripe_snapshot():
    """
    Stripe snapshot for platform owner. Requires Stripe env vars + dependency.
    """
    _require_admin_or_403()
    if not _stripe_available() or not _stripe_init():
        return jsonify({"ok": False, "error": "stripe_not_configured"}), 400

    days = _parse_int(request.args.get("days"), 30, lo=1, hi=365)
    start_dt, end_dt = _dt_range_days(days)

    try:
        subs = stripe.Subscription.list(status="all", limit=100, expand=["data.items.data.price"])
        mrr_info = _stripe_mrr_from_subscriptions(subs.get("data") or [])

        invoices = stripe.Invoice.list(limit=100, created={"gte": int(start_dt.timestamp()), "lt": int(end_dt.timestamp())})
        inv = invoices.get("data") or []
        revenue = 0.0
        paid = 0
        for i in inv:
            if i.get("paid"):
                paid += 1
                revenue += float(i.get("amount_paid") or 0) / 100.0

        customers = stripe.Customer.list(limit=100)
        return jsonify({
            "ok": True,
            "range": {"days": days, "start": start_dt.isoformat() + "Z", "end": end_dt.isoformat() + "Z"},
            "customers_estimate": len(customers.get("data") or []),
            "active_subscriptions_estimate": mrr_info["active_subscriptions"],
            "mrr": mrr_info["mrr"],
            "paid_invoices": paid,
            "revenue_period": round(revenue, 2),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": "stripe_fetch_failed", "details": str(e)}), 500

@app.route("/admin", methods=["GET"])
def platform_admin_home():
    """
    Minimal owner dashboard page (white-label KPI admin).
    Uses the analytics endpoints above.
    """
    _require_admin_or_403()

    # NOTE: Avoid f-strings here; keep JS template literals safe.
    html = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>YiThume Platform Admin</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="min-h-screen bg-slate-50 text-slate-900">
  <header class="sticky top-0 z-10 bg-white/80 backdrop-blur border-b border-slate-200">
    <div class="max-w-6xl mx-auto px-4 py-3 flex items-center justify-between">
      <div class="font-semibold">Platform Admin • KPIs</div>
      <div class="text-sm text-slate-600">Use ?admin_pin=XXXX or header X-Admin-Secret</div>
    </div>
  </header>

  <main class="max-w-6xl mx-auto px-4 py-6 space-y-6">
    <div class="flex flex-wrap gap-3 items-end">
      <div>
        <label class="block text-xs text-slate-500 mb-1">Days</label>
        <input id="days" type="number" value="30" min="1" max="365"
               class="w-28 px-3 py-2 border border-slate-300 rounded-lg bg-white" />
      </div>
      <div>
        <label class="block text-xs text-slate-500 mb-1">Node ID (optional)</label>
        <input id="node_id" type="text" placeholder="node_..."
               class="w-64 px-3 py-2 border border-slate-300 rounded-lg bg-white" />
      </div>
      <div>
        <label class="block text-xs text-slate-500 mb-1">Store ID (optional)</label>
        <input id="store_id" type="text" placeholder="store_..."
               class="w-64 px-3 py-2 border border-slate-300 rounded-lg bg-white" />
      </div>
      <button id="refresh"
              class="px-4 py-2 rounded-lg bg-emerald-600 text-white hover:bg-emerald-700">
        Refresh
      </button>
    </div>

    <section class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4" id="cards"></section>

    <section class="bg-white border border-slate-200 rounded-xl p-4">
      <div class="flex items-center justify-between">
        <h2 class="font-semibold">Orders (daily)</h2>
        <div class="text-xs text-slate-500" id="range"></div>
      </div>
      <div class="mt-3 overflow-x-auto">
        <table class="min-w-full text-sm">
          <thead class="text-slate-600">
            <tr>
              <th class="text-left py-2 pr-4">Day</th>
              <th class="text-left py-2 pr-4">Orders</th>
              <th class="text-left py-2 pr-4">GMV (est.)</th>
            </tr>
          </thead>
          <tbody id="series"></tbody>
        </table>
      </div>
    </section>

    <section class="bg-white border border-slate-200 rounded-xl p-4">
      <div class="flex items-center justify-between">
        <h2 class="font-semibold">Drivers (snapshot)</h2>
        <div class="text-xs text-slate-500" id="driversCount"></div>
      </div>
      <div class="mt-3 overflow-x-auto">
        <table class="min-w-full text-sm">
          <thead class="text-slate-600">
            <tr>
              <th class="text-left py-2 pr-4">Driver</th>
              <th class="text-left py-2 pr-4">Node</th>
              <th class="text-left py-2 pr-4">Zone</th>
              <th class="text-left py-2 pr-4">Online</th>
              <th class="text-left py-2 pr-4">Last seen</th>
            </tr>
          </thead>
          <tbody id="drivers"></tbody>
        </table>
      </div>
    </section>

  </main>

<script>
  const qs = new URLSearchParams(window.location.search);
  const adminPin = qs.get('admin_pin') || '';

  function withPin(url) {
    if (!adminPin) return url;
    const u = new URL(url, window.location.origin);
    u.searchParams.set('admin_pin', adminPin);
    return u.toString();
  }

  function fmtMoney(n) {
    try { return new Intl.NumberFormat(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}).format(n); }
    catch(e){ return (n || 0).toFixed(2); }
  }

  function card(title, value, sub) {
    return `
      <div class="bg-white border border-slate-200 rounded-xl p-4">
        <div class="text-xs text-slate-500">${title}</div>
        <div class="text-2xl font-semibold mt-1">${value}</div>
        ${sub ? `<div class="text-xs text-slate-500 mt-1">${sub}</div>` : ``}
      </div>
    `;
  }

  async function load() {
    const days = document.getElementById('days').value || '30';
    const nodeId = document.getElementById('node_id').value.trim();
    const storeId = document.getElementById('store_id').value.trim();

    const baseParams = new URLSearchParams();
    baseParams.set('days', days);
    if (nodeId) baseParams.set('node_id', nodeId);
    if (storeId) baseParams.set('store_id', storeId);

    const overviewUrl = withPin(`/admin/api/analytics/overview?${baseParams.toString()}`);
    const seriesUrl = withPin(`/admin/api/analytics/orders_timeseries?${baseParams.toString()}`);
    const driversUrl = withPin(`/admin/api/analytics/drivers${nodeId ? `?node_id=${encodeURIComponent(nodeId)}${adminPin ? `&admin_pin=${encodeURIComponent(adminPin)}` : ``}` : (adminPin ? `?admin_pin=${encodeURIComponent(adminPin)}` : ``)}`);

    const [ovRes, tsRes, drRes] = await Promise.all([
      fetch(overviewUrl),
      fetch(seriesUrl),
      fetch(driversUrl),
    ]);

    const ov = await ovRes.json();
    const ts = await tsRes.json();
    const dr = await drRes.json();

    if (!ov.ok) {
      alert(`Admin auth or error: ${ov.error || 'unknown'}`);
      return;
    }

    document.getElementById('range').textContent = `${ov.range.start} → ${ov.range.end}`;

    const cards = [];
    cards.push(card('Orders', ov.orders.total, `By status: ${Object.entries(ov.orders.by_status || {}).map(([k,v]) => `${k}:${v}`).join(', ')}`));
    cards.push(card('GMV (est.)', fmtMoney(ov.orders.gmv_estimate)));
    cards.push(card('Drivers', ov.drivers.total, `Online est: ${ov.drivers.online_estimate} • Active 30d est: ${ov.drivers.active_30d_estimate}`));
    cards.push(card('Stores', ov.stores.total, `Nodes: ${ov.nodes.total}`));
    cards.push(card('Driver payouts (est.)', fmtMoney(ov.payouts.driver_payouts_estimate)));
    if (ov.stripe && ov.stripe.enabled) {
      cards.push(card('Stripe MRR', fmtMoney(ov.stripe.mrr || 0), `Active subs: ${ov.stripe.active_subscriptions || 0}`));
      cards.push(card('Stripe revenue (period)', fmtMoney(ov.stripe.revenue_period || 0), `Paid invoices: ${ov.stripe.paid_invoices || 0}`));
    } else {
      cards.push(card('Stripe', 'Not configured', 'Set STRIPE_SECRET_KEY to enable'));
    }
    document.getElementById('cards').innerHTML = cards.join('');

    // timeseries
    const rows = (ts.series || []).map(r => `
      <tr class="border-t border-slate-100">
        <td class="py-2 pr-4">${r.day}</td>
        <td class="py-2 pr-4">${r.orders}</td>
        <td class="py-2 pr-4">${fmtMoney(r.gmv_estimate || 0)}</td>
      </tr>
    `).join('');
    document.getElementById('series').innerHTML = rows;

    // drivers
    document.getElementById('driversCount').textContent = `${dr.count || 0} drivers`;
    const drows = (dr.drivers || []).slice(0, 200).map(d => `
      <tr class="border-t border-slate-100">
        <td class="py-2 pr-4">${(d.name || '').replaceAll('<','&lt;')}</td>
        <td class="py-2 pr-4">${d.node_id || ''}</td>
        <td class="py-2 pr-4">${d.zone || ''}</td>
        <td class="py-2 pr-4">${d.online_estimate ? 'Yes' : 'No'}</td>
        <td class="py-2 pr-4">${d.last_seen || ''}</td>
      </tr>
    `).join('');
    document.getElementById('drivers').innerHTML = drows;
  }

  document.getElementById('refresh').addEventListener('click', load);
  load();
</script>
</body>
</html>
"""
    return html

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
# Marketplace (public catalog)
# -----------------------------

@app.route("/marketplace", methods=["GET"])
@app.route("/api/app/marketplace", methods=["GET"])
def marketplace_page():
    """Serves: api/static/marketplace.html"""
    return send_from_directory(app.static_folder, "marketplace.html")


@app.route("/v1/marketplace/products", methods=["GET"])
@app.route("/api/v1/marketplace/products", methods=["GET"])
@app.route("/api/app/v1/marketplace/products", methods=["GET"])
def marketplace_products():
    """
    Public marketplace feed across ALL stores.

    Includes:
      - legacy items in db.store_items
      - SaaS addon items in saas_products

    Query params:
      - q: search in name/category/store
      - category: filter by category
      - limit: max items (default 400)
    """
    try:
        db = get_db()

        q = _str(request.args.get("q"))
        cat = _str(request.args.get("category"))
        limit = _safe_int(request.args.get("limit"), 400)
        if limit <= 0:
            limit = 400
        limit = min(limit, 2000)

        # -------------------------
        # Legacy store items
        # -------------------------
        legacy_items = list(db.store_items.find({"active": True}).sort("created_at", -1).limit(limit))

        # -------------------------
        # SaaS addon products
        # -------------------------
        saas_items = list(_col(db, "saas_products").find({"active": True}).sort("created_at", -1).limit(limit))

        # Collect store ids for name/slug hydration
        store_ids = set()
        for it in legacy_items:
            sid = it.get("store_id")
            if sid:
                store_ids.add(sid)
        for p in saas_items:
            sid = p.get("store_id")
            if sid:
                store_ids.add(sid)

        # Hydrate from both store collections (some deployments use db.stores; SaaS uses saas_stores)
        stores_map = {}
        if store_ids:
            try:
                for s in db.stores.find({"_internal_id": {"$in": list(store_ids)}}):
                    stores_map[s.get("_internal_id")] = s
            except Exception:
                pass
            try:
                for s in _col(db, "saas_stores").find({"_internal_id": {"$in": list(store_ids)}}):
                    stores_map[s.get("_internal_id")] = s
            except Exception:
                pass

        def _category_of(x):
            c = x.get("category") or x.get("Category") or x.get("type") or "General"
            c = _str(c, "General").strip() or "General"
            return c[:60]

        def _money(x):
            try:
                return float(x)
            except Exception:
                return 0.0

        out = []

        # Legacy items -> normalized
        for it in legacy_items:
            sid = it.get("store_id")
            s = stores_map.get(sid) or {}
            out.append({
                "source": "legacy",
                "id": _str(it.get("_id")),
                "name": _str(it.get("name"), "Item"),
                "description": _str(it.get("description")),
                "category": _category_of(it),
                "price": _money(it.get("price")),
                "currency": _str((s.get("currency") or "ZAR")),
                "image_url": _str(it.get("image_url")),
                "store_id": _str(sid),
                "store_name": _str(s.get("name"), "Store"),
                "store_public_slug": s.get("public_slug"),
                "store_public_url": _store_public_url(s.get("public_slug")) if s.get("public_slug") else None,
                "created_at": (it.get("created_at").isoformat() + "Z") if isinstance(it.get("created_at"), datetime) else None,
            })

        # SaaS products -> normalized
        for p in saas_items:
            sid = p.get("store_id")
            s = stores_map.get(sid) or {}
            out.append({
                "source": "saas",
                "id": _str(p.get("_internal_id") or p.get("sku") or p.get("name")),
                "name": _str(p.get("name"), "Item"),
                "description": _str(p.get("description")),
                "category": _category_of(p),
                "price": _money(p.get("price")),
                "currency": _str((s.get("currency") or "ZAR")),
                "image_url": _str(p.get("image_url")),
                "store_id": _str(sid),
                "store_name": _str(s.get("name"), "Store"),
                "store_public_slug": s.get("public_slug"),
                "store_public_url": _store_public_url(s.get("public_slug")) if s.get("public_slug") else None,
                "created_at": (p.get("created_at").isoformat() + "Z") if isinstance(p.get("created_at"), datetime) else None,
            })

        # Search + category filters
        if q:
            ql = q.lower().strip()
            out = [
                x for x in out
                if ql in (x.get("name") or "").lower()
                or ql in (x.get("category") or "").lower()
                or ql in (x.get("store_name") or "").lower()
            ]
        if cat:
            cl = cat.lower().strip()
            out = [x for x in out if (x.get("category") or "").lower().strip() == cl]

        cats = sorted(list({(x.get("category") or "General").strip() or "General" for x in out}))

        # Most recent first (best-effort)
        def _ts(x):
            return x.get("created_at") or ""
        out.sort(key=_ts, reverse=True)

        return jsonify({
            "ok": True,
            "count": len(out),
            "categories": cats,
            "products": out[:limit]
        })
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500

# -----------------------------
# Manager Setup Wizard (proper onboarding)
# -----------------------------

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
@app.get("/dashboard")
@app.get("/dashboard.html")
@app.get("/login")
@app.get("/seller")
@app.get("/driver")
def dashboard():
    # Always serve the same dashboard shell; role-based UI happens client-side.
    return send_from_directory(app.static_folder, "dashboard.html")
@app.route("/api/app/dashboard", methods=["GET"])
@app.route("/api/dashboard", methods=["GET"])
def dashboards():
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
        "created_at": _now_dt(),

        # Live store publishing
        "published": False,
        "public_slug": None,
        "published_at": None,

        # Billing gate (Stripe payment links); if billing is disabled, stores are active by default
        "billing_active": (str(os.environ.get("BILLING_ENABLED", "0")).strip() not in ("1","true","True","yes","YES")),
        "billing_plan": None,

        # Payments config per store
        "payments": {
            "mode": "demo",              # demo | live
            "provider": "demo",          # demo | stripe_connect | stripe_key | yoco
            "stripe": {
                "account_id": None,
                "onboarded": False,
                "last_sync_at": None
            },
            "yoco": {
                "public_key": None,
                "mode": None
            }
        }
    }
    try:
        db = get_db()
        db.stores.insert_one(store_doc)
        return jsonify({"ok": True, "store_id": internal_id}), 201
    except mongo_errors.PyMongoError as e:
        return jsonify({"ok": False, "error": "db_write_failed", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500



# ---------------- STORE PAYMENTS (MANAGER) --------------
@app.route("/manager/api/store/<store_id>/payments", methods=["POST"])
@app.route("/api/app/manager/api/store/<store_id>/payments", methods=["POST"])
def manager_set_store_payments(store_id):
    """
    Configure store payment provider + mode.
    Body:
      { mode: "demo"|"live", provider: "demo"|"stripe_connect"|"stripe_key"|"yoco" }
    Notes:
      - We strongly recommend stripe_connect so you do NOT store any merchant API keys.
      - For yoco, we only store public key (client-side). Server-side charging requires secret key (not implemented here).
    """
    if not require_admin():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    db = get_db()
    s = db.stores.find_one({"_internal_id": store_id})
    if not s:
        return jsonify({"ok": False, "error": "store_not_found"}), 404

    body = request.get_json(silent=True) or {}
    mode = (body.get("mode") or "demo").lower().strip()
    provider = (body.get("provider") or ("stripe_connect" if mode == "live" else "demo")).lower().strip()

    if mode not in ("demo", "live"):
        return jsonify({"ok": False, "error": "bad_mode"}), 400
    if provider not in ("demo", "stripe_connect", "stripe_key", "yoco"):
        return jsonify({"ok": False, "error": "bad_provider"}), 400

    p = _store_payment_cfg(s)
    p["mode"] = mode
    p["provider"] = provider

    # If switching away from demo, keep stripe connect flags but don't auto-onboard
    if provider != "stripe_connect":
        # Keep stripe block but mark not onboarded unless they come back
        p.setdefault("stripe", {})
        if provider == "stripe_key":
            # We do NOT store merchant keys. Provide instructions to use Connect instead.
            p["stripe"].setdefault("note", "Use Stripe Connect onboarding; do not paste API keys into this app.")
        if provider == "yoco":
            p.setdefault("yoco", {})
            p["yoco"]["public_key"] = (body.get("yoco_public_key") or p["yoco"].get("public_key"))

    db.stores.update_one({"_internal_id": store_id}, {"$set": {"payments": p}})
    return jsonify({"ok": True, "store_id": store_id, "payments": p}), 200


@app.route("/manager/api/store/<store_id>/stripe/connect/start", methods=["POST"])
@app.route("/api/app/manager/api/store/<store_id>/stripe/connect/start", methods=["POST"])
def manager_stripe_connect_start(store_id):
    """
    Starts Stripe Connect onboarding for this store.
    Returns an onboarding URL (Stripe Account Link).
    Requires STRIPE_PLATFORM_SECRET_KEY set.
    """
    if not require_admin():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    if not stripe:
        return jsonify({"ok": False, "error": "stripe_not_installed"}), 400
    if not _stripe_platform_ready() or not _stripe_platform_init():
        return jsonify({"ok": False, "error": "stripe_platform_not_configured"}), 400

    db = get_db()
    s = db.stores.find_one({"_internal_id": store_id})
    if not s:
        return jsonify({"ok": False, "error": "store_not_found"}), 404

    p = _store_payment_cfg(s)
    p["mode"] = "live"
    p["provider"] = "stripe_connect"
    p.setdefault("stripe", {})

    acct = p["stripe"].get("account_id")
    if not acct:
        # Express account is simplest for operators
        acct_obj = stripe.Account.create(
            type="express",
            country=(s.get("country") or "ZA"),
            business_type="individual",
            capabilities={
                "card_payments": {"requested": True},
                "transfers": {"requested": True}
            },
            metadata={"store_id": store_id}
        )
        acct = acct_obj.get("id")
        p["stripe"]["account_id"] = acct
        p["stripe"]["onboarded"] = False
        db.stores.update_one({"_internal_id": store_id}, {"$set": {"payments": p}})

    refresh_url = STRIPE_CONNECT_REFRESH_URL or (request.url_root.rstrip("/") + "/manager_setup.html")
    return_url  = STRIPE_CONNECT_RETURN_URL  or (request.url_root.rstrip("/") + "/manager_dashboard.html")

    link = stripe.AccountLink.create(
        account=acct,
        refresh_url=refresh_url,
        return_url=return_url,
        type="account_onboarding"
    )
    return jsonify({"ok": True, "account_id": acct, "onboarding_url": link.get("url")}), 200


@app.route("/manager/api/store/<store_id>/stripe/connect/status", methods=["GET"])
@app.route("/api/app/manager/api/store/<store_id>/stripe/connect/status", methods=["GET"])
def manager_stripe_connect_status(store_id):
    if not require_admin():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    if not stripe or not _stripe_platform_ready() or not _stripe_platform_init():
        return jsonify({"ok": False, "error": "stripe_platform_not_configured"}), 400

    db = get_db()
    s = db.stores.find_one({"_internal_id": store_id})
    if not s:
        return jsonify({"ok": False, "error": "store_not_found"}), 404
    acct = _stripe_connect_account_id(s)
    if not acct:
        return jsonify({"ok": True, "connected": False}), 200

    try:
        acct_obj = stripe.Account.retrieve(acct)
        details_submitted = bool(acct_obj.get("details_submitted"))
        charges_enabled = bool(acct_obj.get("charges_enabled"))
        payouts_enabled = bool(acct_obj.get("payouts_enabled"))
        onboarded = bool(details_submitted and charges_enabled)

        p = _store_payment_cfg(s)
        p.setdefault("stripe", {})
        p["stripe"]["onboarded"] = onboarded
        db.stores.update_one({"_internal_id": store_id}, {"$set": {"payments": p}})

        return jsonify({"ok": True, "connected": True, "account_id": acct,
                        "details_submitted": details_submitted,
                        "charges_enabled": charges_enabled,
                        "payouts_enabled": payouts_enabled,
                        "onboarded": onboarded}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": "stripe_error", "details": str(e)}), 400


@app.route("/manager/api/store/<store_id>/stripe/sync_products", methods=["POST"])
@app.route("/api/app/manager/api/store/<store_id>/stripe/sync_products", methods=["POST"])
def manager_stripe_sync_products(store_id):
    """
    Sync store_items -> Stripe Products/Prices (on the CONNECTED account).
    Stores stripe_product_id and stripe_price_id on each store item.
    Requires store payments provider stripe_connect and live mode.
    """
    if not require_admin():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    if not stripe or not _stripe_platform_ready() or not _stripe_platform_init():
        return jsonify({"ok": False, "error": "stripe_platform_not_configured"}), 400

    db = get_db()
    s = db.stores.find_one({"_internal_id": store_id})
    if not s:
        return jsonify({"ok": False, "error": "store_not_found"}), 404

    p = _store_payment_cfg(s)
    if (p.get("provider") or "").lower() != "stripe_connect" or (p.get("mode") or "").lower() != "live":
        return jsonify({"ok": False, "error": "store_not_in_stripe_connect_live"}), 400

    acct = _stripe_connect_account_id(s)
    if not acct:
        return jsonify({"ok": False, "error": "stripe_connect_not_started"}), 400

    items = list(db.store_items.find({"store_id": store_id, "active": True}).limit(5000))
    synced = 0
    for it in items:
        try:
            name = (it.get("name") or "").strip()
            if not name:
                continue
            price = float(it.get("price") or 0)
            if price <= 0:
                continue
            currency = (s.get("currency") or "ZAR").lower()

            stripe_product_id = it.get("stripe_product_id")
            stripe_price_id = it.get("stripe_price_id")

            if not stripe_product_id:
                prod = stripe.Product.create(
                    name=name[:200],
                    metadata={"store_id": store_id, "item_id": it["_internal_id"]},
                    stripe_account=acct
                )
                stripe_product_id = prod.get("id")

            # Always ensure there is a price (recreate if missing)
            if not stripe_price_id:
                pr = stripe.Price.create(
                    product=stripe_product_id,
                    unit_amount=int(round(price * 100)),
                    currency=currency,
                    stripe_account=acct
                )
                stripe_price_id = pr.get("id")

            db.store_items.update_one(
                {"_internal_id": it["_internal_id"]},
                {"$set": {"stripe_product_id": stripe_product_id, "stripe_price_id": stripe_price_id}}
            )
            synced += 1
        except Exception:
            # skip bad item; keep syncing others
            continue

    p.setdefault("stripe", {})
    p["stripe"]["last_sync_at"] = _now_iso()
    db.stores.update_one({"_internal_id": store_id}, {"$set": {"payments": p}})
    return jsonify({"ok": True, "synced": synced, "total_items": len(items)}), 200


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
    
    
    
# ============================================================
# YiThume SaaS Add-on (Non-breaking, new routes + new collections)
# Prefix: /v1/...
# Collections: saas_stores, saas_products, saas_orders
#
# Requires: get_db(), _now_dt(), _now_iso(), make_order_public_id()
# ============================================================



# -----------------------------
# Helpers
# -----------------------------
def _col(db, name: str):
    return db[name]

def _safe_float(x, default=0.0):
    try:
        return float(str(x).strip())
    except Exception:
        return float(default)

def _safe_int(x, default=0):
    try:
        return int(float(str(x).strip()))
    except Exception:
        return int(default)

def _str(x, default=""):
    s = str(x) if x is not None else default
    return s.strip()

def _slug(s: str):
    s = _str(s).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "item"

def _ensure_saas_indexes(db):
    try:
        _col(db, "saas_stores").create_index([("_internal_id", ASCENDING)], unique=True)
        _col(db, "saas_stores").create_index([("created_at", DESCENDING)])

        _col(db, "saas_products").create_index([("store_id", ASCENDING), ("active", ASCENDING)])
        _col(db, "saas_products").create_index([("store_id", ASCENDING), ("sku", ASCENDING)])
        _col(db, "saas_products").create_index([("store_id", ASCENDING), ("name", ASCENDING)])
        _col(db, "saas_products").create_index([("store_id", ASCENDING), ("category", ASCENDING)])

        _col(db, "saas_orders").create_index([("store_id", ASCENDING), ("created_at", DESCENDING)])
        _col(db, "saas_orders").create_index([("order_id", ASCENDING)], unique=True)
        _col(db, "saas_orders").create_index([("status", ASCENDING), ("created_at", DESCENDING)])
    except Exception:
        pass

try:
    _ensure_saas_indexes(get_db())
except Exception:
    pass

def _get_store(db, store_id: str):
    return _col(db, "saas_stores").find_one({"_internal_id": store_id})

def _store_public(doc):
    if not doc:
        return None
    out = dict(doc)
    out.pop("_id", None)
    if isinstance(out.get("created_at"), datetime):
        out["created_at"] = out["created_at"].isoformat() + "Z"
    return out

def _product_public(p):
    if not p:
        return None
    out = dict(p)
    out.pop("_id", None)
    if isinstance(out.get("created_at"), datetime):
        out["created_at"] = out["created_at"].isoformat() + "Z"
    return out

def _order_public(o):
    if not o:
        return None
    out = dict(o)
    out.pop("_id", None)
    for k in ("created_at", "confirmed_at"):
        if isinstance(out.get(k), datetime):
            out[k] = out[k].isoformat() + "Z"
    return out


# -----------------------------
# Store creation (wizard step 1)
# -----------------------------
@app.post("/v1/store/create")
@app.post("/api/v1/store/create")
def v1_store_create():
    """
    Creates a store with delivery + payment config.
    Body JSON example:
    {
      "name":"Port Alfred Pharmacy",
      "template":"pharmacy",
      "currency":"ZAR",
      "address": {"line1":"12 High St", "city":"Port Alfred", "province":"EC", "postal":"6170", "country":"ZA"},
      "delivery": {"mode":"manual", "fee_flat": 35},
      "payment": {"method":"eft"}  // or "payfast" later
    }
    """
    db = get_db()
    data = request.get_json(silent=True) or {}

    name = _str(data.get("name"), "New Store")
    template = _str(data.get("template"), "general")
    currency = _str(data.get("currency"), "ZAR")

    address = data.get("address") if isinstance(data.get("address"), dict) else {}
    delivery = data.get("delivery") if isinstance(data.get("delivery"), dict) else {}
    payment = data.get("payment") if isinstance(data.get("payment"), dict) else {}

    store_id = str(uuid.uuid4())

    doc = {
        "_internal_id": store_id,
        "name": name,
        "template": template,
        "currency": currency,
        "address": {
            "line1": _str(address.get("line1")),
            "line2": _str(address.get("line2")),
            "city": _str(address.get("city")),
            "province": _str(address.get("province")),
            "postal": _str(address.get("postal")),
            "country": _str(address.get("country"), "ZA"),
        },
        "delivery": {
            "mode": _str(delivery.get("mode"), "manual"),  # manual | external | yithume
            "fee_flat": _safe_float(delivery.get("fee_flat"), 35.0),
            "notes": _str(delivery.get("notes")),
        },
        "payment": {
            "method": _str(payment.get("method"), "eft"),   # eft | payfast | ozow | yoco (later)
            "notes": _str(payment.get("notes")),
        },
        "active": True,
        "created_at": _now_dt(),
    }

    _col(db, "saas_stores").insert_one(doc)

    return jsonify({
        "ok": True,
        "store": _store_public(doc),
        "storefront_url": f"/s/{store_id}",
        "dashboard_url": f"/dashboard?store_id={quote(store_id)}"
    })


# -----------------------------
# CSV import (wizard step 2)
# -----------------------------
@app.post("/v1/store/<store_id>/import_csv")
@app.post("/api/v1/store/<store_id>/import_csv")
def v1_store_import_csv(store_id):
    """
    Multipart form-data:
      - file: CSV
    CSV columns required:
      name, price
    Optional:
      category, sku, stock, description, image_url
    """
    db = get_db()
    store = _get_store(db, store_id)
    if not store:
        return jsonify({"ok": False, "error": "Store not found"}), 404

    if "file" not in request.files:
        return jsonify({"ok": False, "error": "Missing file"}), 400

    f = request.files["file"]
    raw = f.read()
    try:
        text = raw.decode("utf-8-sig", errors="ignore")
    except Exception:
        text = raw.decode("latin-1", errors="ignore")

    reader = csv.DictReader(io.StringIO(text))
    inserted = 0
    updated = 0
    skipped = 0

    for row in reader:
        name = _str(row.get("name"))
        price = _safe_float(row.get("price"), None)

        if not name or price is None:
            skipped += 1
            continue

        category = _str(row.get("category"), "General")
        sku = _str(row.get("sku"))
        stock = _safe_int(row.get("stock"), 999999)
        desc = _str(row.get("description"))
        image_url = _str(row.get("image_url"))

        key = {"store_id": store_id, "sku": sku} if sku else {"store_id": store_id, "name": name, "category": category}
        existing = _col(db, "saas_products").find_one(key)

        base_doc = {
            "store_id": store_id,
            "name": name,
            "slug": _slug(name),
            "category": category,
            "sku": sku or None,
            "price": float(price),
            "stock": int(stock),
            "description": desc,
            "image_url": image_url,
            "active": True,
            "updated_at": _now_dt(),
        }

        if existing:
            _col(db, "saas_products").update_one({"_id": existing["_id"]}, {"$set": base_doc})
            updated += 1
        else:
            base_doc["_internal_id"] = str(uuid.uuid4())
            base_doc["created_at"] = _now_dt()
            _col(db, "saas_products").insert_one(base_doc)
            inserted += 1

    return jsonify({
        "ok": True,
        "store_id": store_id,
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "storefront_url": f"/s/{store_id}"
    })


# -----------------------------
# Public storefront (simple + nice)
# GET /s/<store_id>?q=panado&cat=Pain%20Relief
# -----------------------------
@app.get("/s/<store_id>")
def s_storefront(store_id):
    db = get_db()
    store = _get_store(db, store_id)
    if not store:
        return Response("Store not found", status=404)

    q = _str(request.args.get("q"))
    cat = _str(request.args.get("cat"))

    query = {"store_id": store_id, "active": True}
    if cat:
        query["category"] = cat

    products = list(_col(db, "saas_products").find(query).sort("name", 1).limit(2000))

    # basic search filter in python (safe MVP)
    if q:
        ql = q.lower()
        products = [p for p in products if ql in _str(p.get("name")).lower() or ql in _str(p.get("category")).lower()]

    # categories
    cats = sorted(list(set([_str(p.get("category"), "General") for p in products])))

    # lightweight HTML (no templates, no breakage)
    # Checkout action = WhatsApp message (works now). Later: embedded checkout.
    wa_text = f"Hi! I want to order from {store.get('name')}. My items: "
    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{store.get('name')} — Store</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-zinc-950 text-zinc-100">
  <div class="sticky top-0 z-20 border-b border-zinc-900 bg-zinc-950/80 backdrop-blur">
    <div class="max-w-6xl mx-auto px-4 py-4 flex items-center justify-between">
      <div class="flex items-center gap-3 min-w-0">
        <div class="w-10 h-10 rounded-2xl bg-emerald-600/15 border border-emerald-600/30 flex items-center justify-center font-bold shrink-0">Y</div>
        <div class="min-w-0">
          <div class="text-xs text-zinc-400">YiThume Storefront</div>
          <div class="text-lg font-semibold truncate">{store.get('name')}</div>
          <div class="text-xs text-zinc-500 truncate">{_str(store.get('address',{}).get('line1'))} · {_str(store.get('address',{}).get('city'))}</div>
        </div>
      </div>
      <a href="/dashboard?store_id={quote(store_id)}"
         class="px-3 py-2 rounded-xl bg-zinc-900 border border-zinc-800 hover:bg-zinc-800 text-sm">Admin</a>
    </div>
  </div>

  <div class="max-w-6xl mx-auto px-4 py-6">
    <div class="rounded-3xl border border-zinc-800 bg-zinc-900/40 p-5">
      <div class="flex flex-col md:flex-row md:items-end md:justify-between gap-4">
        <div>
          <div class="text-2xl font-semibold">Browse & order</div>
          <div class="text-sm text-zinc-400 mt-2">Add items, then send your order for a quote. You will only be charged after you reply <b>CONFIRM</b>.</div>
        </div>
        <div class="flex gap-2">
          <form class="flex gap-2" method="get" action="/s/{store_id}">
            <input name="q" value="{q}" placeholder="Search products..."
              class="px-3 py-2 rounded-xl bg-zinc-950/50 border border-zinc-800 text-sm w-48" />
            <select name="cat" class="px-3 py-2 rounded-xl bg-zinc-950/50 border border-zinc-800 text-sm">
              <option value="">All categories</option>
              {''.join([f'<option value="{c}" {"selected" if c==cat else ""}>{c}</option>' for c in cats])}
            </select>
            <button class="px-3 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-sm font-semibold">Go</button>
          </form>
        </div>
      </div>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-12 gap-4 mt-6">
      <div class="lg:col-span-8">
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
          {"".join([f'''
          <div class="rounded-3xl border border-zinc-800 bg-zinc-900/40 p-4">
            <div class="font-semibold">{_str(p.get("name"))}</div>
            <div class="text-xs text-zinc-400 mt-1">{_str(p.get("category"), "General")}</div>
            <div class="text-lg font-semibold mt-3">{store.get("currency","ZAR")} {_safe_float(p.get("price"),0):.2f}</div>
            <div class="text-sm text-zinc-400 mt-2 line-clamp-2">{_str(p.get("description"))}</div>
            <div class="mt-3 flex items-center gap-2">
              <input type="number" min="1" value="1" class="qty px-3 py-2 rounded-xl bg-zinc-950/50 border border-zinc-800 text-sm w-20" data-name="{_str(p.get("name"))}" />
              <button class="add px-3 py-2 rounded-xl bg-zinc-950/60 border border-zinc-800 hover:bg-zinc-900 text-sm"
                data-name="{_str(p.get("name"))}" data-price="{_safe_float(p.get("price"),0)}">Add</button>
            </div>
          </div>
          ''' for p in products[:60]])}
        </div>
        <div class="text-xs text-zinc-500 mt-4">Showing {len(products[:60])} of {len(products)} products.</div>
      </div>

      <div class="lg:col-span-4">
        <div class="sticky top-24 rounded-3xl border border-zinc-800 bg-zinc-900/40 p-4">
          <div class="text-lg font-semibold">Your cart</div>
          <div id="cart" class="mt-3 space-y-2 text-sm text-zinc-300"></div>
          <div class="mt-3 border-t border-zinc-800 pt-3 flex items-center justify-between">
            <div class="text-sm text-zinc-400">Subtotal</div>
            <div id="subtotal" class="text-lg font-semibold">0.00</div>
          </div>

          <button id="quoteBtn" class="mt-4 w-full px-4 py-3 rounded-2xl bg-emerald-600 hover:bg-emerald-500 font-semibold">
            Get quote (requires CONFIRM)
          </button>

          <div id="quoteOut" class="mt-3 text-sm text-zinc-300"></div>

          <div class="mt-4 rounded-2xl border border-emerald-600/30 bg-emerald-600/10 p-3 text-sm">
            <b>Confirm gating:</b> we only create orders after you reply <b>CONFIRM</b>.
          </div>
        </div>
      </div>
    </div>
  </div>

<script>
(() => {{
  const storeId = "{store_id}";
  const cart = {{}};

  function render() {{
    const el = document.getElementById("cart");
    el.innerHTML = "";
    let subtotal = 0;

    Object.keys(cart).forEach(name => {{
      const it = cart[name];
      subtotal += it.price * it.qty;
      const row = document.createElement("div");
      row.className = "flex items-center justify-between rounded-xl explain bg-zinc-950/40 border border-zinc-800 p-2";
      row.innerHTML = `
        <div class="min-w-0">
          <div class="font-medium truncate">${{name}}</div>
          <div class="text-xs text-zinc-400">${{it.qty}} × ${{it.price.toFixed(2)}}</div>
        </div>
        <button class="text-xs px-2 py-1 rounded-lg bg-zinc-900 border border-zinc-800 hover:bg-zinc-800" data-del="${{name}}">Remove</button>
      `;
      el.appendChild(row);
    }});

    document.getElementById("subtotal").textContent = subtotal.toFixed(2);

    el.querySelectorAll("button[data-del]").forEach(b => {{
      b.addEventListener("click", () => {{
        delete cart[b.getAttribute("data-del")];
        render();
      }});
    }});
  }}

  document.querySelectorAll("button.add").forEach(btn => {{
    btn.addEventListener("click", () => {{
      const name = btn.getAttribute("data-name");
      const price = parseFloat(btn.getAttribute("data-price") || "0");
      const qtyEl = document.querySelector('input.qty[data-name="'+name.replace(/"/g,'\\"')+'"]');
      const qty = qtyEl ? parseInt(qtyEl.value || "1") : 1;
      if (!cart[name]) cart[name] = {{name, price, qty: 0}};
      cart[name].qty += Math.max(1, qty);
      render();
    }});
  }});

  document.getElementById("quoteBtn").addEventListener("click", async () => {{
    const items = Object.keys(cart).map(name => ({{ name, qty: cart[name].qty }}));
    if (!items.length) {{
      document.getElementById("quoteOut").textContent = "Add items first.";
      return;
    }}

    const res = await fetch("/v1/order/quote", {{
      method: "POST",
      headers: {{"Content-Type":"application/json"}},
      body: JSON.stringify({{
        store_id: storeId,
        customer: {{}},
        items
      }})
    }});
    const out = await res.json().catch(() => null);

    if (!out || !out.ok) {{
      document.getElementById("quoteOut").textContent = out?.error || "Quote failed.";
      return;
    }}

    document.getElementById("quoteOut").innerHTML = `
      <div class="rounded-2xl border border-zinc-800 bg-zinc-950/40 p-3">
        <div class="font-semibold">Quote</div>
        <div class="text-sm text-zinc-300 mt-2">${{out.quote_text.replace(/\\n/g,"<br>")}}</div>
        <div class="text-xs text-zinc-400 mt-2">To place the order: send <b>CONFIRM</b> (WhatsApp) or click confirm below (web demo).</div>
        <button id="confirmBtn" class="mt-3 w-full px-3 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-sm font-semibold">CONFIRM order</button>
      </div>
    `;

    document.getElementById("confirmBtn").addEventListener("click", async () => {{
      const res2 = await fetch("/v1/order/confirm", {{
        method: "POST",
        headers: {{"Content-Type":"application/json"}},
        body: JSON.stringify({{
          store_id: storeId,
          confirm: "CONFIRM",
          quote_id: out.quote_id,
          customer: {{}}
        }})
      }});
      const out2 = await res2.json().catch(() => null);
      if (!out2 || !out2.ok) {{
        document.getElementById("quoteOut").textContent = out2?.error || "Confirm failed.";
        return;
      }}
      document.getElementById("quoteOut").innerHTML = `
        <div class="rounded-2xl border border-emerald-600/30 bg-emerald-600/10 p-3">
          <div class="font-semibold">Order created</div>
          <div class="text-sm mt-2">Order ID: <b>${{out2.order_id}}</b></div>
          <div class="text-sm mt-1">Status: ${{out2.status}}</div>
        </div>
      `;
    }});
  }});
}})();
</script>
</body>
</html>
"""
    return Response(html, mimetype="text/html")


# -----------------------------
# Quote (build preview; does NOT create order)
# -----------------------------
@app.post("/v1/order/quote")
@app.post("/api/v1/order/quote")
def v1_order_quote():
    db = get_db()
    data = request.get_json(silent=True) or {}

    store_id = _str(data.get("store_id"))
    if not store_id:
        return jsonify({"ok": False, "error": "Missing store_id"}), 400
    store = _get_store(db, store_id)
    if not store:
        return jsonify({"ok": False, "error": "Store not found"}), 404

    items = data.get("items") if isinstance(data.get("items"), list) else []
    if not items:
        return jsonify({"ok": False, "error": "No items"}), 400

    resolved = []
    subtotal = 0.0

    for it in items:
        name = _str((it or {}).get("name"))
        qty = _safe_int((it or {}).get("qty"), 1)
        if not name:
            continue

        # MVP: "contains" match
        q = {"store_id": store_id, "active": True, "name": {"$regex": re.escape(name), "$options": "i"}}
        prod = _col(db, "saas_products").find_one(q)
        if not prod:
            # try looser: first token
            token = re.split(r"\s+", name)[0]
            q2 = {"store_id": store_id, "active": True, "name": {"$regex": re.escape(token), "$options": "i"}}
            prod = _col(db, "saas_products").find_one(q2)

        if not prod:
            resolved.append({"name": name, "qty": qty, "found": False})
            continue

        price = float(prod.get("price", 0))
        line = price * qty
        subtotal += line
        resolved.append({
            "name": prod.get("name"),
            "sku": prod.get("sku"),
            "category": prod.get("category"),
            "price": price,
            "qty": qty,
            "line_total": round(line, 2),
            "found": True
        })

    delivery_fee = float((store.get("delivery") or {}).get("fee_flat", 35.0))
    total = round(subtotal + delivery_fee, 2)

    quote_id = str(uuid.uuid4())
    quote_doc = {
        "_internal_id": quote_id,
        "store_id": store_id,
        "resolved": resolved,
        "subtotal": round(subtotal, 2),
        "delivery_fee": round(delivery_fee, 2),
        "total": total,
        "created_at": _now_dt(),
        "expires_at": _now_dt() + timedelta(minutes=30),
        "status": "quoted"
    }
    # store quotes in saas_orders as "quote" (MVP) OR create separate collection; using orders keeps it simple
    _col(db, "saas_orders").insert_one({
        "_internal_id": str(uuid.uuid4()),
        "order_id": f"Q-{quote_id[:8].upper()}",
        "store_id": store_id,
        "type": "quote",
        "quote_id": quote_id,
        "items": resolved,
        "subtotal": quote_doc["subtotal"],
        "delivery_fee": quote_doc["delivery_fee"],
        "total": quote_doc["total"],
        "status": "quoted",
        "created_at": quote_doc["created_at"],
        "expires_at": quote_doc["expires_at"]
    })

    # WhatsApp-style quote text
    lines = []
    lines.append(f"{store.get('name')} — Quote")
    for r in resolved:
        if not r.get("found"):
            lines.append(f"- {r.get('name')} x{r.get('qty')} (not found)")
        else:
            lines.append(f"- {r.get('name')} x{r.get('qty')} = {store.get('currency','ZAR')} {r.get('line_total'):.2f}")
    lines.append(f"Delivery: {store.get('currency','ZAR')} {delivery_fee:.2f}")
    lines.append(f"Total: {store.get('currency','ZAR')} {total:.2f}")
    lines.append("Reply CONFIRM to place the order.")

    return jsonify({
        "ok": True,
        "quote_id": quote_id,
        "resolved": resolved,
        "subtotal": round(subtotal, 2),
        "delivery_fee": round(delivery_fee, 2),
        "total": total,
        "quote_text": "\n".join(lines)
    })


# -----------------------------
# CONFIRM gating (creates order ONLY if confirm == "CONFIRM")
# -----------------------------
@app.post("/v1/order/confirm")
@app.post("/api/v1/order/confirm")
def v1_order_confirm():
    db = get_db()
    data = request.get_json(silent=True) or {}

    store_id = _str(data.get("store_id"))
    confirm = _str(data.get("confirm"))
    quote_id = _str(data.get("quote_id"))

    if not store_id:
        return jsonify({"ok": False, "error": "Missing store_id"}), 400
    store = _get_store(db, store_id)
    if not store:
        return jsonify({"ok": False, "error": "Store not found"}), 404

    # HARD RULE
    if confirm.upper() != "CONFIRM":
        return jsonify({"ok": False, "error": "Order not created. Send CONFIRM to place order."}), 400

    if not quote_id:
        return jsonify({"ok": False, "error": "Missing quote_id"}), 400

    # find latest quote record
    qdoc = _col(db, "saas_orders").find_one({"store_id": store_id, "type": "quote", "quote_id": quote_id})
    if not qdoc:
        return jsonify({"ok": False, "error": "Quote not found"}), 404

    exp = qdoc.get("expires_at")
    if isinstance(exp, datetime) and exp < _now_dt():
        return jsonify({"ok": False, "error": "Quote expired"}), 400

    # create order
    order_id = make_order_public_id()
    customer = data.get("customer") if isinstance(data.get("customer"), dict) else {}

    order_doc = {
        "_internal_id": str(uuid.uuid4()),
        "order_id": order_id,
        "store_id": store_id,
        "type": "order",
        "items": qdoc.get("items", []),
        "subtotal": float(qdoc.get("subtotal", 0)),
        "delivery_fee": float(qdoc.get("delivery_fee", 0)),
        "total": float(qdoc.get("total", 0)),
        "currency": store.get("currency", "ZAR"),
        "customer": {
            "name": _str(customer.get("name")),
            "phone": _str(customer.get("phone")),
            "address": customer.get("address") if isinstance(customer.get("address"), dict) else {}
        },
        "status": "queued",   # queued -> dispatched -> delivered
        "created_at": _now_dt(),
        "confirmed_at": _now_dt(),
        "source": _str(data.get("source"), "web"),
        "payment": {"method": (store.get("payment") or {}).get("method", "eft"), "status": "unpaid"},
        "delivery": {"mode": (store.get("delivery") or {}).get("mode", "manual"), "pickup": store.get("address")},
    }

    

    # Create a logistics order record so drivers can see/accept/complete deliveries.
    try:
        lo = {
            "_internal_id": str(uuid.uuid4()),
            "order_id": order_id,
            "store_id": store_id,
            "customer": order_doc.get("customer"),
            "items": order_doc.get("items"),
            "subtotal": order_doc.get("subtotal"),
            "delivery_fee": order_doc.get("delivery_fee"),
            "total": order_doc.get("total"),
            "currency": order_doc.get("currency"),
            "status": "queued",
            "created_at": _now_dt(),
            "assigned_driver_id": None,
            "source": order_doc.get("source") or "marketplace"
        }
        db.orders.insert_one(lo)
    except Exception:
        pass

    # Attach payment info (optional)
    pay = data.get("payment") if isinstance(data.get("payment"), dict) else {}
    payment_mode = _str(pay.get("mode") or data.get("payment_mode") or "demo")
    payment_provider = _str(pay.get("provider") or data.get("payment_provider") or "demo")
    order_doc["payment"] = {
        "mode": payment_mode,
        "provider": payment_provider,
        "status": "demo_pending" if payment_mode != "live" else "pending"
    }

_col(db, "saas_orders").insert_one(order_doc)

    # optionally mark the quote "converted"
    try:
        _col(db, "saas_orders").update_one({"_id": qdoc["_id"]}, {"$set": {"status": "converted", "converted_order_id": order_id}})
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "order_id": order_id,
        "status": order_doc["status"]
    })


# -----------------------------
# Orders list (dashboard JS)
# -----------------------------
@app.get("/v1/store/<store_id>/orders")
@app.get("/api/v1/store/<store_id>/orders")
def v1_store_orders(store_id):
    db = get_db()
    store = _get_store(db, store_id)
    if not store:
        return jsonify({"ok": False, "error": "Store not found"}), 404

    limit = _safe_int(request.args.get("limit"), 50)
    docs = list(_col(db, "saas_orders").find({"store_id": store_id, "type": "order"}).sort("created_at", -1).limit(max(1, min(limit, 200))))

    return jsonify({
        "ok": True,
        "store": _store_public(store),
        "orders": [_order_public(o) for o in docs]
    })


# =============================
# LIVE STORE PUBLISHING (Go Live)
# =============================

def _slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "store"

def _billing_enabled() -> bool:
    return str(os.environ.get("BILLING_ENABLED", "0")).strip().lower() in ("1", "true", "yes")

def _store_public_url(public_slug: str) -> str:
    # Served by this same Flask app (vercel routes everything here)
    return f"/shop/{public_slug}"

@app.route("/v1/store/<store_id>/status", methods=["GET"])
@app.route("/api/v1/store/<store_id>/status", methods=["GET"])
@app.route("/api/app/v1/store/<store_id>/status", methods=["GET"])
def store_status(store_id):
    try:
        db = get_db()
        s = db.stores.find_one({"_internal_id": store_id})
        if not s:
            return jsonify({"ok": False, "error": "store_not_found"}), 404
        count = db.store_items.count_documents({"store_id": store_id, "active": True})
        return jsonify({
            "ok": True,
            "store_id": store_id,
            "name": s.get("name"),
            "products_count": int(count),
            "published": bool(s.get("published")),
            "public_slug": s.get("public_slug"),
            "public_url": _store_public_url(s["public_slug"]) if s.get("public_slug") else None,
            "billing_enabled": _billing_enabled(),
            "billing_active": bool(s.get("billing_active", not _billing_enabled())),
            "billing_plan": s.get("billing_plan")
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500

@app.route("/v1/store/<store_id>/activate", methods=["POST"])
@app.route("/api/v1/store/<store_id>/activate", methods=["POST"])
@app.route("/api/app/v1/store/<store_id>/activate", methods=["POST"])
def store_activate_billing(store_id):
    # Admin-only activation (Stripe payment links use-case)
    if not require_admin():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    if not _billing_enabled():
        return jsonify({"ok": True, "billing_active": True, "note": "billing_disabled"}), 200

    body = request.get_json(silent=True) or {}
    code = (body.get("activation_code") or "").strip()
    expected = (os.environ.get("BILLING_ACTIVATION_CODE") or "").strip()
    if not expected:
        return jsonify({"ok": False, "error": "billing_activation_code_not_configured"}), 500
    if code != expected:
        return jsonify({"ok": False, "error": "invalid_activation_code"}), 403

    plan = (body.get("plan") or "basic").strip().lower()
    if plan not in ("basic", "agent", "node"):
        plan = "basic"

    try:
        db = get_db()
        res = db.stores.update_one(
            {"_internal_id": store_id},
            {"$set": {"billing_active": True, "billing_plan": plan}}
        )
        if res.matched_count == 0:
            return jsonify({"ok": False, "error": "store_not_found"}), 404
        return jsonify({"ok": True, "billing_active": True, "billing_plan": plan}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500

@app.route("/v1/store/<store_id>/publish", methods=["POST"])
@app.route("/api/v1/store/<store_id>/publish", methods=["POST"])
@app.route("/api/app/v1/store/<store_id>/publish", methods=["POST"])
def store_publish(store_id):
    try:
        db = get_db()
        s = db.stores.find_one({"_internal_id": store_id})
        if not s:
            return jsonify({"ok": False, "error": "store_not_found"}), 404

        if _billing_enabled() and not bool(s.get("billing_active")):
            return jsonify({
                "ok": False,
                "error": "payment_required",
                "billing": {
                    "stripe": {
                        "basic": os.environ.get("BILLING_LINK_BASIC"),
                        "agent": os.environ.get("BILLING_LINK_AGENT"),
                        "node": os.environ.get("BILLING_LINK_NODE"),
                    },
                    "coming_soon": ["payfast", "ozow", "yoco"]
                }
            }), 402

        if s.get("published") and s.get("public_slug"):
            return jsonify({"ok": True, "public_url": _store_public_url(s["public_slug"]), "already_published": True}), 200

        base = _slugify(s.get("name") or "store")
        slug = f"{base}-{store_id[:6]}"
        # Ensure uniqueness
        i = 1
        while db.stores.find_one({"public_slug": slug, "_internal_id": {"$ne": store_id}}):
            i += 1
            slug = f"{base}-{store_id[:6]}-{i}"

        db.stores.update_one(
            {"_internal_id": store_id},
            {"$set": {"published": True, "public_slug": slug, "published_at": _now_dt()}}
        )
        return jsonify({"ok": True, "public_url": _store_public_url(slug), "public_slug": slug}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500

@app.route("/shop/<public_slug>", methods=["GET"])
@app.route("/shop/<public_slug>/", methods=["GET"])
@app.route("/api/app/shop/<public_slug>", methods=["GET"])
@app.route("/api/app/shop/<public_slug>/", methods=["GET"])
def shop_public_storefront(public_slug):

    """
    Public live storefront:
      - Works only after store is published
      - If a custom HTML frontend has been uploaded for this store, serve it.
      - Otherwise, fall back to a simple built-in HTML list.
    """
    try:
        # Ensure trailing slash so relative assets like "styles.css" resolve to /shop/<slug>/styles.css
        if not request.path.endswith("/"):
            return redirect(request.path + "/", code=302)
        db = get_db()
        s = db.stores.find_one({"public_slug": public_slug, "published": True})
        if not s:
            return ("Store not found or not live yet.", 404)

        # ---------------------------------------------------------
        # Custom storefront HTML (uploaded by store owner/admin)
        # Stored in GridFS via /v1/store/<store_id>/frontend/upload
        # ---------------------------------------------------------
        frontend = (s.get("frontend") or {})
        html_file_id = frontend.get("html_file_id")
        if html_file_id:
            fs = get_fs(db)
            try:
                g = fs.get(ObjectId(html_file_id))
                html = g.read().decode("utf-8", errors="replace")
            except Exception:
                html = None

            if html:
                # Inject a small config block + helper script tag before </head> if possible.
                # The custom HTML can call window.YITHUME.storefront.* endpoints.
                api_base = (os.environ.get("PUBLIC_API_BASE") or request.url_root.rstrip("/"))
                cfg = f"""
<script>
window.YITHUME = window.YITHUME || {{}};
window.YITHUME.store = {{
  store_id: "{s.get('_internal_id')}",
  public_slug: "{public_slug}",
  api_base: "{api_base}",
  products_url: "{api_base}/v1/storefront/{public_slug}/products",
  quote_url: "{api_base}/v1/storefront/{public_slug}/quote",
  checkout_url: "{api_base}/v1/storefront/{public_slug}/checkout",
  track_url: "{api_base}/v1/storefront/{public_slug}/order/"
}};
</script>
"""
                if "</head>" in html.lower():
                    # do a case-insensitive insertion preserving original casing
                    idx = re.search(r"</head>", html, flags=re.I).start()
                    html = html[:idx] + cfg + html[idx:]
                else:
                    html = cfg + html

                return (html, 200, {"Content-Type": "text/html; charset=utf-8"})

        # ---------------------------------------------------------
        # Fallback: basic HTML storefront (mobile friendly)
        # ---------------------------------------------------------
        items = list(db.store_items.find({"store_id": s["_internal_id"], "active": True}).sort("created_at", -1).limit(500))
        html = []
        html.append("<!doctype html><html><head><meta charset='utf-8'/>")
        html.append("<meta name='viewport' content='width=device-width,initial-scale=1'/>")
        html.append(f"<title>{(s.get('name') or 'Store')}</title>")
        html.append("<style>body{font-family:ui-sans-serif,system-ui,Arial;margin:0;background:#f8fafc;color:#0f172a} .wrap{max-width:900px;margin:0 auto;padding:18px} .card{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:14px;margin:10px 0} .grid{display:grid;grid-template-columns:80px 1fr;gap:12px;align-items:center} img{width:80px;height:80px;object-fit:cover;border-radius:12px;background:#f1f5f9} .price{font-weight:700} .muted{color:#64748b;font-size:13px} .btn{display:inline-block;padding:10px 12px;border-radius:12px;background:#0ea5e9;color:white;text-decoration:none;font-weight:700}</style>")
        html.append("</head><body><div class='wrap'>")
        html.append(f"<h2 style='margin:6px 0 4px'>{s.get('name') or 'Store'}</h2>")
        html.append("<div class='muted'>Live store</div>")
        html.append("<div style='height:12px'></div>")
        if not items:
            html.append("<div class='card'>No products yet.</div>")
        else:
            for it in items:
                name = (it.get("name") or "Item")
                price = it.get("price") or 0
                sku = it.get("sku") or ""
                img = it.get("image_url") or ""
                html.append("<div class='card'><div class='grid'>")
                if img:
                    html.append(f"<img src='{img}' alt='product'/>")
                else:
                    html.append("<img src='data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==' alt='product'/>")
                html.append("<div>")
                html.append(f"<div style='font-weight:800'>{name}</div>")
                if sku:
                    html.append(f"<div class='muted'>SKU: {sku}</div>")
                html.append(f"<div class='price'>R {float(price):.2f}</div>")
                html.append("</div></div></div>")
        html.append("<div style='height:14px'></div>")
        html.append("<div class='card'><div style='font-weight:800;margin-bottom:6px'>Order</div><div class='muted'>Use /v1/storefront/* API to build a checkout UI. This fallback page is just a catalog list.</div></div>")
        html.append("</div></body></html>")
        return ("".join(html), 200, {"Content-Type": "text/html; charset=utf-8"})
    except Exception as e:
        return (f"Server error: {e}", 500)

# -----------------------------
# Product image upload (GridFS)
# -----------------------------
@app.route("/v1/store/<store_id>/product/<product_id>/image", methods=["POST"])
@app.route("/api/v1/store/<store_id>/product/<product_id>/image", methods=["POST"])
@app.route("/api/app/v1/store/<store_id>/product/<product_id>/image", methods=["POST"])
def upload_product_image(store_id, product_id):
    # Admin-only for now (avoid abuse). You can loosen later.
    if not require_admin():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    if "file" not in request.files:
        return jsonify({"ok": False, "error": "file_required"}), 400

    f = request.files["file"]
    if not f or not getattr(f, "filename", ""):
        return jsonify({"ok": False, "error": "file_required"}), 400

    filename = secure_filename(f.filename)
    content = f.read()
    if not content:
        return jsonify({"ok": False, "error": "empty_file"}), 400

    # Basic size limit (2MB) to keep serverless happy
    if len(content) > 2 * 1024 * 1024:
        return jsonify({"ok": False, "error": "file_too_large"}), 413

    try:
        db = get_db()
        if not db.stores.find_one({"_internal_id": store_id}):
            return jsonify({"ok": False, "error": "store_not_found"}), 404

        item = db.store_items.find_one({"_internal_id": product_id, "store_id": store_id})
        if not item:
            return jsonify({"ok": False, "error": "product_not_found"}), 404

        fs = GridFS(db)
        fid = fs.put(content, filename=filename, contentType=getattr(f, "mimetype", "application/octet-stream"))
        image_url = f"/files/{fid}"
        db.store_items.update_one({"_internal_id": product_id}, {"$set": {"image_url": image_url}})
        return jsonify({"ok": True, "image_url": image_url}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500


# ==========================================================
# SELF-UPDATING CODE WORKFLOW (Admin prompt -> PR proposal)
# ==========================================================
# This does NOT merge to main. It only creates a branch + commit (or PR-ready branch)
# after explicit admin approval ("APPROVE"). This keeps production safe.

def _http_json(method: str, url: str, headers: dict, body_obj=None, timeout=20):
    import json
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError

    data = None
    if body_obj is not None:
        data = json.dumps(body_obj).encode("utf-8")
        headers = dict(headers or {})
        headers["Content-Type"] = "application/json"

    req = Request(url, data=data, headers=headers or {}, method=method.upper())
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.getcode(), json.loads(raw) if raw else {}
            except Exception:
                return resp.getcode(), {"raw": raw}
    except HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw) if raw else {}
        except Exception:
            return e.code, {"raw": raw}
    except URLError as e:
        return 0, {"error": "network_error", "details": str(e)}

def _github_headers():
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token:
        return None
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "YiThume-SelfUpdater"
    }

def _github_repo_full_name():
    return (os.environ.get("GITHUB_REPO_FULL_NAME") or "").strip()

def _github_api_base():
    return "https://api.github.com"

def _github_get_ref_sha(repo_full_name: str, ref: str):
    h = _github_headers()
    if not h:
        return None, "github_token_missing"
    url = f"{_github_api_base()}/repos/{repo_full_name}/git/ref/{quote(ref)}"
    code, js = _http_json("GET", url, h)
    if code != 200:
        return None, f"github_get_ref_failed:{code}"
    return js.get("object", {}).get("sha"), None

def _github_create_branch(repo_full_name: str, new_branch: str, base_sha: str):
    h = _github_headers()
    if not h:
        return False, "github_token_missing"
    url = f"{_github_api_base()}/repos/{repo_full_name}/git/refs"
    payload = {"ref": f"refs/heads/{new_branch}", "sha": base_sha}
    code, js = _http_json("POST", url, h, payload)
    if code in (201, 200):
        return True, None
    # If it already exists, treat as ok
    if code == 422:
        return True, "branch_exists"
    return False, f"github_create_branch_failed:{code}"

def _github_fetch_file(repo_full_name: str, path: str, ref: str):
    h = _github_headers()
    if not h:
        return None, "github_token_missing"
    from urllib.parse import quote as q
    url = f"{_github_api_base()}/repos/{repo_full_name}/contents/{q(path)}?ref={q(ref)}"
    code, js = _http_json("GET", url, h)
    if code != 200:
        return None, f"github_fetch_file_failed:{code}"
    if js.get("encoding") == "base64" and js.get("content"):
        import base64
        return base64.b64decode(js["content"]).decode("utf-8", errors="replace"), None
    return None, "github_fetch_file_unexpected"

def _openai_enabled():
    return bool((os.environ.get("OPENAI_API_KEY") or "").strip())

def _openai_chat(prompt: str, files_context: dict):
    """
    Returns: { files: {path: new_content}, message }
    Uses OpenAI Responses/Chat Completions style via HTTP (no extra deps).
    """
    import json
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    model = (os.environ.get("OPENAI_MODEL") or "gpt-4o-mini").strip()

    # Keep it very constrained: produce JSON only.
    sys = (
        "You are a senior engineer. Output STRICT JSON only with keys: "
        "`message` (string) and `files` (object mapping file path -> full new file content). "
        "Do not include markdown. Only modify requested files. Preserve existing routes and behavior unless asked."
    )

    user = {
        "instruction": prompt,
        "files_context": files_context
    }

    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys},
            {"role": "user", "content": json.dumps(user)}
        ],
        "temperature": 0.2
    }
    code, js = _http_json("POST", url, headers, payload, timeout=35)
    if code != 200:
        return None, f"openai_failed:{code}", js
    try:
        content = js["choices"][0]["message"]["content"]
        out = json.loads(content)
        if not isinstance(out, dict) or "files" not in out:
            return None, "openai_invalid_json", {"raw": content}
        return out, None, None
    except Exception as e:
        return None, f"openai_parse_error:{e}", {"raw": js}

def _allowed_update_paths():
    raw = (os.environ.get("SELF_UPDATE_ALLOWED_PATHS") or "").strip()
    if raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    # Safe defaults: allow html + this python file + vercel configs (you can tighten)
    return [
        "api/app.py",
        "api/static/dashboard.html",
        "api/static/index.html",
        "api/static/manager_dashboard.html",
        "api/static/manager_setup.html",
        "vercel.json",
        "requirements.txt"
    ]

def _self_updates_col(db):
    col = db["self_update_requests"]
    try:
        col.create_index("created_at")
        col.create_index([("status", 1), ("created_at", -1)])
    except Exception:
        pass
    return col

@app.route("/manager/self_update", methods=["GET"])
@app.route("/api/app/manager/self_update", methods=["GET"])
def manager_self_update_page():
    if not require_admin():
        return ("Unauthorized", 401)

    allowed = _allowed_update_paths()

    html = """
<!doctype html>
<html><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>YiThume • Self Update</title>
<style>
body{font-family:ui-sans-serif,system-ui,Arial;background:#0b1220;color:#e5e7eb;margin:0}
.wrap{max-width:980px;margin:0 auto;padding:18px}
.card{background:#111827;border:1px solid #1f2937;border-radius:16px;padding:14px;margin:12px 0}
label{display:block;font-weight:800;margin:6px 0}
textarea,input{width:100%;border-radius:12px;border:1px solid #334155;background:#0b1220;color:#e5e7eb;padding:12px}
button{border:0;border-radius:12px;padding:10px 12px;font-weight:900;cursor:pointer}
.btn{background:#22c55e;color:#052e16}
.btn2{background:#38bdf8;color:#082f49}
.small{color:#94a3b8;font-size:13px}
pre{white-space:pre-wrap;background:#0b1220;border:1px solid #334155;border-radius:12px;padding:12px;overflow:auto}
</style></head>

<body><div class="wrap">
<h2 style="margin:6px 0">Self-updating code (Admin)</h2>
<div class="small">Flow: draft → propose changes (GPT) → approve → create GitHub branch + commit (never merges main automatically).</div>

<div class="card">
<form method="post" action="/manager/api/self_update/create">
<label>What do you want to update?</label>
<textarea name="prompt" rows="6" placeholder="Example: Add a new button on dashboard to publish store; keep all existing routes unchanged."></textarea>
<div style="height:10px"></div>
<label>Branch name (optional)</label>
<input name="branch" placeholder="e.g. update-dashboard-publish"/>
<div style="height:10px"></div>
<button class="btn" type="submit">Create request</button>
</form>
</div>

<div class="card">
<div style="font-weight:900;margin-bottom:6px">Allowed paths</div>
<div class="small">__ALLOWED__</div>
</div>

<div class="card">
<div style="font-weight:900;margin-bottom:6px">Recent requests</div>
<div id="list" class="small">Loading...</div>
</div>

<script>
  // --- Admin pin passthrough (FIXES Unauthorized on API calls) ---
  const QS = new URLSearchParams(window.location.search);
  const ADMIN_PIN = QS.get("admin_pin") || "";

  function withPin(url) {
    if (!ADMIN_PIN) return url;
    return url.includes("?") ? `${url}&admin_pin=${encodeURIComponent(ADMIN_PIN)}`
                             : `${url}?admin_pin=${encodeURIComponent(ADMIN_PIN)}`;
  }

  async function loadList(){
    const r = await fetch(withPin('/manager/api/self_update/list'));
    const j = await r.json();
    if(!j.ok){document.getElementById('list').innerText = 'Error loading.'; return;}
    if(!j.items.length){document.getElementById('list').innerText = 'No requests yet.'; return;}
    let html = '';
    j.items.forEach(it=>{
      html += `<div style="padding:10px 0;border-top:1px solid #1f2937">
        <div><b>${it.title}</b> • <span style="color:#94a3b8">${it.status}</span></div>
        <div style="color:#94a3b8;font-size:12px">ID: ${it.id} • Branch: ${it.branch||'-'} • ${it.created_at}</div>
        <div style="margin-top:8px; display:flex; gap:8px; flex-wrap:wrap">
          <button class="btn2" onclick="propose('${it.id}')">Propose (GPT)</button>
          <button class="btn2" onclick="preview('${it.id}')">Preview</button>
          <button class="btn2" onclick="approve('${it.id}')">Approve GitHub</button>
          <button class="btn" onclick="commit('${it.id}')">Create branch + commit</button>
        </div>
      </div>`;
    });
    document.getElementById('list').innerHTML = html;
  }

  async function propose(id){
    const r = await fetch(withPin(`/manager/api/self_update/${id}/propose`), {method:'POST'});
    const j = await r.json(); alert(JSON.stringify(j,null,2)); loadList();
  }
  async function preview(id){
    const r = await fetch(withPin(`/manager/api/self_update/${id}/preview`));
    const t = await r.text(); const w = window.open(); w.document.write(t);
  }
  async function approve(id){
    const confirm = prompt('Type APPROVE to allow GitHub operations for this request:');
    const r = await fetch(withPin(`/manager/api/self_update/${id}/approve_github`), {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({confirm})
    });
    const j = await r.json(); alert(JSON.stringify(j,null,2)); loadList();
  }
  async function commit(id){
    const confirm = prompt('Type COMMIT to create a branch + commit to GitHub:');
    const r = await fetch(withPin(`/manager/api/self_update/${id}/github_commit`), {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({confirm})
    });
    const j = await r.json(); alert(JSON.stringify(j,null,2)); loadList();
  }
  loadList();
</script>

</div></body></html>
"""
    html = html.replace("__ALLOWED__", ", ".join(allowed))
    return (html, 200, {"Content-Type": "text/html; charset=utf-8"})

@app.route("/manager/api/self_update/create", methods=["POST"])
@app.route("/api/app/manager/api/self_update/create", methods=["POST"])
def api_self_update_create():
    if not require_admin():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    prompt = (request.form.get("prompt") if request.form else None) or (request.get_json(silent=True) or {}).get("prompt") or ""
    prompt = prompt.strip()
    if not prompt:
        return jsonify({"ok": False, "error": "prompt_required"}), 400

    branch = (request.form.get("branch") if request.form else None) or (request.get_json(silent=True) or {}).get("branch") or ""
    branch = (branch or "").strip()
    if not branch:
        branch = f"self-update-{uuid.uuid4().hex[:8]}"

    title = prompt.splitlines()[0][:80]

    db = manager_db()
    col = _self_updates_col(db)
    doc = {
        "title": title,
        "prompt": prompt,
        "branch": branch,
        "status": "draft",
        "created_at": _now_dt(),
        "approved_github": False,
        "proposed_files": {},
        "proposal_message": None,
        "last_error": None
    }
    rid = str(col.insert_one(doc).inserted_id)
    return redirect(f"/manager/self_update")  # keep iPad UX simple

@app.route("/manager/api/self_update/list", methods=["GET"])
@app.route("/api/app/manager/api/self_update/list", methods=["GET"])
def api_self_update_list():
    if not require_admin():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    db = manager_db()
    col = _self_updates_col(db)
    items = list(col.find({}).sort("created_at", -1).limit(20))
    def _p(d):
        return {
            "id": str(d["_id"]),
            "title": d.get("title"),
            "status": d.get("status"),
            "branch": d.get("branch"),
            "created_at": str(d.get("created_at")),
            "approved_github": bool(d.get("approved_github"))
        }
    return jsonify({"ok": True, "items": [_p(d) for d in items]}), 200

@app.route("/manager/api/self_update/<rid>/propose", methods=["POST"])
@app.route("/api/app/manager/api/self_update/<rid>/propose", methods=["POST"])
def api_self_update_propose(rid):
    if not require_admin():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    db = manager_db()
    col = _self_updates_col(db)
    d = col.find_one({"_id": ObjectId(rid)})
    if not d:
        return jsonify({"ok": False, "error": "not_found"}), 404

    if not _openai_enabled():
        return jsonify({"ok": False, "error": "openai_not_configured", "hint": "Set OPENAI_API_KEY and optionally OPENAI_MODEL"}), 400

    repo = _github_repo_full_name()
    if not repo:
        return jsonify({"ok": False, "error": "github_repo_not_configured", "hint": "Set GITHUB_REPO_FULL_NAME"}), 400

    allowed = _allowed_update_paths()
    # Fetch current file contents from GitHub main as context (lightweight)
    files_context = {}
    for p in allowed:
        txt, err = _github_fetch_file(repo, p, "main")
        if txt and not err:
            files_context[p] = txt[:30000]  # cap context
    # Ask GPT to produce full new contents for any modified files
    out, err, details = _openai_chat(d.get("prompt", ""), files_context)
    if err:
        col.update_one({"_id": d["_id"]}, {"$set": {"status": "error", "last_error": {"err": err, "details": details}}})
        return jsonify({"ok": False, "error": err, "details": details}), 500

    files = out.get("files") or {}
    # Enforce allowlist
    safe_files = {}
    for path, content in files.items():
        if path in allowed and isinstance(content, str):
            safe_files[path] = content

    if not safe_files:
        col.update_one({"_id": d["_id"]}, {"$set": {"status": "error", "last_error": "no_allowed_files_in_proposal"}})
        return jsonify({"ok": False, "error": "no_allowed_files_in_proposal"}), 400

    col.update_one({"_id": d["_id"]}, {"$set": {
        "status": "proposed",
        "proposal_message": out.get("message"),
        "proposed_files": safe_files,
        "last_error": None
    }})
    return jsonify({"ok": True, "status": "proposed", "files": list(safe_files.keys()), "message": out.get("message")}), 200

@app.route("/manager/api/self_update/<rid>/preview", methods=["GET"])
@app.route("/api/app/manager/api/self_update/<rid>/preview", methods=["GET"])
def api_self_update_preview(rid):
    if not require_admin():
        return ("Unauthorized", 401)
    db = manager_db()
    col = _self_updates_col(db)
    d = col.find_one({"_id": ObjectId(rid)})
    if not d:
        return ("Not found", 404)
    files = d.get("proposed_files") or {}
    msg = d.get("proposal_message") or ""
    html = ["<!doctype html><html><head><meta charset='utf-8'/><meta name='viewport' content='width=device-width,initial-scale=1'/>"]
    html.append("<title>Proposal preview</title><style>body{font-family:ui-monospace,Menlo,monospace;background:#0b1220;color:#e5e7eb;margin:0} .wrap{max-width:1100px;margin:0 auto;padding:16px} .card{background:#111827;border:1px solid #1f2937;border-radius:14px;padding:12px;margin:12px 0} pre{white-space:pre-wrap}</style></head><body><div class='wrap'>")
    html.append("<h3>GPT proposal preview</h3>")
    html.append(f"<div class='card'><b>Message:</b><pre>{msg}</pre></div>")
    for path, content in files.items():
        html.append(f"<div class='card'><b>{path}</b><pre>{content[:20000]}</pre><div style='color:#94a3b8'>Preview truncated.</div></div>")
    html.append("</div></body></html>")
    return ("".join(html), 200, {"Content-Type": "text/html; charset=utf-8"})

@app.route("/manager/api/self_update/<rid>/approve_github", methods=["POST"])
@app.route("/api/app/manager/api/self_update/<rid>/approve_github", methods=["POST"])
def api_self_update_approve_github(rid):
    if not require_admin():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    confirm = (body.get("confirm") or "").strip().upper()
    if confirm != "APPROVE":
        return jsonify({"ok": False, "error": "type_APPROVE_to_confirm"}), 400

    db = manager_db()
    col = _self_updates_col(db)
    d = col.find_one({"_id": ObjectId(rid)})
    if not d:
        return jsonify({"ok": False, "error": "not_found"}), 404

    col.update_one({"_id": d["_id"]}, {"$set": {"approved_github": True, "status": "approved"}})
    return jsonify({"ok": True, "approved_github": True}), 200

@app.route("/manager/api/self_update/<rid>/github_commit", methods=["POST"])
@app.route("/api/app/manager/api/self_update/<rid>/github_commit", methods=["POST"])
def api_self_update_github_commit(rid):
    if not require_admin():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    confirm = (body.get("confirm") or "").strip().upper()
    if confirm != "COMMIT":
        return jsonify({"ok": False, "error": "type_COMMIT_to_confirm"}), 400

    repo = _github_repo_full_name()
    if not repo:
        return jsonify({"ok": False, "error": "github_repo_not_configured"}), 400
    if not _github_headers():
        return jsonify({"ok": False, "error": "github_token_missing"}), 400

    db = manager_db()
    col = _self_updates_col(db)
    d = col.find_one({"_id": ObjectId(rid)})
    if not d:
        return jsonify({"ok": False, "error": "not_found"}), 404

    if not d.get("approved_github"):
        return jsonify({"ok": False, "error": "github_not_approved"}), 403

    files = d.get("proposed_files") or {}
    if not files:
        return jsonify({"ok": False, "error": "no_proposed_files"}), 400

    branch = (d.get("branch") or f"self-update-{uuid.uuid4().hex[:8]}").strip()

    base_sha, err = _github_get_ref_sha(repo, "heads/main")
    if err or not base_sha:
        col.update_one({"_id": d["_id"]}, {"$set": {"status": "error", "last_error": err or "no_base_sha"}})
        return jsonify({"ok": False, "error": err or "no_base_sha"}), 500

    ok, berr = _github_create_branch(repo, branch, base_sha)
    if not ok:
        col.update_one({"_id": d["_id"]}, {"$set": {"status": "error", "last_error": berr}})
        return jsonify({"ok": False, "error": berr}), 500

    # Create commit via GitHub git data API
    h = _github_headers()
    # Get base tree
    code, base_commit = _http_json("GET", f"{_github_api_base()}/repos/{repo}/git/commits/{base_sha}", h)
    if code != 200:
        return jsonify({"ok": False, "error": "github_get_base_commit_failed", "details": base_commit}), 500
    base_tree_sha = (base_commit.get("tree") or {}).get("sha")
    if not base_tree_sha:
        return jsonify({"ok": False, "error": "github_base_tree_missing"}), 500

    # Create blobs
    tree_elems = []
    for path, content in files.items():
        code, blob = _http_json("POST", f"{_github_api_base()}/repos/{repo}/git/blobs", h, {"content": content, "encoding": "utf-8"})
        if code not in (200, 201):
            return jsonify({"ok": False, "error": "github_create_blob_failed", "path": path, "details": blob}), 500
        tree_elems.append({"path": path, "mode": "100644", "type": "blob", "sha": blob.get("sha")})

    # Create tree
    code, tree = _http_json("POST", f"{_github_api_base()}/repos/{repo}/git/trees", h, {"base_tree": base_tree_sha, "tree": tree_elems})
    if code not in (200, 201):
        return jsonify({"ok": False, "error": "github_create_tree_failed", "details": tree}), 500
    tree_sha = tree.get("sha")

    # Create commit
    msg = f"Self update: {d.get('title') or 'update'}"
    code, commit = _http_json("POST", f"{_github_api_base()}/repos/{repo}/git/commits", h, {"message": msg, "tree": tree_sha, "parents": [base_sha]})
    if code not in (200, 201):
        return jsonify({"ok": False, "error": "github_create_commit_failed", "details": commit}), 500
    commit_sha = commit.get("sha")

    # Update branch ref
    code, upd = _http_json("PATCH", f"{_github_api_base()}/repos/{repo}/git/refs/heads/{branch}", h, {"sha": commit_sha, "force": False})
    if code not in (200, 201):
        return jsonify({"ok": False, "error": "github_update_ref_failed", "details": upd}), 500

    col.update_one({"_id": d["_id"]}, {"$set": {"status": "committed", "last_error": None, "commit_sha": commit_sha}})
    return jsonify({"ok": True, "status": "committed", "branch": branch, "commit_sha": commit_sha}), 200

# ==========================================================
# MULTI-NODE + STOREFRONT API (2026 upgrade)
# ==========================================================
# Goals:
#  - Allow "nodes" (local operators) to exist globally, each with stores + drivers + payouts.
#  - Provide a clean, headless storefront API so any UI builder (Webflow/Lovable/custom HTML)
#    can connect to the same backend.
#  - Optional Stripe support (guarded; safe if stripe isn't installed yet).

try:
    import stripe  # optional
except Exception:
    stripe = None

STRIPE_SECRET_KEY = (os.environ.get("STRIPE_SECRET_KEY") or "").strip()
STRIPE_WEBHOOK_SECRET = (os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip()

def _stripe_ready() -> bool:
    return bool(stripe) and bool(STRIPE_SECRET_KEY)

def _stripe_init():
    if _stripe_ready():
        stripe.api_key = STRIPE_SECRET_KEY

def nodes_col(db):
    return db.nodes

def _ensure_nodes_indexes(db):
    try:
        nodes_col(db).create_index([("_internal_id", ASCENDING)], unique=True)
        nodes_col(db).create_index([("public_slug", ASCENDING)], unique=True, sparse=True)
        nodes_col(db).create_index([("created_at", DESCENDING)])
    except Exception:
        pass

def _node_slugify(name: str) -> str:
    s = (name or "node").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "node"

def _get_node_or_404(db, node_id: str):
    n = db.nodes.find_one({"_internal_id": node_id})
    if not n:
        abort(404, description="node_not_found")
    return n

def _require_node_admin():
    # For now we reuse ADMIN_SECRET. Later you can add per-node auth.
    return require_admin()

# -----------------------------
# Nodes CRUD (admin for now)
# -----------------------------
@app.route("/v1/nodes", methods=["GET", "POST"])
@app.route("/api/v1/nodes", methods=["GET", "POST"])
@app.route("/api/app/v1/nodes", methods=["GET", "POST"])
def v1_nodes():
    db = get_db()
    _ensure_nodes_indexes(db)

    if request.method == "GET":
        # Admin-only list for now
        if not _require_node_admin():
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        limit = max(1, min(int(request.args.get("limit", "50")), 200))
        cur = db.nodes.find({}).sort("created_at", -1).limit(limit)
        return jsonify({"ok": True, "nodes": [safe_doc(x) for x in cur]}), 200

    # POST create
    if not _require_node_admin():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    node_id = str(uuid.uuid4())
    name = (body.get("name") or "Node").strip()
    base_slug = _node_slugify(body.get("public_slug") or name)

    # ensure unique slug
    slug = base_slug
    i = 1
    while db.nodes.find_one({"public_slug": slug}):
        i += 1
        slug = f"{base_slug}-{i}"

    doc = {
        "_internal_id": node_id,
        "name": name,
        "public_slug": slug,
        "country": (body.get("country") or "ZA").strip(),
        "city": (body.get("city") or "").strip(),
        "currency": (body.get("currency") or "ZAR").strip(),
        "commission_rate": float(body.get("commission_rate") or 0.10),  # node operator cut
        "platform_fee_rate": float(body.get("platform_fee_rate") or PLATFORM_FEE_RATE),
        "delivery": {
            "base_fee": float((body.get("delivery") or {}).get("base_fee") or DEFAULT_DELIVERY_FEE if "DEFAULT_DELIVERY_FEE" in globals() else 20),
            "per_km": float((body.get("delivery") or {}).get("per_km") or 6.0),
            "mode": (body.get("delivery") or {}).get("mode") or "auto",  # auto/manual
        },
        "active": True,
        "created_at": _now_dt()
    }
    db.nodes.insert_one(doc)
    return jsonify({"ok": True, "node_id": node_id, "public_slug": slug}), 201

@app.route("/v1/nodes/<node_id>", methods=["GET", "PATCH"])
@app.route("/api/v1/nodes/<node_id>", methods=["GET", "PATCH"])
@app.route("/api/app/v1/nodes/<node_id>", methods=["GET", "PATCH"])
def v1_node_detail(node_id):
    db = get_db()
    _ensure_nodes_indexes(db)
    n = db.nodes.find_one({"_internal_id": node_id})
    if not n:
        return jsonify({"ok": False, "error": "node_not_found"}), 404

    if request.method == "GET":
        if not _require_node_admin():
            # public minimal view (safe)
            return jsonify({"ok": True, "node": {
                "_internal_id": n["_internal_id"],
                "name": n.get("name"),
                "public_slug": n.get("public_slug"),
                "country": n.get("country"),
                "city": n.get("city"),
                "currency": n.get("currency"),
                "active": bool(n.get("active", True))
            }}), 200
        return jsonify({"ok": True, "node": safe_doc(n)}), 200

    # PATCH admin-only
    if not _require_node_admin():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    updates = {}
    for k in ("name", "country", "city", "currency", "active"):
        if k in body:
            updates[k] = body.get(k)
    if "commission_rate" in body:
        updates["commission_rate"] = float(body.get("commission_rate") or 0)
    if "platform_fee_rate" in body:
        updates["platform_fee_rate"] = float(body.get("platform_fee_rate") or 0)
    if "delivery" in body and isinstance(body["delivery"], dict):
        d = body["delivery"]
        updates["delivery.base_fee"] = float(d.get("base_fee") or 0)
        updates["delivery.per_km"] = float(d.get("per_km") or 0)
        if "mode" in d:
            updates["delivery.mode"] = (d.get("mode") or "auto").strip()

    if updates:
        db.nodes.update_one({"_internal_id": node_id}, {"$set": updates})
    n = db.nodes.find_one({"_internal_id": node_id})
    return jsonify({"ok": True, "node": safe_doc(n)}), 200

# -----------------------------
# Store ↔ Node assignment
# -----------------------------
@app.route("/v1/store/<store_id>/assign_node", methods=["POST"])
@app.route("/api/v1/store/<store_id>/assign_node", methods=["POST"])
@app.route("/api/app/v1/store/<store_id>/assign_node", methods=["POST"])
def v1_store_assign_node(store_id):
    if not _require_node_admin():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    db = get_db()
    body = request.get_json(silent=True) or {}
    node_id = (body.get("node_id") or "").strip()
    if not node_id:
        return jsonify({"ok": False, "error": "node_id_required"}), 400
    if not db.nodes.find_one({"_internal_id": node_id}):
        return jsonify({"ok": False, "error": "node_not_found"}), 404
    res = db.stores.update_one({"_internal_id": store_id}, {"$set": {"node_id": node_id}})
    if res.matched_count == 0:
        return jsonify({"ok": False, "error": "store_not_found"}), 404
    return jsonify({"ok": True, "store_id": store_id, "node_id": node_id}), 200

# -----------------------------

def _ensure_full_html_document(html_text: str, title: str = "YiThume Store") -> str:
    """
    Normalize user-provided HTML so it always renders as a valid document:
      - Ensures <!doctype html>, <html>, <head>, <meta charset>, <meta viewport>, <title>, <body>.
      - If user uploaded a fragment, we wrap it.
    """
    t = (html_text or "").strip()
    if not t:
        return "<!doctype html><html><head><meta charset='utf-8'/><meta name='viewport' content='width=device-width,initial-scale=1'/><title>%s</title></head><body></body></html>" % (title or "Store")

    lower = t.lower()
    is_doc = ("<html" in lower) or ("<head" in lower) or ("<body" in lower)

    if not is_doc:
        body = t
        t = f"<!doctype html><html><head><meta charset='utf-8'/><meta name='viewport' content='width=device-width,initial-scale=1'/><title>{title}</title></head><body>{body}</body></html>"
        lower = t.lower()

    if "<!doctype" not in lower:
        t = "<!doctype html>\n" + t
        lower = t.lower()

    if "<head" not in lower:
        if "<html" in lower:
            t = re.sub(r"<html[^>]*>", lambda m: m.group(0) + "<head></head>", t, count=1, flags=re.I)
        else:
            t = "<head></head>" + t
        lower = t.lower()

    if "charset=" not in lower:
        t = re.sub(r"</head>", "<meta charset='utf-8'/>\n</head>", t, count=1, flags=re.I)
        lower = t.lower()
    if "name='viewport'" not in lower and 'name="viewport"' not in lower:
        t = re.sub(r"</head>", "<meta name='viewport' content='width=device-width,initial-scale=1'/>\n</head>", t, count=1, flags=re.I)
        lower = t.lower()

    if "<title" not in lower:
        t = re.sub(r"</head>", f"<title>{title}</title>\n</head>", t, count=1, flags=re.I)
        lower = t.lower()

    if "<body" not in lower:
        parts = re.split(r"</head>", t, flags=re.I, maxsplit=1)
        if len(parts) == 2:
            t = parts[0] + "</head><body>" + parts[1] + "</body>"
        else:
            t = t + "<body></body>"

    return t

# Custom storefront upload (HTML + optional assets)
# -----------------------------
@app.route("/v1/store/<store_id>/frontend/upload", methods=["POST"])
@app.route("/api/v1/store/<store_id>/frontend/upload", methods=["POST"])
@app.route("/api/app/v1/store/<store_id>/frontend/upload", methods=["POST"])
def v1_storefront_upload_frontend(store_id):
    """
    Upload a custom storefront for a store.
    - Accepts multipart/form-data:
        - html: required (text/html)
        - assets: optional multiple files (css/js/images)
    Files are stored in GridFS. Store doc stores:
      stores.frontend = { html_file_id: "...", assets: [{path, file_id, contentType}] }
    """
    if not require_admin():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    if "html" not in request.files:
        return jsonify({"ok": False, "error": "html_file_required"}), 400

    html_f = request.files["html"]
    html_name = secure_filename(getattr(html_f, "filename", "") or "index.html")
    html_bytes = html_f.read() or b""
    if not html_bytes:
        return jsonify({"ok": False, "error": "empty_html"}), 400
    if len(html_bytes) > 2 * 1024 * 1024:
        return jsonify({"ok": False, "error": "html_too_large"}), 413

    # Normalize HTML so missing <title>/<head> etc never breaks the storefront
    try:
        html_text = html_bytes.decode("utf-8", errors="replace")
    except Exception:
        html_text = ""
    html_text = _ensure_full_html_document(html_text, title="YiThume Store")
    html_bytes = html_text.encode("utf-8")

    db = get_db()
    s = db.stores.find_one({"_internal_id": store_id})
    if not s:
        return jsonify({"ok": False, "error": "store_not_found"}), 404

    fs = get_fs(db)
    html_id = fs.put(html_bytes, filename=html_name, contentType=getattr(html_f, "mimetype", "text/html"))

    assets_meta = []
    # optional: request.files.getlist("assets")
    try:
        assets = request.files.getlist("assets") if hasattr(request.files, "getlist") else []
    except Exception:
        assets = []

    # limit number + size to protect serverless
    max_assets = 25
    max_asset_size = 2 * 1024 * 1024
    for a in (assets or [])[:max_assets]:
        if not a or not getattr(a, "filename", ""):
            continue
        name = secure_filename(a.filename)
        content = a.read() or b""
        if not content:
            continue
        if len(content) > max_asset_size:
            continue
        fid = fs.put(content, filename=name, contentType=getattr(a, "mimetype", "application/octet-stream"))
        assets_meta.append({"path": name, "file_id": str(fid), "contentType": getattr(a, "mimetype", "application/octet-stream")})

    db.stores.update_one(
        {"_internal_id": store_id},
        {"$set": {
            "frontend": {
                "html_file_id": str(html_id),
                "assets": assets_meta,
                "updated_at": _now_dt()
            }
        }}
    )

    return jsonify({
        "ok": True,
        "store_id": store_id,
        "html_file_id": str(html_id),
        "assets_count": len(assets_meta)
    }), 200


@app.route("/shop/<public_slug>/<path:asset_path>", methods=["GET"])
@app.route("/api/app/shop/<public_slug>/<path:asset_path>", methods=["GET"])
def shop_public_asset_fallback(public_slug, asset_path):
    """
    Convenience asset route so custom storefronts can reference assets relatively:
      - When the storefront URL ends with a slash (/shop/<slug>/), a link to "styles.css"
        resolves to /shop/<slug>/styles.css which will hit this endpoint.
      - Internally we serve from the same GridFS assets list as /shop/<slug>/asset/<path>.
    """
    if not asset_path or asset_path == "/":
        return ("Not found", 404)
    if asset_path.startswith("asset/"):
        return shop_public_asset(public_slug, asset_path.split("asset/", 1)[1])
    return shop_public_asset(public_slug, asset_path)

@app.route("/shop/<public_slug>/asset/<path:asset_path>", methods=["GET"])
@app.route("/api/app/shop/<public_slug>/asset/<path:asset_path>", methods=["GET"])
def shop_public_asset(public_slug, asset_path):
    """
    Serve uploaded storefront assets for a published store.
    """
    try:
        db = get_db()
        s = db.stores.find_one({"public_slug": public_slug, "published": True})
        if not s:
            return ("Not found", 404)
        frontend = (s.get("frontend") or {})
        assets = frontend.get("assets") or []
        match = None
        for a in assets:
            if (a.get("path") or "") == asset_path:
                match = a
                break
        if not match:
            return ("Not found", 404)

        fs = get_fs(db)
        g = fs.get(ObjectId(match["file_id"]))
        data = g.read()
        ct = match.get("contentType") or g.content_type or "application/octet-stream"
        return (data, 200, {"Content-Type": ct})
    except Exception:
        return ("Not found", 404)

# -----------------------------
# Headless Storefront API (public, safe)
# -----------------------------
def _store_by_public_slug(db, public_slug: str):
    return db.stores.find_one({"public_slug": public_slug, "published": True})

@app.route("/v1/storefront/<public_slug>/products", methods=["GET"])
@app.route("/api/v1/storefront/<public_slug>/products", methods=["GET"])
@app.route("/api/app/v1/storefront/<public_slug>/products", methods=["GET"])
def storefront_products(public_slug):
    db = get_db()
    s = _store_by_public_slug(db, public_slug)
    if not s:
        return jsonify({"ok": False, "error": "store_not_found"}), 404

    items = list(db.store_items.find({"store_id": s["_internal_id"], "active": True}).sort("created_at", -1).limit(500))
    out = []
    for it in items:
        out.append({
            "id": it.get("_internal_id"),
            "name": it.get("name"),
            "price": float(it.get("price") or 0),
            "sku": it.get("sku"),
            "image_url": it.get("image_url"),
        })
    return jsonify({
        "ok": True,
        "store": {"id": s.get("_internal_id"), "name": s.get("name"), "currency": (s.get("currency") or "ZAR")},
        "products": out
    }), 200

def _calc_delivery_fee_for_store(db, store_doc, customer_coords=None) -> float:
    # If store belongs to a node with pricing rules, use them; else fall back to DEFAULT_DELIVERY_FEE.
    node_id = store_doc.get("node_id")
    if node_id:
        n = db.nodes.find_one({"_internal_id": node_id})
    else:
        n = None

    base = float((n or {}).get("delivery", {}).get("base_fee") or os.environ.get("AGENT_DEFAULT_DELIVERY_FEE", "20") or 20)
    per_km = float((n or {}).get("delivery", {}).get("per_km") or 0)

    # If coords present and store has coords, compute
    try:
        store_coords = (((store_doc.get("address") or {}).get("coords") or {}))
        slat, slng = store_coords.get("lat"), store_coords.get("lng")
        clat, clng = (customer_coords or {}).get("lat"), (customer_coords or {}).get("lng")
        if slat is not None and slng is not None and clat is not None and clng is not None:
            dist_km = haversine_km(float(slat), float(slng), float(clat), float(clng))
            return round(base + per_km * dist_km, 2)
    except Exception:
        pass
    return round(base, 2)

@app.route("/v1/storefront/<public_slug>/quote", methods=["POST"])
@app.route("/api/v1/storefront/<public_slug>/quote", methods=["POST"])
@app.route("/api/app/v1/storefront/<public_slug>/quote", methods=["POST"])
def storefront_quote(public_slug):
    """
    Quote subtotal + delivery + total for a given cart.
    Body:
      { items: [{product_id, qty}], customer: { address: { coords:{lat,lng} } } }
    """
    db = get_db()
    s = _store_by_public_slug(db, public_slug)
    if not s:
        return jsonify({"ok": False, "error": "store_not_found"}), 404

    body = request.get_json(silent=True) or {}
    items = body.get("items") or []
    if not isinstance(items, list) or not items:
        return jsonify({"ok": False, "error": "items_required"}), 400

    # Build subtotal from DB prices (never trust client price)
    subtotal = 0.0
    resolved = []
    for row in items[:200]:
        pid = (row.get("product_id") or row.get("id") or "").strip()
        qty = int(row.get("qty") or 1)
        qty = max(1, min(qty, 50))
        it = db.store_items.find_one({"_internal_id": pid, "store_id": s["_internal_id"], "active": True})
        if not it:
            continue
        price = float(it.get("price") or 0)
        subtotal += price * qty
        resolved.append({"product_id": pid, "name": it.get("name"), "qty": qty, "price": price})

    if not resolved:
        return jsonify({"ok": False, "error": "no_valid_items"}), 400

    cust = body.get("customer") or {}
    coords = (((cust.get("address") or {}).get("coords") or {}))
    delivery_fee = _calc_delivery_fee_for_store(db, s, coords)
    total = round(subtotal + delivery_fee, 2)

    return jsonify({
        "ok": True,
        "store_id": s["_internal_id"],
        "public_slug": public_slug,
        "items": resolved,
        "subtotal": round(subtotal, 2),
        "delivery_fee": delivery_fee,
        "total": total,
        "currency": (s.get("currency") or "ZAR")
    }), 200

@app.route("/v1/storefront/<public_slug>/checkout", methods=["POST"])
@app.route("/api/v1/storefront/<public_slug>/checkout", methods=["POST"])
@app.route("/api/app/v1/storefront/<public_slug>/checkout", methods=["POST"])
def storefront_checkout(public_slug):
    """
    Create an order from a storefront cart.

    SAFETY RAIL:
      - An order is created only when confirm == "CONFIRM" (same guard as WhatsApp).

    Body:
      {
        "confirm": "CONFIRM",
        "customer": { "name": "...", "phone": "...", "address": { "line1": "...", "city": "...", "coords": {"lat":..,"lng":..} } },
        "items": [ { "product_id": "...", "qty": 1 } ],
        "payment": { "method": "card"|"cash"|"eft" }
      }

    If Stripe is available, returns a PaymentIntent client_secret (simple mode).
    """
    db = get_db()
    s = _store_by_public_slug(db, public_slug)
    if not s:
        return jsonify({"ok": False, "error": "store_not_found"}), 404

    body = request.get_json(silent=True) or {}
    confirm = (body.get("confirm") or "").strip().upper()
    if confirm != "CONFIRM":
        return jsonify({"ok": False, "error": "type_CONFIRM_to_place_order"}), 400

    items_in = body.get("items") or []
    if not isinstance(items_in, list) or not items_in:
        return jsonify({"ok": False, "error": "items_required"}), 400

    # Build subtotal from DB prices (never trust client price)
    subtotal = 0.0
    resolved_items = []
    for row in items_in[:200]:
        pid = (row.get("product_id") or row.get("id") or "").strip()
        qty = int(row.get("qty") or 1)
        qty = max(1, min(qty, 50))
        it = db.store_items.find_one({"_internal_id": pid, "store_id": s["_internal_id"], "active": True})
        if not it:
            continue
        price = float(it.get("price") or 0)
        subtotal += price * qty
        resolved_items.append({
            "product_id": pid,
            "name": it.get("name"),
            "qty": qty,
            "price": price,
            "sku": it.get("sku"),
            "image_url": it.get("image_url"),
            "stripe_price_id": it.get("stripe_price_id")
        })

    if not resolved_items:
        return jsonify({"ok": False, "error": "no_valid_items"}), 400

    customer = body.get("customer") or {}
    coords = (((customer.get("address") or {}).get("coords") or {}))
    delivery_fee = _calc_delivery_fee_for_store(db, s, coords)
    total = round(subtotal + delivery_fee, 2)

    payment_in = body.get("payment") or {}
    method = (payment_in.get("method") or "card").strip().lower()
    if method not in ("card", "cash", "eft"):
        method = "card"

    order_internal_id = str(uuid.uuid4())
    public_id = make_order_public_id()

    order_doc = {
        "_internal_id": order_internal_id,
        "order_id": public_id,
        "created_at": _now_dt(),
        "created_at_iso": _now_iso(),
        "store_id": s["_internal_id"],
        "node_id": s.get("node_id"),
        "channel": "storefront",
        "customer": customer,
        "items": resolved_items,
        "subtotal": round(subtotal, 2),
        "delivery_fee": float(delivery_fee),
        "total": float(total),
        "payment": {
            "method": method,
            "status": "pending" if method == "card" else "unpaid",
            "provider_ref": None,
            "fake_checkout_url": f"https://pay.yithume.example/checkout/{order_internal_id}"
        },
        "status": "pending",
        "assigned_driver_id": None,
        "assigned_at": None,
        "delivered_at": None,
        "route": {},
        "created_by": "storefront",
        "meta": {"public_slug": public_slug},
        "fraud_score": 0.0,
        "fraud_flags": {},
        "cluster_key": None,
        "delivery_photo_file_id": None,
        "delivery_photo_url": None,
        "driver_pay_status": "pending",
        "driver_pay_pending": 0.0,
        "driver_pay_approved": 0.0,
        "settlement": {"driver": 0.0, "platform": 0.0, "settled": False}
    }

    try:
        intent = None
        if method == "card" and _stripe_ready():
            _stripe_init()
            currency = (os.environ.get("STORE_CURRENCY") or (s.get("currency") or "ZAR")).lower()
            # Stripe expects ISO currency like "zar"
            currency = currency.lower()
            if len(currency) != 3:
                currency = "zar"
            intent = stripe.PaymentIntent.create(
                amount=int(round(order_doc["total"] * 100)),
                currency=currency,
                metadata={"order_id": order_doc["order_id"], "store_id": s["_internal_id"], "public_slug": public_slug},
                automatic_payment_methods={"enabled": True},
            )
            order_doc["payment"]["provider_ref"] = intent.get("id")
            order_doc["payment"]["status"] = "requires_payment"


        # ---------------- Payment handling (demo vs live) ----------------
        p = _store_payment_cfg(s)
        mode = (p.get("mode") or "demo").lower()
        provider = (p.get("provider") or "demo").lower()

        intent = None
        session = None

        if mode == "demo" or provider == "demo":
            order_doc["payment"] = {
                "mode": "demo",
                "provider": "demo",
                "status": "demo",
                "fake_checkout_url": f"{request.url_root.rstrip('/')}/shop/{public_slug}?demo_order={order_internal_id}"
            }
        elif provider == "stripe_connect":
            if not stripe or not _stripe_platform_ready() or not _stripe_platform_init():
                return jsonify({"ok": False, "error": "stripe_platform_not_configured"}), 400
            acct = _stripe_connect_account_id(s)
            if not acct:
                return jsonify({"ok": False, "error": "stripe_connect_not_connected"}), 400

            # Ensure items have Stripe price IDs on the connected account
            # If missing, tell manager to run sync endpoint
            missing = False
            line_items = []
            for row in resolved_items:
                price_id = (row.get("stripe_price_id") or "").strip()
                if not price_id:
                    missing = True
                    break
                line_items.append({"price": price_id, "quantity": int(row.get("qty") or 1)})

            if missing:
                return jsonify({"ok": False, "error": "stripe_prices_missing", "note": "Run /manager/api/store/<store_id>/stripe/sync_products first."}), 400

            fee_cents = _stripe_fee_amount_cents(order_doc["total"])

            success_url = (request.url_root.rstrip("/") + f"/shop/{public_slug}?paid=1&order_id={public_id}")
            cancel_url  = (request.url_root.rstrip("/") + f"/shop/{public_slug}?cancel=1&order_id={public_id}")

            session = stripe.checkout.Session.create(
                mode="payment",
                line_items=line_items,
                success_url=success_url,
                cancel_url=cancel_url,
                payment_intent_data={
                    "application_fee_amount": fee_cents,
                    "transfer_data": {"destination": acct},
                    "metadata": {"order_id": public_id, "store_id": s["_internal_id"]}
                },
                metadata={"order_id": public_id, "store_id": s["_internal_id"]}
            )

            order_doc["payment"] = {
                "mode": "live",
                "provider": "stripe_connect",
                "status": "requires_payment",
                "checkout_url": session.get("url"),
                "provider_ref": session.get("id")
            }
        elif provider == "yoco":
            # Placeholder: Yoco checkout is client-side + secret key server-side charging (not implemented here).
            order_doc["payment"] = {"mode": "live", "provider": "yoco", "status": "requires_payment", "note": "Yoco not yet implemented server-side."}
        else:
            order_doc["payment"] = {"mode": mode, "provider": provider, "status": "requires_payment"}

        db.orders.insert_one(order_doc)

        out = {
            "ok": True,
            "order_db_id": order_internal_id,
            "order_public_id": public_id,
            "status": order_doc["status"],
            "totals": {"subtotal": order_doc["subtotal"], "delivery_fee": order_doc["delivery_fee"], "total": order_doc["total"]},
            "currency": (s.get("currency") or "ZAR"),
            "payment": order_doc["payment"]
        }
        if session is not None:
            out["stripe"] = {"checkout_url": session.get("url"), "session_id": session.get("id")}
        elif intent is not None:
            out["stripe"] = {"client_secret": intent.get("client_secret")}
        else:
            out["stripe"] = {"available": False, "note": "Demo mode or provider not configured."}
        return jsonify(out), 201
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500


@app.route("/v1/storefront/<public_slug>/order/<order_id>", methods=["GET"])
@app.route("/api/v1/storefront/<public_slug>/order/<order_id>", methods=["GET"])

@app.route("/api/app/v1/storefront/<public_slug>/order/<order_id>", methods=["GET"])
def storefront_order_status(public_slug, order_id):
    db = get_db()
    s = _store_by_public_slug(db, public_slug)
    if not s:
        return jsonify({"ok": False, "error": "store_not_found"}), 404
    o = db.orders.find_one({"order_id": order_id, "store_id": s["_internal_id"]})
    if not o:
        return jsonify({"ok": False, "error": "order_not_found"}), 404
    return jsonify({"ok": True, "order": {
        "order_id": o.get("order_id"),
        "status": o.get("status"),
        "payment": (o.get("payment") or {}).get("status"),
        "total": o.get("total"),
        "delivery_fee": o.get("delivery_fee"),
        "assigned_driver_id": o.get("assigned_driver_id"),
        "created_at": o.get("created_at_iso")
    }}), 200

# -----------------------------
# Stripe webhook (optional)
# -----------------------------
@app.route("/v1/stripe/webhook", methods=["POST"])
@app.route("/api/v1/stripe/webhook", methods=["POST"])
@app.route("/api/app/v1/stripe/webhook", methods=["POST"])
def stripe_webhook():
    if not _stripe_ready():
        return jsonify({"ok": False, "error": "stripe_not_configured"}), 400
    _stripe_init()

    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "stripe_webhook_secret_missing"}), 400

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        return jsonify({"ok": False, "error": "invalid_signature", "details": str(e)}), 400

    # Handle common events (extend as needed)
    et = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}
    try:
        db = get_db()
        if et in ("checkout.session.completed",):
            sid = obj.get("id")
            if sid:
                db.orders.update_one({"payment.provider_ref": sid}, {"$set": {"payment.status": "paid"}})
        if et in ("payment_intent.succeeded", "payment_intent.payment_failed"):
            pid = obj.get("id")
            status = obj.get("status")
            if pid:
                new_status = "paid" if et == "payment_intent.succeeded" else "failed"
                db.orders.update_one({"payment.provider_ref": pid}, {"$set": {"payment.status": new_status}})
        return jsonify({"ok": True}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500

# -----------------------------
# Platform fees / Connect
# -----------------------------
PLATFORM_FEE_PCT = float(os.environ.get("PLATFORM_FEE_PCT", "0.05"))  # 5% platform fee on Stripe destination charges
STRIPE_PLATFORM_SECRET_KEY = os.environ.get("STRIPE_PLATFORM_SECRET_KEY") or os.environ.get("STRIPE_SECRET_KEY") or os.environ.get("STRIPE_API_KEY")
STRIPE_CONNECT_REFRESH_URL = os.environ.get("STRIPE_CONNECT_REFRESH_URL")  # e.g. https://yourdomain.com/manager_setup.html
STRIPE_CONNECT_RETURN_URL  = os.environ.get("STRIPE_CONNECT_RETURN_URL")   # e.g. https://yourdomain.com/manager_dashboard.html



### --- V1 AUTH + STOREFRONT + GITHUB (ADDED) ---
# -------------------------------------------------
# V1 AUTH + RBAC + STORES + STOREFRONT + GITHUB
# (Additive; does NOT change existing endpoints)
# -------------------------------------------------

SESSION_TTL_HOURS_V1 = int(os.environ.get("SESSION_TTL_HOURS_V1", "168"))  # 7 days
GITHUB_ENCRYPTION_KEY = os.environ.get("GITHUB_ENCRYPTION_KEY", "")

def _utcnow():
    return datetime.utcnow()

def _users_col(db=None):
    if db is None:
        db = get_db()
    try:
        db["users"].create_index([("email", 1)], unique=True, sparse=True)
        db["users"].create_index([("phone", 1)], unique=True, sparse=True)
    except Exception:
        pass
    return db["users"]

def _sessions_col(db=None):
    if db is None:
        db = get_db()
    try:
        db["sessions"].create_index("expires_at", expireAfterSeconds=0)
        db["sessions"].create_index([("token", 1)], unique=True)
        db["sessions"].create_index([("user_id", 1)])
    except Exception:
        pass
    return db["sessions"]

def _stores_col(db=None):
    if db is None:
        db = get_db()
    try:
        db["stores"].create_index([("seller_id", 1)])
    except Exception:
        pass
    return db["stores"]

def _pbkdf2_hash_password(password: str, salt: bytes = None, iters: int = 200_000) -> str:
    if not isinstance(password, str) or len(password) < 6:
        raise ValueError("Password must be at least 6 characters.")
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters, dklen=32)
    return "pbkdf2$%d$%s$%s" % (
        iters,
        base64.urlsafe_b64encode(salt).decode("utf-8").rstrip("="),
        base64.urlsafe_b64encode(dk).decode("utf-8").rstrip("="),
    )

def _pbkdf2_verify_password(password: str, stored: str) -> bool:
    try:
        parts = stored.split("$")
        if len(parts) != 4 or parts[0] != "pbkdf2":
            return False
        iters = int(parts[1])
        salt = base64.urlsafe_b64decode(parts[2] + "==")
        expected = base64.urlsafe_b64decode(parts[3] + "==")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters, dklen=32)
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False

def _new_session_token() -> str:
    return secrets.token_urlsafe(32)

def _issue_session(db, user_id: str):
    token = _new_session_token()
    expires_at = _utcnow() + timedelta(hours=SESSION_TTL_HOURS_V1)
    _sessions_col(db).insert_one({
        "token": token,
        "user_id": user_id,
        "created_at": _utcnow(),
        "expires_at": expires_at
    })
    return token, expires_at

def _get_bearer_token(req):
    auth = req.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return None

def _require_auth(req, db=None):
    if db is None:
        db = get_db()
    token = _get_bearer_token(req)
    if not token:
        return None, ("Missing Authorization Bearer token", 401)
    sess = _sessions_col(db).find_one({"token": token})
    if not sess:
        return None, ("Invalid session token", 401)
    user = _users_col(db).find_one({"_id": ObjectId(sess["user_id"])}) if isinstance(sess["user_id"], str) else _users_col(db).find_one({"_id": sess["user_id"]})
    if not user:
        return None, ("User not found", 401)
    return user, None

def _require_role(user, roles):
    return user and user.get("role") in set(roles)

def _ensure_store_for_seller(db, seller_id):
    stores = _stores_col(db)
    store = stores.find_one({"seller_id": str(seller_id)})
    if store:
        return store
    new_store = {
        "seller_id": str(seller_id),
        "created_at": _utcnow(),
        "github": {"connected": False}
    }
    res = stores.insert_one(new_store)
    new_store["_id"] = res.inserted_id
    return new_store

# -----------------------------
# Lightweight PAT encryption (no external deps)
# NOTE: Best practice is OAuth or KMS-backed encryption.
# This uses a derived key + nonce XOR stream as a minimal reversible wrapper.
# -----------------------------
def _derive_key(enc_key: str, nonce: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", enc_key.encode("utf-8"), nonce, 200_000, dklen=32)

def _xor_bytes(data: bytes, key: bytes) -> bytes:
    out = bytearray(len(data))
    for i, b in enumerate(data):
        out[i] = b ^ key[i % len(key)]
    return bytes(out)

def encrypt_pat(pat: str) -> str:
    if not GITHUB_ENCRYPTION_KEY:
        raise ValueError("GITHUB_ENCRYPTION_KEY is required to store GitHub tokens securely.")
    nonce = secrets.token_bytes(16)
    key = _derive_key(GITHUB_ENCRYPTION_KEY, nonce)
    ct = _xor_bytes(pat.encode("utf-8"), key)
    return "enc1$%s$%s" % (
        base64.urlsafe_b64encode(nonce).decode("utf-8").rstrip("="),
        base64.urlsafe_b64encode(ct).decode("utf-8").rstrip("="),
    )

def decrypt_pat(enc: str) -> str:
    parts = (enc or "").split("$")
    if len(parts) != 3 or parts[0] != "enc1":
        raise ValueError("Invalid encrypted token format.")
    nonce = base64.urlsafe_b64decode(parts[1] + "==")
    ct = base64.urlsafe_b64decode(parts[2] + "==")
    key = _derive_key(GITHUB_ENCRYPTION_KEY, nonce)
    pt = _xor_bytes(ct, key)
    return pt.decode("utf-8")

# -----------------------------
# GitHub HTTP helpers (urllib)
# -----------------------------
def _gh_request(method: str, url: str, token: str, body: dict = None):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "YiThume-Vercel",
        "Authorization": f"Bearer {token}",
    }
    data = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        data = payload
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8")
            return e.code, json.loads(raw or "{}")
        except Exception:
            return e.code, {"error": "github_http_error"}
    except Exception:
        return 0, {"error": "github_request_failed"}

# -----------------------------
# V1 AUTH
# -----------------------------
@app.post("/v1/auth/register")
@app.post("/api/v1/auth/register")
def v1_register():
    db = get_db()
    data = request.get_json(force=True, silent=True) or {}

    # Role is OPTIONAL at registration. If omitted, the user will pick after login.
    role = (data.get("role") or "").lower().strip()
    if role and role not in {"admin", "seller", "driver", "network_operator", "buyer"}:
        return jsonify({"ok": False, "error": "Invalid role"}), 400

    email = (data.get("email") or "").strip().lower()
    phone = (data.get("phone") or "").strip()
    password = (data.get("password") or "")

    if not email and not phone:
        return jsonify({"ok": False, "error": "Email or phone is required"}), 400

    # Admin registration is not allowed through public register
    if role == "admin":
        return jsonify({"ok": False, "error": "Admin role cannot be registered here"}), 403

    users = _users_col(db)

    # Basic uniqueness checks
    if email and users.find_one({"email": email}):
        return jsonify({"ok": False, "error": "Email already registered"}), 409
    if phone and users.find_one({"phone": phone}):
        return jsonify({"ok": False, "error": "Phone already registered"}), 409

    try:
        pw_hash = _pbkdf2_hash_password(password)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    roles = []
    primary_role = None
    if role:
        roles = [role]
        primary_role = role

    doc = {
        "email": email or None,
        "phone": phone or None,
        "password_hash": pw_hash,
        # Backward compatible single-role field (nullable until user picks)
        "role": primary_role,
        # Multi-role support going forward
        "roles": roles,
        "created_at": _utcnow(),
        "last_login": None,
    }
    res = users.insert_one(doc)
    user_id = str(res.inserted_id)

    # Auto-create store for sellers (only once they have a seller role)
    store_id = None
    if primary_role == "seller":
        store = _ensure_store_for_seller(db, user_id)
        store_id = str(store["_id"])

    
    # Auto-create driver profile for drivers
    if primary_role == "driver":
        try:
            _ensure_driver_for_user(db, {"phone": phone, "email": email, "role": "driver"})
        except Exception:
            pass

token, expires_at = _issue_session(db, user_id)

    return jsonify({
        "ok": True,
        "token": token,
        "expires_at": expires_at.isoformat() + "Z",
        "user": {"id": user_id, "role": primary_role, "roles": roles, "email": email, "phone": phone},
        "store_id": store_id,
        "needs_role_select": True if not primary_role else False
    })


@app.post("/v1/auth/logout")
@app.post("/api/v1/auth/logout")
def v1_logout():
    db = get_db()
    user, err = _require_auth(request, db)
    if err:
        msg, code = err
        return jsonify({"ok": False, "error": msg}), code
    token = _get_bearer_token(request)
    try:
        _sessions_col(db).delete_one({"token": token})
    except Exception:
        pass
    return jsonify({"ok": True}), 200

@app.post("/v1/auth/login")
@app.post("/api/v1/auth/login")
def v1_login():
    db = get_db()
    data = request.get_json(force=True, silent=True) or {}

    requested_role = (data.get("role") or "").lower().strip()
    email = (data.get("email") or "").strip().lower()
    phone = (data.get("phone") or "").strip()
    password = (data.get("password") or "")

    # Admin login is via ADMIN_SECRET (PIN) for backward compatibility
    if requested_role == "admin":
        pin = (data.get("admin_secret") or data.get("pin") or "").strip()
        if not ADMIN_SECRET:
            return jsonify({"ok": False, "error": "ADMIN_SECRET not set"}), 500
        if pin != str(ADMIN_SECRET):
            return jsonify({"ok": False, "error": "Invalid admin secret"}), 401

        users = _users_col(db)
        admin = users.find_one({"role": "admin", "email": "admin@yithume.local"})
        if not admin:
            res = users.insert_one({
                "email": "admin@yithume.local",
                "phone": None,
                "password_hash": None,
                "role": "admin",
                "roles": ["admin"],
                "created_at": _utcnow(),
                "last_login": _utcnow(),
            })
            admin_id = str(res.inserted_id)
        else:
            admin_id = str(admin["_id"])
            users.update_one({"_id": admin["_id"]}, {"$set": {"last_login": _utcnow()}})
        token, expires_at = _issue_session(db, admin_id)
        return jsonify({
            "ok": True,
            "token": token,
            "expires_at": expires_at.isoformat() + "Z",
            "user": {"id": admin_id, "role": "admin", "roles": ["admin"]}
        })

    users = _users_col(db)

    q = {}
    if email:
        q["email"] = email
    elif phone:
        q["phone"] = phone
    else:
        return jsonify({"ok": False, "error": "Email or phone is required"}), 400

    user = users.find_one(q)
    if not user:
        return jsonify({"ok": False, "error": "Invalid credentials"}), 401

    if not user.get("password_hash") or not _pbkdf2_verify_password(password, user["password_hash"]):
        return jsonify({"ok": False, "error": "Invalid credentials"}), 401

    # Normalize roles for older users
    roles = user.get("roles") or []
    legacy_role = (user.get("role") or None)
    if legacy_role and legacy_role not in roles:
        roles = roles + [legacy_role]

    # If caller requested a role, it must be one the user has (or we'll ask them to pick)
    active_role = legacy_role
    if requested_role:
        if requested_role in roles:
            active_role = requested_role
        else:
            return jsonify({"ok": False, "error": "Role not allowed for this account", "roles": roles}), 403

    # If the user has no active role yet, force role selection after login
    needs_role_select = False
    if not active_role:
        needs_role_select = True

    users.update_one({"_id": user["_id"]}, {"$set": {"last_login": _utcnow(), "roles": roles, "role": active_role}})

    user_id = str(user["_id"])
    store_id = None
    if active_role == "seller":
        store = _ensure_store_for_seller(db, user_id)
        store_id = str(store["_id"])

    token, expires_at = _issue_session(db, user_id)

    return jsonify({
        "ok": True,
        "token": token,
        "expires_at": expires_at.isoformat() + "Z",
        "user": {"id": user_id, "role": active_role, "roles": roles, "email": user.get("email"), "phone": user.get("phone")},
        "store_id": store_id,
        "needs_role_select": needs_role_select
    })

@app.get("/v1/auth/me")
@app.get("/api/v1/auth/me")
def v1_me():
    db = get_db()
    user, err = _require_auth(request, db)
    if err:
        msg, code = err
        return jsonify({"ok": False, "error": msg}), code

    roles = user.get("roles") or []
    legacy_role = user.get("role") or None
    if legacy_role and legacy_role not in roles:
        roles = roles + [legacy_role]

    out = {
        "id": str(user["_id"]),
        "role": legacy_role,
        "roles": roles,
        "email": user.get("email"),
        "phone": user.get("phone")
    }
    return jsonify({"ok": True, "user": out})


@app.post("/v1/auth/set_role")
@app.post("/api/v1/auth/set_role")
def v1_set_role():
    db = get_db()
    user, err = _require_auth(request, db)
    if err:
        msg, code = err
        return jsonify({"ok": False, "error": msg}), code

    data = request.get_json(force=True, silent=True) or {}
    role = (data.get("role") or "").lower().strip()
    if role not in {"seller", "driver", "network_operator", "buyer"}:
        return jsonify({"ok": False, "error": "Invalid role"}), 400

    users = _users_col(db)

    roles = user.get("roles") or []
    legacy_role = user.get("role") or None
    if legacy_role and legacy_role not in roles:
        roles = roles + [legacy_role]
    if role not in roles:
        roles = roles + [role]

    # Set active role (legacy field) for compatibility
    users.update_one({"_id": user["_id"]}, {"$set": {"role": role, "roles": roles}})

    store_id = None
    if role == "seller":
        store = _ensure_store_for_seller(db, str(user["_id"]))
        store_id = str(store["_id"])

    return jsonify({"ok": True, "role": role, "roles": roles, "store_id": store_id})
# -----------------------------
# Account (buyer settings)
# -----------------------------
@app.get("/v1/account/profile")
@app.get("/api/v1/account/profile")
def v1_account_profile_get():
    db = get_db()
    user, err = _require_auth(request, db)
    if err:
        msg, code = err
        return jsonify({"ok": False, "error": msg}), code

    users = _users_col(db)
    doc = users.find_one({"_id": user["_id"]}) or {}
    profile = {
        "email": doc.get("email"),
        "phone": doc.get("phone"),
        "name": doc.get("name") or "",
        "billing_address": doc.get("billing_address") or {},
        "delivery_address": doc.get("delivery_address") or {}
    }
    
    # Ensure driver profile exists when switching to driver
    if role == "driver":
        try:
            _ensure_driver_for_user(db, user)
        except Exception:
            pass
return jsonify({"ok": True, "profile": profile}), 200


@app.post("/v1/account/profile")
@app.post("/api/v1/account/profile")
def v1_account_profile_post():
    db = get_db()
    user, err = _require_auth(request, db)
    if err:
        msg, code = err
        return jsonify({"ok": False, "error": msg}), code

    data = request.get_json(force=True, silent=True) or {}
    # Keep it flexible — UI can send partial updates.
    patch = {}
    for k in ("name", "phone"):
        if k in data and isinstance(data.get(k), str):
            patch[k] = data.get(k).strip()
    for k in ("billing_address", "delivery_address"):
        if k in data and isinstance(data.get(k), dict):
            patch[k] = data.get(k)

    if not patch:
        return jsonify({"ok": True, "updated": False}), 200

    users = _users_col(db)
    users.update_one({"_id": user["_id"]}, {"$set": patch})
    return jsonify({"ok": True, "updated": True}), 200


@app.get("/v1/account/orders")
@app.get("/api/v1/account/orders")
def v1_account_orders():
    db = get_db()
    user, err = _require_auth(request, db)
    if err:
        msg, code = err
        return jsonify({"ok": False, "error": msg}), code

    email = (user.get("email") or "").lower().strip()
    limit = int(request.args.get("limit", "50"))
    limit = max(1, min(limit, 200))

    q = {"customer_email": email} if email else {"user_id": str(user["_id"])}
    try:
        cur = db.orders.find(q).sort("created_at", DESCENDING).limit(limit)
        orders = [safe_doc(o) for o in cur]
        return jsonify({"ok": True, "orders": orders}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e), "orders": []}), 500


# -----------------------------
# V1 STORE HELPERS
# -----------------------------
@app.get("/v1/store/my")
@app.get("/api/v1/store/my")
def v1_store_my():
    db = get_db()
    user, err = _require_auth(request, db)
    if err:
        msg, code = err
        return jsonify({"ok": False, "error": msg}), code
    if user.get("role") != "seller":
        return jsonify({"ok": False, "error": "Seller role required"}), 403
    store = _ensure_store_for_seller(db, str(user["_id"]))
    return jsonify({"ok": True, "store": {"id": str(store["_id"]), "seller_id": store["seller_id"], "github": {k:v for k,v in (store.get("github") or {}).items() if k != "token_enc"}}})

# -----------------------------
# STOREFRONT HTML UPLOAD (BUG FIX)
# -----------------------------
@app.post("/v1/store/<store_id>/storefront_html")
@app.post("/api/v1/store/<store_id>/storefront_html")
def v1_storefront_upload(store_id):
    db = get_db()
    user, err = _require_auth(request, db)
    if err:
        msg, code = err
        return jsonify({"ok": False, "error": msg}), code
    if user.get("role") != "seller":
        return jsonify({"ok": False, "error": "Seller role required"}), 403

    store = _stores_col(db).find_one({"_id": ObjectId(store_id)})
    if not store or store.get("seller_id") != str(user["_id"]):
        return jsonify({"ok": False, "error": "Store not found or not owned"}), 404

    if "html" not in request.files:
        return jsonify({"ok": False, "error": "multipart field 'html' is required"}), 400

    f = request.files["html"]
    html_bytes = f.read()
    if not html_bytes:
        return jsonify({"ok": False, "error": "Empty HTML file"}), 400

    filename = f"storefront_{store_id}.html"
    fs = get_fs(db)

    # Remove previous version if exists
    try:
        existing = fs.find_one({"filename": filename})
        if existing:
            fs.delete(existing._id)
    except Exception:
        pass

    file_id = fs.put(html_bytes, filename=filename, contentType="text/html", metadata={
        "store_id": store_id,
        "seller_id": str(user["_id"]),
        "uploaded_at": _utcnow()
    })

    _stores_col(db).update_one({"_id": ObjectId(store_id)}, {"$set": {"storefront_file_id": file_id, "storefront_updated_at": _utcnow()}})

    return jsonify({"ok": True, "store_id": store_id, "file_id": str(file_id), "open_url": f"/s/{store_id}"})

def _ensure_driver_for_user(db, user):
    """
    Ensure there is a drivers document for a user when their active role is driver.
    Links by phone (preferred) or email if you later add email to drivers.
    """
    phone = _str(user.get("phone"))
    if not phone:
        return None
    d = db.drivers.find_one({"phone": phone})
    if d:
        return d
    # Create minimal driver profile
    internal_id = str(uuid.uuid4())
    doc = {
        "_internal_id": internal_id,
        "driver_id": f"DRV-{internal_id[:6].upper()}",
        "name": _str(user.get("email") or user.get("phone") or "Driver"),
        "phone": phone,
        "vehicle": "car",
        "active": True,
        "available": True,
        "current_location": {"lat": None, "lng": None, "updated_at": _now_dt()},
        "weekly_payout_due": 0.0,
        "earnings_history": [],
        "ratings": {"count": 0, "avg": None},
        "docs": {"id_doc_ref": None, "licence_ref": None, "vehicle_reg_ref": None},
        "auth": {"pin_hash": None, "pin_expiry": None, "sessions": []},
        "meta": {"zone": None},
        "created_at": _now_dt(),
    }
    db.drivers.insert_one(doc)
    return doc


@app.get("/v1/driver/me")
@app.get("/api/v1/driver/me")
def v1_driver_me():
    db = get_db()
    user, err = _require_auth(request, db)
    if err:
        msg, code = err
        return jsonify({"ok": False, "error": msg}), code
    if (user.get("role") or "") != "driver":
        return jsonify({"ok": False, "error": "Not a driver"}), 403
    d = _ensure_driver_for_user(db, user)
    if not d:
        return jsonify({"ok": False, "error": "Driver profile missing phone"}), 400
    return jsonify({"ok": True, "driver": safe_doc(d)}), 200


@app.get("/v1/driver/kpis")
@app.get("/api/v1/driver/kpis")
def v1_driver_kpis():
    db = get_db()
    user, err = _require_auth(request, db)
    if err:
        msg, code = err
        return jsonify({"ok": False, "error": msg}), code
    if (user.get("role") or "") != "driver":
        return jsonify({"ok": False, "error": "Not a driver"}), 403
    d = _ensure_driver_for_user(db, user)
    if not d:
        return jsonify({"ok": False, "error": "Driver profile missing phone"}), 400

    did = d.get("_internal_id")
    now = _now_dt()
    day_start = datetime(now.year, now.month, now.day)
    week_ago = now - timedelta(days=7)

    # Orders KPIs (logistics collection)
    assigned_active = db.orders.count_documents({"assigned_driver_id": did, "status": {"$in": ["queued", "assigned", "picked_up", "dispatched"]}})
    delivered_7d = db.orders.count_documents({"assigned_driver_id": did, "status": "delivered", "delivered_at": {"$gte": week_ago}})
    delivered_today = db.orders.count_documents({"assigned_driver_id": did, "status": "delivered", "delivered_at": {"$gte": day_start}})
    pending_pickups = db.orders.count_documents({"assigned_driver_id": did, "status": {"$in": ["assigned", "picked_up"]}})

    # Earnings / ratings from driver doc
    payout_due = float(d.get("weekly_payout_due") or 0.0)
    rating_avg = (d.get("ratings") or {}).get("avg")
    rating_cnt = int((d.get("ratings") or {}).get("count") or 0)

    return jsonify({
        "ok": True,
        "driver": {"id": did, "name": d.get("name"), "phone": d.get("phone")},
        "kpis": {
            "assigned_active": assigned_active,
            "pending_pickups": pending_pickups,
            "delivered_today": delivered_today,
            "delivered_7d": delivered_7d,
            "payout_due": payout_due,
            "rating_avg": rating_avg,
            "rating_count": rating_cnt
        }
    }), 200


@app.get("/v1/driver/orders")
@app.get("/api/v1/driver/orders")
def v1_driver_orders():
    db = get_db()
    user, err = _require_auth(request, db)
    if err:
        msg, code = err
        return jsonify({"ok": False, "error": msg}), code
    if (user.get("role") or "") != "driver":
        return jsonify({"ok": False, "error": "Not a driver"}), 403
    d = _ensure_driver_for_user(db, user)
    if not d:
        return jsonify({"ok": False, "error": "Driver profile missing phone"}), 400
    did = d.get("_internal_id")
    cur = db.orders.find({"assigned_driver_id": did}).sort("created_at", -1).limit(50)
    return jsonify({"ok": True, "orders": [safe_doc(o) for o in cur]}), 200



@app.get("/s/<store_id>")
def public_storefront(store_id):
    db = get_db()
    fs = get_fs(db)
    filename = f"storefront_{store_id}.html"
    file_obj = fs.find_one({"filename": filename})
    if not file_obj:
        return "<h1>Storefront not found</h1>", 404
    data = file_obj.read()
    return app.response_class(data, mimetype="text/html")

# -----------------------------
# GITHUB CONNECT
# -----------------------------
@app.post("/v1/github/connect")
@app.post("/api/v1/github/connect")
def v1_github_connect():
    db = get_db()
    user, err = _require_auth(request, db)
    if err:
        msg, code = err
        return jsonify({"ok": False, "error": msg}), code
    if user.get("role") != "seller":
        return jsonify({"ok": False, "error": "Seller role required"}), 403

    data = request.get_json(force=True, silent=True) or {}
    store_id = (data.get("store_id") or "").strip()
    gh_user = (data.get("username") or "").strip()
    repo = (data.get("repo") or "").strip()
    branch = (data.get("branch") or "main").strip()
    pat = (data.get("pat") or "").strip()

    if not (store_id and gh_user and repo and pat):
        return jsonify({"ok": False, "error": "store_id, username, repo, and pat are required"}), 400

    store = _stores_col(db).find_one({"_id": ObjectId(store_id)})
    if not store or store.get("seller_id") != str(user["_id"]):
        return jsonify({"ok": False, "error": "Store not found or not owned"}), 404

    try:
        token_enc = encrypt_pat(pat)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    _stores_col(db).update_one({"_id": ObjectId(store_id)}, {"$set": {
        "github": {
            "connected": True,
            "username": gh_user,
            "repo": repo,
            "branch": branch,
            "token_enc": token_enc,
            "connected_at": _utcnow()
        }
    }})

    return jsonify({"ok": True, "connected": True, "repo": {"username": gh_user, "repo": repo, "branch": branch}})

@app.get("/v1/github/status")
@app.get("/api/v1/github/status")
def v1_github_status():
    db = get_db()
    user, err = _require_auth(request, db)
    if err:
        msg, code = err
        return jsonify({"ok": False, "error": msg}), code
    if user.get("role") != "seller":
        return jsonify({"ok": False, "error": "Seller role required"}), 403

    store_id = (request.args.get("store_id") or "").strip()
    if not store_id:
        store = _ensure_store_for_seller(db, str(user["_id"]))
        store_id = str(store["_id"])

    store = _stores_col(db).find_one({"_id": ObjectId(store_id)})
    if not store or store.get("seller_id") != str(user["_id"]):
        return jsonify({"ok": False, "error": "Store not found or not owned"}), 404

    gh = store.get("github") or {}
    return jsonify({"ok": True, "connected": bool(gh.get("connected")), "repo": {"username": gh.get("username"), "repo": gh.get("repo"), "branch": gh.get("branch")}})

@app.post("/v1/github/test")
@app.post("/api/v1/github/test")
def v1_github_test():
    db = get_db()
    user, err = _require_auth(request, db)
    if err:
        msg, code = err
        return jsonify({"ok": False, "error": msg}), code
    if user.get("role") != "seller":
        return jsonify({"ok": False, "error": "Seller role required"}), 403

    data = request.get_json(force=True, silent=True) or {}
    store_id = (data.get("store_id") or "").strip()
    if not store_id:
        return jsonify({"ok": False, "error": "store_id required"}), 400

    store = _stores_col(db).find_one({"_id": ObjectId(store_id)})
    if not store or store.get("seller_id") != str(user["_id"]):
        return jsonify({"ok": False, "error": "Store not found or not owned"}), 404

    gh = store.get("github") or {}
    if not gh.get("connected") or not gh.get("token_enc"):
        return jsonify({"ok": False, "error": "GitHub not connected"}), 400

    try:
        pat = decrypt_pat(gh["token_enc"])
    except Exception:
        return jsonify({"ok": False, "error": "Failed to decrypt token"}), 500

    url = f"https://api.github.com/repos/{gh.get('username')}/{gh.get('repo')}"
    status, payload = _gh_request("GET", url, pat)
    if status == 200:
        return jsonify({"ok": True, "repo": {"full_name": payload.get("full_name"), "private": payload.get("private")}})
    return jsonify({"ok": False, "status": status, "details": payload}), 400

@app.post("/v1/github/commit_storefront")
@app.post("/api/v1/github/commit_storefront")
def v1_github_commit_storefront():
    db = get_db()
    user, err = _require_auth(request, db)
    if err:
        msg, code = err
        return jsonify({"ok": False, "error": msg}), code
    if user.get("role") != "seller":
        return jsonify({"ok": False, "error": "Seller role required"}), 403

    data = request.get_json(force=True, silent=True) or {}
    store_id = (data.get("store_id") or "").strip()
    if not store_id:
        return jsonify({"ok": False, "error": "store_id required"}), 400

    store = _stores_col(db).find_one({"_id": ObjectId(store_id)})
    if not store or store.get("seller_id") != str(user["_id"]):
        return jsonify({"ok": False, "error": "Store not found or not owned"}), 404

    gh = store.get("github") or {}
    if not gh.get("connected") or not gh.get("token_enc"):
        return jsonify({"ok": False, "error": "GitHub not connected"}), 400

    try:
        pat = decrypt_pat(gh["token_enc"])
    except Exception:
        return jsonify({"ok": False, "error": "Failed to decrypt token"}), 500

    # Load storefront HTML from GridFS
    fs = get_fs(db)
    filename = f"storefront_{store_id}.html"
    file_obj = fs.find_one({"filename": filename})
    if not file_obj:
        return jsonify({"ok": False, "error": "No storefront HTML uploaded yet"}), 400
    html_bytes = file_obj.read()

    path = f"storefronts/{store_id}/index.html"
    owner = gh.get("username")
    repo = gh.get("repo")
    branch = gh.get("branch") or "main"

    # Get existing file SHA if any
    get_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{quote(path)}?ref={quote(branch)}"
    status, existing = _gh_request("GET", get_url, pat)

    sha = existing.get("sha") if status == 200 else None

    put_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{quote(path)}"
    body = {
        "message": f"Publish storefront for store {store_id}",
        "content": base64.b64encode(html_bytes).decode("utf-8"),
        "branch": branch
    }
    if sha:
        body["sha"] = sha

    status2, payload2 = _gh_request("PUT", put_url, pat, body=body)
    if status2 in (200, 201):
        commit_sha = (payload2.get("commit") or {}).get("sha")
        html_url = ((payload2.get("content") or {}).get("html_url")) or None
        return jsonify({"ok": True, "commit_sha": commit_sha, "file_url": html_url, "path": path})
    return jsonify({"ok": False, "status": status2, "details": payload2}), 400
