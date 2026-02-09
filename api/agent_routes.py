# api/agent_routes.py
# YiThume Web Agent (single-file blueprint)
# - GET /chat
# - POST /api/app/agent/message
# - Mongo session memory in db.agent_sessions
# - Catalog lookup using db.catalog
# - CONFIRM gate: will not create order unless user types "CONFIRM"

import os
import re
import uuid
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, Response

# Reuse your existing app helpers for consistency
# This assumes agent_routes.py sits next to app.py inside /api
from api.app import (
    get_db,
    make_order_public_id,
    _now_dt,
    _now_iso,
    wa_order_text,
    find_available_driver,
    rule_based_fraud_score,
    cluster_key,
    log_zone_demand,
    recent_zone_demand_snapshot,
)

agent_bp = Blueprint("agent_bp", __name__)

# -----------------------------
# Config
# -----------------------------
SESSION_TTL_HOURS = int(os.environ.get("AGENT_SESSION_TTL_HOURS", "24"))
DEFAULT_DELIVERY_FEE = float(os.environ.get("AGENT_DEFAULT_DELIVERY_FEE", "20"))
MAX_SEARCH_RESULTS = int(os.environ.get("AGENT_MAX_SEARCH_RESULTS", "5"))

CONFIRM_RE = re.compile(r"^\s*CONFIRM\s*$", re.I)
RESET_RE   = re.compile(r"^\s*(RESET|START OVER|CLEAR)\s*$", re.I)
HELP_RE    = re.compile(r"^\s*(HELP|\?)\s*$", re.I)


# -----------------------------
# Session helpers (Mongo)
# -----------------------------
def _utcnow():
    return datetime.utcnow()

def sessions_col(db):
    col = db["agent_sessions"]
    try:
        col.create_index("expires_at", expireAfterSeconds=0)
        col.create_index([("user_id", 1), ("channel", 1)], unique=True)
    except Exception:
        pass
    return col

def load_session(db, user_id: str, channel: str):
    col = sessions_col(db)
    sess = col.find_one({"user_id": user_id, "channel": channel})
    if sess:
        return sess

    now = _utcnow()
    doc = {
        "user_id": user_id,
        "channel": channel,
        "active_flow": "ordering",
        "cart": [],  # [{sku, name, qty, price}]
        "awaiting_confirm": False,
        "last_quote": None,
        "draft_customer": {
            "name": None,
            "phone": None,
            "address_line1": None,
            "coords": {"lat": None, "lng": None},
            "zone": None,
            "collection_name": None,
        },
        "created_at": now,
        "updated_at": now,
        "expires_at": now + timedelta(hours=SESSION_TTL_HOURS),
    }

    try:
        col.insert_one(doc)
    except Exception:
        # race: if someone inserted at same time
        return col.find_one({"user_id": user_id, "channel": channel})

    return col.find_one({"user_id": user_id, "channel": channel})

def save_session(db, sess: dict):
    col = sessions_col(db)
    now = _utcnow()
    sess["updated_at"] = now
    sess["expires_at"] = now + timedelta(hours=SESSION_TTL_HOURS)

    sid = sess.get("_id")
    update_doc = {k: v for k, v in sess.items() if k != "_id"}

    if sid:
        col.update_one({"_id": sid}, {"$set": update_doc}, upsert=True)
    else:
        col.update_one(
            {"user_id": sess["user_id"], "channel": sess["channel"]},
            {"$set": update_doc},
            upsert=True
        )

def reset_session(db, user_id: str, channel: str):
    sessions_col(db).delete_one({"user_id": user_id, "channel": channel})


# -----------------------------
# Catalog helpers (db.catalog)
# -----------------------------
def search_catalog(db, q: str, limit: int = MAX_SEARCH_RESULTS):
    q = (q or "").strip()
    if not q:
        return []
    base = {"active": True, "name": {"$regex": re.escape(q), "$options": "i"}}
    items = list(db.catalog.find(base).limit(limit))
    out = []
    for it in items:
        out.append({
            "id": it.get("_internal_id"),
            "name": it.get("name"),
            "category": it.get("category"),
            "price": float(it.get("price") or 0),
            "sku": it.get("sku") or it.get("_internal_id"),
        })
    return out

def best_match(db, raw_name: str):
    items = search_catalog(db, raw_name, limit=1)
    return items[0] if items else None


# -----------------------------
# Cart parsing + totals
# -----------------------------
def is_confirm(text: str) -> bool:
    return bool(CONFIRM_RE.match(text or ""))

def is_reset(text: str) -> bool:
    return bool(RESET_RE.match(text or ""))

