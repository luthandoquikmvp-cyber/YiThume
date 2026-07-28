# app.py — YiThume: the commerce network that reaches every market.
#
# One clean backend: a DB with an API on top.
#   - MongoDB when MONGO_URI is set (persistent, production)
#   - In-memory demo store otherwise (works instantly, resets on restart)
#
# Entities: products, sellers, runners, orders, nodes, marketplaces,
#           buyers (reliability), remittances, settings, ussd_sessions
# Surfaces: /            → marketplace (buyers shop, checkout with OTP, COD/EFT)
#           /runner      → runner mobile view (jobs, PIN delivery, cash, remit)
#           /dashboard   → admin dashboard (orders, settlement, payouts, settings)
#           /ussd        → USSD simulator (POST /api/ussd is aggregator-shaped)

import os
import re
import copy
import uuid
import random
import string
import threading
from datetime import datetime
from urllib.parse import quote

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

try:
    from . import whatsapp, payments, ussd
except ImportError:
    import whatsapp
    import payments
    import ussd

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
    "platform_rate": float(os.environ.get("PLATFORM_FEE_RATE", "0.10")),   # of item subtotal
    "runner_rate": float(os.environ.get("RUNNER_FEE_RATE", "0.80")),       # of delivery fee
    "node_rate": float(os.environ.get("NODE_FEE_RATE", "0.20")),           # of delivery fee
    "cod_cap": float(os.environ.get("COD_CAP", "400.0")),                  # above this: EFT only
    "strike_limit": int(os.environ.get("STRIKE_LIMIT", "2")),              # strikes → prepay only
    "runner_cash_limit": float(os.environ.get("RUNNER_CASH_LIMIT", "1000.0")),
    "eft_details": os.environ.get(
        "EFT_DETAILS", "EFT to: YiThume, Capitec, account 1234567890."),
}
SETTINGS_NUMERIC = ["delivery_fee", "platform_rate", "runner_rate", "node_rate",
                    "cod_cap", "runner_cash_limit"]

STATUS_FLOW = {
    "awaiting_otp": ["pending", "cancelled"],
    "pending": ["confirmed", "cancelled"],
    "confirmed": ["assigned", "cancelled"],
    "assigned": ["picked_up", "cancelled"],
    "picked_up": ["delivered", "cancelled"],
    "delivered": [],
    "cancelled": [],
}

COLLECTIONS = ["products", "sellers", "runners", "orders", "nodes", "marketplaces",
               "buyers", "remittances", "settings", "ussd_sessions"]

payment_provider = payments.get_provider()

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
def get_settings():
    doc = col("settings").find_one({"_id": "settings"})
    if not doc:
        doc = {"_id": "settings", **DEFAULT_SETTINGS, "updated_at": _now()}
        col("settings").insert_one(doc)
    for k, v in DEFAULT_SETTINGS.items():
        doc.setdefault(k, v)
    return doc


