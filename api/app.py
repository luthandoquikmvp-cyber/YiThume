# app.py — YiThume: software for creating marketplaces on a shared runner network.
#
# YiThume is not a delivery business. It is the layer between hard-to-reach
# markets and their customers: anyone can create their own marketplace, and the
# courier network is already underneath it.
#
# One clean backend: a DB with an API on top.
#   - MongoDB when MONGO_URI is set (persistent, production)
#   - In-memory demo store otherwise (works instantly, resets on restart)
#
# Tenancy: a `marketplace` IS the tenant. Products, sellers, orders and nodes
# all carry `marketplace_id`. Runners with marketplace_id == "" are the shared
# YiThume pool that gives a brand-new marketplace delivery on day one.
#
# Surfaces: /              → landing page (create your marketplace)
#           /markets       → directory of live marketplaces
#           /m/<slug>      → a tenant's branded storefront
#           /console       → operator console (phone + PIN)
#           /dashboard     → platform admin (all tenants, ledger, payouts)
#           /runner        → runner mobile view (jobs, PIN delivery, cash, remit)
#           /kiosk         → pickup-point operator view (arrivals, collections)
#           /ussd          → USSD simulator (POST /api/ussd is aggregator-shaped)

import os
import re
import copy
import json
import uuid
import random
import string
import hmac
import hashlib
import threading
from datetime import datetime
from urllib.parse import quote

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

try:
    from . import whatsapp, payments, ussd, ledger, rails
except ImportError:
    import whatsapp
    import payments
    import ussd
    import ledger
    import rails

# -------------------------------------------------
# CONFIG
# -------------------------------------------------
MONGO_URI = os.environ.get("MONGO_URI", "").strip()
DB_NAME = os.environ.get("MONGO_DB", "yithume")

ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "1234")  # override in production
WHATSAPP_NUMBER = os.environ.get("WHATSAPP_NUMBER", "27600000000")  # no leading +
AUTO_SEED = os.environ.get("AUTO_SEED", "true").lower() == "true"

# Editable from the dashboard (Settings tab); env vars only set the defaults
# used when the settings record is first created.
DEFAULT_SETTINGS = {
    "delivery_fee": float(os.environ.get("DELIVERY_FEE", "25.0")),
    # marketplace_rate: the OPERATOR's cut of their sellers' subtotal (tenant-editable)
    "marketplace_rate": float(os.environ.get("MARKETPLACE_FEE_RATE", "0.10")),
    # network_rate: YiThume's cut of the subtotal (platform-only)
    "network_rate": float(os.environ.get("NETWORK_FEE_RATE", "0.02")),
    "runner_rate": float(os.environ.get("RUNNER_FEE_RATE", "0.80")),       # of delivery fee
    "node_rate": float(os.environ.get("NODE_FEE_RATE", "0.20")),           # of delivery fee
    "cod_cap": float(os.environ.get("COD_CAP", "400.0")),                  # above this: no COD
    "cod_surcharge": float(os.environ.get("COD_SURCHARGE", "10.0")),       # COD costs more to serve
    "strike_limit": int(os.environ.get("STRIKE_LIMIT", "2")),              # strikes → prepay only
    "runner_cash_limit": float(os.environ.get("RUNNER_CASH_LIMIT", "1000.0")),
    # failed-delivery economics (§ who pays when a COD buyer no-shows)
    "reserve_rate": float(os.environ.get("RESERVE_RATE", "0.01")),         # of delivery fee → reserve
    "failed_delivery_fee": float(os.environ.get("FAILED_DELIVERY_FEE", "25.0")),
    "failed_attempt_fee_rate": float(os.environ.get("FAILED_ATTEMPT_FEE_RATE", "0.60")),
    "dispute_window_hours": float(os.environ.get("DISPUTE_WINDOW_HOURS", "24")),
    "float_min": float(os.environ.get("FLOAT_MIN", "500.0")),
    "eft_details": os.environ.get(
        "EFT_DETAILS", "EFT to: YiThume, Capitec, account 1234567890."),
}

# What a marketplace operator may change on their own settings. Everything else
# in DEFAULT_SETTINGS is platform-controlled.
OPERATOR_SETTINGS = ["delivery_fee", "marketplace_rate", "cod_cap", "cod_surcharge",
                     "eft_details"]

SETTINGS_NUMERIC = ["delivery_fee", "marketplace_rate", "network_rate", "runner_rate",
                    "node_rate", "cod_cap", "cod_surcharge", "runner_cash_limit",
                    "reserve_rate", "failed_delivery_fee", "failed_attempt_fee_rate",
                    "dispute_window_hours", "float_min"]

# Two-leg fulfilment: a carrier moves the parcel to a pickup point, then a
# runner does the last mile — or the buyer collects at the point.
STATUS_FLOW = {
    "awaiting_otp": ["pending", "cancelled"],
    "pending": ["confirmed", "cancelled"],
    "confirmed": ["assigned", "in_transit", "cancelled"],
    "in_transit": ["at_point", "cancelled"],          # carrier leg
    "at_point": ["assigned", "collected", "cancelled"],
    "assigned": ["picked_up", "cancelled"],
    "picked_up": ["delivered", "cancelled"],
    "delivered": [],
    "collected": [],
    "cancelled": [],
}
# Terminal states that mean the buyer got their goods.
FULFILLED = ("delivered", "collected")

COLLECTIONS = ["products", "sellers", "runners", "orders", "nodes", "marketplaces",
               "buyers", "remittances", "settings", "ussd_sessions",
               "sessions", "accounts", "ledger", "topups", "withdrawals",
               "disputes", "carriers", "pickup_points"]

# Trust tiers gate how much money an unproven marketplace can touch.
TRUST_TIERS = {
    "unverified": {"escrow_cap": 500.0, "withdrawals": False, "deliveries_required": 0},
    "verified": {"escrow_cap": 5000.0, "withdrawals": True, "deliveries_required": 5},
    "trusted": {"escrow_cap": 100000.0, "withdrawals": True, "deliveries_required": 50},
}

payment_provider = payments.get_provider()
rail = rails.get_rail()

PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")


def LD():
    """Ledger dependencies — bound late so `col` is always the live store."""
    return ledger.Deps(col=col, now=_now, new_id=_new_id)

# -------------------------------------------------
# STORAGE — Mongo when configured, in-memory otherwise
# -------------------------------------------------
def _now():
    return datetime.utcnow().isoformat() + "Z"


def _new_id():
    return uuid.uuid4().hex


def _order_code():
    return "YT-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=5))


def _otp_code():
    return "".join(random.choices(string.digits, k=6))


def _pin_code():
    return "".join(random.choices(string.digits, k=4))


def _token():
    return uuid.uuid4().hex + uuid.uuid4().hex


def _ref_code(prefix):
    return f"{prefix}-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def _slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return slug[:40] or "market"


def _unique_slug(name):
    base = _slugify(name)
    slug, n = base, 1
    while col("marketplaces").find_one({"slug": slug}):
        n += 1
        slug = f"{base}-{n}"
    return slug


def _hash_pin(pin, salt=None):
    """PBKDF2 via stdlib — no new dependency, and the PIN is never stored raw."""
    salt = salt or uuid.uuid4().hex
    digest = hashlib.pbkdf2_hmac("sha256", str(pin).encode(), salt.encode(), 120000)
    return digest.hex(), salt


def _check_pin(pin, pin_hash, salt):
    if not pin_hash or not salt:
        return False
    candidate, _ = _hash_pin(pin, salt)
    return hmac.compare_digest(candidate, pin_hash)


def _get_path(doc, key):
    cur = doc
    for part in key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


class MemoryCollection:
    """Tiny pymongo-compatible subset: equality (incl. dotted paths), $or, $in,
    $ne, $regex filters."""

    def __init__(self):
        self._docs = []
        self._lock = threading.Lock()

    @staticmethod
    def _match(doc, flt):
        for key, cond in (flt or {}).items():
            if key == "$or":
                if not any(MemoryCollection._match(doc, sub) for sub in cond):
                    return False
                continue
            val = _get_path(doc, key)
            if isinstance(cond, dict):
                if "$regex" in cond:
                    flags = re.I if "i" in cond.get("$options", "") else 0
                    if not re.search(cond["$regex"], str(val or ""), flags):
                        return False
                elif "$in" in cond:
                    if val not in cond["$in"]:
                        return False
                elif "$ne" in cond:
                    if val == cond["$ne"]:
                        return False
                else:
                    if val != cond:
                        return False
            elif val != cond:
                return False
        return True

    def find(self, flt=None):
        with self._lock:
            return [copy.deepcopy(d) for d in self._docs if self._match(d, flt)]

    def find_one(self, flt=None):
        with self._lock:
            for d in self._docs:
                if self._match(d, flt):
                    return copy.deepcopy(d)
        return None

    def insert_one(self, doc):
        with self._lock:
            self._docs.append(copy.deepcopy(doc))

    def update_one(self, flt, update):
        with self._lock:
            for d in self._docs:
                if self._match(d, flt):
                    d.update(copy.deepcopy(update.get("$set", {})))
                    return True
        return False

    def delete_one(self, flt):
        with self._lock:
            for i, d in enumerate(self._docs):
                if self._match(d, flt):
                    self._docs.pop(i)
                    return True
        return False

    def count_documents(self, flt=None):
        with self._lock:
            return sum(1 for d in self._docs if self._match(d, flt))


_memory = {name: MemoryCollection() for name in COLLECTIONS}
_mongo_db = None
_mongo_failed = False
_seeded = False
_store_lock = threading.Lock()


def _connect_mongo():
    global _mongo_db, _mongo_failed
    if _mongo_db is not None or _mongo_failed or not MONGO_URI:
        return
    try:
        from pymongo import MongoClient

        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=4000)
        client.admin.command("ping")
        _mongo_db = client[DB_NAME]
    except Exception:
        _mongo_failed = True


def col(name):
    _connect_mongo()
    _maybe_seed()
    if _mongo_db is not None:
        return _mongo_db[name]
    return _memory[name]


def storage_mode():
    _connect_mongo()
    return "mongodb" if _mongo_db is not None else "memory (demo — set MONGO_URI to persist)"


def find_all(name, flt=None):
    c = col(name)
    return list(c.find(flt or {}))


# -------------------------------------------------
# SETTINGS — fee split & policy config, editable from the dashboard
# -------------------------------------------------
def get_settings(marketplace_id=""):
    """Platform defaults, with a tenant's overrides layered on top.

    The `settings` singleton holds platform-wide defaults; a document keyed by a
    marketplace_id holds only that tenant's overrides.
    """
    doc = col("settings").find_one({"_id": "settings"})
    if not doc:
        doc = {"_id": "settings", **DEFAULT_SETTINGS, "updated_at": _now()}
        col("settings").insert_one(doc)
    for k, v in DEFAULT_SETTINGS.items():
        doc.setdefault(k, v)
    if marketplace_id:
        override = col("settings").find_one({"_id": marketplace_id})
        if override:
            for k in OPERATOR_SETTINGS:
                if k in override:
                    doc[k] = override[k]
    return doc


# -------------------------------------------------
# SEED DATA — so the product works out of the box
# -------------------------------------------------
def _seed_marketplace(name, areas, owner, phone, pin, tagline, accent, tier="verified",
                      deliveries=0):
    pin_hash, salt = _hash_pin(pin)
    return {
        "_id": _new_id(), "slug": _slugify(name), "name": name, "tagline": tagline,
        "about": "", "logo_url": "", "accent": accent,
        "owner_name": owner, "phone": phone, "pin_hash": pin_hash, "pin_salt": salt,
        "areas": areas, "use_shared_runners": True,
        "trust": {"tier": tier, "score": deliveries, "deliveries": deliveries,
                  "disputes": 0, "refunds": 0, "bank_submitted": tier != "unverified"},
        "status": "live", "otp": "", "otp_attempts": 0,
        "bank": {}, "created_at": _now(),
    }


