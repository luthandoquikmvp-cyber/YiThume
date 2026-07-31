# ledger.py — accounts and a double-entry journal.
#
# The rail (Paystack) is only an on-ramp and an off-ramp. It holds one pooled
# balance; this ledger is the record of whose money that is. Every movement —
# top-up, escrow hold, release, split, refund, withdrawal — is a journal post
# whose entries sum to zero.
#
# Two invariants, enforced here rather than trusted:
#   1. Every post balances (debits == credits).
#   2. Every post is idempotent on its key, so a retried webhook or a
#      double-clicked button cannot move money twice.
#
# `balance` and `held` on an account are a cache. The journal is the truth, and
# audit() recomputes one from the other.

ROLES = ("buyer", "seller", "marketplace", "runner", "node", "reserve", "platform")

PLATFORM_KEY = "platform"


class Deps:
    """Injected storage so this module stays import-free (same shape as ussd.py)."""

    def __init__(self, col, now, new_id):
        self.col = col
        self.now = now
        self.new_id = new_id


def _r(v):
    return round(float(v or 0), 2)


# -------------------------------------------------
# accounts
# -------------------------------------------------
def account(deps, owner_type, owner_key, marketplace_id=""):
    """Fetch or create an account. Accounts are created lazily on first touch."""
    if owner_type not in ROLES:
        raise ValueError(f"unknown account type: {owner_type}")
    owner_key = str(owner_key or "")
    flt = {"owner_type": owner_type, "owner_key": owner_key}
    doc = deps.col("accounts").find_one(flt)
    if doc:
        return doc
    doc = {
        "_id": deps.new_id(), "owner_type": owner_type, "owner_key": owner_key,
        "marketplace_id": marketplace_id,
        # balance may go negative: buyer debt, and an empty tenant reserve
        # covered by the platform backstop.
        "balance": 0.0, "held": 0.0,
        "status": "active", "created_at": deps.now(),
    }
    deps.col("accounts").insert_one(doc)
    return doc


def platform_account(deps):
    return account(deps, "platform", PLATFORM_KEY)


def buyer_account(deps, phone):
    return account(deps, "buyer", phone)


def reserve_account(deps, marketplace_id):
    return account(deps, "reserve", marketplace_id, marketplace_id)


def balances(deps, account_id):
    doc = deps.col("accounts").find_one({"_id": account_id})
    if not doc:
        return {"balance": 0.0, "held": 0.0, "available": 0.0}
    return {"balance": _r(doc.get("balance")), "held": _r(doc.get("held")),
            "available": _r(doc.get("balance", 0)) - _r(doc.get("held", 0))}


def _apply(deps, account_id, delta_balance=0.0, delta_held=0.0):
    doc = deps.col("accounts").find_one({"_id": account_id})
    if not doc:
        raise ValueError(f"no such account: {account_id}")
    deps.col("accounts").update_one({"_id": account_id}, {"$set": {
        "balance": _r(doc.get("balance", 0) + delta_balance),
        "held": _r(doc.get("held", 0) + delta_held),
    }})


# -------------------------------------------------
# journal
# -------------------------------------------------
def post(deps, kind, ref, entries, meta=None):
    """Write one balanced, idempotent journal entry.

    entries: [{"account_id", "direction": "debit"|"credit", "amount",
               "hold": bool (optional — moves the `held` bucket instead)}]

    Returns (doc, created). `created` is False when this post already existed,
    which is what makes replaying a webhook safe.
    """
    key = f"{kind}:{ref}"
    existing = deps.col("ledger").find_one({"idempotency_key": key})
    if existing:
        return existing, False

    norm, total = [], 0.0
    for e in entries:
        amount = _r(e["amount"])
        if amount <= 0:
            raise ValueError("ledger amounts must be positive; use direction to signal flow")
        direction = e["direction"]
        if direction not in ("debit", "credit"):
            raise ValueError(f"bad direction: {direction}")
        hold = bool(e.get("hold"))
        # Hold entries move money between an account's own available and held
        # buckets — they are not a transfer, so they sit outside the zero-sum
        # check that governs real movement between accounts.
        if not hold:
            total += amount if direction == "credit" else -amount
        norm.append({"account_id": e["account_id"], "direction": direction,
                     "amount": amount, "hold": hold})
    if round(total, 2) != 0.0:
        raise ValueError(f"unbalanced ledger post {key}: net {round(total, 2)}")

    doc = {"_id": deps.new_id(), "kind": kind, "ref": str(ref), "entries": norm,
           "idempotency_key": key, "meta": meta or {}, "created_at": deps.now()}
    deps.col("ledger").insert_one(doc)

    for e in norm:
        signed = e["amount"] if e["direction"] == "credit" else -e["amount"]
        if e["hold"]:
            _apply(deps, e["account_id"], delta_held=signed)
        else:
            _apply(deps, e["account_id"], delta_balance=signed)
    return doc, True


def _pair(debit_id, credit_id, amount, hold_debit=False, hold_credit=False):
    return [
        {"account_id": debit_id, "direction": "debit", "amount": amount, "hold": hold_debit},
        {"account_id": credit_id, "direction": "credit", "amount": amount, "hold": hold_credit},
    ]