# -------------------------------------------------
# SEED DATA — so the product works out of the box
# -------------------------------------------------
def _seed_docs():
    sellers = [
        {"_id": _new_id(), "shop_name": "Nomsa's Spaza", "owner_name": "Nomsa Dlamini",
         "phone": "27710000001", "area": "Mthatha", "approved": True, "created_at": _now()},
        {"_id": _new_id(), "shop_name": "Kei Fresh Produce", "owner_name": "Sipho Mbeki",
         "phone": "27710000002", "area": "Butterworth", "approved": True, "created_at": _now()},
        {"_id": _new_id(), "shop_name": "Lwazi Electronics", "owner_name": "Lwazi Nkosi",
         "phone": "27710000003", "area": "Mthatha", "approved": True, "created_at": _now()},
    ]
    marketplaces = [
        {"_id": _new_id(), "name": "Mthatha Town Market", "town": "Mthatha",
         "contact_name": "Thandi Jara", "phone": "27720000001", "status": "connected", "created_at": _now()},
        {"_id": _new_id(), "name": "Idutywa Street Market", "town": "Idutywa",
         "contact_name": "Bongani Sithole", "phone": "27720000002", "status": "pending", "created_at": _now()},
    ]
    runners = [
        {"_id": _new_id(), "name": "Andile M.", "phone": "27730000001", "area": "Mthatha",
         "vehicle": "Motorbike", "status": "online", "approved": True, "deliveries_done": 128, "created_at": _now()},
        {"_id": _new_id(), "name": "Zinhle K.", "phone": "27730000002", "area": "Butterworth",
         "vehicle": "Bicycle", "status": "online", "approved": True, "deliveries_done": 74, "created_at": _now()},
        {"_id": _new_id(), "name": "Thabo S.", "phone": "27730000003", "area": "Mthatha",
         "vehicle": "Car", "status": "offline", "approved": True, "deliveries_done": 211, "created_at": _now()},
    ]
    nodes = [
        {"_id": _new_id(), "name": "Mthatha Node", "operator_name": "Vuyo Mda", "phone": "27740000001",
         "territory": "Mthatha & surrounds", "commission_rate": 0.05, "runners_count": 2, "created_at": _now()},
    ]
    s = {x["shop_name"]: x["_id"] for x in sellers}
    m = {x["name"]: x["_id"] for x in marketplaces}
    products = [
        ("Maize Meal 10kg", 89.99, "Groceries", "Nomsa's Spaza", None, 40),
        ("Cooking Oil 2L", 74.50, "Groceries", "Nomsa's Spaza", None, 35),
        ("Sugar 5kg", 62.00, "Groceries", "Nomsa's Spaza", None, 50),
        ("Fresh Spinach Bundle", 18.00, "Fresh Produce", "Kei Fresh Produce", "Mthatha Town Market", 60),
        ("Tomatoes 1kg", 22.50, "Fresh Produce", "Kei Fresh Produce", "Mthatha Town Market", 45),
        ("Free-range Eggs (18)", 55.00, "Fresh Produce", "Kei Fresh Produce", None, 30),
        ("Phone Charger (USB-C)", 95.00, "Electronics", "Lwazi Electronics", None, 25),
        ("Solar Lantern", 189.00, "Electronics", "Lwazi Electronics", "Mthatha Town Market", 15),
        ("Paraffin 5L", 120.00, "Household", "Nomsa's Spaza", None, 20),
        ("Washing Powder 2kg", 68.00, "Household", "Nomsa's Spaza", None, 32),
        ("Airtime Voucher R50", 50.00, "Airtime & Data", "Lwazi Electronics", None, 999),
        ("School Shoes (Black)", 249.00, "Clothing", "Mthatha Town Market Stall 14", "Mthatha Town Market", 12),
    ]
    product_docs = []
    for name, price, cat, seller_name, mkt, stock in products:
        product_docs.append({
            "_id": _new_id(), "name": name, "price": price, "category": cat,
            "image_url": "", "seller_id": s.get(seller_name, ""), "seller_name": seller_name,
            "marketplace_id": m.get(mkt, "") if mkt else "", "marketplace_name": mkt or "",
            "stock": stock, "active": True, "created_at": _now(),
        })
    return {"sellers": sellers, "marketplaces": marketplaces, "runners": runners,
            "nodes": nodes, "products": product_docs, "orders": []}


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
    pin = request.headers.get("X-Admin-Pin") or request.args.get("pin") or ""
    return pin == ADMIN_SECRET


def require_admin():
    if not is_admin():
        return jsonify({"ok": False, "error": "admin pin required"}), 401
    return None


def body():
    return request.get_json(silent=True) or {}


def clean_phone(p):
    return re.sub(r"\D", "", str(p or ""))


# ---------------- pages ----------------
@app.get("/")
def page_index():
    return send_from_directory(app.static_folder, "index.html")


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
    s = get_settings()
    return jsonify({"ok": True, "config": {
        "delivery_fee": s["delivery_fee"], "cod_cap": s["cod_cap"],
        "eft_details": s["eft_details"], "whatsapp_enabled": whatsapp.enabled(),
    }})


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
@app.get("/api/products")
def list_products():
    flt = {"active": True} if request.args.get("all") != "1" else {}
    if request.args.get("category"):
        flt["category"] = request.args["category"]
    if request.args.get("seller_id"):
        flt["seller_id"] = request.args["seller_id"]
    if request.args.get("marketplace_id"):
        flt["marketplace_id"] = request.args["marketplace_id"]
    q = (request.args.get("q") or "").strip()
    if q:
        rx = {"$regex": re.escape(q), "$options": "i"}
        flt["$or"] = [{"name": rx}, {"category": rx}, {"seller_name": rx}]
    items = find_all("products", flt)
    items.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return jsonify({"ok": True, "products": items})


@app.get("/api/categories")
def list_categories():
    cats = sorted({p.get("category", "") for p in find_all("products", {"active": True})} - {""})
    return jsonify({"ok": True, "categories": cats})