def _seed_docs():
    """Two live marketplaces on one shared runner pool — the shape the product
    is actually about. Demo PINs are in the README."""
    mthatha = _seed_marketplace(
        "Mthatha Town Market", ["Mthatha", "Ngangelizwe"], "Thandi Jara",
        "27720000001", "1234", "Everything from the town market, delivered.",
        "emerald", tier="verified", deliveries=12)
    kei = _seed_marketplace(
        "Kei Fresh Market", ["Butterworth", "Idutywa"], "Bongani Sithole",
        "27720000002", "4321", "Fresh produce from the Kei, to your door.",
        "amber", tier="unverified", deliveries=2)
    marketplaces = [mthatha, kei]

    sellers = [
        {"_id": _new_id(), "marketplace_id": mthatha["_id"], "shop_name": "Nomsa's Spaza",
         "owner_name": "Nomsa Dlamini", "phone": "27710000001", "area": "Mthatha",
         "approved": True, "strikes": 0, "created_at": _now()},
        {"_id": _new_id(), "marketplace_id": mthatha["_id"], "shop_name": "Lwazi Electronics",
         "owner_name": "Lwazi Nkosi", "phone": "27710000003", "area": "Mthatha",
         "approved": True, "strikes": 0, "created_at": _now()},
        {"_id": _new_id(), "marketplace_id": kei["_id"], "shop_name": "Kei Fresh Produce",
         "owner_name": "Sipho Mbeki", "phone": "27710000002", "area": "Butterworth",
         "approved": True, "strikes": 0, "created_at": _now()},
    ]

    # marketplace_id "" == the shared YiThume pool. This is what lets a brand
    # new marketplace deliver on its first day.
    runners = [
        {"_id": _new_id(), "marketplace_id": "", "name": "Andile M.", "phone": "27730000001",
         "area": "Mthatha", "vehicle": "Motorbike", "status": "online", "approved": True,
         "deliveries_done": 128, "created_at": _now()},
        {"_id": _new_id(), "marketplace_id": "", "name": "Zinhle K.", "phone": "27730000002",
         "area": "Butterworth", "vehicle": "Bicycle", "status": "online", "approved": True,
         "deliveries_done": 74, "created_at": _now()},
        {"_id": _new_id(), "marketplace_id": "", "name": "Thabo S.", "phone": "27730000003",
         "area": "Idutywa", "vehicle": "Car", "status": "online", "approved": True,
         "deliveries_done": 211, "created_at": _now()},
        # One marketplace-exclusive runner, to show the priority rule working.
        {"_id": _new_id(), "marketplace_id": mthatha["_id"], "name": "Sive N.",
         "phone": "27730000004", "area": "Mthatha", "vehicle": "On foot", "status": "online",
         "approved": True, "deliveries_done": 9, "created_at": _now()},
    ]

    nodes = [
        {"_id": _new_id(), "marketplace_id": "", "name": "Mthatha Node",
         "operator_name": "Vuyo Mda", "phone": "27740000001",
         "territory": "Mthatha & surrounds", "commission_rate": 0.05,
         "runners_count": 2, "created_at": _now()},
    ]

    # The Courier Guy runs as a manual carrier: an operator books on their own
    # system and pastes the waybill. No partnership needed to ship.
    carriers = [
        {"_id": _new_id(), "name": "The Courier Guy", "slug": "courier-guy", "mode": "manual",
         "areas": ["Mthatha", "Butterworth", "Idutywa"],
         "services": ["point_to_point"], "active": True, "created_at": _now()},
    ]
    points = [
        {"_id": _new_id(), "marketplace_id": "", "name": "Mthatha Plaza Kiosk", "type": "kiosk",
         "carrier_id": carriers[0]["_id"], "area": "Mthatha", "address": "Shop 4, Mthatha Plaza",
         "contact_name": "Nosipho", "phone": "27750000001", "hours": "08:00–18:00",
         "capacity": 60, "active": True, "created_at": _now()},
        {"_id": _new_id(), "marketplace_id": "", "name": "Butterworth Depot", "type": "depot",
         "carrier_id": carriers[0]["_id"], "area": "Butterworth", "address": "12 High Street",
         "contact_name": "Sabelo", "phone": "27750000002", "hours": "08:00–17:00",
         "capacity": 120, "active": True, "created_at": _now()},
    ]

    s = {x["shop_name"]: x for x in sellers}
    products = [
        ("Maize Meal 10kg", 89.99, "Groceries", "Nomsa's Spaza", 40),
        ("Cooking Oil 2L", 74.50, "Groceries", "Nomsa's Spaza", 35),
        ("Sugar 5kg", 62.00, "Groceries", "Nomsa's Spaza", 50),
        ("Paraffin 5L", 120.00, "Household", "Nomsa's Spaza", 20),
        ("Washing Powder 2kg", 68.00, "Household", "Nomsa's Spaza", 32),
        ("Phone Charger (USB-C)", 95.00, "Electronics", "Lwazi Electronics", 25),
        ("Solar Lantern", 189.00, "Electronics", "Lwazi Electronics", 15),
        ("Airtime Voucher R50", 50.00, "Airtime & Data", "Lwazi Electronics", 999),
        ("Fresh Spinach Bundle", 18.00, "Fresh Produce", "Kei Fresh Produce", 60),
        ("Tomatoes 1kg", 22.50, "Fresh Produce", "Kei Fresh Produce", 45),
        ("Free-range Eggs (18)", 55.00, "Fresh Produce", "Kei Fresh Produce", 30),
        ("Butternut 2kg", 39.00, "Fresh Produce", "Kei Fresh Produce", 24),
    ]
    mkt_by_id = {m["_id"]: m for m in marketplaces}
    product_docs = []
    for name, price, cat, seller_name, stock in products:
        seller = s[seller_name]
        mkt = mkt_by_id[seller["marketplace_id"]]
        product_docs.append({
            "_id": _new_id(), "name": name, "price": price, "category": cat,
            "image_url": "", "seller_id": seller["_id"], "seller_name": seller_name,
            "marketplace_id": mkt["_id"], "marketplace_name": mkt["name"],
            "stock": stock, "active": True, "created_at": _now(),
        })
    return {"marketplaces": marketplaces, "sellers": sellers, "runners": runners,
            "nodes": nodes, "carriers": carriers, "pickup_points": points,
            "products": product_docs, "orders": []}


def _maybe_seed():
    global _seeded
    if _seeded or not AUTO_SEED:
        return
    with _store_lock:
        if _seeded:
            return
        target = _mongo_db if _mongo_db is not None else None
        try:
            if target is not None:
                empty = target["products"].count_documents({}) == 0
            else:
                empty = _memory["products"].count_documents({}) == 0
            if empty:
                for name, docs in _seed_docs().items():
                    for doc in docs:
                        if target is not None:
                            target[name].insert_one(doc)
                        else:
                            _memory[name].insert_one(doc)
        except Exception:
            pass
        _seeded = True


# -------------------------------------------------
# FLASK
# -------------------------------------------------
app = Flask(__name__, static_folder="static")
CORS(app)


def is_admin():
    # Header only — a PIN in the query string leaks into logs and referrers.
    return (request.headers.get("X-Admin-Pin") or "") == ADMIN_SECRET


def require_admin():
    if not is_admin():
        return jsonify({"ok": False, "error": "admin pin required"}), 401
    return None


# -------------------------------------------------
# ACTORS — one resolver, so every handler scopes the same way
# -------------------------------------------------
def current_actor():
    """Who is calling?

    platform → X-Admin-Pin matches ADMIN_SECRET (sees every tenant)
    operator → X-Console-Token resolves to a live session (sees one tenant)
    """
    if is_admin():
        return {"role": "platform", "marketplace_id": "", "marketplace": None}
    token = request.headers.get("X-Console-Token") or ""
    if not token:
        return None
    sess = col("sessions").find_one({"_id": token})
    if not sess or sess.get("expires_at", "") < _now():
        return None
    mkt = col("marketplaces").find_one({"_id": sess.get("marketplace_id", "")})
    if not mkt or mkt.get("status") == "suspended":
        return None
    return {"role": "operator", "marketplace_id": mkt["_id"], "marketplace": mkt}


def require_actor():
    """(actor, error_response) — error is None when authorised."""
    actor = current_actor()
    if not actor:
        return None, (jsonify({"ok": False, "error": "sign in to continue"}), 401)
    return actor, None


def require_platform():
    actor = current_actor()
    if not actor or actor["role"] != "platform":
        return None, (jsonify({"ok": False, "error": "admin pin required"}), 401)
    return actor, None


def scoped(actor, flt=None):
    """Narrow a query to the actor's marketplace. Platform admin sees everything."""
    flt = dict(flt or {})
    if actor and actor["role"] == "operator":
        flt["marketplace_id"] = actor["marketplace_id"]
    return flt


def owns(actor, doc):
    """Guard for single-document reads/writes."""
    if not doc:
        return False
    if actor["role"] == "platform":
        return True
    return doc.get("marketplace_id", "") == actor["marketplace_id"]


def actor_marketplace_id(actor, d=None):
    """Which tenant does a write belong to? Operators are pinned to their own;
    platform admin must say."""
    if actor["role"] == "operator":
        return actor["marketplace_id"]
    return str((d or {}).get("marketplace_id", "")).strip()


def body():
    return request.get_json(silent=True) or {}


def clean_phone(p):
    return re.sub(r"\D", "", str(p or ""))


# ---------------- pages ----------------
@app.get("/")
def page_index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/markets")
def page_markets():
    return send_from_directory(app.static_folder, "markets.html")


@app.get("/m/<slug>")
def page_store(slug):
    return send_from_directory(app.static_folder, "store.html")


@app.get("/console")
def page_console():
    return send_from_directory(app.static_folder, "console.html")


@app.get("/kiosk")
def page_kiosk():
    return send_from_directory(app.static_folder, "kiosk.html")


@app.get("/dashboard")
def page_dashboard():
    return send_from_directory(app.static_folder, "dashboard.html")


@app.get("/runner")
def page_runner():
    return send_from_directory(app.static_folder, "runner.html")


@app.get("/ussd")
def page_ussd():
    return send_from_directory(app.static_folder, "ussd.html")


@app.get("/favicon.ico")
def favicon_ico():
    try:
        return send_from_directory(app.static_folder, "favicon.png")
    except Exception:
        return ("", 204)


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "YiThume", "storage": storage_mode(),
                    "whatsapp": "cloud_api" if whatsapp.enabled() else "fallback", "time": _now()})


@app.get("/api/config")
def public_config():
    mid = _requested_marketplace_id()
    s = get_settings(mid if mid != "__none__" else "")
    return jsonify({"ok": True, "config": {
        "delivery_fee": s["delivery_fee"], "cod_cap": s["cod_cap"],
        "cod_surcharge": s["cod_surcharge"], "eft_details": s["eft_details"],
        "whatsapp_enabled": whatsapp.enabled(),
        "wallet_enabled": rail.enabled(), "rail": rail.name, "rail_mode": rail.mode(),
    }})


@app.get("/api/pickup_points/public")
def public_pickup_points():
    """Collect-at-a-point options for a storefront's checkout."""
    m = _marketplace_by_slug(request.args.get("marketplace", ""))
    if not m:
        return jsonify({"ok": True, "pickup_points": []})
    areas = [a.lower() for a in m.get("areas", [])]
    rows = [p for p in find_all("pickup_points", {"active": True})
            if (not p.get("marketplace_id") or p["marketplace_id"] == m["_id"])
            and (not areas or (p.get("area", "").lower() in areas))]
    return jsonify({"ok": True, "pickup_points": [
        {"_id": p["_id"], "name": p.get("name", ""), "type": p.get("type", "kiosk"),
         "area": p.get("area", ""), "address": p.get("address", ""),
         "hours": p.get("hours", "")} for p in rows]})


# ---------------- notifications (WhatsApp Cloud API or console fallback) ----------------
def _notify(phone, text):
    try:
        return whatsapp.send_text(clean_phone(phone), text)
    except Exception:
        return False


def _items_summary(order):
    return ", ".join(f"{i['qty']} x {i['name']}" for i in order["items"])


def _notify_otp(order):
    return _notify(order["buyer"]["phone"],
                   f"YiThume: your order code for {order['code']} is {order['otp']}. "
                   f"Enter it to confirm your order. If you did not order, ignore this message.")


def _notify_confirmed(order):
    method = order.get("payment", {}).get("method", "cod")
    pay_line = ("Pay the runner R%.2f cash on delivery." % order["total"]) if method == "cod" \
        else "Paid by EFT — nothing to pay on delivery."
    return _notify(order["buyer"]["phone"],
                   f"YiThume: order {order['code']} is confirmed. {pay_line} "
                   f"Your delivery PIN is {order['delivery_pin']}. "
                   f"Only give this PIN to the runner when you receive your goods.")


def _notify_status(order, status):
    lines = {
        "assigned": f"YiThume: {order.get('runner_name', 'a runner')} will deliver order {order['code']}.",
        "picked_up": f"YiThume: order {order['code']} has been collected and is on its way.",
        "delivered": f"YiThume: order {order['code']} was delivered. Thank you for shopping with us.",
        "cancelled": f"YiThume: order {order['code']} was cancelled.",
    }
    if status in lines:
        return _notify(order["buyer"]["phone"], lines[status])
    return False


def _notify_runner_job(order, runner):
    return _notify(runner.get("phone"),
                   f"YiThume job: order {order['code']} — {_items_summary(order)}. "
                   f"Deliver to {order['buyer']['address']}, {order['buyer']['area']}. "
                   f"{'Collect R%.2f cash on delivery.' % order['total'] if order.get('payment', {}).get('method') == 'cod' else 'Prepaid (EFT) — do not collect cash.'} "
                   f"Open /runner to manage the job.")


def _whatsapp_url(order):
    lines = [f"YiThume order {order['code']}", ""]
    for it in order["items"]:
        lines.append(f"{it['qty']} x {it['name']} — R{it['price'] * it['qty']:.2f}")
    lines += ["", f"Delivery: R{order['delivery_fee']:.2f}", f"Total: R{order['total']:.2f}",
              f"Deliver to: {order['buyer']['address']}, {order['buyer']['area']}", "", "Reply CONFIRM to place this order."]
    return f"https://wa.me/{WHATSAPP_NUMBER}?text={quote(chr(10).join(lines))}"


# ---------------- products ----------------
def _marketplace_by_slug(slug):
    return col("marketplaces").find_one({"slug": str(slug or "").strip()})


def _requested_marketplace_id():
    """Storefront reads scope by slug; the console scopes by session."""
    if request.args.get("marketplace"):
        m = _marketplace_by_slug(request.args["marketplace"])
        return m["_id"] if m else "__none__"
    if request.args.get("marketplace_id"):
        return request.args["marketplace_id"]
    actor = current_actor()
    if actor and actor["role"] == "operator":
        return actor["marketplace_id"]
    return ""


@app.get("/api/products")
def list_products():
    flt = {"active": True} if request.args.get("all") != "1" else {}
    mid = _requested_marketplace_id()
    if mid:
        flt["marketplace_id"] = mid
    if request.args.get("category"):
        flt["category"] = request.args["category"]
    if request.args.get("seller_id"):
        flt["seller_id"] = request.args["seller_id"]
    q = (request.args.get("q") or "").strip()
    if q:
        rx = {"$regex": re.escape(q), "$options": "i"}
        flt["$or"] = [{"name": rx}, {"category": rx}, {"seller_name": rx}]
    items = find_all("products", flt)
    items.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return jsonify({"ok": True, "products": items})


@app.get("/api/categories")
def list_categories():
    flt = {"active": True}
    mid = _requested_marketplace_id()
    if mid:
        flt["marketplace_id"] = mid
    cats = sorted({p.get("category", "") for p in find_all("products", flt)} - {""})
    return jsonify({"ok": True, "categories": cats})


@app.post("/api/products")
def create_product():
    actor, err = require_actor()
    if err:
        return err
    d = body()
    if not d.get("name") or d.get("price") in (None, ""):
        return jsonify({"ok": False, "error": "name and price required"}), 400
    mid = actor_marketplace_id(actor, d)
    if not mid:
        return jsonify({"ok": False, "error": "marketplace_id required"}), 400
    mkt = col("marketplaces").find_one({"_id": mid})
    if not mkt:
        return jsonify({"ok": False, "error": "unknown marketplace"}), 404
    seller = col("sellers").find_one({"_id": d.get("seller_id", "")}) if d.get("seller_id") else None
    if seller and seller.get("marketplace_id") != mid:
        return jsonify({"ok": False, "error": "that seller is not on this marketplace"}), 403
    doc = {
        "_id": _new_id(), "name": str(d["name"]).strip(), "price": float(d["price"]),
        "category": str(d.get("category", "General")).strip() or "General",
        "image_url": str(d.get("image_url", "")).strip(),
        "seller_id": seller["_id"] if seller else "",
        "seller_name": seller["shop_name"] if seller else str(d.get("seller_name", "")).strip(),
        "marketplace_id": mid, "marketplace_name": mkt.get("name", ""),
        "stock": int(d.get("stock", 0) or 0), "active": bool(d.get("active", True)),
        "created_at": _now(),
    }
    col("products").insert_one(doc)
    return jsonify({"ok": True, "product": doc})


@app.patch("/api/products/<pid>")
def update_product(pid):
    actor, err = require_actor()
    if err:
        return err
    product = col("products").find_one({"_id": pid})
    if not product or not owns(actor, product):
        return jsonify({"ok": False, "error": "not found"}), 404
    d = body()
    allowed = {}
    for k in ["name", "category", "image_url", "seller_name"]:
        if k in d:
            allowed[k] = str(d[k]).strip()
    if "price" in d:
        allowed["price"] = float(d["price"])
    if "stock" in d:
        allowed["stock"] = int(d["stock"])
    if "active" in d:
        allowed["active"] = bool(d["active"])
    if not allowed:
        return jsonify({"ok": False, "error": "nothing to update"}), 400
    col("products").update_one({"_id": pid}, {"$set": allowed})
    return jsonify({"ok": True, "product": col("products").find_one({"_id": pid})})


