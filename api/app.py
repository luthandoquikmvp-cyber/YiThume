# app.py — YiThume: the commerce network that reaches every market.
#
# One clean backend: a DB with an API on top.
#   - MongoDB when MONGO_URI is set (persistent, production)
#   - In-memory demo store otherwise (works instantly, resets on restart)
#
# Entities: products, sellers, runners, orders, nodes, marketplaces
# Surfaces: /            → marketplace (buyers shop, checkout via WhatsApp or web)
#           /dashboard   → admin dashboard (the whole DB with a UI on top)

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

# -------------------------------------------------
# CONFIG
# -------------------------------------------------
MONGO_URI = os.environ.get("MONGO_URI", "").strip()
DB_NAME = os.environ.get("MONGO_DB", "yithume")

ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "1234")  # override in production
WHATSAPP_NUMBER = os.environ.get("WHATSAPP_NUMBER", "27600000000")  # no leading +

PLATFORM_FEE_RATE = float(os.environ.get("PLATFORM_FEE_RATE", "0.10"))
ITEM_MARGIN_RATE = float(os.environ.get("ITEM_MARGIN_RATE", "0.12"))
DELIVERY_FEE = float(os.environ.get("DELIVERY_FEE", "25.0"))
AUTO_SEED = os.environ.get("AUTO_SEED", "true").lower() == "true"

STATUS_FLOW = {
    "pending": ["confirmed", "cancelled"],
    "confirmed": ["assigned", "cancelled"],
    "assigned": ["picked_up", "cancelled"],
    "picked_up": ["delivered"],
    "delivered": [],
    "cancelled": [],
}

COLLECTIONS = ["products", "sellers", "runners", "orders", "nodes", "marketplaces"]

# -------------------------------------------------
# STORAGE — Mongo when configured, in-memory otherwise
# -------------------------------------------------
def _now():
    return datetime.utcnow().isoformat() + "Z"


def _new_id():
    return uuid.uuid4().hex


def _order_code():
    return "YT-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=5))


class MemoryCollection:
    """Tiny pymongo-compatible subset: equality, $or, $in, $ne, $regex filters."""

    def __init__(self):
        self._docs = []
        self._lock = threading.Lock()

    @staticmethod
    def _match(doc, flt):
        for key, cond in (flt or {}).items():
            if key == "$or":
                if not any(MemoryCollection._match(doc, sub) for sub in cond):
                    return False
            elif isinstance(cond, dict):
                if "$regex" in cond:
                    flags = re.I if "i" in cond.get("$options", "") else 0
                    if not re.search(cond["$regex"], str(doc.get(key, "")), flags):
                        return False
                elif "$in" in cond:
                    if doc.get(key) not in cond["$in"]:
                        return False
                elif "$ne" in cond:
                    if doc.get(key) == cond["$ne"]:
                        return False
                else:
                    if doc.get(key) != cond:
                        return False
            elif doc.get(key) != cond:
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
        ("Maize Meal 10kg", 89.99, "Groceries", "🌽", "Nomsa's Spaza", None, 40),
        ("Cooking Oil 2L", 74.50, "Groceries", "🫒", "Nomsa's Spaza", None, 35),
        ("Sugar 5kg", 62.00, "Groceries", "🍬", "Nomsa's Spaza", None, 50),
        ("Fresh Spinach Bundle", 18.00, "Fresh Produce", "🥬", "Kei Fresh Produce", "Mthatha Town Market", 60),
        ("Tomatoes 1kg", 22.50, "Fresh Produce", "🍅", "Kei Fresh Produce", "Mthatha Town Market", 45),
        ("Free-range Eggs (18)", 55.00, "Fresh Produce", "🥚", "Kei Fresh Produce", None, 30),
        ("Phone Charger (USB-C)", 95.00, "Electronics", "🔌", "Lwazi Electronics", None, 25),
        ("Solar Lantern", 189.00, "Electronics", "💡", "Lwazi Electronics", "Mthatha Town Market", 15),
        ("Paraffin 5L", 120.00, "Household", "🛢️", "Nomsa's Spaza", None, 20),
        ("Washing Powder 2kg", 68.00, "Household", "🧺", "Nomsa's Spaza", None, 32),
        ("Airtime Voucher R50", 50.00, "Airtime & Data", "📱", "Lwazi Electronics", None, 999),
        ("School Shoes (Black)", 249.00, "Clothing", "👞", "Mthatha Town Market Stall 14", "Mthatha Town Market", 12),
    ]
    product_docs = []
    for name, price, cat, emoji, seller_name, mkt, stock in products:
        product_docs.append({
            "_id": _new_id(), "name": name, "price": price, "category": cat, "emoji": emoji,
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


@app.get("/favicon.ico")
def favicon_ico():
    try:
        return send_from_directory(app.static_folder, "favicon.png")
    except Exception:
        return ("", 204)


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "YiThume", "storage": storage_mode(), "time": _now()})


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
        "emoji": str(d.get("emoji", "🛍️")).strip() or "🛍️",
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
    for k in ["name", "category", "emoji", "image_url", "seller_name", "marketplace_name"]:
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