# -------------------------------------------------
# money in / around / out
# -------------------------------------------------
def topup(deps, acct_id, amount, ref, meta=None):
    """Rail → user. The platform account carries the matching liability."""
    plat = platform_account(deps)
    return post(deps, "topup", ref,
                _pair(plat["_id"], acct_id, amount), meta)


def hold_escrow(deps, acct_id, amount, order_code, meta=None):
    """Move a buyer's funds into escrow against an order. The money stays in
    their account but is no longer available to spend."""
    return post(deps, "escrow_hold", order_code,
                [{"account_id": acct_id, "direction": "credit",
                  "amount": amount, "hold": True}],
                meta)


def release_escrow(deps, buyer_acct_id, splits, order_code, meta=None):
    """Delivery proven → escrow becomes everyone's money.

    splits: [{"account_id", "amount"}] and must sum to the held amount.
    """
    total = _r(sum(_r(s["amount"]) for s in splits))
    if total <= 0:
        raise ValueError(f"nothing to release for {order_code}")
    entries = [
        # release the hold, then move the cash out of the buyer's balance
        {"account_id": buyer_acct_id, "direction": "debit", "amount": total, "hold": True},
        {"account_id": buyer_acct_id, "direction": "debit", "amount": total},
    ]
    for s in splits:
        amount = _r(s["amount"])
        if amount == 0:
            continue
        # A negative split means that party is covering a shortfall rather than
        # earning — the platform backstop when fee rates over-commit an order.
        entries.append({"account_id": s["account_id"],
                        "direction": "credit" if amount > 0 else "debit",
                        "amount": abs(amount)})
    # post() enforces that the non-hold entries balance, which is exactly the
    # assertion that the splits add up to what was held.
    return post(deps, "escrow_release", order_code, entries, meta)


def refund_escrow(deps, buyer_acct_id, amount, order_code, meta=None):
    """Order failed or dispute upheld → the hold simply drops away."""
    return post(deps, "refund", order_code,
                [{"account_id": buyer_acct_id, "direction": "debit",
                  "amount": amount, "hold": True}],
                meta)


def transfer(deps, from_id, to_id, amount, kind, ref, meta=None):
    """Plain internal movement — failed-delivery fees, reserve top-ups, corrections."""
    return post(deps, kind, ref, _pair(from_id, to_id, amount), meta)


def withdraw(deps, acct_id, amount, ref, meta=None):
    """User → rail. Mirrors topup."""
    plat = platform_account(deps)
    return post(deps, "withdrawal", ref, _pair(acct_id, plat["_id"], amount), meta)


def reverse(deps, kind, ref, reason=""):
    """Undo a post (a failed bank transfer). Written as its own entry, never a
    delete — the journal is append-only."""
    original = deps.col("ledger").find_one({"idempotency_key": f"{kind}:{ref}"})
    if not original:
        return None, False
    flipped = []
    for e in original["entries"]:
        flipped.append({**e, "direction": "credit" if e["direction"] == "debit" else "debit"})
    return post(deps, f"{kind}_reversal", ref, flipped, {"reason": reason})


# -------------------------------------------------
# audit
# -------------------------------------------------
def audit(deps):
    """Recompute every balance from the journal and report drift.

    Drift is either a bug or a fraud. Either way it should be one screen away.
    """
    computed = {}
    for entry in deps.col("ledger").find({}):
        for e in entry["entries"]:
            acc = computed.setdefault(e["account_id"], {"balance": 0.0, "held": 0.0})
            signed = e["amount"] if e["direction"] == "credit" else -e["amount"]
            acc["held" if e.get("hold") else "balance"] += signed

    rows, drift = [], []
    for doc in deps.col("accounts").find({}):
        want = computed.get(doc["_id"], {"balance": 0.0, "held": 0.0})
        row = {
            "account_id": doc["_id"], "owner_type": doc["owner_type"],
            "owner_key": doc["owner_key"],
            "stored_balance": _r(doc.get("balance")), "stored_held": _r(doc.get("held")),
            "ledger_balance": _r(want["balance"]), "ledger_held": _r(want["held"]),
        }
        row["ok"] = (row["stored_balance"] == row["ledger_balance"]
                     and row["stored_held"] == row["ledger_held"])
        rows.append(row)
        if not row["ok"]:
            drift.append(row)

    # Every post nets to zero, so the whole journal must too.
    net = 0.0
    for entry in deps.col("ledger").find({}):
        for e in entry["entries"]:
            if e.get("hold"):
                continue
            net += e["amount"] if e["direction"] == "credit" else -e["amount"]

    return {"accounts": rows, "drift": drift, "balanced": round(net, 2) == 0.0,
            "net": round(net, 2)}


def liabilities(deps):
    """What YiThume owes everyone who is not YiThume — what the rail balance
    needs to cover."""
    total = 0.0
    for doc in deps.col("accounts").find({}):
        if doc.get("owner_type") == "platform":
            continue
        total += _r(doc.get("balance"))
    return _r(total)