@app.delete("/api/products/<pid>")
def delete_product(pid):
    actor, err = require_actor()
    if err:
        return err
    product = col("products").find_one({"_id": pid})
    if not product:
        return jsonify({"ok": True})
    if not owns(actor, product):
        return jsonify({"ok": False, "error": "not found"}), 404
    col("products").delete_one({"_id": pid})
    return jsonify({"ok": True})


# ---------------- generic CRUD: sellers / runners / nodes / marketplaces ----------------
ENTITY_FIELDS = {
    "sellers": ["shop_name", "owner_name", "phone", "area", "approved"],
    "runners": ["name", "phone", "area", "vehicle", "status", "approved", "marketplace_id"],
    "nodes": ["name", "operator_name", "phone", "territory", "commission_rate", "runners_count"],
    "pickup_points": ["name", "type", "area", "address", "contact_name", "phone",
                      "hours", "capacity", "carrier_id", "active"],
    "carriers": ["name", "slug", "mode", "areas", "services", "active"],
}
# Entities a marketplace operator may manage within their own tenant. Runners
# and carriers are network infrastructure — platform admin owns those.
OPERATOR_ENTITIES = ("sellers", "pickup_points")


def _coerce(entity, key, val):
    if key in ("approved", "active"):
        return bool(val)
    if key == "commission_rate":
        return float(val)
    if key in ("runners_count", "capacity"):
        return int(val or 0)
    if key == "phone":
        return clean_phone(val)
    if key in ("areas", "services"):
        if isinstance(val, str):
            return [s.strip() for s in val.split(",") if s.strip()]
        return [str(s).strip() for s in (val or [])]
    return str(val).strip()


def _entity_allowed(actor, entity):
    if actor["role"] == "platform":
        return True
    return entity in OPERATOR_ENTITIES


@app.get("/api/<entity>")
def list_entity(entity):
    if entity not in ENTITY_FIELDS:
        return jsonify({"ok": False, "error": "unknown entity"}), 404
    actor, err = require_actor()
    if err:
        return err
    if not _entity_allowed(actor, entity):
        return jsonify({"ok": False, "error": "not available for this account"}), 403
    if entity == "runners":
        items = find_all(entity)  # platform-only; the pool spans every tenant
    else:
        items = find_all(entity, scoped(actor))
    items.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    if entity == "runners":
        for r in items:
            r["cash_in_hand"] = _cash_in_hand(r["_id"])
    return jsonify({"ok": True, entity: items})


@app.post("/api/<entity>")
def create_entity(entity):
    if entity not in ENTITY_FIELDS:
        return jsonify({"ok": False, "error": "unknown entity"}), 404
    actor, err = require_actor()
    if err:
        return err
    if not _entity_allowed(actor, entity):
        return jsonify({"ok": False, "error": "not available for this account"}), 403
    d = body()
    doc = {"_id": _new_id(), "created_at": _now()}
    for k in ENTITY_FIELDS[entity]:
        if k in d:
            doc[k] = _coerce(entity, k, d[k])
    name_key = ENTITY_FIELDS[entity][0]
    if not doc.get(name_key):
        return jsonify({"ok": False, "error": f"{name_key} required"}), 400
    if entity != "carriers":
        doc["marketplace_id"] = actor_marketplace_id(actor, d)
    doc.setdefault("approved", True)
    if entity == "runners":
        doc.setdefault("status", "offline")
        doc.setdefault("deliveries_done", 0)
    if entity in ("pickup_points", "carriers"):
        doc.setdefault("active", True)
    col(entity).insert_one(doc)
    return jsonify({"ok": True, "item": doc})


@app.patch("/api/<entity>/<eid>")
def update_entity(entity, eid):
    if entity not in ENTITY_FIELDS:
        return jsonify({"ok": False, "error": "unknown entity"}), 404
    actor, err = require_actor()
    if err:
        return err
    if not _entity_allowed(actor, entity):
        return jsonify({"ok": False, "error": "not available for this account"}), 403
    item = col(entity).find_one({"_id": eid})
    if not item:
        return jsonify({"ok": False, "error": "not found"}), 404
    if entity != "carriers" and not owns(actor, item):
        return jsonify({"ok": False, "error": "not found"}), 404
    d = body()
    fields = [f for f in ENTITY_FIELDS[entity] if f != "marketplace_id"
              or actor["role"] == "platform"]
    allowed = {k: _coerce(entity, k, v) for k, v in d.items() if k in fields}
    if not allowed:
        return jsonify({"ok": False, "error": "nothing to update"}), 400
    col(entity).update_one({"_id": eid}, {"$set": allowed})
    return jsonify({"ok": True, "item": col(entity).find_one({"_id": eid})})


@app.delete("/api/<entity>/<eid>")
def delete_entity(entity, eid):
    if entity not in ENTITY_FIELDS:
        return jsonify({"ok": False, "error": "unknown entity"}), 404
    actor, err = require_actor()
    if err:
        return err
    if not _entity_allowed(actor, entity):
        return jsonify({"ok": False, "error": "not available for this account"}), 403
    item = col(entity).find_one({"_id": eid})
    if not item:
        return jsonify({"ok": True})
    if entity != "carriers" and not owns(actor, item):
        return jsonify({"ok": False, "error": "not found"}), 404
    col(entity).delete_one({"_id": eid})
    return jsonify({"ok": True})


# ---------------- marketplaces: the tenant ----------------
def _public_marketplace(m):
    s = get_settings(m["_id"])
    return {
        "_id": m["_id"], "slug": m.get("slug", ""), "name": m.get("name", ""),
        "tagline": m.get("tagline", ""), "about": m.get("about", ""),
        "logo_url": m.get("logo_url", ""), "accent": m.get("accent", "emerald"),
        "areas": m.get("areas", []), "town": (m.get("areas") or [""])[0],
        "trust_tier": m.get("trust", {}).get("tier", "unverified"),
        "deliveries": m.get("trust", {}).get("deliveries", 0),
        "delivery_fee": s["delivery_fee"], "cod_cap": s["cod_cap"],
        "cod_surcharge": s["cod_surcharge"], "eft_details": s["eft_details"],
        "created_at": m.get("created_at", ""),
    }


def _new_trust():
    return {"tier": "unverified", "score": 0, "deliveries": 0, "disputes": 0,
            "refunds": 0, "bank_submitted": False}


def _recheck_tier(marketplace_id):
    """Promote a marketplace as it earns it. Called after each fulfilment."""
    m = col("marketplaces").find_one({"_id": marketplace_id})
    if not m:
        return None
    t = dict(m.get("trust") or _new_trust())
    deliveries, disputes = int(t.get("deliveries", 0)), int(t.get("disputes", 0))
    rate = (disputes / deliveries) if deliveries else 0.0
    tier = "unverified"
    if t.get("bank_submitted") and deliveries >= TRUST_TIERS["verified"]["deliveries_required"]:
        tier = "verified"
    if (t.get("bank_submitted")
            and deliveries >= TRUST_TIERS["trusted"]["deliveries_required"]
            and rate < 0.02):
        tier = "trusted"
    t["tier"] = tier
    t["score"] = max(0, deliveries - disputes * 5)
    col("marketplaces").update_one({"_id": marketplace_id}, {"$set": {"trust": t}})
    return t


@app.get("/api/marketplaces")
def list_marketplaces():
    """Public directory. Platform admin sees every tenant, including suspended."""
    if is_admin():
        items = find_all("marketplaces")
        for m in items:
            m["orders"] = col("orders").count_documents({"marketplace_id": m["_id"]})
        items.sort(key=lambda d: d.get("created_at", ""), reverse=True)
        return jsonify({"ok": True, "marketplaces": items})
    live = [m for m in find_all("marketplaces") if m.get("status") == "live"]
    q = (request.args.get("q") or "").strip().lower()
    if q:
        live = [m for m in live
                if q in m.get("name", "").lower()
                or any(q in a.lower() for a in m.get("areas", []))]
    tier_rank = {"trusted": 0, "verified": 1, "unverified": 2}
    live.sort(key=lambda m: (tier_rank.get(m.get("trust", {}).get("tier"), 3),
                             -int(m.get("trust", {}).get("deliveries", 0))))
    return jsonify({"ok": True, "marketplaces": [_public_marketplace(m) for m in live]})


@app.get("/api/marketplaces/<slug>")
def get_marketplace(slug):
    m = col("marketplaces").find_one({"slug": slug})
    if not m or m.get("status") != "live":
        return jsonify({"ok": False, "error": "marketplace not found"}), 404
    return jsonify({"ok": True, "marketplace": _public_marketplace(m)})


_signup_hits = {}


def _rate_limited(key, limit=5, window_seconds=3600):
    """Crude per-key throttle. Enough to stop scripted signup floods; a real
    limiter belongs at the edge."""
    now = datetime.utcnow().timestamp()
    hits = [t for t in _signup_hits.get(key, []) if now - t < window_seconds]
    hits.append(now)
    _signup_hits[key] = hits
    return len(hits) > limit


@app.post("/api/marketplaces")
def create_marketplace():
    """Self-serve signup. The marketplace is created immediately but stays
    `pending` until the phone number is proven by OTP."""
    d = body()
    name = str(d.get("name", "")).strip()
    phone = clean_phone(d.get("phone"))
    pin = re.sub(r"\D", "", str(d.get("pin", "")))
    if not name or not phone:
        return jsonify({"ok": False, "error": "marketplace name and phone required"}), 400
    if len(pin) < 4:
        return jsonify({"ok": False, "error": "choose a PIN of at least 4 digits"}), 400
    if _rate_limited(request.headers.get("X-Forwarded-For", request.remote_addr or "local")):
        return jsonify({"ok": False, "error": "too many signups from here — try again later"}), 429
    # One live marketplace per verified number: the cheapest brake on fake tenants.
    existing = col("marketplaces").find_one({"phone": phone, "status": "live"})
    if existing:
        return jsonify({"ok": False,
                        "error": "this number already runs a marketplace — sign in instead"}), 409

    areas = d.get("areas") or ([d["town"]] if d.get("town") else [])
    if isinstance(areas, str):
        areas = [a.strip() for a in areas.split(",") if a.strip()]
    pin_hash, salt = _hash_pin(pin)
    otp = _otp_code()
    doc = {
        "_id": _new_id(), "slug": _unique_slug(name), "name": name,
        "tagline": str(d.get("tagline", "")).strip(),
        "about": str(d.get("about", "")).strip(),
        "logo_url": "", "accent": str(d.get("accent", "emerald")).strip() or "emerald",
        "owner_name": str(d.get("owner_name", "")).strip(),
        "phone": phone, "pin_hash": pin_hash, "pin_salt": salt,
        "areas": [str(a).strip() for a in areas if str(a).strip()],
        "use_shared_runners": bool(d.get("use_shared_runners", True)),
        "trust": _new_trust(), "status": "pending",
        "otp": otp, "otp_attempts": 0,
        "bank": {}, "created_at": _now(),
    }
    col("marketplaces").insert_one(doc)
    sent = _notify(phone, f"YiThume: your code to open {name} is {otp}.")
    resp = {"ok": True, "marketplace_id": doc["_id"], "slug": doc["slug"],
            "message": "Enter the code we sent to your phone."}
    if not sent:
        resp["otp_demo"] = otp
    return jsonify(resp)


@app.post("/api/marketplaces/verify")
def verify_marketplace():
    """OTP proves the phone. The storefront goes live and the operator is
    signed straight in — no second login step."""
    d = body()
    m = col("marketplaces").find_one({"_id": str(d.get("marketplace_id", ""))})
    if not m:
        return jsonify({"ok": False, "error": "not found"}), 404
    if m.get("status") == "live":
        return jsonify({"ok": False, "error": "already verified — sign in"}), 409
    code = re.sub(r"\D", "", str(d.get("code", "")))
    if code != m.get("otp"):
        attempts = int(m.get("otp_attempts", 0)) + 1
        col("marketplaces").update_one({"_id": m["_id"]}, {"$set": {"otp_attempts": attempts}})
        if attempts >= 5:
            col("marketplaces").delete_one({"_id": m["_id"]})
            return jsonify({"ok": False, "error": "too many wrong codes — start again"}), 409
        return jsonify({"ok": False, "error": "wrong code"}), 400
    col("marketplaces").update_one({"_id": m["_id"]},
                                   {"$set": {"status": "live", "otp": "", "otp_attempts": 0}})
    ledger.account(LD(), "marketplace", m["_id"], m["_id"])
    ledger.reserve_account(LD(), m["_id"])
    return jsonify({"ok": True, "slug": m["slug"], "token": _issue_session(m),
                    "marketplace": _public_marketplace(col("marketplaces").find_one({"_id": m["_id"]}))})


def _issue_session(m):
    token = _token()
    expires = datetime.utcnow().timestamp() + 60 * 60 * 24 * 30
    col("sessions").insert_one({
        "_id": token, "marketplace_id": m["_id"], "phone": m.get("phone", ""),
        "created_at": _now(),
        "expires_at": datetime.utcfromtimestamp(expires).isoformat() + "Z"})
    return token


@app.post("/api/console/login")
def console_login():
    d = body()
    phone = clean_phone(d.get("phone"))
    pin = re.sub(r"\D", "", str(d.get("pin", "")))
    m = col("marketplaces").find_one({"phone": phone, "status": "live"})
    if not m or not _check_pin(pin, m.get("pin_hash"), m.get("pin_salt")):
        return jsonify({"ok": False, "error": "wrong number or PIN"}), 401
    return jsonify({"ok": True, "token": _issue_session(m), "slug": m["slug"],
                    "marketplace": _public_marketplace(m)})


@app.post("/api/console/logout")
def console_logout():
    token = request.headers.get("X-Console-Token") or ""
    if token:
        col("sessions").delete_one({"_id": token})
    return jsonify({"ok": True})


@app.get("/api/console/me")
def console_me():
    actor, err = require_actor()
    if err:
        return err
    if actor["role"] == "platform":
        return jsonify({"ok": True, "role": "platform"})
    m = actor["marketplace"]
    mid = m["_id"]
    acct = ledger.account(LD(), "marketplace", mid, mid)
    reserve = ledger.reserve_account(LD(), mid)
    sellers = col("sellers").count_documents({"marketplace_id": mid})
    products = col("products").count_documents({"marketplace_id": mid})
    pool = col("runners").count_documents({"marketplace_id": "", "approved": True})
    own = col("runners").count_documents({"marketplace_id": mid, "approved": True})
    return jsonify({"ok": True, "role": "operator",
                    "marketplace": {**_public_marketplace(m),
                                    "use_shared_runners": m.get("use_shared_runners", True),
                                    "status": m.get("status"),
                                    "bank": m.get("bank", {}),
                                    "trust": m.get("trust", _new_trust())},
                    "wallet": ledger.balances(LD(), acct["_id"]),
                    "reserve": ledger.balances(LD(), reserve["_id"]),
                    "setup": {"sellers": sellers, "products": products,
                              "runners_available": pool + own,
                              "storefront_url": f"/m/{m.get('slug', '')}"}})