@app.post("/api/products")
def create_product():
    err = require_admin()
    if err:
        return err
    d = body()
    if not d.get("name") or d.get("price") in (None, ""):
        return jsonify({"ok": False, "error": "name and price required"}), 400
    seller = col("sellers").find_one({"_id": d.get("seller_id", "")}) if d.get("seller_id") else None
    mkt = col("marketplaces").find_one({"_id": d.get("marketplace_id", "")}) if d.get("marketplace_id") else None
    doc = {
        "_id": _new_id(), "name": str(d["name"]).strip(), "price": float(d["price"]),
        "category": str(d.get("category", "General")).strip() or "General",
        "image_url": str(d.get("image_url", "")).strip(),
        "seller_id": seller["_id"] if seller else "",
        "seller_name": seller["shop_name"] if seller else str(d.get("seller_name", "")).strip(),
        "marketplace_id": mkt["_id"] if mkt else "",
        "marketplace_name": mkt["name"] if mkt else "",
        "stock": int(d.get("stock", 0) or 0), "active": bool(d.get("active", True)),
        "created_at": _now(),
    }
    col("products").insert_one(doc)
    return jsonify({"ok": True, "product": doc})


@app.patch("/api/products/<pid>")
def update_product(pid):
    err = require_admin()
    if err:
        return err
    d = body()
    allowed = {}
    for k in ["name", "category", "image_url", "seller_name", "marketplace_name"]:
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
    if not col("products").update_one({"_id": pid}, {"$set": allowed}):
        if not col("products").find_one({"_id": pid}):
            return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": True, "product": col("products").find_one({"_id": pid})})


@app.delete("/api/products/<pid>")
def delete_product(pid):
    err = require_admin()
    if err:
        return err
    col("products").delete_one({"_id": pid})
    return jsonify({"ok": True})


# ---------------- generic CRUD: sellers / runners / nodes / marketplaces ----------------
ENTITY_FIELDS = {
    "sellers": ["shop_name", "owner_name", "phone", "area", "approved"],
    "runners": ["name", "phone", "area", "vehicle", "status", "approved"],
    "nodes": ["name", "operator_name", "phone", "territory", "commission_rate", "runners_count"],
    "marketplaces": ["name", "town", "contact_name", "phone", "status"],
}


def _coerce(entity, key, val):
    if key == "approved":
        return bool(val)
    if key == "commission_rate":
        return float(val)
    if key == "runners_count":
        return int(val)
    if key == "phone":
        return clean_phone(val)
    return str(val).strip()


@app.get("/api/<entity>")
def list_entity(entity):
    if entity not in ENTITY_FIELDS:
        return jsonify({"ok": False, "error": "unknown entity"}), 404
    err = require_admin()
    if err:
        return err
    items = find_all(entity)
    items.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    if entity == "runners":
        for r in items:
            r["cash_in_hand"] = _cash_in_hand(r["_id"])
    return jsonify({"ok": True, entity: items})


@app.post("/api/<entity>")
def create_entity(entity):
    if entity not in ENTITY_FIELDS:
        return jsonify({"ok": False, "error": "unknown entity"}), 404
    err = require_admin()
    if err:
        return err
    d = body()
    doc = {"_id": _new_id(), "created_at": _now()}
    for k in ENTITY_FIELDS[entity]:
        if k in d:
            doc[k] = _coerce(entity, k, d[k])
    name_key = ENTITY_FIELDS[entity][0]
    if not doc.get(name_key):
        return jsonify({"ok": False, "error": f"{name_key} required"}), 400
    doc.setdefault("approved", True)
    if entity == "runners":
        doc.setdefault("status", "offline")
        doc.setdefault("deliveries_done", 0)
    if entity == "marketplaces":
        doc.setdefault("status", "pending")
    col(entity).insert_one(doc)
    return jsonify({"ok": True, "item": doc})


@app.patch("/api/<entity>/<eid>")
def update_entity(entity, eid):
    if entity not in ENTITY_FIELDS:
        return jsonify({"ok": False, "error": "unknown entity"}), 404
    err = require_admin()
    if err:
        return err
    d = body()
    allowed = {k: _coerce(entity, k, v) for k, v in d.items() if k in ENTITY_FIELDS[entity]}
    if not allowed:
        return jsonify({"ok": False, "error": "nothing to update"}), 400
    col(entity).update_one({"_id": eid}, {"$set": allowed})
    item = col(entity).find_one({"_id": eid})
    if not item:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": True, "item": item})