def is_help(text: str) -> bool:
    return bool(HELP_RE.match(text or ""))

def _parse_qty(text: str):
    """
    Extract qty from:
      - "Panado x2"
      - "qty 3 Panado"
      - "2 Panado"
    Returns (clean_name, qty)
    """
    t = (text or "").strip()
    if not t:
        return "", 0

    m = re.search(r"\bx\s*(\d+)\b", t, re.I)
    if m:
        qty = int(m.group(1))
        name = re.sub(r"\bx\s*\d+\b", "", t, flags=re.I).strip(" ,.-")
        return name, max(1, qty)

    m = re.search(r"\bqty\s*(\d+)\b", t, re.I)
    if m:
        qty = int(m.group(1))
        name = re.sub(r"\bqty\s*\d+\b", "", t, flags=re.I).strip(" ,.-")
        return name, max(1, qty)

    m = re.match(r"^\s*(\d+)\s+(.+)$", t)
    if m:
        qty = int(m.group(1))
        name = m.group(2).strip(" ,.-")
        return name, max(1, qty)

    return t, 1

def parse_items_from_message(text: str):
    t = (text or "").strip()
    if not t:
        return []
    t = re.sub(r"[\n;|]+", ",", t)
    parts = [p.strip() for p in t.split(",") if p.strip()]
    out = []
    for p in parts:
        name, qty = _parse_qty(p)
        name = re.sub(r"\b(i need|need|want|please|buy|get|order)\b", "", name, flags=re.I).strip()
        if name and qty > 0:
            out.append({"raw": name, "qty": qty})
    return out

def add_items_to_cart(db, sess: dict, raw_items: list):
    cart = sess.get("cart", []) or []
    added = []
    missing = []

    for ri in raw_items:
        raw = ri["raw"]
        qty = int(ri.get("qty") or 1)

        item = best_match(db, raw)
        if not item:
            missing.append(raw)
            continue

        sku = item["sku"]
        existing = next((c for c in cart if c.get("sku") == sku), None)
        if existing:
            existing["qty"] = int(existing.get("qty", 1)) + qty
        else:
            cart.append({
                "sku": sku,
                "name": item["name"],
                "qty": qty,
                "price": float(item["price"]),
            })

        added.append({"name": item["name"], "qty": qty})

    sess["cart"] = cart
    return added, missing

def cart_totals(sess: dict):
    cart = sess.get("cart", []) or []
    subtotal = 0.0
    for c in cart:
        subtotal += float(c.get("price") or 0) * int(c.get("qty") or 1)
    delivery_fee = DEFAULT_DELIVERY_FEE if subtotal > 0 else 0.0
    total = round(subtotal + delivery_fee, 2)
    return {"subtotal": round(subtotal, 2), "delivery_fee": round(delivery_fee, 2), "total": total}

def format_cart(sess: dict):
    cart = sess.get("cart", []) or []
    if not cart:
        return "Cart is empty."
    lines = []
    for c in cart:
        lines.append(f"- {c['name']} x{c['qty']} (R{int(c['price'])})")
    return "\n".join(lines)


# -----------------------------
# Agent brain (simple v1)
# -----------------------------
HELP_TEXT = (
    "YiThume Web Agent 🛒\n"
    "Send items like: Panado x2, Strepsils\n"
    "When ready type: CONFIRM\n"
    "Clear cart: RESET"
)