@app.patch("/api/console/marketplace")
def update_marketplace():
    actor, err = require_actor()
    if err:
        return err
    if actor["role"] != "operator":
        return jsonify({"ok": False, "error": "operator account required"}), 403
    d = body()
    updates = {}
    for k in ["name", "tagline", "about", "logo_url", "accent", "owner_name"]:
        if k in d:
            updates[k] = str(d[k]).strip()
    if "areas" in d:
        areas = d["areas"]
        if isinstance(areas, str):
            areas = [a.strip() for a in areas.split(",") if a.strip()]
        updates["areas"] = [str(a).strip() for a in areas if str(a).strip()]
    if "use_shared_runners" in d:
        updates["use_shared_runners"] = bool(d["use_shared_runners"])
    if "bank" in d and isinstance(d["bank"], dict):
        bank = {k: str(v).strip() for k, v in d["bank"].items()
                if k in ("holder", "bank_name", "bank_code", "account_no")}
        updates["bank"] = bank
        trust = dict(actor["marketplace"].get("trust") or _new_trust())
        trust["bank_submitted"] = bool(bank.get("account_no"))
        updates["trust"] = trust
        # Changing payout details freezes withdrawals — the standard defence
        # against an account takeover cashing out.
        updates["bank_changed_at"] = _now()
    if not updates:
        return jsonify({"ok": False, "error": "nothing to update"}), 400
    col("marketplaces").update_one({"_id": actor["marketplace_id"]}, {"$set": updates})
    if "bank" in updates:
        _recheck_tier(actor["marketplace_id"])
    m = col("marketplaces").find_one({"_id": actor["marketplace_id"]})
    return jsonify({"ok": True, "marketplace": _public_marketplace(m)})


@app.post("/api/marketplaces/<mid>/suspend")
def suspend_marketplace(mid):
    _actor, err = require_platform()
    if err:
        return err
    live = bool(body().get("live"))
    col("marketplaces").update_one({"_id": mid}, {"$set": {"status": "live" if live else "suspended"}})
    return jsonify({"ok": True, "marketplace": col("marketplaces").find_one({"_id": mid})})


@app.get("/api/coverage")
def coverage():
    """Runner and pickup-point reach per area. This is the landing page's proof
    that the network exists before anyone signs up."""
    areas = {}
    for r in find_all("runners", {"approved": True}):
        a = (r.get("area") or "").strip()
        if not a:
            continue
        row = areas.setdefault(a, {"area": a, "runners": 0, "online": 0, "points": 0})
        row["runners"] += 1
        if r.get("status") == "online":
            row["online"] += 1
    for p in find_all("pickup_points", {"active": True}):
        a = (p.get("area") or "").strip()
        if not a:
            continue
        areas.setdefault(a, {"area": a, "runners": 0, "online": 0, "points": 0})["points"] += 1
    rows = sorted(areas.values(), key=lambda r: -r["runners"])
    return jsonify({"ok": True, "coverage": rows,
                    "totals": {"areas": len(rows),
                               "runners": col("runners").count_documents({"approved": True}),
                               "points": col("pickup_points").count_documents({"active": True}),
                               "marketplaces": col("marketplaces").count_documents({"status": "live"})}})


# ---------------- public applications (join the network) ----------------
@app.post("/api/apply/seller")
def apply_seller():
    """Sellers apply to a specific marketplace, from that storefront."""
    d = body()
    if not d.get("shop_name") or not d.get("phone"):
        return jsonify({"ok": False, "error": "shop_name and phone required"}), 400
    mid = ""
    if d.get("marketplace"):
        m = _marketplace_by_slug(d["marketplace"])
        if not m:
            return jsonify({"ok": False, "error": "unknown marketplace"}), 404
        mid = m["_id"]
    elif d.get("marketplace_id"):
        mid = str(d["marketplace_id"])
    if not mid:
        return jsonify({"ok": False, "error": "choose a marketplace to sell on"}), 400
    doc = {"_id": _new_id(), "marketplace_id": mid, "shop_name": str(d["shop_name"]).strip(),
           "owner_name": str(d.get("owner_name", "")).strip(), "phone": clean_phone(d["phone"]),
           "area": str(d.get("area", "")).strip(), "approved": False, "strikes": 0,
           "created_at": _now()}
    col("sellers").insert_one(doc)
    return jsonify({"ok": True, "message": "Application received — we'll contact you on WhatsApp.", "id": doc["_id"]})


@app.post("/api/apply/runner")
def apply_runner():
    """Runners join the shared YiThume pool by default — that pool is what
    lets any new marketplace deliver from day one."""
    d = body()
    if not d.get("name") or not d.get("phone"):
        return jsonify({"ok": False, "error": "name and phone required"}), 400
    mid = ""
    if d.get("marketplace"):
        m = _marketplace_by_slug(d["marketplace"])
        mid = m["_id"] if m else ""
    doc = {"_id": _new_id(), "marketplace_id": mid, "name": str(d["name"]).strip(),
           "phone": clean_phone(d["phone"]),
           "area": str(d.get("area", "")).strip(), "vehicle": str(d.get("vehicle", "")).strip(),
           "status": "offline", "approved": False, "deliveries_done": 0, "created_at": _now()}
    col("runners").insert_one(doc)
    return jsonify({"ok": True, "message": "Application received — we'll contact you on WhatsApp.", "id": doc["_id"]})


# ---------------- buyers (reliability tracking) ----------------
def get_buyer(phone):
    phone = clean_phone(phone)
    if not phone:
        return None
    b = col("buyers").find_one({"phone": phone})
    if not b:
        b = {"_id": _new_id(), "phone": phone, "strikes": 0, "prepay_only": False,
             "incidents": [], "orders_placed": 0, "owed": 0.0,
             "blocked_until_settled": False, "created_at": _now()}
        col("buyers").insert_one(b)
    b.setdefault("owed", 0.0)
    b.setdefault("blocked_until_settled", False)
    return b


def add_strike(phone, order_code, reason):
    b = get_buyer(phone)
    if not b:
        return None
    s = get_settings()
    strikes = int(b.get("strikes", 0)) + 1
    incidents = b.get("incidents", []) + [{"order_code": order_code, "reason": reason, "at": _now()}]
    updates = {"strikes": strikes, "incidents": incidents}
    if strikes >= int(s["strike_limit"]):
        updates["prepay_only"] = True
    col("buyers").update_one({"_id": b["_id"]}, {"$set": updates})
    return col("buyers").find_one({"_id": b["_id"]})


@app.get("/api/buyers")
def list_buyers():
    """Buyer reliability is deliberately network-wide: a strike follows the
    phone number across every marketplace, which is a feature of being one
    network rather than a leak between tenants."""
    _actor, err = require_platform()
    if err:
        return err
    items = [b for b in find_all("buyers")
             if b.get("strikes", 0) > 0 or b.get("prepay_only") or float(b.get("owed", 0)) > 0]
    items.sort(key=lambda d: -int(d.get("strikes", 0)))
    return jsonify({"ok": True, "buyers": items})


@app.patch("/api/buyers/<bid>")
def update_buyer(bid):
    _actor, err = require_platform()
    if err:
        return err
    d = body()
    updates = {}
    if "prepay_only" in d:
        updates["prepay_only"] = bool(d["prepay_only"])
    if d.get("reset_strikes"):
        updates["strikes"] = 0
        updates["prepay_only"] = False
    if d.get("clear_debt"):
        updates["owed"] = 0.0
        updates["blocked_until_settled"] = False
    if not updates:
        return jsonify({"ok": False, "error": "nothing to update"}), 400
    col("buyers").update_one({"_id": bid}, {"$set": updates})
    b = col("buyers").find_one({"_id": bid})
    if not b:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": True, "buyer": b})


# ---------------- orders: fees, creation, lifecycle ----------------
def _find_node_for_area(area):
    area = (area or "").strip().lower()
    if not area:
        return None
    for n in find_all("nodes"):
        territory = str(n.get("territory", "")).lower()
        if area and (area in territory or territory.split("&")[0].strip() in area):
            return n
    return None


def _compute_fees(items, subtotal, delivery_fee, area, settings, carrier_fee=0.0):
    """Fee breakdown frozen onto the order at checkout time.

    Two takers on the goods (the operator's cut and YiThume's cut) and up to
    three on the delivery (runner, node, carrier), plus a small levy into the
    marketplace's failed-delivery reserve.
    """
    mkt_rate = float(settings["marketplace_rate"])
    net_rate = float(settings["network_rate"])
    marketplace_commission = round(subtotal * mkt_rate, 2)
    network_commission = round(subtotal * net_rate, 2)
    runner_earning = round(delivery_fee * float(settings["runner_rate"]), 2)
    reserve_levy = round(delivery_fee * float(settings["reserve_rate"]), 2)
    node = _find_node_for_area(area)
    node_commission = round(delivery_fee * float(settings["node_rate"]), 2) if node else 0.0
    per_seller = {}
    for it in items:
        key = (it.get("seller_id", ""), it.get("seller_name") or "Unknown seller")
        per_seller[key] = per_seller.get(key, 0.0) + it["price"] * it["qty"]
    sellers = [{"seller_id": sid, "seller_name": name,
                "amount": round(amt * (1 - mkt_rate - net_rate), 2)}
               for (sid, name), amt in per_seller.items()]
    return {
        "subtotal": subtotal, "delivery_fee": delivery_fee, "carrier_fee": round(carrier_fee, 2),
        "marketplace_commission": marketplace_commission,
        "network_commission": network_commission,
        "runner_earning": runner_earning,
        "node_commission": node_commission,
        "reserve_levy": reserve_levy,
        "node_id": node["_id"] if node else "", "node_name": node["name"] if node else "",
        "seller_payout": round(subtotal - marketplace_commission - network_commission, 2),
        "sellers": sellers,
        "rates": {"marketplace_rate": mkt_rate, "network_rate": net_rate,
                  "runner_rate": float(settings["runner_rate"]),
                  "node_rate": float(settings["node_rate"]),
                  "reserve_rate": float(settings["reserve_rate"])},
    }


def _initial_payouts(fees):
    payouts = [{"role": "seller", "key": s["seller_name"], "amount": s["amount"], "paid": False}
               for s in fees["sellers"]]
    if fees["node_name"] and fees["node_commission"] > 0:
        payouts.append({"role": "node", "key": fees["node_name"],
                        "amount": fees["node_commission"], "paid": False})
    return payouts


def _escrow_splits(order):
    """Who gets what when escrow releases. Must total the amount held, which
    ledger.release_escrow asserts."""
    fees = order.get("fees", {})
    mid = order.get("marketplace_id", "")
    deps = LD()
    splits, total = [], round(float(order.get("total", 0)), 2)

    for s in fees.get("sellers", []):
        acct = ledger.account(deps, "seller", s.get("seller_id") or s["seller_name"], mid)
        splits.append({"account_id": acct["_id"], "amount": round(float(s["amount"]), 2),
                       "who": f"seller:{s['seller_name']}"})
    mkt = ledger.account(deps, "marketplace", mid, mid)
    splits.append({"account_id": mkt["_id"], "amount": fees.get("marketplace_commission", 0),
                   "who": "marketplace"})
    if order.get("runner_id") and fees.get("runner_earning"):
        racct = ledger.account(deps, "runner", order["runner_id"])
        splits.append({"account_id": racct["_id"], "amount": fees["runner_earning"],
                       "who": "runner"})
    if fees.get("node_id") and fees.get("node_commission"):
        nacct = ledger.account(deps, "node", fees["node_id"], mid)
        splits.append({"account_id": nacct["_id"], "amount": fees["node_commission"],
                       "who": "node"})
    if fees.get("reserve_levy"):
        racct = ledger.reserve_account(deps, mid)
        splits.append({"account_id": racct["_id"], "amount": fees["reserve_levy"],
                       "who": "reserve"})

    # Whatever is left over is YiThume's: the network commission, plus any
    # delivery money not claimed by a runner, node or carrier on this order.
    # Whatever is left is YiThume's. If the configured rates over-commit the
    # order this goes negative, and the platform covers the gap rather than the
    # release failing — but that is a misconfiguration, so it is worth seeing.
    claimed = round(sum(s["amount"] for s in splits), 2)
    remainder = round(total - claimed, 2)
    plat = ledger.platform_account(deps)
    if remainder:
        splits.append({"account_id": plat["_id"], "amount": remainder, "who": "platform"})
    return [s for s in splits if s["amount"] != 0]