@app.delete("/api/<entity>/<eid>")
def delete_entity(entity, eid):
    if entity not in ENTITY_FIELDS:
        return jsonify({"ok": False, "error": "unknown entity"}), 404
    err = require_admin()
    if err:
        return err
    col(entity).delete_one({"_id": eid})
    return jsonify({"ok": True})


# ---------------- public applications (join the network) ----------------
@app.post("/api/apply/seller")
def apply_seller():
    d = body()
    if not d.get("shop_name") or not d.get("phone"):
        return jsonify({"ok": False, "error": "shop_name and phone required"}), 400
    doc = {"_id": _new_id(), "shop_name": str(d["shop_name"]).strip(),
           "owner_name": str(d.get("owner_name", "")).strip(), "phone": clean_phone(d["phone"]),
           "area": str(d.get("area", "")).strip(), "approved": False, "created_at": _now()}
    col("sellers").insert_one(doc)
    return jsonify({"ok": True, "message": "Application received — we'll contact you on WhatsApp.", "id": doc["_id"]})


@app.post("/api/apply/runner")
def apply_runner():
    d = body()
    if not d.get("name") or not d.get("phone"):
        return jsonify({"ok": False, "error": "name and phone required"}), 400
    doc = {"_id": _new_id(), "name": str(d["name"]).strip(), "phone": clean_phone(d["phone"]),
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
             "incidents": [], "orders_placed": 0, "created_at": _now()}
        col("buyers").insert_one(b)
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
    err = require_admin()
    if err:
        return err
    items = [b for b in find_all("buyers") if b.get("strikes", 0) > 0 or b.get("prepay_only")]
    items.sort(key=lambda d: -int(d.get("strikes", 0)))
    return jsonify({"ok": True, "buyers": items})


@app.patch("/api/buyers/<bid>")
def update_buyer(bid):
    err = require_admin()
    if err:
        return err
    d = body()
    updates = {}
    if "prepay_only" in d:
        updates["prepay_only"] = bool(d["prepay_only"])
    if d.get("reset_strikes"):
        updates["strikes"] = 0
        updates["prepay_only"] = False
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


def _compute_fees(items, subtotal, delivery_fee, area, settings):
    """Fee breakdown frozen onto the order at checkout time."""
    platform_commission = round(subtotal * float(settings["platform_rate"]), 2)
    runner_earning = round(delivery_fee * float(settings["runner_rate"]), 2)
    node = _find_node_for_area(area)
    node_commission = round(delivery_fee * float(settings["node_rate"]), 2) if node else 0.0
    per_seller = {}
    for it in items:
        name = it.get("seller_name") or "Unknown seller"
        per_seller[name] = per_seller.get(name, 0.0) + it["price"] * it["qty"]
    sellers = [{"seller_name": n, "amount": round(amt * (1 - float(settings["platform_rate"])), 2)}
               for n, amt in per_seller.items()]
    return {
        "subtotal": subtotal, "delivery_fee": delivery_fee,
        "platform_commission": platform_commission,
        "runner_earning": runner_earning,
        "node_commission": node_commission,
        "node_id": node["_id"] if node else "", "node_name": node["name"] if node else "",
        "seller_payout": round(subtotal - platform_commission, 2),
        "sellers": sellers,
        "rates": {"platform_rate": float(settings["platform_rate"]),
                  "runner_rate": float(settings["runner_rate"]),
                  "node_rate": float(settings["node_rate"])},
    }


def _initial_payouts(fees):
    payouts = [{"role": "seller", "key": s["seller_name"], "amount": s["amount"], "paid": False}
               for s in fees["sellers"]]
    if fees["node_name"] and fees["node_commission"] > 0:
        payouts.append({"role": "node", "key": fees["node_name"],
                        "amount": fees["node_commission"], "paid": False})
    return payouts


