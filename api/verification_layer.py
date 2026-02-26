# verification_layer.py
# YiThume Validator / Verification Layer (non-crypto, commission-based)
# Vercel-safe: tick/cron driven, no background loops.

from __future__ import annotations

import os
import time
import json
import hmac
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple, List

from flask import request, jsonify

try:
    from pymongo import ASCENDING, DESCENDING, ReturnDocument
except Exception:
    ASCENDING = 1
    DESCENDING = -1
    ReturnDocument = None


# ------------------------------
# Config helpers
# ------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def _hmac_sha256_hex(key: str, msg: str) -> str:
    return hmac.new(key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


# ------------------------------
# Admin auth (minimal, append-only)
# ------------------------------

def _require_admin() -> Optional[Tuple[Any, int]]:
    admin_secret = os.getenv("ADMIN_SECRET") or ""
    if not admin_secret:
        return jsonify({"ok": False, "error": "ADMIN_SECRET not set"}), 500

    # support header or body
    got = request.headers.get("X-Admin-Secret")
    if not got:
        try:
            body = request.get_json(silent=True) or {}
            got = body.get("admin_secret")
        except Exception:
            got = None

    if not got or got != admin_secret:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    return None


# ------------------------------
# Mongo: collection names (namespaced)
# ------------------------------

COL_NODES = "validator_nodes"
COL_VALIDATIONS = "validator_validations"
COL_CONSENSUS = "validator_consensus"
COL_COMMISSIONS = "validator_commissions"
COL_JOBS = "validator_jobs"
COL_RATE = "validator_rate_limits"
COL_REPLAY = "validator_replay"


def ensure_validator_indexes(db):
    """
    Best-effort index creation. Safe to call multiple times.
    """
    try:
        db[COL_REPLAY].create_index([("created_at", ASCENDING)], expireAfterSeconds=3600)
    except Exception:
        pass

    try:
        db[COL_RATE].create_index([("created_at", ASCENDING)], expireAfterSeconds=180)
        db[COL_RATE].create_index([("node_id", ASCENDING), ("bucket", ASCENDING)], unique=True)
    except Exception:
        pass

    try:
        db[COL_VALIDATIONS].create_index([("round_id", ASCENDING), ("node_id", ASCENDING)], unique=True)
        db[COL_VALIDATIONS].create_index([("round_id", ASCENDING), ("validation_hash", ASCENDING)])
        db[COL_VALIDATIONS].create_index([("subject_type", ASCENDING), ("subject_id", ASCENDING), ("timestamp", DESCENDING)])
    except Exception:
        pass

    try:
        db[COL_CONSENSUS].create_index([("round_id", ASCENDING)], unique=True)
        db[COL_CONSENSUS].create_index([("subject_type", ASCENDING), ("subject_id", ASCENDING), ("status", ASCENDING)])
    except Exception:
        pass

    try:
        db[COL_COMMISSIONS].create_index([("transaction_id", ASCENDING), ("node_id", ASCENDING)], unique=True)
        db[COL_COMMISSIONS].create_index([("status", ASCENDING), ("created_at", DESCENDING)])
    except Exception:
        pass

    try:
        db[COL_JOBS].create_index([("status", ASCENDING), ("next_run_at", ASCENDING)])
        db[COL_JOBS].create_index([("job_id", ASCENDING)], unique=True)
        db[COL_JOBS].create_index([("round_id", ASCENDING), ("status", ASCENDING)])
    except Exception:
        pass

    try:
        db[COL_NODES].create_index([("node_id", ASCENDING)], unique=True)
        db[COL_NODES].create_index([("status", ASCENDING), ("reputation_score", DESCENDING)])
    except Exception:
        pass


# ------------------------------
# Node secrets / auth
# ------------------------------

def _pepper() -> str:
    p = os.getenv("VALIDATOR_PEPPER") or ""
    return p

def _hash_node_secret(node_secret_plain: str) -> str:
    # No plaintext storage: store HMAC(pepper, node_secret_plain)
    p = _pepper()
    if not p:
        # still hash so we don't store plaintext, but verification will fail until set
        p = "MISSING_VALIDATOR_PEPPER"
    return _hmac_sha256_hex(p, node_secret_plain)

def _verify_node_secret(db, node_id: str, node_secret_plain: str) -> bool:
    try:
        node = db[COL_NODES].find_one({"node_id": node_id, "status": {"$ne": "disabled"}})
        if not node:
            return False
        expected = node.get("secret_hash")
        got = _hash_node_secret(node_secret_plain)
        return bool(expected) and hmac.compare_digest(expected, got)
    except Exception:
        return False

def _rate_limit_ok(db, node_id: str) -> Tuple[bool, Optional[int]]:
    limit = _env_int("VALIDATOR_RATE_LIMIT_PER_MIN", 120)
    now = _now()
    bucket = now.strftime("%Y%m%d%H%M")  # per-minute bucket
    key = {"node_id": node_id, "bucket": bucket}
    try:
        doc = db[COL_RATE].find_one_and_update(
            key,
            {"$inc": {"count": 1}, "$setOnInsert": {"created_at": now}},
            upsert=True,
            return_document=ReturnDocument.AFTER if ReturnDocument else True,
        )
        count = (doc or {}).get("count", 1)
        if count > limit:
            return False, limit
        return True, limit
    except Exception:
        return True, limit

def _replay_ok(db, node_id: str, nonce: str) -> bool:
    try:
        db[COL_REPLAY].insert_one({"node_id": node_id, "nonce": nonce, "created_at": _now()})
        return True
    except Exception:
        # duplicate nonce insert fails if we create unique index; we didn't to keep TTL simple,
        # so use a manual check:
        try:
            exists = db[COL_REPLAY].find_one({"node_id": node_id, "nonce": nonce})
            return not bool(exists)
        except Exception:
            return False

def _require_node_signed(db) -> Tuple[Optional[str], Optional[Tuple[Any, int]]]:
    node_id = request.headers.get("X-Node-Id") or ""
    ts = request.headers.get("X-Node-TS") or ""
    nonce = request.headers.get("X-Node-Nonce") or ""
    sig = request.headers.get("X-Node-Signature") or ""

    if not node_id or not ts or not nonce or not sig:
        return None, (jsonify({"ok": False, "error": "Missing node auth headers"}), 401)

    # basic time window
    try:
        ts_i = int(ts)
        if abs(int(time.time()) - ts_i) > 300:
            return None, (jsonify({"ok": False, "error": "Stale timestamp"}), 401)
    except Exception:
        return None, (jsonify({"ok": False, "error": "Bad timestamp"}), 401)

    # replay check
    if not _replay_ok(db, node_id, nonce):
        return None, (jsonify({"ok": False, "error": "Replay detected"}), 401)

    ok, _ = _rate_limit_ok(db, node_id)
    if not ok:
        return None, (jsonify({"ok": False, "error": "Rate limit"}), 429)

    # We cannot reconstruct node_secret_plain server-side (not stored),
    # so node must supply it in body? NO — we verify signature by re-hashing stored secret?
    # Instead: store secret_hash = HMAC(pepper, node_secret_plain) and use it as signing key.
    # Node signs using node_secret_plain; server uses secret_hash as derived key.
    node = db[COL_NODES].find_one({"node_id": node_id, "status": {"$ne": "disabled"}})
    if not node:
        return None, (jsonify({"ok": False, "error": "Unknown node"}), 401)

    signing_key = node.get("secret_hash") or ""
    if not signing_key:
        return None, (jsonify({"ok": False, "error": "Node not initialized"}), 401)

    raw = request.get_data(as_text=True) or ""
    msg = f"{ts}.{nonce}.{raw}"
    expected = hmac.new(signing_key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None, (jsonify({"ok": False, "error": "Bad signature"}), 401)

    # update last_seen
    try:
        db[COL_NODES].update_one({"node_id": node_id}, {"$set": {"last_seen_at": _now()}})
    except Exception:
        pass

    return node_id, None


# ------------------------------
# YiThume schema compatibility
# ------------------------------

def _pick_collection(db, candidates: List[str]) -> Optional[str]:
    try:
        existing = set(db.list_collection_names())
    except Exception:
        existing = set()
    for name in candidates:
        if name in existing:
            return name
    # fallback: first candidate
    return candidates[0] if candidates else None

def _stores_coll(db) -> str:
    return _pick_collection(db, ["stores", "merchant_stores", "yithume_stores"]) or "stores"

def _orders_coll(db) -> str:
    return _pick_collection(db, ["orders", "transactions", "sales_orders", "yithume_orders"]) or "orders"

def _products_coll(db) -> str:
    return _pick_collection(db, ["products", "store_products", "inventory", "yithume_products"]) or "products"

def _get_store(db, store_id: str) -> Optional[Dict[str, Any]]:
    sc = _stores_coll(db)
    return db[sc].find_one({"$or": [{"store_id": store_id}, {"_id": store_id}, {"code": store_id}]})

def _get_order(db, order_id: str) -> Optional[Dict[str, Any]]:
    oc = _orders_coll(db)
    return db[oc].find_one({"$or": [{"order_id": order_id}, {"transaction_id": order_id}, {"_id": order_id}]})

def _extract_order_total(order: Dict[str, Any]) -> Optional[float]:
    for k in ("total", "total_amount", "amount_total", "grand_total", "order_total"):
        v = order.get(k)
        if v is not None:
            try:
                return float(v)
            except Exception:
                pass
    return None

def _extract_items(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = order.get("items") or order.get("line_items") or order.get("cart_items") or []
    if isinstance(items, dict):
        items = list(items.values())
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        out.append(it)
    return out

def _recompute_total(items: List[Dict[str, Any]]) -> float:
    total = 0.0
    for it in items:
        qty = it.get("qty", it.get("quantity", 1))
        price = it.get("price", it.get("unit_price", it.get("amount", 0)))
        try:
            qty_f = float(qty)
        except Exception:
            qty_f = 1.0
        try:
            price_f = float(price)
        except Exception:
            price_f = 0.0
        total += qty_f * price_f
    return round(total, 2)


# ------------------------------
# Deterministic validations (server snapshot)
# ------------------------------

def _build_store_snapshot(db, store_id: str) -> Dict[str, Any]:
    store = _get_store(db, store_id) or {}
    pc = _products_coll(db)

    # products by store linkage: try common fields
    products = []
    try:
        cursor = db[pc].find({"$or": [{"store_id": store_id}, {"store_code": store_id}, {"merchant_store_id": store_id}]})
        for p in cursor.limit(500):
            # keep stable fields only
            products.append({
                "sku": p.get("sku") or p.get("SKU") or p.get("barcode") or str(p.get("_id")),
                "name": p.get("name") or p.get("title"),
                "price": p.get("price"),
                "stock": p.get("stock") if "stock" in p else p.get("qty_available"),
                "active": p.get("active", True),
            })
    except Exception:
        pass

    snap = {
        "subject_type": "store",
        "store_id": store_id,
        "reachable": True,  # network ping is optional; we treat DB existence as reachable
        "store_present": bool(store),
        "store": {
            "name": store.get("name") or store.get("store_name"),
            "status": store.get("status") or store.get("active"),
            "flagged": bool(store.get("flagged") or store.get("is_flagged") or False),
        },
        "products": products,
        "generated_at": _now().isoformat(),
    }
    return snap

def _build_tx_snapshot(db, tx_id: str) -> Dict[str, Any]:
    order = _get_order(db, tx_id) or {}
    items = _extract_items(order)
    stored_total = _extract_order_total(order)
    recomputed = _recompute_total(items)

    payment_id = order.get("payment_id") or order.get("payment_intent_id") or order.get("provider_payment_id")
    payment_status = order.get("payment_status") or order.get("status_payment")
    demo_mode = bool(order.get("demo_mode") or order.get("is_demo") or False)

    snap = {
        "subject_type": "transaction",
        "transaction_id": tx_id,
        "order_present": bool(order),
        "stored_total": stored_total,
        "recomputed_total": recomputed,
        "items": [
            {
                "sku": it.get("sku") or it.get("SKU") or it.get("barcode"),
                "qty": it.get("qty", it.get("quantity")),
                "price": it.get("price", it.get("unit_price")),
            }
            for it in items
        ],
        "payment": {
            "payment_id": payment_id,
            "payment_status": payment_status,
            "demo_mode": demo_mode,
        },
        "generated_at": _now().isoformat(),
    }
    return snap


# ------------------------------
# Job / consensus logic
# ------------------------------

def _new_round_id(subject_type: str, subject_id: str, round_n: int) -> str:
    return f"{subject_type}:{subject_id}:r{round_n}"

def _queue_job(db, job_type: str, subject_type: str, subject_id: str, round_n: int = 1) -> Dict[str, Any]:
    rid = _new_round_id(subject_type, subject_id, round_n)
    job_id = _sha256_hex(f"{job_type}|{rid}|{secrets.token_hex(8)}")
    job = {
        "job_id": job_id,
        "job_type": job_type,  # store_check / tx_check
        "subject_type": subject_type,
        "subject_id": subject_id,
        "round_id": rid,
        "round_n": round_n,
        "status": "queued",
        "assigned_to": None,
        "created_at": _now(),
        "next_run_at": _now(),
        "attempts": 0,
    }
    try:
        db[COL_JOBS].insert_one(job)
    except Exception:
        # if duplicate, ignore
        pass

    # ensure consensus doc exists
    required = _env_int("VALIDATOR_REQUIRED_VALIDATORS", 3)
    try:
        db[COL_CONSENSUS].update_one(
            {"round_id": rid},
            {"$setOnInsert": {
                "round_id": rid,
                "round_n": round_n,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "required_validators": required,
                "status": "collecting",
                "winning_hash": None,
                "agree": [],
                "disagree": [],
                "created_at": _now(),
                "updated_at": _now(),
            }},
            upsert=True
        )
    except Exception:
        pass

    return job


def _finalize_consensus_if_ready(db, round_id: str) -> Optional[Dict[str, Any]]:
    cons = db[COL_CONSENSUS].find_one({"round_id": round_id})
    if not cons or cons.get("status") not in ("collecting", "disputed"):
        return None

    required = int(cons.get("required_validators") or _env_int("VALIDATOR_REQUIRED_VALIDATORS", 3))
    vals = list(db[COL_VALIDATIONS].find({"round_id": round_id}).limit(50))
    if len(vals) < required:
        return None

    # Count hashes
    counts: Dict[str, List[str]] = {}
    for v in vals:
        hsh = v.get("validation_hash") or ""
        nid = v.get("node_id") or ""
        if not hsh or not nid:
            continue
        counts.setdefault(hsh, []).append(nid)

    if not counts:
        return None

    # winning = max count
    winning_hash, winners = max(counts.items(), key=lambda kv: len(kv[1]))
    if len(winners) >= 2:
        # 2/3 agreement check
        if len(winners) * 3 >= required * 2:
            db[COL_CONSENSUS].update_one(
                {"round_id": round_id},
                {"$set": {
                    "status": "finalized",
                    "winning_hash": winning_hash,
                    "agree": winners,
                    "disagree": [n for h, ns in counts.items() if h != winning_hash for n in ns],
                    "updated_at": _now(),
                }}
            )
            return db[COL_CONSENSUS].find_one({"round_id": round_id})

    # disputed path
    db[COL_CONSENSUS].update_one(
        {"round_id": round_id},
        {"$set": {
            "status": "disputed",
            "winning_hash": None,
            "agree": [],
            "disagree": [n for ns in counts.values() for n in ns],
            "updated_at": _now(),
        }}
    )
    return db[COL_CONSENSUS].find_one({"round_id": round_id})


def _create_escalation_round(db, cons: Dict[str, Any]) -> None:
    if not cons or cons.get("status") != "disputed":
        return
    subject_type = cons.get("subject_type")
    subject_id = cons.get("subject_id")
    round_n = int(cons.get("round_n") or 1) + 1
    required = int(cons.get("required_validators") or 3) + 2  # add more validators for escalation
    rid2 = _new_round_id(subject_type, subject_id, round_n)

    # create consensus doc for new round with increased requirement
    try:
        db[COL_CONSENSUS].update_one(
            {"round_id": rid2},
            {"$setOnInsert": {
                "round_id": rid2,
                "round_n": round_n,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "required_validators": required,
                "status": "collecting",
                "winning_hash": None,
                "agree": [],
                "disagree": [],
                "created_at": _now(),
                "updated_at": _now(),
            }},
            upsert=True
        )
        # queue jobs for new round
        job_type = "tx_check" if subject_type == "transaction" else "store_check"
        _queue_job(db, job_type, subject_type, subject_id, round_n=round_n)
        # mark old round escalated
        db[COL_CONSENSUS].update_one({"round_id": cons["round_id"]}, {"$set": {"status": "escalated", "updated_at": _now()}})
    except Exception:
        pass


# ------------------------------
# Commission logic
# ------------------------------

def _compute_commission_pool(total: float) -> float:
    platform_fee_rate = _env_float("VALIDATOR_PLATFORM_FEE_RATE", 0.05)
    pool_rate = _env_float("VALIDATOR_POOL_RATE", 0.20)
    min_pct = _env_float("VALIDATOR_POOL_MIN_PCT_TOTAL", 0.0025)
    max_pct = _env_float("VALIDATOR_POOL_MAX_PCT_TOTAL", 0.01)

    fee = total * platform_fee_rate
    pool = fee * pool_rate
    # clamp
    pool = max(pool, total * min_pct)
    pool = min(pool, total * max_pct)
    return round(pool, 2)

def _create_commissions_for_round(db, cons: Dict[str, Any], tx_snapshot: Dict[str, Any]) -> None:
    if not cons or cons.get("status") != "finalized":
        return
    tx_id = cons.get("subject_id")
    winners = cons.get("agree") or []
    if not winners:
        return

    total = tx_snapshot.get("stored_total")
    if total is None:
        total = tx_snapshot.get("recomputed_total") or 0.0
    try:
        total_f = float(total)
    except Exception:
        total_f = 0.0

    pool = _compute_commission_pool(total_f)
    per = round(pool / max(1, len(winners)), 2)

    for nid in winners:
        doc = {
            "transaction_id": tx_id,
            "node_id": nid,
            "amount": per,
            "status": "pending",
            "created_at": _now(),
            "updated_at": _now(),
        }
        try:
            db[COL_COMMISSIONS].insert_one(doc)
        except Exception:
            pass
        try:
            db[COL_NODES].update_one({"node_id": nid}, {"$inc": {"total_earnings": per}})
        except Exception:
            pass


# ------------------------------
# Routes
# ------------------------------

def register_validator_routes(app, get_db):
    # -------- Admin endpoints --------

    @app.post("/v1/validator/nodes/register")
    def validator_register_node():
        db = get_db()
        err = _require_admin()
        if err:
            return err

        body = request.get_json(silent=True) or {}
        owner = body.get("owner") or "admin"
        node_id = body.get("node_id") or f"node_{secrets.token_hex(6)}"

        # secret returned once; store only secret_hash
        node_secret_plain = secrets.token_urlsafe(32)
        secret_hash = _hash_node_secret(node_secret_plain)

        node = {
            "node_id": node_id,
            "owner": owner,
            "status": "active",
            "reputation_score": 0.0,
            "total_validations": 0,
            "total_earnings": 0.0,
            "secret_hash": secret_hash,
            "created_at": _now(),
            "last_seen_at": None,
        }
        try:
            db[COL_NODES].insert_one(node)
        except Exception:
            # if already exists, refuse to overwrite secret
            return jsonify({"ok": False, "error": "node_id already exists"}), 409

        return jsonify({"ok": True, "node_id": node_id, "node_secret": node_secret_plain, "node_signing_key": secret_hash})

    @app.post("/v1/validator/tick")
    def validator_tick():
        db = get_db()
        err = _require_admin()
        if err:
            return err

        body = request.get_json(silent=True) or {}
        max_store = int(body.get("max_store") or 5)
        max_tx = int(body.get("max_tx") or 5)

        enq = {"stores": 0, "transactions": 0}

        # enqueue store checks
        sc = _stores_coll(db)
        try:
            for s in db[sc].find({}).sort([("_id", DESCENDING)]).limit(max_store):
                sid = s.get("store_id") or s.get("code") or str(s.get("_id"))
                if sid:
                    _queue_job(db, "store_check", "store", sid, round_n=1)
                    enq["stores"] += 1
        except Exception:
            pass

        # enqueue tx checks (recent)
        oc = _orders_coll(db)
        try:
            for o in db[oc].find({}).sort([("_id", DESCENDING)]).limit(max_tx):
                tid = o.get("order_id") or o.get("transaction_id") or str(o.get("_id"))
                if tid:
                    _queue_job(db, "tx_check", "transaction", tid, round_n=1)
                    enq["transactions"] += 1
        except Exception:
            pass

        return jsonify({"ok": True, "enqueued": enq})

    @app.get("/v1/validator/admin/metrics")
    def validator_admin_metrics():
        db = get_db()
        err = _require_admin()
        if err:
            return err

        nodes_active = db[COL_NODES].count_documents({"status": "active"})
        pending_commissions = db[COL_COMMISSIONS].count_documents({"status": "pending"})
        disputed = db[COL_CONSENSUS].count_documents({"status": "disputed"})
        finalized = db[COL_CONSENSUS].count_documents({"status": "finalized"})
        queued_jobs = db[COL_JOBS].count_documents({"status": "queued"})
        assigned_jobs = db[COL_JOBS].count_documents({"status": "assigned"})

        # throughput (last hour)
        one_hour_ago = _now() - timedelta(hours=1)
        vph = db[COL_VALIDATIONS].count_documents({"timestamp": {"$gte": one_hour_ago}})

        return jsonify({
            "ok": True,
            "nodes_active": nodes_active,
            "pending_commissions": pending_commissions,
            "consensus": {"finalized": finalized, "disputed": disputed},
            "jobs": {"queued": queued_jobs, "assigned": assigned_jobs},
            "validations_last_hour": vph,
        })

    @app.get("/v1/validator/admin/commissions/pending")
    def validator_admin_pending_commissions():
        db = get_db()
        err = _require_admin()
        if err:
            return err
        items = list(db[COL_COMMISSIONS].find({"status": "pending"}).sort([("created_at", DESCENDING)]).limit(200))
        for it in items:
            it["_id"] = str(it.get("_id"))
            it["created_at"] = it.get("created_at").isoformat() if it.get("created_at") else None
            it["updated_at"] = it.get("updated_at").isoformat() if it.get("updated_at") else None
        return jsonify({"ok": True, "items": items})

    @app.get("/v1/validator/admin/disputes")
    def validator_admin_disputes():
        db = get_db()
        err = _require_admin()
        if err:
            return err
        items = list(db[COL_CONSENSUS].find({"status": {"$in": ["disputed", "escalated"]}}).sort([("updated_at", DESCENDING)]).limit(200))
        for it in items:
            it["_id"] = str(it.get("_id"))
            it["created_at"] = it.get("created_at").isoformat() if it.get("created_at") else None
            it["updated_at"] = it.get("updated_at").isoformat() if it.get("updated_at") else None
        return jsonify({"ok": True, "items": items})

    @app.get("/v1/validator/admin/nodes")
    def validator_admin_nodes():
        db = get_db()
        err = _require_admin()
        if err:
            return err
        items = list(db[COL_NODES].find({}).sort([("created_at", DESCENDING)]).limit(200))
        for it in items:
            it["_id"] = str(it.get("_id"))
            it.pop("secret_hash", None)
            it["created_at"] = it.get("created_at").isoformat() if it.get("created_at") else None
            it["last_seen_at"] = it.get("last_seen_at").isoformat() if it.get("last_seen_at") else None
        return jsonify({"ok": True, "items": items})

    # -------- Node endpoints (signed) --------

    @app.post("/v1/validator/jobs/poll")
    def validator_jobs_poll():
        db = get_db()
        node_id, err = _require_node_signed(db)
        if err:
            return err

        # assign oldest queued job that is ready
        now = _now()
        job = db[COL_JOBS].find_one_and_update(
            {"status": "queued", "next_run_at": {"$lte": now}},
            {"$set": {"status": "assigned", "assigned_to": node_id, "assigned_at": now}, "$inc": {"attempts": 1}},
            sort=[("next_run_at", ASCENDING)],
            return_document=ReturnDocument.AFTER if ReturnDocument else True,
        )

        if not job:
            return jsonify({"ok": True, "job": None})

        subject_type = job.get("subject_type")
        subject_id = job.get("subject_id")

        if subject_type == "store":
            snapshot = _build_store_snapshot(db, subject_id)
        else:
            snapshot = _build_tx_snapshot(db, subject_id)

        snapshot_canon = _canonical_json(snapshot)
        snapshot_hash = _sha256_hex(snapshot_canon)

        return jsonify({
            "ok": True,
            "job": {
                "job_id": job.get("job_id"),
                "job_type": job.get("job_type"),
                "round_id": job.get("round_id"),
                "subject_type": subject_type,
                "subject_id": subject_id,
                "snapshot": snapshot,
                "snapshot_hash": snapshot_hash,
            }
        })

    @app.post("/v1/validator/validations/submit")
    def validator_submit_validation():
        db = get_db()
        node_id, err = _require_node_signed(db)
        if err:
            return err

        body = request.get_json(silent=True) or {}
        round_id = body.get("round_id") or ""
        subject_type = body.get("subject_type") or ""
        subject_id = body.get("subject_id") or ""
        result = body.get("result") or {}
        snapshot_hash = body.get("snapshot_hash") or None

        if not round_id or not subject_type or not subject_id:
            return jsonify({"ok": False, "error": "Missing round/subject fields"}), 400

        # deterministic hash of result
        result_canon = _canonical_json(result)
        validation_hash = _sha256_hex(result_canon)

        doc = {
            "round_id": round_id,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "node_id": node_id,
            "validation_hash": validation_hash,
            "snapshot_hash": snapshot_hash,
            "result": result,
            "timestamp": _now(),
        }

        try:
            db[COL_VALIDATIONS].insert_one(doc)
        except Exception:
            return jsonify({"ok": False, "error": "Duplicate submission"}), 409

        # update node stats
        try:
            db[COL_NODES].update_one({"node_id": node_id}, {"$inc": {"total_validations": 1}})
        except Exception:
            pass

        # attempt consensus finalization
        cons = _finalize_consensus_if_ready(db, round_id)
        if cons and cons.get("status") == "finalized":
            # mark jobs done
            try:
                db[COL_JOBS].update_many({"round_id": round_id}, {"$set": {"status": "done", "done_at": _now()}})
            except Exception:
                pass

            # if transaction, create commissions
            if cons.get("subject_type") == "transaction":
                tx_snap = _build_tx_snapshot(db, cons.get("subject_id"))
                _create_commissions_for_round(db, cons, tx_snap)

        elif cons and cons.get("status") == "disputed":
            _create_escalation_round(db, cons)

        return jsonify({"ok": True, "validation_hash": validation_hash, "consensus": cons})

    @app.post("/v1/validator/node/summary")
    def validator_node_summary():
        db = get_db()
        node_id, err = _require_node_signed(db)
        if err:
            return err

        node = db[COL_NODES].find_one({"node_id": node_id}) or {}
        pending = db[COL_COMMISSIONS].count_documents({"node_id": node_id, "status": "pending"})
        total_earnings = float(node.get("total_earnings") or 0.0)
        total_validations = int(node.get("total_validations") or 0)
        rep = float(node.get("reputation_score") or 0.0)

        return jsonify({
            "ok": True,
            "node_id": node_id,
            "total_earnings": total_earnings,
            "total_validations": total_validations,
            "pending_commissions": pending,
            "reputation_score": rep,
        })