def place_order(buyer, items_in, channel="web", payment_method="wallet", skip_otp=False,
                marketplace_id="", pickup_point_id=""):
    """Core order creation shared by the web API and the USSD flow.
    Returns (order, extras, error, http_status)."""
    payment_method = (payment_method or "wallet").lower()
    if payment_method not in ("wallet", "cod", "eft"):
        return None, None, "unknown payment method", 400
    if not buyer.get("name") or not buyer.get("phone"):
        return None, None, "buyer name and phone required", 400
    if not items_in:
        return None, None, "cart is empty", 400

    items, subtotal = [], 0.0
    for it in items_in:
        p = col("products").find_one({"_id": str(it.get("product_id", ""))})
        if not p or not p.get("active", True):
            return None, None, f"product not available: {it.get('product_id')}", 400
        # Every item in one order must belong to one marketplace — a basket is
        # scoped to a storefront.
        marketplace_id = marketplace_id or p.get("marketplace_id", "")
        if p.get("marketplace_id", "") != marketplace_id:
            return None, None, "items must all come from the same marketplace", 400
        qty = max(1, int(it.get("qty", 1)))
        items.append({"product_id": p["_id"], "name": p["name"], "price": float(p["price"]),
                      "qty": qty, "seller_id": p.get("seller_id", ""),
                      "seller_name": p.get("seller_name", "")})
        subtotal += float(p["price"]) * qty

    market = col("marketplaces").find_one({"_id": marketplace_id})
    if not market or market.get("status") != "live":
        return None, None, "this marketplace is not open", 409
    settings = get_settings(marketplace_id)

    subtotal = round(subtotal, 2)
    delivery_fee = float(settings["delivery_fee"])
    carrier_fee = 0.0
    fulfilment = {"mode": "runner_direct", "pickup_point_id": "", "pickup_point_name": "",
                  "legs": []}
    if pickup_point_id:
        point = col("pickup_points").find_one({"_id": pickup_point_id, "active": True})
        if not point:
            return None, None, "pickup point not available", 400
        # Collecting at a point drops the last-mile leg, so it costs less —
        # which is exactly why the prepaid kiosk model is popular.
        carrier_fee = round(delivery_fee * 0.6, 2)
        delivery_fee = carrier_fee
        fulfilment = {"mode": "collect_at_point", "pickup_point_id": point["_id"],
                      "pickup_point_name": point.get("name", ""),
                      "legs": [{"seq": 1, "type": "carrier",
                                "carrier_id": point.get("carrier_id", ""), "waybill": "",
                                "from": "seller", "to": "pickup_point",
                                "status": "pending", "at": ""}]}

    if payment_method == "cod":
        delivery_fee = round(delivery_fee + float(settings["cod_surcharge"]), 2)
    total = round(subtotal + delivery_fee, 2)
    phone = clean_phone(buyer["phone"])
    buyer_rec = get_buyer(phone)

    if payment_method == "cod":
        if buyer_rec and buyer_rec.get("prepay_only"):
            return None, None, ("Cash on delivery is not available for this number. "
                                "Top up your wallet to order."), 409
        if buyer_rec and round(float(buyer_rec.get("owed", 0)), 2) > 0:
            return None, None, (f"You owe R{float(buyer_rec['owed']):.2f} from a failed "
                                f"delivery. Settle it to order again."), 409
        if total > float(settings["cod_cap"]):
            return None, None, (f"Orders over R{float(settings['cod_cap']):.0f} must be "
                                f"paid from your wallet."), 409

    # Escrow cap: an unproven marketplace cannot hold much of other people's money.
    if payment_method == "wallet":
        tier = market.get("trust", {}).get("tier", "unverified")
        cap = TRUST_TIERS.get(tier, TRUST_TIERS["unverified"])["escrow_cap"]
        if total > cap:
            return None, None, (f"This marketplace can take orders up to R{cap:.0f} "
                                f"while it is {tier}."), 409
        acct = ledger.buyer_account(LD(), phone)
        avail = ledger.balances(LD(), acct["_id"])["available"]
        if avail < total:
            return None, {"shortfall": round(total - avail, 2), "balance": avail,
                          "required": total}, "wallet_short", 402

    code = _order_code()
    fees = _compute_fees(items, subtotal, delivery_fee, buyer.get("area", ""), settings,
                         carrier_fee=carrier_fee)
    payment = payment_provider.initiate(payment_method, code, settings)
    otp = _otp_code()
    status = "pending" if skip_otp else "awaiting_otp"
    order = {
        "_id": _new_id(), "code": code, "marketplace_id": marketplace_id,
        "marketplace_name": market.get("name", ""),
        "buyer": {"name": str(buyer["name"]).strip(), "phone": phone,
                  "area": str(buyer.get("area", "")).strip(), "address": str(buyer.get("address", "")).strip()},
        "items": items, "subtotal": subtotal, "delivery_fee": delivery_fee,
        "marketplace_fee": fees["marketplace_commission"],
        "network_fee": fees["network_commission"], "total": total,
        "fees": fees, "payment": payment, "fulfilment": fulfilment,
        "otp": otp, "otp_verified": skip_otp, "otp_attempts": 0,
        "delivery_pin": "",
        "settlement": {"cash_status": _initial_cash_status(payment_method),
                       "remittance_id": "", "payouts": _initial_payouts(fees)},
        "channel": str(channel), "status": status,
        "runner_id": "", "runner_name": "",
        "created_at": _now(), "updated_at": _now(),
        "timeline": [{"status": status, "at": _now()}],
    }
    col("orders").insert_one(order)

    if payment_method == "wallet":
        acct = ledger.buyer_account(LD(), phone)
        ledger.hold_escrow(LD(), acct["_id"], total, code,
                           {"marketplace_id": marketplace_id, "order_id": order["_id"]})

    for it in items:  # best-effort stock decrement
        p = col("products").find_one({"_id": it["product_id"]})
        if p and isinstance(p.get("stock"), int) and p["stock"] > 0:
            col("products").update_one({"_id": p["_id"]}, {"$set": {"stock": max(0, p["stock"] - it["qty"])}})
    b = get_buyer(phone)
    if b:
        col("buyers").update_one({"_id": b["_id"]}, {"$set": {"orders_placed": int(b.get("orders_placed", 0)) + 1}})

    extras = {"whatsapp_url": _whatsapp_url(order)}
    if not skip_otp:
        sent = _notify_otp(order)
        if not sent:
            extras["otp_demo"] = otp  # WhatsApp not configured: surface the code so the flow still works
    if payment_method == "eft":
        extras["eft_instructions"] = payment.get("instructions", "")
    return order, extras, None, 200


def _initial_cash_status(method):
    if method == "wallet":
        return "in_escrow"
    if method == "eft":
        return "prepaid"
    return "unpaid"


def _public_order(order):
    o = {k: order[k] for k in ["code", "status", "total", "created_at"]}
    o["items"] = [{"name": i["name"], "qty": i["qty"]} for i in order["items"]]
    o["runner_name"] = order.get("runner_name", "")
    o["timeline"] = order.get("timeline", [])
    o["payment_method"] = order.get("payment", {}).get("method", "")
    # Demo mode only: without WhatsApp configured the buyer has no channel to
    # receive the PIN, so tracking (by their code/phone) surfaces it.
    if not whatsapp.enabled() and order.get("delivery_pin") and order["status"] in ("confirmed", "assigned", "picked_up"):
        o["delivery_pin"] = order["delivery_pin"]
    return o


@app.post("/api/orders")
def create_order():
    d = body()
    mid = ""
    if d.get("marketplace"):
        m = _marketplace_by_slug(d["marketplace"])
        if not m:
            return jsonify({"ok": False, "error": "unknown marketplace"}), 404
        mid = m["_id"]
    order, extras, err, code = place_order(d.get("buyer") or {}, d.get("items") or [],
                                           channel=str(d.get("channel", "web")),
                                           payment_method=d.get("payment_method", "wallet"),
                                           marketplace_id=mid,
                                           pickup_point_id=str(d.get("pickup_point_id", "")))
    if err == "wallet_short":
        # Not an error the buyer should be bounced on — offer to top up exactly
        # the shortfall and carry on. This is the best moment to win a wallet.
        return jsonify({"ok": False, "error": "wallet_short", **(extras or {})}), 402
    if err:
        return jsonify({"ok": False, "error": err}), code
    resp = {"ok": True, "order": order}
    resp.update(extras)
    return jsonify(resp)


@app.post("/api/orders/<oid>/verify-otp")
def verify_otp(oid):
    d = body()
    code = re.sub(r"\D", "", str(d.get("code", "")))
    order = col("orders").find_one({"_id": oid})
    if not order:
        return jsonify({"ok": False, "error": "not found"}), 404
    if order["status"] != "awaiting_otp":
        return jsonify({"ok": False, "error": "order does not need verification"}), 409
    if code != order.get("otp"):
        attempts = int(order.get("otp_attempts", 0)) + 1
        updates = {"otp_attempts": attempts, "updated_at": _now()}
        if attempts >= 5:
            updates["status"] = "cancelled"
            updates["timeline"] = order.get("timeline", []) + [{"status": "cancelled", "at": _now(), "note": "too many wrong codes"}]
        col("orders").update_one({"_id": oid}, {"$set": updates})
        if attempts >= 5:
            return jsonify({"ok": False, "error": "Too many wrong codes — order cancelled."}), 409
        return jsonify({"ok": False, "error": "Wrong code — check the message we sent you."}), 400
    updates = {"status": "pending", "otp_verified": True, "updated_at": _now(),
               "timeline": order.get("timeline", []) + [{"status": "pending", "at": _now()}]}
    col("orders").update_one({"_id": oid}, {"$set": updates})
    return jsonify({"ok": True, "order": col("orders").find_one({"_id": oid})})


@app.post("/api/orders/<oid>/resend-otp")
def resend_otp(oid):
    order = col("orders").find_one({"_id": oid})
    if not order:
        return jsonify({"ok": False, "error": "not found"}), 404
    if order["status"] != "awaiting_otp":
        return jsonify({"ok": False, "error": "order does not need verification"}), 409
    sent = _notify_otp(order)
    resp = {"ok": True}
    if not sent:
        resp["otp_demo"] = order["otp"]
    return jsonify(resp)


@app.get("/api/orders")
def list_orders():
    actor, err = require_actor()
    if err:
        return err
    flt = scoped(actor)
    if request.args.get("status"):
        flt["status"] = request.args["status"]
    if actor["role"] == "platform" and request.args.get("marketplace_id"):
        flt["marketplace_id"] = request.args["marketplace_id"]
    orders = find_all("orders", flt)
    orders.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return jsonify({"ok": True, "orders": orders})


@app.get("/api/orders/track")
def track_order():
    code = (request.args.get("code") or "").strip().upper()
    phone = clean_phone(request.args.get("phone"))
    if not code and not phone:
        return jsonify({"ok": False, "error": "code or phone required"}), 400
    orders = find_all("orders")
    if code:
        orders = [o for o in orders if o.get("code", "").upper() == code]
    else:
        orders = [o for o in orders if o.get("buyer", {}).get("phone") == phone]
    orders.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return jsonify({"ok": True, "orders": [_public_order(o) for o in orders[:5]]})


def _runner_available(runner, settings=None):
    settings = settings or get_settings()
    return _cash_in_hand(runner["_id"]) < float(settings["runner_cash_limit"])


def _apply_status(order, new_status, updates, pin=None):
    """Shared transition guard. Mutates `updates`; returns error string or None."""
    if new_status not in STATUS_FLOW:
        return "unknown status"
    if new_status not in STATUS_FLOW[order["status"]]:
        return f"cannot go {order['status']} → {new_status}"
    if new_status == "confirmed" and not order.get("otp_verified"):
        return "order not verified by buyer yet (OTP)"
    if new_status == "assigned" and not (updates.get("runner_id") or order.get("runner_id")):
        return "assign a runner first"
    if new_status in FULFILLED:
        # The same PIN proves handover whether a runner hands it over at a door
        # or a kiosk operator hands it over at a counter.
        if not order.get("delivery_pin"):
            return "order has no delivery PIN"
        if str(pin or "").strip() != order["delivery_pin"]:
            return "wrong delivery PIN — ask the buyer for their 4-digit PIN"
    updates["status"] = new_status
    updates["timeline"] = order.get("timeline", []) + [{"status": new_status, "at": _now()}]
    if new_status == "confirmed":
        updates["delivery_pin"] = _pin_code()
    return None


def _finalize_delivery(order, updates):
    """Settlement effects of a delivery. `order` is the pre-update doc."""
    payment = payment_provider.on_delivered(order.get("payment", {}))
    updates["payment"] = payment
    settlement = dict(order.get("settlement", {}))
    method = payment.get("method")
    if method == "cod":
        settlement["cash_status"] = "with_runner"
    elif method == "wallet":
        # Escrow releases here — the PIN that proved handover is the same PIN
        # that turns held money into everyone's money.
        merged = {**order, **updates}
        settlement["cash_status"] = "settled"
        try:
            splits = _escrow_splits(merged)
            acct = ledger.buyer_account(LD(), order["buyer"]["phone"])
            ledger.release_escrow(LD(), acct["_id"], splits, order["code"],
                                  {"marketplace_id": order.get("marketplace_id", ""),
                                   "splits": [{"who": s["who"], "amount": s["amount"]} for s in splits]})
            settlement["released_at"] = _now()
        except ValueError as exc:
            # Never lose the goods-delivered fact because the money maths failed.
            settlement["cash_status"] = "release_failed"
            settlement["release_error"] = str(exc)
    else:
        settlement["cash_status"] = "settled"  # money already digital
    payouts = list(settlement.get("payouts", []))
    fees = order.get("fees", {})
    runner_name = updates.get("runner_name") or order.get("runner_name", "")
    if fees.get("runner_earning") and not any(p["role"] == "runner" for p in payouts):
        payouts.append({"role": "runner", "key": runner_name, "amount": fees["runner_earning"], "paid": False})
    settlement["payouts"] = payouts
    updates["settlement"] = settlement
    if order.get("runner_id"):
        r = col("runners").find_one({"_id": order["runner_id"]})
        if r:
            col("runners").update_one({"_id": r["_id"]},
                                      {"$set": {"deliveries_done": int(r.get("deliveries_done", 0)) + 1}})
    mid = order.get("marketplace_id", "")
    if mid:
        m = col("marketplaces").find_one({"_id": mid})
        if m:
            t = dict(m.get("trust") or _new_trust())
            t["deliveries"] = int(t.get("deliveries", 0)) + 1
            col("marketplaces").update_one({"_id": mid}, {"$set": {"trust": t}})
            _recheck_tier(mid)


def _cover_failed_delivery(order, reason):
    """Who pays when a buyer no-shows or refuses to pay.

    Today the runner eats it: the trip happened, the goods travelled, and the
    strike system only punishes the buyer afterwards. That is backwards — it
    charges the side that makes the network work. So:

      1. the buyer's wallet, if there is anything in it
      2. the buyer's balance goes negative and they are blocked until it clears
      3. the marketplace's reserve pays the runner immediately either way
      4. an empty reserve goes negative — the platform backstop, recovered
         from that tenant's future earnings

    The runner is paid for the attempt in every branch.
    """
    settings = get_settings(order.get("marketplace_id", ""))
    deps = LD()
    code, mid = order["code"], order.get("marketplace_id", "")
    phone = order.get("buyer", {}).get("phone", "")
    fee = round(float(settings["failed_delivery_fee"]), 2)
    attempt = round(float(order.get("fees", {}).get("runner_earning", 0))
                    * float(settings["failed_attempt_fee_rate"]), 2)
    outcome = {"reason": reason, "fee": fee, "attempt_fee": attempt,
               "recovered_from": "", "runner_paid": 0.0}

    reserve = ledger.reserve_account(deps, mid)
    if order.get("runner_id") and attempt > 0:
        racct = ledger.account(deps, "runner", order["runner_id"])
        ledger.transfer(deps, reserve["_id"], racct["_id"], attempt,
                        "failed_attempt_fee", code, {"marketplace_id": mid})
        outcome["runner_paid"] = attempt

    if order.get("payment", {}).get("method") == "wallet":
        # Prepaid orders refund cleanly — this is the whole point of escrow.
        acct = ledger.buyer_account(deps, phone)
        ledger.refund_escrow(deps, acct["_id"], round(float(order.get("total", 0)), 2), code,
                             {"reason": reason})
        outcome["recovered_from"] = "escrow_refund"
        return outcome

    if fee > 0 and phone:
        acct = ledger.buyer_account(deps, phone)
        ledger.transfer(deps, acct["_id"], reserve["_id"], fee,
                        "failed_delivery_fee", code, {"marketplace_id": mid})
        after = ledger.balances(deps, acct["_id"])["balance"]
        outcome["recovered_from"] = "buyer_wallet" if after >= 0 else "buyer_debt"
        b = get_buyer(phone)
        if b and after < 0:
            col("buyers").update_one({"_id": b["_id"]}, {"$set": {
                "owed": round(abs(after), 2), "blocked_until_settled": True}})
    if ledger.balances(deps, reserve["_id"])["balance"] < 0:
        outcome["backstop"] = "platform"
    return outcome