def place_order(buyer, items_in, channel="web", payment_method="cod", skip_otp=False):
    """Core order creation shared by the web API and the USSD flow.
    Returns (order, extras, error, http_status)."""
    settings = get_settings()
    payment_method = (payment_method or "cod").lower()
    if not payment_provider.validate_method(payment_method):
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
        qty = max(1, int(it.get("qty", 1)))
        items.append({"product_id": p["_id"], "name": p["name"], "price": float(p["price"]),
                      "qty": qty, "seller_name": p.get("seller_name", "")})
        subtotal += float(p["price"]) * qty

    subtotal = round(subtotal, 2)
    delivery_fee = float(settings["delivery_fee"])
    total = round(subtotal + delivery_fee, 2)
    phone = clean_phone(buyer["phone"])

    if payment_method == "cod":
        buyer_rec = get_buyer(phone)
        if buyer_rec and buyer_rec.get("prepay_only"):
            return None, None, "Cash on delivery is not available for this number. Please pay by EFT first.", 409
        if total > float(settings["cod_cap"]):
            return None, None, f"Orders over R{float(settings['cod_cap']):.0f} must be paid by EFT upfront.", 409

    code = _order_code()
    fees = _compute_fees(items, subtotal, delivery_fee, buyer.get("area", ""), settings)
    payment = payment_provider.initiate(payment_method, code, settings)
    otp = _otp_code()
    status = "pending" if skip_otp else "awaiting_otp"
    order = {
        "_id": _new_id(), "code": code,
        "buyer": {"name": str(buyer["name"]).strip(), "phone": phone,
                  "area": str(buyer.get("area", "")).strip(), "address": str(buyer.get("address", "")).strip()},
        "items": items, "subtotal": subtotal, "delivery_fee": delivery_fee,
        "platform_fee": fees["platform_commission"], "total": total,
        "fees": fees, "payment": payment,
        "otp": otp, "otp_verified": skip_otp, "otp_attempts": 0,
        "delivery_pin": "",
        "settlement": {"cash_status": "prepaid" if payment_method == "eft" else "unpaid",
                       "remittance_id": "", "payouts": _initial_payouts(fees)},
        "channel": str(channel), "status": status,
        "runner_id": "", "runner_name": "",
        "created_at": _now(), "updated_at": _now(),
        "timeline": [{"status": status, "at": _now()}],
    }
    col("orders").insert_one(order)
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
    order, extras, err, code = place_order(d.get("buyer") or {}, d.get("items") or [],
                                           channel=str(d.get("channel", "web")),
                                           payment_method=d.get("payment_method", "cod"))
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
    err = require_admin()
    if err:
        return err
    flt = {}
    if request.args.get("status"):
        flt["status"] = request.args["status"]
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
    if new_status == "delivered":
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
    if payment.get("method") == "cod":
        settlement["cash_status"] = "with_runner"
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


@app.patch("/api/orders/<oid>")
def update_order(oid):
    err = require_admin()
    if err:
        return err
    d = body()
    order = col("orders").find_one({"_id": oid})
    if not order:
        return jsonify({"ok": False, "error": "not found"}), 404
    updates = {}
    assigned_runner = None

    if d.get("runner_id"):
        rid = d["runner_id"]
        settings = get_settings()
        if rid == "auto":
            candidates = [r for r in find_all("runners", {"approved": True, "status": "online"})
                          if _runner_available(r, settings)]
            area = order.get("buyer", {}).get("area", "")
            candidates.sort(key=lambda r: (0 if r.get("area", "").lower() == area.lower() else 1,
                                           r.get("deliveries_done", 0)))
            if not candidates:
                return jsonify({"ok": False, "error": "no available runners (online and under cash limit)"}), 409
            runner = candidates[0]
        else:
            runner = col("runners").find_one({"_id": rid})
            if not runner:
                return jsonify({"ok": False, "error": "runner not found"}), 404
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
        if d["status"] == "delivered":
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
    """Admin: log a buyer no-show / refused-COD incident. Cancels the order
    and adds a strike against the buyer's number."""
    err = require_admin()
    if err:
        return err
    return _record_incident(oid, str(body().get("reason", "no_show")))