# ---------------- orders ----------------
def _whatsapp_url(order):
    lines = [f"YiThume order {order['code']}", ""]
    for it in order["items"]:
        lines.append(f"{it['qty']} x {it['name']} — R{it['price'] * it['qty']:.2f}")
    lines += ["", f"Delivery: R{order['delivery_fee']:.2f}", f"Total: R{order['total']:.2f}",
              f"Deliver to: {order['buyer']['address']}, {order['buyer']['area']}", "", "Reply CONFIRM to place this order."]
    return f"https://wa.me/{WHATSAPP_NUMBER}?text={quote(chr(10).join(lines))}"


@app.post("/api/orders")
def create_order():
    d = body()
    buyer = d.get("buyer") or {}
    items_in = d.get("items") or []
    if not buyer.get("name") or not buyer.get("phone"):
        return jsonify({"ok": False, "error": "buyer name and phone required"}), 400
    if not items_in:
        return jsonify({"ok": False, "error": "cart is empty"}), 400

    items, subtotal = [], 0.0
    for it in items_in:
        p = col("products").find_one({"_id": str(it.get("product_id", ""))})
        if not p or not p.get("active", True):
            return jsonify({"ok": False, "error": f"product not available: {it.get('product_id')}"}), 400
        qty = max(1, int(it.get("qty", 1)))
        items.append({"product_id": p["_id"], "name": p["name"], "price": float(p["price"]),
                      "qty": qty, "seller_name": p.get("seller_name", "")})
        subtotal += float(p["price"]) * qty

    subtotal = round(subtotal, 2)
    platform_fee = round(subtotal * PLATFORM_FEE_RATE, 2)
    total = round(subtotal + DELIVERY_FEE, 2)
    order = {
        "_id": _new_id(), "code": _order_code(),
        "buyer": {"name": str(buyer["name"]).strip(), "phone": clean_phone(buyer["phone"]),
                  "area": str(buyer.get("area", "")).strip(), "address": str(buyer.get("address", "")).strip()},
        "items": items, "subtotal": subtotal, "delivery_fee": DELIVERY_FEE,
        "platform_fee": platform_fee, "total": total,
        "channel": str(d.get("channel", "web")), "status": "pending",
        "runner_id": "", "runner_name": "",
        "created_at": _now(), "updated_at": _now(),
        "timeline": [{"status": "pending", "at": _now()}],
    }
    col("orders").insert_one(order)
    for it in items:  # best-effort stock decrement
        p = col("products").find_one({"_id": it["product_id"]})
        if p and isinstance(p.get("stock"), int) and p["stock"] > 0:
            col("products").update_one({"_id": p["_id"]}, {"$set": {"stock": max(0, p["stock"] - it["qty"])}})
    return jsonify({"ok": True, "order": order, "whatsapp_url": _whatsapp_url(order)})


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
    flt = {"code": code} if code else {"buyer.phone": phone}
    orders = find_all("orders")
    if code:
        orders = [o for o in orders if o.get("code", "").upper() == code]
    else:
        orders = [o for o in orders if o.get("buyer", {}).get("phone") == phone]
    orders.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    public = [{"code": o["code"], "status": o["status"], "total": o["total"],
               "items": [{"name": i["name"], "qty": i["qty"]} for i in o["items"]],
               "runner_name": o.get("runner_name", ""), "created_at": o["created_at"],
               "timeline": o.get("timeline", [])} for o in orders[:5]]
    return jsonify({"ok": True, "orders": public})


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

    if d.get("runner_id"):
        rid = d["runner_id"]
        if rid == "auto":
            candidates = [r for r in find_all("runners", {"approved": True, "status": "online"})]
            area = order.get("buyer", {}).get("area", "")
            candidates.sort(key=lambda r: (0 if r.get("area", "").lower() == area.lower() else 1,
                                           r.get("deliveries_done", 0)))
            if not candidates:
                return jsonify({"ok": False, "error": "no online runners available"}), 409
            runner = candidates[0]
        else:
            runner = col("runners").find_one({"_id": rid})
            if not runner:
                return jsonify({"ok": False, "error": "runner not found"}), 404
        updates["runner_id"] = runner["_id"]
        updates["runner_name"] = runner["name"]
        if not d.get("status") and order["status"] == "confirmed":
            d["status"] = "assigned"

    if d.get("status"):
        new_status = d["status"]
        if new_status not in STATUS_FLOW:
            return jsonify({"ok": False, "error": "unknown status"}), 400
        if new_status not in STATUS_FLOW[order["status"]]:
            return jsonify({"ok": False, "error": f"cannot go {order['status']} → {new_status}"}), 409
        if new_status == "assigned" and not (updates.get("runner_id") or order.get("runner_id")):
            return jsonify({"ok": False, "error": "assign a runner first"}), 409
        updates["status"] = new_status
        updates["timeline"] = order.get("timeline", []) + [{"status": new_status, "at": _now()}]
        if new_status == "delivered" and order.get("runner_id"):
            r = col("runners").find_one({"_id": order["runner_id"]})
            if r:
                col("runners").update_one({"_id": r["_id"]},
                                          {"$set": {"deliveries_done": int(r.get("deliveries_done", 0)) + 1}})

    if not updates:
        return jsonify({"ok": False, "error": "nothing to update"}), 400
    updates["updated_at"] = _now()
    col("orders").update_one({"_id": oid}, {"$set": updates})
    return jsonify({"ok": True, "order": col("orders").find_one({"_id": oid})})


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
        "top_products": [{"name": n, "qty": q} for n, q in top_products],
        "recent_orders": orders_sorted[:8],
        "storage": storage_mode(),
    }})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