def _runner_serves(runner, order):
    """A runner may take an order if they belong to that marketplace, or if
    they are in the shared pool and the marketplace draws on it."""
    mid = order.get("marketplace_id", "")
    if runner.get("marketplace_id"):
        return runner["marketplace_id"] == mid
    m = col("marketplaces").find_one({"_id": mid})
    return bool(m and m.get("use_shared_runners", True))


@app.patch("/api/orders/<oid>")
def update_order(oid):
    actor, err = require_actor()
    if err:
        return err
    d = body()
    order = col("orders").find_one({"_id": oid})
    if not order or not owns(actor, order):
        return jsonify({"ok": False, "error": "not found"}), 404
    updates = {}
    assigned_runner = None

    if d.get("runner_id"):
        rid = d["runner_id"]
        settings = get_settings(order.get("marketplace_id", ""))
        if rid == "auto":
            candidates = [r for r in find_all("runners", {"approved": True, "status": "online"})
                          if _runner_available(r, settings) and _runner_serves(r, order)]
            area = order.get("buyer", {}).get("area", "")
            mid = order.get("marketplace_id", "")
            # Own runners first, then local knowledge, then whoever is least busy.
            candidates.sort(key=lambda r: (0 if r.get("marketplace_id") == mid else 1,
                                           0 if r.get("area", "").lower() == area.lower() else 1,
                                           r.get("deliveries_done", 0)))
            if not candidates:
                return jsonify({"ok": False, "error": "no available runners (online and under cash limit)"}), 409
            runner = candidates[0]
        else:
            runner = col("runners").find_one({"_id": rid})
            if not runner:
                return jsonify({"ok": False, "error": "runner not found"}), 404
            if not _runner_serves(runner, order):
                return jsonify({"ok": False, "error": "that runner does not serve this marketplace"}), 403
            if not _runner_available(runner, settings):
                return jsonify({"ok": False, "error": "runner is over the cash-in-hand limit — confirm their remittance first"}), 409
        updates["runner_id"] = runner["_id"]
        updates["runner_name"] = runner["name"]
        assigned_runner = runner
        if not d.get("status") and order["status"] == "confirmed":
            d["status"] = "assigned"

    if d.get("status"):
        terr = _apply_status(order, d["status"], updates, pin=d.get("delivery_pin"))
        if terr:
            return jsonify({"ok": False, "error": terr}), 409
        if d["status"] in FULFILLED:
            _finalize_delivery(order, updates)

    if not updates:
        return jsonify({"ok": False, "error": "nothing to update"}), 400
    updates["updated_at"] = _now()
    col("orders").update_one({"_id": oid}, {"$set": updates})
    fresh = col("orders").find_one({"_id": oid})
    new_status = updates.get("status")
    if new_status == "confirmed":
        _notify_confirmed(fresh)
    elif new_status:
        _notify_status(fresh, new_status)
    if assigned_runner and new_status == "assigned":
        _notify_runner_job(fresh, assigned_runner)
    return jsonify({"ok": True, "order": fresh})


@app.post("/api/orders/<oid>/incident")
def order_incident(oid):
    """Log a buyer no-show / refused-COD incident. Cancels the order, adds a
    strike, and runs the failed-delivery waterfall so the runner still gets
    paid for the trip."""
    actor, err = require_actor()
    if err:
        return err
    order = col("orders").find_one({"_id": oid})
    if not order or not owns(actor, order):
        return jsonify({"ok": False, "error": "not found"}), 404
    return _record_incident(oid, str(body().get("reason", "no_show")))


def _record_incident(oid, reason):
    order = col("orders").find_one({"_id": oid})
    if not order:
        return jsonify({"ok": False, "error": "not found"}), 404
    if order["status"] in FULFILLED + ("cancelled",):
        return jsonify({"ok": False, "error": "order already closed"}), 409
    outcome = _cover_failed_delivery(order, reason)
    updates = {"status": "cancelled", "updated_at": _now(),
               "failure": outcome,
               "timeline": order.get("timeline", []) + [{"status": "cancelled", "at": _now(), "note": f"incident: {reason}"}]}
    col("orders").update_one({"_id": oid}, {"$set": updates})
    buyer = add_strike(order["buyer"]["phone"], order["code"], reason)
    return jsonify({"ok": True, "order": col("orders").find_one({"_id": oid}),
                    "buyer": buyer, "failure": outcome})


# ---------------- runner API (phone-identified, mobile view) ----------------
def _runner_by_phone(phone):
    phone = clean_phone(phone)
    if not phone:
        return None
    return col("runners").find_one({"phone": phone, "approved": True})


def _cash_in_hand(runner_id):
    orders = find_all("orders", {"runner_id": runner_id, "status": "delivered"})
    return round(sum(o.get("total", 0) for o in orders
                     if o.get("payment", {}).get("method") == "cod"
                     and o.get("settlement", {}).get("cash_status") == "with_runner"), 2)


def _remit_pending_total(runner_id):
    rems = find_all("remittances", {"runner_id": runner_id, "status": "pending"})
    return round(sum(r.get("expected_total", 0) for r in rems), 2)


def _runner_job(order):
    payment = order.get("payment", {})
    fulfilment = order.get("fulfilment", {})
    return {
        "_id": order["_id"], "code": order["code"], "status": order["status"],
        "marketplace_id": order.get("marketplace_id", ""),
        "marketplace_name": order.get("marketplace_name", ""),
        "items": [{"name": i["name"], "qty": i["qty"], "seller_name": i.get("seller_name", "")} for i in order["items"]],
        "buyer_name": order["buyer"]["name"], "buyer_phone": order["buyer"]["phone"],
        "area": order["buyer"]["area"], "address": order["buyer"]["address"],
        "total": order["total"], "payment_method": payment.get("method", "cod"),
        "collect_cash": payment.get("method") == "cod" and order["status"] not in FULFILLED,
        "prepaid": payment.get("method") == "wallet",
        "pickup_point": fulfilment.get("pickup_point_name", ""),
        "runner_earning": order.get("fees", {}).get("runner_earning", 0),
        "created_at": order["created_at"],
    }


def _require_runner():
    phone = request.args.get("phone") or body().get("phone") or ""
    runner = _runner_by_phone(phone)
    if not runner:
        return None, (jsonify({"ok": False, "error": "runner not found — use the phone number you registered with"}), 404)
    return runner, None


@app.get("/api/runner/me")
def runner_me():
    runner, err = _require_runner()
    if err:
        return err
    settings = get_settings()
    cash = _cash_in_hand(runner["_id"])
    delivered = find_all("orders", {"runner_id": runner["_id"], "status": "delivered"})
    earned = round(sum(o.get("fees", {}).get("runner_earning", 0) for o in delivered), 2)
    return jsonify({"ok": True, "runner": {
        "_id": runner["_id"], "name": runner["name"], "phone": runner["phone"],
        "area": runner.get("area", ""), "status": runner.get("status", "offline"),
        "deliveries_done": runner.get("deliveries_done", 0),
        "cash_in_hand": cash, "cash_limit": float(settings["runner_cash_limit"]),
        "blocked": cash >= float(settings["runner_cash_limit"]),
        "remit_pending": _remit_pending_total(runner["_id"]),
        "total_earned": earned,
    }})


@app.post("/api/runner/status")
def runner_set_status():
    runner, err = _require_runner()
    if err:
        return err
    status = str(body().get("status", "")).strip()
    if status not in ("online", "offline"):
        return jsonify({"ok": False, "error": "status must be online or offline"}), 400
    col("runners").update_one({"_id": runner["_id"]}, {"$set": {"status": status}})
    return jsonify({"ok": True, "status": status})


@app.get("/api/runner/jobs")
def runner_jobs():
    runner, err = _require_runner()
    if err:
        return err
    mine = find_all("orders", {"runner_id": runner["_id"]})
    active = [o for o in mine if o["status"] in ("assigned", "picked_up")]
    done = [o for o in mine if o["status"] in FULFILLED]
    done.sort(key=lambda d: d.get("updated_at", ""), reverse=True)
    available = [o for o in find_all("orders", {"runner_id": ""})
                 if o["status"] in ("confirmed", "at_point") and _runner_serves(runner, o)]
    my_mid = runner.get("marketplace_id", "")
    # Own marketplace first, then prepaid (guaranteed money, so runners feel the
    # difference between a wallet order and a COD one), then local, then age.
    available.sort(key=lambda d: (
        0 if my_mid and d.get("marketplace_id") == my_mid else 1,
        0 if d.get("payment", {}).get("method") == "wallet" else 1,
        0 if d.get("buyer", {}).get("area", "").lower() == runner.get("area", "").lower() else 1,
        d.get("created_at", "")))
    return jsonify({"ok": True,
                    "active": [_runner_job(o) for o in active],
                    "available": [_runner_job(o) for o in available[:20]],
                    "delivered": [_runner_job(o) for o in done[:10]]})


def _runner_order_action(runner, oid, new_status, pin=None):
    order = col("orders").find_one({"_id": oid})
    if not order:
        return jsonify({"ok": False, "error": "order not found"}), 404
    updates = {}
    if new_status == "assigned":
        if order.get("runner_id"):
            return jsonify({"ok": False, "error": "job already taken"}), 409
        if not _runner_serves(runner, order):
            return jsonify({"ok": False, "error": "this marketplace does not use the shared pool"}), 403
        if not _runner_available(runner):
            return jsonify({"ok": False, "error": "you are over the cash limit — deposit your cash first"}), 409
        updates["runner_id"] = runner["_id"]
        updates["runner_name"] = runner["name"]
    else:
        if order.get("runner_id") != runner["_id"]:
            return jsonify({"ok": False, "error": "this job is not assigned to you"}), 403
    terr = _apply_status(order, new_status, updates, pin=pin)
    if terr:
        return jsonify({"ok": False, "error": terr}), 409
    if new_status in FULFILLED:
        _finalize_delivery(order, updates)
    updates["updated_at"] = _now()
    col("orders").update_one({"_id": oid}, {"$set": updates})
    fresh = col("orders").find_one({"_id": oid})
    _notify_status(fresh, new_status)
    return jsonify({"ok": True, "job": _runner_job(fresh)})


@app.post("/api/runner/orders/<oid>/accept")
def runner_accept(oid):
    runner, err = _require_runner()
    if err:
        return err
    return _runner_order_action(runner, oid, "assigned")


@app.post("/api/runner/orders/<oid>/pickup")
def runner_pickup(oid):
    runner, err = _require_runner()
    if err:
        return err
    return _runner_order_action(runner, oid, "picked_up")


@app.post("/api/runner/orders/<oid>/deliver")
def runner_deliver(oid):
    runner, err = _require_runner()
    if err:
        return err
    return _runner_order_action(runner, oid, "delivered", pin=body().get("pin"))


@app.post("/api/runner/orders/<oid>/incident")
def runner_incident(oid):
    runner, err = _require_runner()
    if err:
        return err
    order = col("orders").find_one({"_id": oid})
    if not order or order.get("runner_id") != runner["_id"]:
        return jsonify({"ok": False, "error": "this job is not assigned to you"}), 403
    return _record_incident(oid, str(body().get("reason", "no_show")))


# ---------------- kiosks / pickup points ----------------
def _require_point():
    """Kiosk operators identify by phone, the same way runners do."""
    phone = clean_phone(request.args.get("phone") or body().get("phone") or "")
    if not phone:
        return None, (jsonify({"ok": False, "error": "phone required"}), 400)
    point = col("pickup_points").find_one({"phone": phone, "active": True})
    if not point:
        return None, (jsonify({"ok": False, "error": "no pickup point registered on this number"}), 404)
    return point, None


def _point_parcel(order):
    return {"_id": order["_id"], "code": order["code"], "status": order["status"],
            "marketplace_name": order.get("marketplace_name", ""),
            "buyer_name": order["buyer"]["name"], "buyer_phone": order["buyer"]["phone"],
            "items": [{"name": i["name"], "qty": i["qty"]} for i in order["items"]],
            "waybill": next((l.get("waybill", "") for l in order.get("fulfilment", {}).get("legs", [])
                             if l.get("type") == "carrier"), ""),
            "total": order["total"],
            "payment_method": order.get("payment", {}).get("method", ""),
            "created_at": order["created_at"]}


@app.get("/api/kiosk/parcels")
def kiosk_parcels():
    point, err = _require_point()
    if err:
        return err
    orders = [o for o in find_all("orders")
              if o.get("fulfilment", {}).get("pickup_point_id") == point["_id"]]
    incoming = [o for o in orders if o["status"] in ("confirmed", "in_transit")]
    waiting = [o for o in orders if o["status"] == "at_point"]
    done = sorted([o for o in orders if o["status"] == "collected"],
                  key=lambda d: d.get("updated_at", ""), reverse=True)
    return jsonify({"ok": True, "point": {"name": point.get("name"), "area": point.get("area")},
                    "incoming": [_point_parcel(o) for o in incoming],
                    "waiting": [_point_parcel(o) for o in waiting],
                    "collected": [_point_parcel(o) for o in done[:10]]})


@app.post("/api/kiosk/orders/<oid>/arrived")
def kiosk_arrived(oid):
    """Parcel reached the counter. The waybill is whatever the carrier gave the
    operator — typed in for a manual carrier like The Courier Guy."""
    point, err = _require_point()
    if err:
        return err
    order = col("orders").find_one({"_id": oid})
    if not order or order.get("fulfilment", {}).get("pickup_point_id") != point["_id"]:
        return jsonify({"ok": False, "error": "not a parcel for this point"}), 403
    updates = {}
    if order["status"] == "confirmed":
        terr = _apply_status(order, "in_transit", updates)
        if terr:
            return jsonify({"ok": False, "error": terr}), 409
        col("orders").update_one({"_id": oid}, {"$set": {**updates, "updated_at": _now()}})
        order = col("orders").find_one({"_id": oid})
        updates = {}
    terr = _apply_status(order, "at_point", updates)
    if terr:
        return jsonify({"ok": False, "error": terr}), 409
    fulfilment = dict(order.get("fulfilment", {}))
    legs = [dict(l) for l in fulfilment.get("legs", [])]
    for leg in legs:
        if leg.get("type") == "carrier":
            leg["status"] = "arrived"
            leg["at"] = _now()
            waybill = str(body().get("waybill", "")).strip()
            if waybill:
                leg["waybill"] = waybill
    fulfilment["legs"] = legs
    updates["fulfilment"] = fulfilment
    updates["updated_at"] = _now()
    col("orders").update_one({"_id": oid}, {"$set": updates})
    fresh = col("orders").find_one({"_id": oid})
    _notify(fresh["buyer"]["phone"],
            f"YiThume: order {fresh['code']} is ready to collect at {point.get('name')}. "
            f"Bring your 4-digit PIN.")
    return jsonify({"ok": True, "parcel": _point_parcel(fresh)})