def run_agent(db, sess: dict, text: str):
    text = (text or "").strip()

    if is_help(text) or not text:
        sess["awaiting_confirm"] = False
        return (HELP_TEXT, sess, None)

    if is_reset(text):
        sess["cart"] = []
        sess["awaiting_confirm"] = False
        sess["last_quote"] = None
        return ("✅ Reset done. Send items to start a new order.", sess, None)

    if is_confirm(text):
        cart = sess.get("cart", []) or []
        if not cart:
            sess["awaiting_confirm"] = False
            return ("Your cart is empty. Send items first.", sess, None)

        totals = cart_totals(sess)
        sess["last_quote"] = totals
        sess["awaiting_confirm"] = False

        cust = sess.get("draft_customer") or {}
        order_payload = {
            "customer": {
                "phone": cust.get("phone") or "0000000000",
                "name": cust.get("name") or "Web Customer",
                "address": {
                    "line1": cust.get("address_line1") or "Web Chat",
                    "coords": cust.get("coords") or {"lat": None, "lng": None}
                }
            },
            "items": [
                {"name": c["name"], "qty": int(c["qty"]), "price": float(c["price"])}
                for c in cart
            ],
            "subtotal": float(totals["subtotal"]),
            "delivery_fee": float(totals["delivery_fee"]),
            "total": float(totals["total"]),
            "payment": {"method": "cod"},
            "route": {},
            "created_by": "web_agent",
            "meta": {
                "channel": "web",
                "collection_name": cust.get("collection_name") or "Web Chat",
                "zone": cust.get("zone")
            }
        }

        return ("✅ Confirmed. Creating your order now...", sess, order_payload)

    raw_items = parse_items_from_message(text)
    if not raw_items:
        return ("I didn’t catch the items. Example: 'Panado x2, Strepsils'. Type HELP.", sess, None)

    added, missing = add_items_to_cart(db, sess, raw_items)
    totals = cart_totals(sess)
    sess["last_quote"] = totals
    sess["awaiting_confirm"] = True

    lines = []
    if added:
        lines.append("✅ Added:")
        for a in added:
            lines.append(f"- {a['name']} x{a['qty']}")

    if missing:
        lines.append("⚠️ Not found in catalog:")
        for m in missing[:5]:
            lines.append(f"- {m}")
        lines.append("Try a different name or add it to catalog.")

    lines.append("")
    lines.append("🧾 Cart:")
    lines.append(format_cart(sess))
    lines.append("")
    lines.append(f"Subtotal: R{totals['subtotal']}")
    lines.append(f"Delivery: R{totals['delivery_fee']}")
    lines.append(f"Total: R{totals['total']}")
    lines.append("")
    lines.append("Reply CONFIRM to place the order.")

    return ("\n".join(lines), sess, None)