def _record_incident(oid, reason):
    order = col("orders").find_one({"_id": oid})
    if not order:
        return jsonify({"ok": False, "error": "not found"}), 404
    if order["status"] in ("delivered", "cancelled"):
        return jsonify({"ok": False, "error": "order already closed"}), 409
    updates = {"status": "cancelled", "updated_at": _now(),
               "timeline": order.get("timeline", []) + [{"status": "cancelled", "at": _now(), "note": f"incident: {reason}"}]}
    col("orders").update_one({"_id": oid}, {"$set": updates})
    buyer = add_strike(order["buyer"]["phone"], order["code"], reason)
    return jsonify({"ok": True, "order": col("orders").find_one({"_id": oid}), "buyer": buyer})


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
    return {
        "_id": order["_id"], "code": order["code"], "status": order["status"],
        "items": [{"name": i["name"], "qty": i["qty"], "seller_name": i.get("seller_name", "")} for i in order["items"]],
        "buyer_name": order["buyer"]["name"], "buyer_phone": order["buyer"]["phone"],
        "area": order["buyer"]["area"], "address": order["buyer"]["address"],
        "total": order["total"], "payment_method": payment.get("method", "cod"),
        "collect_cash": payment.get("method") == "cod" and order["status"] != "delivered",
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
    done = [o for o in mine if o["status"] == "delivered"]
    done.sort(key=lambda d: d.get("updated_at", ""), reverse=True)
    available = [o for o in find_all("orders", {"status": "confirmed", "runner_id": ""})]
    available.sort(key=lambda d: (0 if d.get("buyer", {}).get("area", "").lower() == runner.get("area", "").lower() else 1,
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
    if new_status == "delivered":
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
    err = require_admin()
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
    err = require_admin()
    if err:
        return err
    return _resolve_remittance(rid, True)


@app.post("/api/remittances/<rid>/reject")
def reject_remittance(rid):
    err = require_admin()
    if err:
        return err
    return _resolve_remittance(rid, False)


@app.get("/api/payouts")
def list_payouts():
    """What's owed to each seller, runner and node for settled, unpaid orders."""
    err = require_admin()
    if err:
        return err
    groups = {}
    for o in find_all("orders", {"status": "delivered"}):
        if o.get("settlement", {}).get("cash_status") != "settled":
            continue
        for p in o.get("settlement", {}).get("payouts", []):
            if p.get("paid"):
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
    err = require_admin()
    if err:
        return err
    d = body()
    role, key, order_ids = d.get("role"), d.get("key"), d.get("order_ids") or []
    if role not in ("seller", "runner", "node") or not key or not order_ids:
        return jsonify({"ok": False, "error": "role, key and order_ids required"}), 400
    marked = 0
    for oid in order_ids:
        o = col("orders").find_one({"_id": oid})
        if not o:
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


# ---------------- settings ----------------
@app.get("/api/settings")
def read_settings():
    err = require_admin()
    if err:
        return err
    return jsonify({"ok": True, "settings": get_settings()})


@app.patch("/api/settings")
def update_settings():
    err = require_admin()
    if err:
        return err
    d = body()
    get_settings()  # ensure the record exists
    updates = {}
    for k in SETTINGS_NUMERIC:
        if k in d:
            try:
                updates[k] = float(d[k])
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": f"{k} must be a number"}), 400
    if "strike_limit" in d:
        try:
            updates["strike_limit"] = int(d["strike_limit"])
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "strike_limit must be a number"}), 400
    if "eft_details" in d:
        updates["eft_details"] = str(d["eft_details"]).strip()
    if not updates:
        return jsonify({"ok": False, "error": "nothing to update"}), 400
    updates["updated_at"] = _now()
    col("settings").update_one({"_id": "settings"}, {"$set": updates})
    return jsonify({"ok": True, "settings": get_settings()})


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
    err = require_admin()
    if err:
        return err
    orders = find_all("orders")
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
    delivered = [o for o in orders if o["status"] == "delivered"]
    cash_out = round(sum(o["total"] for o in delivered
                         if o.get("payment", {}).get("method") == "cod"
                         and o.get("settlement", {}).get("cash_status") in ("with_runner", "remit_pending")), 2)
    payouts_owed = round(sum(p.get("amount", 0) for o in delivered
                             if o.get("settlement", {}).get("cash_status") == "settled"
                             for p in o.get("settlement", {}).get("payouts", []) if not p.get("paid")), 2)
    return jsonify({"ok": True, "stats": {
        "gmv": round(sum(o.get("total", 0) for o in live), 2),
        "platform_revenue": round(sum(o.get("platform_fee", 0) for o in live), 2),
        "orders_total": len(orders),
        "orders_by_status": by_status,
        "products": col("products").count_documents({}),
        "sellers": col("sellers").count_documents({}),
        "sellers_pending": col("sellers").count_documents({"approved": False}),
        "runners": col("runners").count_documents({}),
        "runners_online": col("runners").count_documents({"status": "online", "approved": True}),
        "runners_pending": col("runners").count_documents({"approved": False}),
        "nodes": col("nodes").count_documents({}),
        "marketplaces": col("marketplaces").count_documents({}),
        "cash_with_runners": cash_out,
        "remittances_pending": col("remittances").count_documents({"status": "pending"}),
        "payouts_owed": payouts_owed,
        "buyers_flagged": col("buyers").count_documents({"prepay_only": True}),
        "top_products": [{"name": n, "qty": q} for n, q in top_products],
        "recent_orders": orders_sorted[:8],
        "storage": storage_mode(),
        "whatsapp": "cloud_api" if whatsapp.enabled() else "fallback",
    }})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