@app.post("/api/kiosk/orders/<oid>/collect")
def kiosk_collect(oid):
    """Buyer collects. Same PIN, same proof — just a counter instead of a door."""
    point, err = _require_point()
    if err:
        return err
    order = col("orders").find_one({"_id": oid})
    if not order or order.get("fulfilment", {}).get("pickup_point_id") != point["_id"]:
        return jsonify({"ok": False, "error": "not a parcel for this point"}), 403
    updates = {}
    terr = _apply_status(order, "collected", updates, pin=body().get("pin"))
    if terr:
        return jsonify({"ok": False, "error": terr}), 409
    _finalize_delivery(order, updates)
    updates["updated_at"] = _now()
    col("orders").update_one({"_id": oid}, {"$set": updates})
    fresh = col("orders").find_one({"_id": oid})
    _notify_status(fresh, "collected")
    return jsonify({"ok": True, "parcel": _point_parcel(fresh)})


@app.post("/api/orders/<oid>/waybill")
def set_waybill(oid):
    """Operator books the long leg on the carrier's own system and records the
    waybill here. This is the whole manual-carrier integration."""
    actor, err = require_actor()
    if err:
        return err
    order = col("orders").find_one({"_id": oid})
    if not order or not owns(actor, order):
        return jsonify({"ok": False, "error": "not found"}), 404
    waybill = str(body().get("waybill", "")).strip()
    if not waybill:
        return jsonify({"ok": False, "error": "waybill required"}), 400
    fulfilment = dict(order.get("fulfilment", {}))
    legs = [dict(l) for l in fulfilment.get("legs", [])]
    if not legs:
        return jsonify({"ok": False, "error": "this order has no carrier leg"}), 409
    for leg in legs:
        if leg.get("type") == "carrier":
            leg["waybill"] = waybill
            leg["status"] = "booked"
            leg["at"] = _now()
    fulfilment["legs"] = legs
    col("orders").update_one({"_id": oid}, {"$set": {"fulfilment": fulfilment,
                                                     "updated_at": _now()}})
    return jsonify({"ok": True, "order": col("orders").find_one({"_id": oid})})


# ---------------- settlement: remittances & payouts ----------------
@app.post("/api/runner/remit")
def runner_remit():
    runner, err = _require_runner()
    if err:
        return err
    d = body()
    try:
        amount = round(float(d.get("amount", 0)), 2)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "amount must be a number"}), 400
    if amount <= 0:
        return jsonify({"ok": False, "error": "amount must be a number"}), 400
    orders = [o for o in find_all("orders", {"runner_id": runner["_id"], "status": "delivered"})
              if o.get("payment", {}).get("method") == "cod"
              and o.get("settlement", {}).get("cash_status") == "with_runner"]
    if not orders:
        return jsonify({"ok": False, "error": "no cash outstanding to remit"}), 409
    expected = round(sum(o["total"] for o in orders), 2)
    rem = {"_id": _new_id(), "runner_id": runner["_id"], "runner_name": runner["name"],
           "amount": amount, "reference": str(d.get("reference", "")).strip(),
           "expected_total": expected,
           "order_ids": [o["_id"] for o in orders], "order_codes": [o["code"] for o in orders],
           "status": "pending", "created_at": _now(), "resolved_at": ""}
    col("remittances").insert_one(rem)
    for o in orders:
        settlement = dict(o.get("settlement", {}))
        settlement["cash_status"] = "remit_pending"
        settlement["remittance_id"] = rem["_id"]
        col("orders").update_one({"_id": o["_id"]}, {"$set": {"settlement": settlement, "updated_at": _now()}})
    return jsonify({"ok": True, "remittance": rem})


@app.get("/api/runner/remittances")
def runner_remittances():
    runner, err = _require_runner()
    if err:
        return err
    rems = find_all("remittances", {"runner_id": runner["_id"]})
    rems.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return jsonify({"ok": True, "remittances": rems[:10]})


@app.get("/api/remittances")
def list_remittances():
    _actor, err = require_platform()
    if err:
        return err
    flt = {}
    if request.args.get("status"):
        flt["status"] = request.args["status"]
    rems = find_all("remittances", flt)
    rems.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return jsonify({"ok": True, "remittances": rems})


def _resolve_remittance(rid, confirm):
    rem = col("remittances").find_one({"_id": rid})
    if not rem:
        return jsonify({"ok": False, "error": "not found"}), 404
    if rem["status"] != "pending":
        return jsonify({"ok": False, "error": "already resolved"}), 409
    new_cash_status = "settled" if confirm else "with_runner"
    for oid in rem.get("order_ids", []):
        o = col("orders").find_one({"_id": oid})
        if not o:
            continue
        settlement = dict(o.get("settlement", {}))
        settlement["cash_status"] = new_cash_status
        if not confirm:
            settlement["remittance_id"] = ""
        col("orders").update_one({"_id": oid}, {"$set": {"settlement": settlement, "updated_at": _now()}})
    col("remittances").update_one({"_id": rid}, {"$set": {
        "status": "confirmed" if confirm else "rejected", "resolved_at": _now()}})
    return jsonify({"ok": True, "remittance": col("remittances").find_one({"_id": rid})})


@app.post("/api/remittances/<rid>/confirm")
def confirm_remittance(rid):
    _actor, err = require_platform()
    if err:
        return err
    return _resolve_remittance(rid, True)


@app.post("/api/remittances/<rid>/reject")
def reject_remittance(rid):
    _actor, err = require_platform()
    if err:
        return err
    return _resolve_remittance(rid, False)


@app.get("/api/payouts")
def list_payouts():
    """What's owed for settled, unpaid orders.

    Operators see the seller payouts they owe; platform admin sees runner and
    node payouts across the whole network.
    """
    actor, err = require_actor()
    if err:
        return err
    roles = ("seller",) if actor["role"] == "operator" else ("seller", "runner", "node")
    groups = {}
    for o in find_all("orders", scoped(actor, {"status": "delivered"})):
        if o.get("settlement", {}).get("cash_status") != "settled":
            continue
        for p in o.get("settlement", {}).get("payouts", []):
            if p.get("paid") or p["role"] not in roles:
                continue
            key = (p["role"], p["key"])
            g = groups.setdefault(key, {"role": p["role"], "key": p["key"], "total": 0.0,
                                        "orders": 0, "order_ids": [], "order_codes": []})
            g["total"] = round(g["total"] + float(p.get("amount", 0)), 2)
            g["orders"] += 1
            g["order_ids"].append(o["_id"])
            g["order_codes"].append(o["code"])
    rows = sorted(groups.values(), key=lambda g: (g["role"], -g["total"]))
    return jsonify({"ok": True, "payouts": rows})


@app.post("/api/payouts/mark_paid")
def mark_payout_paid():
    actor, err = require_actor()
    if err:
        return err
    d = body()
    role, key, order_ids = d.get("role"), d.get("key"), d.get("order_ids") or []
    if role not in ("seller", "runner", "node") or not key or not order_ids:
        return jsonify({"ok": False, "error": "role, key and order_ids required"}), 400
    marked = 0
    for oid in order_ids:
        o = col("orders").find_one({"_id": oid})
        if not o or not owns(actor, o):
            continue
        settlement = dict(o.get("settlement", {}))
        changed = False
        for p in settlement.get("payouts", []):
            if p["role"] == role and p["key"] == key and not p.get("paid"):
                p["paid"] = True
                p["paid_at"] = _now()
                changed = True
        if changed:
            col("orders").update_one({"_id": oid}, {"$set": {"settlement": settlement, "updated_at": _now()}})
            marked += 1
    return jsonify({"ok": True, "marked": marked})


# ---------------- wallet: money in, around, out ----------------
def _actor_account(actor=None, phone=""):
    """Resolve whose wallet a request is about."""
    if phone:
        return ledger.buyer_account(LD(), clean_phone(phone))
    if actor and actor["role"] == "operator":
        mid = actor["marketplace_id"]
        return ledger.account(LD(), "marketplace", mid, mid)
    if actor and actor["role"] == "platform":
        return ledger.platform_account(LD())
    return None


@app.get("/api/wallet")
def wallet_balance():
    """Buyers identify by phone; operators and admin by their credentials."""
    phone = request.args.get("phone", "")
    actor = current_actor()
    acct = _actor_account(actor, phone)
    if not acct:
        return jsonify({"ok": False, "error": "phone or sign-in required"}), 400
    bal = ledger.balances(LD(), acct["_id"])
    entries = []
    for e in find_all("ledger"):
        if not any(x["account_id"] == acct["_id"] for x in e.get("entries", [])):
            continue
        mine = [x for x in e["entries"] if x["account_id"] == acct["_id"]]
        delta = sum((x["amount"] if x["direction"] == "credit" else -x["amount"])
                    for x in mine if not x.get("hold"))
        entries.append({"kind": e["kind"], "ref": e["ref"], "amount": round(delta, 2),
                        "at": e["created_at"]})
    entries.sort(key=lambda x: x["at"], reverse=True)
    owed = 0.0
    if phone:
        owed = round(float(get_buyer(clean_phone(phone)).get("owed", 0)), 2)
    return jsonify({"ok": True, "account_id": acct["_id"], **bal, "owed": owed,
                    "history": entries[:25]})


@app.post("/api/wallet/topup")
def wallet_topup():
    """Start a real Paystack payment. The wallet is credited only after the
    rail confirms — never on the browser's say-so."""
    d = body()
    phone = clean_phone(d.get("phone"))
    actor = current_actor()
    acct = _actor_account(actor, phone)
    if not acct:
        return jsonify({"ok": False, "error": "phone or sign-in required"}), 400
    try:
        amount = round(float(d.get("amount", 0)), 2)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "amount must be a number"}), 400
    if amount <= 0:
        return jsonify({"ok": False, "error": "amount must be more than zero"}), 400
    reference = _ref_code("YT-TU")
    doc = {"_id": _new_id(), "account_id": acct["_id"], "phone": phone,
           "amount": amount, "reference": reference, "method": "paystack",
           "status": "pending", "created_at": _now()}
    col("topups").insert_one(doc)
    callback = f"{PUBLIC_URL}/wallet/return" if PUBLIC_URL else ""
    try:
        init = rail.topup_init(d.get("email", ""), amount, reference, callback)
    except rails.RailError as exc:
        col("topups").update_one({"_id": doc["_id"]},
                                 {"$set": {"status": "failed", "error": str(exc)}})
        return jsonify({"ok": False, "error": str(exc)}), 502
    return jsonify({"ok": True, "reference": reference,
                    "checkout_url": init["checkout_url"], "amount": amount})


def _credit_topup(reference):
    """Verify against the rail, then credit once. Safe to call repeatedly —
    the ledger's idempotency key is what makes webhook replay harmless."""
    top = col("topups").find_one({"reference": reference})
    if not top:
        return None, "unknown reference"
    if top["status"] == "confirmed":
        return top, None
    try:
        result = rail.topup_verify(reference)
    except rails.RailError as exc:
        return None, str(exc)
    if result.get("status") != "success":
        col("topups").update_one({"_id": top["_id"]}, {"$set": {"status": "failed"}})
        return None, "payment not successful"
    amount = round(float(result.get("amount") or top["amount"]), 2)
    ledger.topup(LD(), top["account_id"], amount, reference,
                 {"phone": top.get("phone", ""), "rail": rail.name})
    col("topups").update_one({"_id": top["_id"]}, {"$set": {
        "status": "confirmed", "amount": amount, "confirmed_at": _now()}})
    # A top-up clears failed-delivery debt before it becomes spendable.
    if top.get("phone"):
        b = get_buyer(top["phone"])
        if b and float(b.get("owed", 0)) > 0:
            bal = ledger.balances(LD(), top["account_id"])["balance"]
            if bal >= 0:
                col("buyers").update_one({"_id": b["_id"]}, {"$set": {
                    "owed": 0.0, "blocked_until_settled": False}})
    return col("topups").find_one({"_id": top["_id"]}), None


@app.post("/api/wallet/topup/verify")
def wallet_topup_verify():
    reference = str(body().get("reference") or request.args.get("reference") or "").strip()
    if not reference:
        return jsonify({"ok": False, "error": "reference required"}), 400
    top, err = _credit_topup(reference)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, "topup": top})


@app.post("/api/rail/webhook")
def rail_webhook():
    """Paystack posts here. An unverified webhook is an open door to free
    money, so the signature check is not optional."""
    raw = request.get_data() or b""
    signature = request.headers.get("x-paystack-signature", "")
    if not rail.verify_webhook(raw, signature):
        return jsonify({"ok": False, "error": "bad signature"}), 401
    try:
        event = json.loads(raw.decode() or "{}")
    except ValueError:
        return jsonify({"ok": False, "error": "bad payload"}), 400
    kind = event.get("event", "")
    data = event.get("data") or {}
    reference = data.get("reference", "")
    if kind == "charge.success" and reference:
        _credit_topup(reference)
    elif kind in ("transfer.success", "transfer.failed", "transfer.reversed") and reference:
        _resolve_transfer(reference, success=(kind == "transfer.success"))
    return jsonify({"ok": True})


@app.post("/api/wallet/withdraw")
def wallet_withdraw():
    """Request a cash-out. Nothing moves until an admin approves — withdrawals
    are where fraud converts to money."""
    actor, err = require_actor()
    if err:
        return err
    acct = _actor_account(actor)
    try:
        amount = round(float(body().get("amount", 0)), 2)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "amount must be a number"}), 400
    if amount <= 0:
        return jsonify({"ok": False, "error": "amount must be more than zero"}), 400
    avail = ledger.balances(LD(), acct["_id"])["available"]
    if amount > avail:
        return jsonify({"ok": False, "error": f"only R{avail:.2f} available"}), 409

    bank, hold_reason = {}, ""
    if actor["role"] == "operator":
        m = actor["marketplace"]
        bank = m.get("bank", {})
        if not bank.get("account_no"):
            return jsonify({"ok": False, "error": "add your bank details first"}), 409
        tier = m.get("trust", {}).get("tier", "unverified")
        if not TRUST_TIERS.get(tier, TRUST_TIERS["unverified"])["withdrawals"]:
            need = TRUST_TIERS["verified"]["deliveries_required"]
            hold_reason = (f"held while {tier}: complete {need} deliveries and add bank "
                           f"details to unlock withdrawals")
        changed = m.get("bank_changed_at", "")
        if changed and (datetime.utcnow() - datetime.fromisoformat(
                changed.replace("Z", ""))).total_seconds() < 48 * 3600:
            hold_reason = "bank details changed in the last 48 hours"
    doc = {"_id": _new_id(), "account_id": acct["_id"],
           "marketplace_id": actor.get("marketplace_id", ""),
           "amount": amount, "bank": bank,
           "status": "held" if hold_reason else "requested",
           "hold_reason": hold_reason, "transfer_ref": "",
           "created_at": _now(), "resolved_at": ""}
    col("withdrawals").insert_one(doc)
    return jsonify({"ok": True, "withdrawal": doc})