# -----------------------------
# Routes
# -----------------------------
@agent_bp.route("/api/app/agent/message", methods=["POST"])
def agent_message():
    body = request.get_json(silent=True) or {}
    channel = (body.get("channel") or "web").strip()
    user_id = (body.get("user_id") or "").strip() or f"web:{request.remote_addr or 'anon'}"
    text = (body.get("text") or "").strip()

    try:
        db = get_db()

        if is_reset(text):
            reset_session(db, user_id, channel)

        sess = load_session(db, user_id, channel)

        reply_text, sess2, order_payload = run_agent(db, sess, text)
        created = None

        if order_payload:
            internal_id = str(uuid.uuid4())
            public_id = make_order_public_id()

            order_doc = {
                "_internal_id": internal_id,
                "order_id": public_id,
                "created_at": _now_dt(),
                "created_at_iso": _now_iso(),

                "customer": order_payload.get("customer", {}),
                "items": order_payload.get("items", []),

                "subtotal": float(order_payload.get("subtotal", 0)),
                "delivery_fee": float(order_payload.get("delivery_fee", 0)),
                "total": float(order_payload.get("total", 0)),

                "payment": {
                    "method": (order_payload.get("payment") or {}).get("method", "card"),
                    "status": "pending",
                    "provider_ref": None,
                    "fake_checkout_url": f"https://pay.yithume.example/checkout/{internal_id}"
                },

                "status": "pending",
                "assigned_driver_id": None,
                "assigned_at": None,
                "delivered_at": None,

                "route": order_payload.get("route", {}),
                "created_by": order_payload.get("created_by", "web_agent"),
                "meta": order_payload.get("meta", {}),

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

            zone = (order_doc["meta"] or {}).get("zone")
            coords = (((order_doc.get("customer") or {}).get("address") or {}).get("coords") or {})
            candidate_driver = find_available_driver(db, zone, coords.get("lat"), coords.get("lng"))

            if not candidate_driver:
                log_zone_demand(db, zone, coords, (order_doc.get("customer") or {}).get("phone"))
                created = {"ok": False, "error": "no_driver_available"}
                reply_text = "⚠️ No driver available right now. Try again later."
            else:
                fs, ff = rule_based_fraud_score(db, order_doc)
                order_doc["fraud_score"], order_doc["fraud_flags"] = fs, ff
                if fs >= 0.75:
                    order_doc["status"] = "review_required"

                order_doc["cluster_key"] = cluster_key(order_doc)
                order_doc["driver_pay_pending"] = round(min(max(order_doc["delivery_fee"], 25), 45), 2)

                db.orders.insert_one(order_doc)

                sess2["cart"] = []
                sess2["last_quote"] = None
                sess2["awaiting_confirm"] = False

                created = {
                    "ok": True,
                    "order_db_id": internal_id,
                    "order_public_id": public_id,
                    "status": order_doc["status"],
                    "wa_message": wa_order_text(order_doc),
                    "zone_demand_snapshot": recent_zone_demand_snapshot(db),
                    "payment_portal_url": order_doc["payment"]["fake_checkout_url"]
                }

                reply_text = (
                    f"✅ Order created!\n"
                    f"Order ID: {public_id}\n"
                    f"Total: R{int(order_doc['total'])}\n"
                    f"Status: {order_doc['status'].replace('_',' ').title()}"
                )

        save_session(db, sess2)

        return jsonify({
            "ok": True,
            "reply_text": reply_text,
            "user_id": user_id,
            "channel": channel,
            "created": created
        }), 200

    except Exception as e:
        return jsonify({"ok": False, "error": "server_error", "details": str(e)}), 500


@agent_bp.route("/chat", methods=["GET"])
def chat_page():
    return Response(_CHAT_HTML, mimetype="text/html")


_CHAT_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>YiThume Web Agent</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; margin:0; background:#0b1220; color:#e9eefc; }
    .wrap { max-width: 900px; margin: 0 auto; padding: 18px; }
    .card { background:#101a33; border:1px solid rgba(255,255,255,.08); border-radius:14px; padding:14px; }
    .row { display:flex; gap:12px; }
    .col { flex:1; }
    .log { height: 62vh; overflow:auto; padding:12px; background:#0c142a; border-radius:12px; border:1px solid rgba(255,255,255,.08); }
    .msg { margin: 10px 0; }
    .me { text-align:right; }
    .bubble { display:inline-block; max-width: 82%; padding:10px 12px; border-radius:14px; line-height:1.3; white-space:pre-wrap; }
    .me .bubble { background:#2a67ff; color:white; }
    .bot .bubble { background:#1b2a55; }
    input, button { font-size:16px; }
    .bar { display:flex; gap:10px; margin-top:12px; }
    .bar input { flex:1; padding:12px; border-radius:12px; border:1px solid rgba(255,255,255,.15); background:#0c142a; color:#e9eefc; outline:none; }
    .bar button { padding:12px 14px; border-radius:12px; border:0; background:#2a67ff; color:white; font-weight:700; }
    .muted { color: rgba(233,238,252,.7); font-size: 13px; margin-top: 10px; }
    .pill { display:inline-block; padding:4px 10px; border:1px solid rgba(255,255,255,.15); border-radius:999px; font-size:12px; margin-right:8px; }
    code { color: rgba(233,238,252,.9); }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="row">
        <div class="col">
          <div><strong>YiThume Web Agent</strong> <span class="pill">v1</span></div>
          <div class="muted">Type items like: <b>Panado x2, Strepsils</b>. When ready type <b>CONFIRM</b>. Type <b>RESET</b> to clear.</div>
        </div>
        <div class="col" style="text-align:right">
          <div class="muted">User ID</div>
          <div><span id="uid" class="pill"></span></div>
        </div>
      </div>

      <div class="log" id="log"></div>

      <div class="bar">
        <input id="txt" placeholder="Message..." />
        <button id="send">Send</button>
      </div>
      <div class="muted">API: <code>/api/app/agent/message</code></div>
    </div>
  </div>

<script>
(function(){
  const log = document.getElementById('log');
  const txt = document.getElementById('txt');
  const send = document.getElementById('send');
  const uidEl = document.getElementById('uid');

  const uidKey = 'yithume_web_uid';
  let uid = localStorage.getItem(uidKey);
  if(!uid){
    uid = 'web:' + Math.random().toString(16).slice(2) + Date.now().toString(16);
    localStorage.setItem(uidKey, uid);
  }
  uidEl.textContent = uid;

  function addMsg(who, text){
    const div = document.createElement('div');
    div.className = 'msg ' + (who === 'me' ? 'me' : 'bot');
    const b = document.createElement('div');
    b.className = 'bubble';
    b.textContent = text;
    div.appendChild(b);
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  async function postMessage(text){
    addMsg('me', text);

    const res = await fetch('/api/app/agent/message', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ channel: 'web', user_id: uid, text })
    });

    const data = await res.json();
    if(!data.ok){
      addMsg('bot', '⚠️ Error: ' + (data.error || 'unknown'));
      return;
    }
    addMsg('bot', data.reply_text || '...');
  }

  send.addEventListener('click', ()=>{
    const v = txt.value.trim();
    if(!v) return;
    txt.value = '';
    postMessage(v);
  });

  txt.addEventListener('keydown', (e)=>{
    if(e.key === 'Enter'){
      send.click();
    }
  });

  addMsg('bot', "Hi 👋 Send items to start. Example: Panado x2, Strepsils.\\nWhen ready type CONFIRM.");
})();
</script>
</body>
</html>
"""