# ussd.py — USSD menu flow (browse / order / track) for feature phones.
#
# Ported from the legacy backend's lean USSD flow, rebuilt against the current
# store. The handler speaks the aggregator protocol (Africa's Talking style):
# it receives sessionId / phoneNumber / text ("1*2*3" step history) and
# returns "CON ..." (continue) or "END ..." (final screen).
#
# No live telecom integration yet — /ussd serves a local simulator page that
# POSTs the same fields, so the flow is testable today and a real short code
# is a config step later.

MAX_LIST = 8


class Deps:
    """Injected backend capabilities so this module stays import-free."""

    def __init__(self, col, settings, place_order, clean_phone, now, new_id,
                 wallet_balance):
        self.col = col
        self.settings = settings
        self.place_order = place_order
        self.clean_phone = clean_phone
        self.now = now
        self.new_id = new_id
        self.wallet_balance = wallet_balance


def _menu():
    return ("CON YiThume — every market, any phone\n"
            "1. Shop a market\n"
            "2. Track order\n"
            "3. My wallet\n"
            "0. Exit")


def _markets(deps):
    rows = [m for m in deps.col("marketplaces").find({"status": "live"})]
    rows.sort(key=lambda m: m.get("name", ""))
    return rows[:MAX_LIST]


def _log_session(deps, session_id, phone):
    if not session_id:
        return
    if not deps.col("ussd_sessions").find_one({"session_id": session_id}):
        deps.col("ussd_sessions").insert_one(
            {"_id": deps.new_id(), "session_id": session_id, "phone": phone, "created_at": deps.now()})


def _categories(deps, marketplace_id):
    cats = sorted({p.get("category", "") for p in deps.col("products").find(
        {"active": True, "marketplace_id": marketplace_id})} - {""})
    return cats[:MAX_LIST]


def _items(deps, marketplace_id, category):
    items = [p for p in deps.col("products").find(
        {"active": True, "marketplace_id": marketplace_id, "category": category})]
    items.sort(key=lambda p: p.get("name", ""))
    return items[:MAX_LIST]


def _pick(options, raw):
    try:
        idx = int(raw) - 1
        if idx < 0:
            return None
        return options[idx]
    except (ValueError, IndexError):
        return None


def handle(deps, session_id, phone, text):
    steps = [s for s in text.split("*") if s != ""] if text else []
    _log_session(deps, session_id, phone)

    if not steps:
        return _menu()

    if steps[0] == "0":
        return "END Thank you for using YiThume."

    # ---- track order ----
    if steps[0] == "2":
        if len(steps) == 1:
            return "CON Enter your order code (e.g. YT-A1B2C):"
        code = steps[1].strip().upper()
        order = deps.col("orders").find_one({"code": code})
        if not order:
            return "END Order not found. Check the code and try again."
        status = order.get("status", "pending").replace("_", " ")
        runner = order.get("runner_name", "")
        line = f"Order {code}: {status}."
        if runner and order.get("status") in ("assigned", "picked_up"):
            line += f" Runner: {runner}."
        return "END " + line

    # ---- wallet ----
    if steps[0] == "3":
        bal = deps.wallet_balance(phone)
        line = f"Wallet: R{bal['available']:.2f} available"
        if bal["held"]:
            line += f", R{bal['held']:.2f} held for orders"
        if bal.get("owed"):
            line += f".\nYou owe R{bal['owed']:.2f} from a failed delivery"
        return "END " + line + ".\nTop up at yithume.app"

    # ---- shop ----
    if steps[0] == "1":
        # Lists are recomputed deterministically each request (USSD resends the
        # full step history), so no per-session menu state is needed.
        markets = _markets(deps)
        if len(steps) == 1:
            if not markets:
                return "END No markets are open right now. Please try later."
            lines = [f"{i + 1}. {m['name']}" for i, m in enumerate(markets)]
            return "CON Choose a market:\n" + "\n".join(lines)

        market = _pick(markets, steps[1])
        if not market:
            return "END Invalid option. Please dial again."
        mid = market["_id"]
        cats = _categories(deps, mid)

        if len(steps) == 2:
            if not cats:
                return "END That market has nothing for sale yet."
            lines = [f"{i + 1}. {c}" for i, c in enumerate(cats)]
            return "CON Choose a category:\n" + "\n".join(lines)

        cat = _pick(cats, steps[2])
        if not cat:
            return "END Invalid option. Please dial again."

        products = [{"id": p["_id"], "name": p["name"], "price": float(p["price"])}
                    for p in _items(deps, mid, cat)]

        if len(steps) == 3:
            if not products:
                return "END No items in that category yet."
            lines = [f"{i + 1}. {it['name']} R{it['price']:.0f}" for i, it in enumerate(products)]
            return "CON Pick an item:\n" + "\n".join(lines)

        item = _pick(products, steps[3])
        if not item:
            return "END Invalid choice. Please dial again."

        if len(steps) == 4:
            return f"CON {item['name']} — R{item['price']:.2f}\nEnter quantity:"

        try:
            qty = max(1, int(steps[4]))
        except ValueError:
            return "END Quantity must be a number. Please dial again."

        if len(steps) == 5:
            return "CON Enter your town or area:"
        area = steps[5].strip()[:40]

        if len(steps) == 6:
            return "CON Enter delivery address or nearest landmark:"
        address = steps[6].strip()[:60]

        settings = deps.settings(mid)
        # Prefer the wallet when there is enough in it — it is cheaper for the
        # buyer and safer for everyone.
        wallet = deps.wallet_balance(phone)
        goods = item["price"] * qty
        wallet_total = goods + float(settings["delivery_fee"])
        use_wallet = wallet["available"] >= wallet_total
        total = wallet_total if use_wallet else goods + float(settings["delivery_fee"]) + float(
            settings["cod_surcharge"])

        if len(steps) == 7:
            how = "from your wallet" if use_wallet else "cash on delivery"
            return ("CON Confirm order:\n"
                    f"{qty} x {item['name']}\n"
                    f"{market['name']}\n"
                    f"Total R{total:.2f} ({how})\n"
                    "1. Confirm\n2. Cancel")

        if steps[7] != "1":
            return "END Order cancelled. Thank you for using YiThume."

        buyer = {"name": phone or "USSD customer", "phone": phone, "area": area, "address": address}
        # Phone identity comes from the network, so the OTP step is skipped.
        order, extras, err, _code = deps.place_order(
            buyer, [{"product_id": item["id"], "qty": qty}],
            channel="ussd", payment_method="wallet" if use_wallet else "cod",
            skip_otp=True, marketplace_id=mid)
        if err == "wallet_short":
            return f"END Your wallet is R{(extras or {}).get('shortfall', 0):.2f} short. Top up and dial again."
        if err:
            return "END " + str(err)
        paid = "paid from your wallet" if use_wallet else "cash on delivery"
        return ("END Order placed: " + order["code"] + "\n"
                f"Total R{order['total']:.2f} {paid}.\n"
                "We will confirm on WhatsApp or SMS.")

    return "END Invalid option. Please dial again."