@app.get("/api/withdrawals")
def list_withdrawals():
    actor, err = require_actor()
    if err:
        return err
    flt = {} if actor["role"] == "platform" else {"marketplace_id": actor["marketplace_id"]}
    if request.args.get("status"):
        flt["status"] = request.args["status"]
    rows = find_all("withdrawals", flt)
    rows.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return jsonify({"ok": True, "withdrawals": rows})


@app.post("/api/withdrawals/<wid>/approve")
def approve_withdrawal(wid):
    """Admin approves, then YiThume actually pays — one human gate, then the
    Transfers API does the work."""
    _actor, err = require_platform()
    if err:
        return err
    w = col("withdrawals").find_one({"_id": wid})
    if not w:
        return jsonify({"ok": False, "error": "not found"}), 404
    if w["status"] in ("paid", "rejected"):
        return jsonify({"ok": False, "error": "already resolved"}), 409
    reference = _ref_code("YT-WD")
    # Debit first: the money must leave the internal account before it leaves
    # the rail, or a crash between the two mints money.
    ledger.withdraw(LD(), w["account_id"], w["amount"], reference,
                    {"withdrawal_id": wid})
    col("withdrawals").update_one({"_id": wid}, {"$set": {
        "status": "approved", "transfer_ref": reference, "resolved_at": _now()}})
    try:
        result = rail.payout(w["amount"], w.get("bank", {}), reference,
                             "YiThume withdrawal")
    except rails.RailError as exc:
        ledger.reverse(LD(), "withdrawal", reference, f"transfer failed: {exc}")
        col("withdrawals").update_one({"_id": wid}, {"$set": {
            "status": "requested", "hold_reason": str(exc), "transfer_ref": ""}})
        return jsonify({"ok": False, "error": str(exc)}), 502
    col("withdrawals").update_one({"_id": wid}, {"$set": {
        "status": "paid" if result.get("status") == "success" else "approved",
        "transfer_code": result.get("transfer_code", "")}})
    return jsonify({"ok": True, "withdrawal": col("withdrawals").find_one({"_id": wid})})


@app.post("/api/withdrawals/<wid>/reject")
def reject_withdrawal(wid):
    _actor, err = require_platform()
    if err:
        return err
    w = col("withdrawals").find_one({"_id": wid})
    if not w:
        return jsonify({"ok": False, "error": "not found"}), 404
    if w["status"] == "paid":
        return jsonify({"ok": False, "error": "already paid"}), 409
    col("withdrawals").update_one({"_id": wid}, {"$set": {
        "status": "rejected", "resolved_at": _now(),
        "hold_reason": str(body().get("reason", "rejected by admin"))}})
    return jsonify({"ok": True})


def _resolve_transfer(reference, success):
    w = col("withdrawals").find_one({"transfer_ref": reference})
    if not w or w["status"] == "paid":
        return
    if success:
        col("withdrawals").update_one({"_id": w["_id"]}, {"$set": {"status": "paid"}})
    else:
        # The bank refused it — put the money back where it came from.
        ledger.reverse(LD(), "withdrawal", reference, "transfer failed at the bank")
        col("withdrawals").update_one({"_id": w["_id"]}, {"$set": {
            "status": "requested", "transfer_ref": "",
            "hold_reason": "the bank rejected the transfer"}})


@app.get("/api/ledger/audit")
def ledger_audit():
    """Recompute every balance from the journal. Drift is a bug or a fraud."""
    _actor, err = require_platform()
    if err:
        return err
    return jsonify({"ok": True, "audit": ledger.audit(LD())})


@app.get("/api/rail/status")
def rail_status():
    """Does the pooled balance cover what we owe people?"""
    _actor, err = require_platform()
    if err:
        return err
    owed = ledger.liabilities(LD())
    pending = round(sum(w["amount"] for w in find_all("withdrawals")
                        if w.get("status") in ("requested", "approved")), 2)
    info = {"rail": rail.name, "mode": rail.mode(), "liabilities": owed,
            "pending_withdrawals": pending, "balance": None, "low": False, "error": ""}
    try:
        info["balance"] = rail.balance()
        info["low"] = info["balance"] < max(pending, float(get_settings()["float_min"]))
    except rails.RailError as exc:
        info["error"] = str(exc)
    return jsonify({"ok": True, "status": info})


# ---------------- disputes ----------------
@app.post("/api/orders/<oid>/dispute")
def raise_dispute(oid):
    """Opening a dispute freezes the money. Buyers identify with the phone the
    order was placed on."""
    order = col("orders").find_one({"_id": oid})
    if not order:
        return jsonify({"ok": False, "error": "not found"}), 404
    d = body()
    actor = current_actor()
    phone = clean_phone(d.get("phone"))
    if not actor and phone != order.get("buyer", {}).get("phone", ""):
        return jsonify({"ok": False, "error": "use the number you ordered with"}), 403
    doc = {"_id": _new_id(), "order_id": oid, "order_code": order["code"],
           "marketplace_id": order.get("marketplace_id", ""),
           "raiser": "operator" if actor and actor["role"] == "operator" else "buyer",
           "reason": str(d.get("reason", "")).strip()[:400],
           "evidence": [str(e)[:300] for e in (d.get("evidence") or [])][:5],
           "status": "open", "created_at": _now(), "resolved_at": ""}
    col("disputes").insert_one(doc)
    col("orders").update_one({"_id": oid}, {"$set": {"disputed": True, "updated_at": _now()}})
    m = col("marketplaces").find_one({"_id": order.get("marketplace_id", "")})
    if m:
        t = dict(m.get("trust") or _new_trust())
        t["disputes"] = int(t.get("disputes", 0)) + 1
        col("marketplaces").update_one({"_id": m["_id"]}, {"$set": {"trust": t}})
        _recheck_tier(m["_id"])
    return jsonify({"ok": True, "dispute": doc})


@app.get("/api/disputes")
def list_disputes():
    actor, err = require_actor()
    if err:
        return err
    rows = find_all("disputes", scoped(actor))
    rows.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return jsonify({"ok": True, "disputes": rows})


@app.post("/api/disputes/<did>/resolve")
def resolve_dispute(did):
    _actor, err = require_platform()
    if err:
        return err
    dispute = col("disputes").find_one({"_id": did})
    if not dispute:
        return jsonify({"ok": False, "error": "not found"}), 404
    if dispute["status"] != "open":
        return jsonify({"ok": False, "error": "already resolved"}), 409
    for_buyer = bool(body().get("for_buyer"))
    order = col("orders").find_one({"_id": dispute["order_id"]})
    if for_buyer and order and order.get("payment", {}).get("method") == "wallet":
        acct = ledger.buyer_account(LD(), order["buyer"]["phone"])
        if order.get("settlement", {}).get("cash_status") == "in_escrow":
            ledger.refund_escrow(LD(), acct["_id"], round(float(order["total"]), 2),
                                 order["code"], {"reason": "dispute upheld"})
        else:
            # Already released — the platform makes the buyer whole rather than
            # clawing back from a seller who may have spent it.
            plat = ledger.platform_account(LD())
            ledger.transfer(LD(), plat["_id"], acct["_id"], round(float(order["total"]), 2),
                            "dispute_refund", order["code"], {"dispute_id": did})
    col("disputes").update_one({"_id": did}, {"$set": {
        "status": "resolved_buyer" if for_buyer else "resolved_seller",
        "resolved_at": _now()}})
    return jsonify({"ok": True, "dispute": col("disputes").find_one({"_id": did})})


# ---------------- settings ----------------
@app.get("/api/settings")
def read_settings():
    actor, err = require_actor()
    if err:
        return err
    s = get_settings(actor["marketplace_id"])
    return jsonify({"ok": True, "settings": s,
                    "editable": OPERATOR_SETTINGS if actor["role"] == "operator"
                    else sorted(DEFAULT_SETTINGS.keys())})


@app.patch("/api/settings")
def update_settings():
    """Operators tune their own storefront's economics; the network-wide rates
    that decide what runners and YiThume earn stay with the platform."""
    actor, err = require_actor()
    if err:
        return err
    d = body()
    target = actor["marketplace_id"] or "settings"
    fields = OPERATOR_SETTINGS if actor["role"] == "operator" else list(DEFAULT_SETTINGS.keys())
    get_settings()  # ensure the platform record exists
    updates = {}
    for k in fields:
        if k not in d:
            continue
        if k == "eft_details":
            updates[k] = str(d[k]).strip()
        elif k == "strike_limit":
            try:
                updates[k] = int(d[k])
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "strike_limit must be a number"}), 400
        else:
            try:
                updates[k] = float(d[k])
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": f"{k} must be a number"}), 400
    if not updates:
        return jsonify({"ok": False, "error": "nothing to update"}), 400

    # Rates that over-commit an order would silently make the platform cover
    # the gap on every sale. Refuse it here rather than discover it at delivery.
    merged = {**get_settings(actor["marketplace_id"]), **updates}
    if float(merged["marketplace_rate"]) + float(merged["network_rate"]) >= 0.9:
        return jsonify({"ok": False,
                        "error": "commission rates must leave the seller at least 10%"}), 400
    if (float(merged["runner_rate"]) + float(merged["node_rate"])
            + float(merged["reserve_rate"])) > 1.0:
        return jsonify({"ok": False,
                        "error": "runner, node and reserve shares cannot exceed the delivery fee"}), 400

    updates["updated_at"] = _now()
    if col("settings").find_one({"_id": target}):
        col("settings").update_one({"_id": target}, {"$set": updates})
    else:
        col("settings").insert_one({"_id": target, **updates})
    return jsonify({"ok": True, "settings": get_settings(actor["marketplace_id"])})


# ---------------- USSD (aggregator-shaped endpoint + simulator page) ----------------
@app.post("/api/ussd")
def ussd_entry():
    """Accepts what a USSD aggregator (e.g. Africa's Talking) POSTs:
    sessionId, phoneNumber, serviceCode, text. Returns text/plain CON/END."""
    session_id = (request.values.get("sessionId") or "").strip()
    phone = clean_phone(request.values.get("phoneNumber") or "")
    text = (request.values.get("text") or "").strip()
    reply = ussd.handle(ussd.Deps(col=col, settings=get_settings, place_order=place_order,
                                  clean_phone=clean_phone, now=_now, new_id=_new_id),
                        session_id, phone, text)
    return reply, 200, {"Content-Type": "text/plain; charset=utf-8"}


# ---------------- stats ----------------
@app.get("/api/stats")
def stats():
    actor, err = require_actor()
    if err:
        return err
    operator = actor["role"] == "operator"
    mid = actor["marketplace_id"]
    orders = find_all("orders", scoped(actor))
    live = [o for o in orders if o.get("status") != "cancelled"]
    by_status = {}
    for o in orders:
        by_status[o["status"]] = by_status.get(o["status"], 0) + 1
    top = {}
    for o in live:
        for it in o.get("items", []):
            top[it["name"]] = top.get(it["name"], 0) + it["qty"]
    top_products = sorted(top.items(), key=lambda kv: -kv[1])[:5]
    orders_sorted = sorted(orders, key=lambda d: d.get("created_at", ""), reverse=True)
    fulfilled = [o for o in orders if o["status"] in FULFILLED]
    cash_out = round(sum(o["total"] for o in fulfilled
                         if o.get("payment", {}).get("method") == "cod"
                         and o.get("settlement", {}).get("cash_status") in ("with_runner", "remit_pending")), 2)
    payout_roles = ("seller",) if operator else ("seller", "runner", "node")
    payouts_owed = round(sum(p.get("amount", 0) for o in fulfilled
                             if o.get("settlement", {}).get("cash_status") == "settled"
                             for p in o.get("settlement", {}).get("payouts", [])
                             if not p.get("paid") and p.get("role") in payout_roles), 2)
    in_escrow = round(sum(o.get("total", 0) for o in orders
                          if o.get("settlement", {}).get("cash_status") == "in_escrow"), 2)
    scope = {"marketplace_id": mid} if operator else {}
    out = {
        "gmv": round(sum(o.get("total", 0) for o in live), 2),
        "marketplace_revenue": round(sum(o.get("marketplace_fee", 0) for o in live), 2),
        "orders_total": len(orders),
        "orders_by_status": by_status,
        "products": col("products").count_documents(scope),
        "sellers": col("sellers").count_documents(scope),
        "sellers_pending": col("sellers").count_documents({**scope, "approved": False}),
        "in_escrow": in_escrow,
        "cash_with_runners": cash_out,
        "payouts_owed": payouts_owed,
        "disputes_open": col("disputes").count_documents({**scope, "status": "open"}),
        "top_products": [{"name": n, "qty": q} for n, q in top_products],
        "recent_orders": orders_sorted[:8],
        "storage": storage_mode(),
        "whatsapp": "cloud_api" if whatsapp.enabled() else "fallback",
    }
    if operator:
        acct = ledger.account(LD(), "marketplace", mid, mid)
        out["wallet"] = ledger.balances(LD(), acct["_id"])
        out["reserve"] = ledger.balances(LD(), ledger.reserve_account(LD(), mid)["_id"])
        out["runners_available"] = (col("runners").count_documents({"marketplace_id": mid, "approved": True})
                                    + col("runners").count_documents({"marketplace_id": "", "approved": True}))
    else:
        out.update({
            "network_revenue": round(sum(o.get("network_fee", 0) for o in live), 2),
            "runners": col("runners").count_documents({}),
            "runners_online": col("runners").count_documents({"status": "online", "approved": True}),
            "runners_pending": col("runners").count_documents({"approved": False}),
            "runners_shared": col("runners").count_documents({"marketplace_id": ""}),
            "nodes": col("nodes").count_documents({}),
            "pickup_points": col("pickup_points").count_documents({}),
            "marketplaces": col("marketplaces").count_documents({}),
            "marketplaces_live": col("marketplaces").count_documents({"status": "live"}),
            "remittances_pending": col("remittances").count_documents({"status": "pending"}),
            "withdrawals_pending": col("withdrawals").count_documents({"status": "requested"}),
            "buyers_flagged": col("buyers").count_documents({"prepay_only": True}),
            "liabilities": ledger.liabilities(LD()),
            "rail": rail.name, "rail_mode": rail.mode(),
        })
    return jsonify({"ok": True, "stats": out})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
