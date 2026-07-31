#!/usr/bin/env python3
"""money_loop_test.py — the R20 round trip.

Money in → around every account → money out, with the ledger balancing at every
step. Run it and watch the balances move:

    python money_loop_test.py

Paystack is the real rail, but its API is stubbed here at the HTTP boundary so
this runs offline and in CI. The stub returns Paystack's documented response
shapes, so the code under test is the same PaystackRail that runs in
production — including webhook signature verification. Once you have test keys,
point PAYSTACK_SECRET_KEY at them and the same flow runs against the sandbox.
"""

import os
import sys
import json
import hmac
import hashlib
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "api"))

os.environ.setdefault("PAYSTACK_SECRET_KEY", "sk_test_moneyloop")
os.environ.setdefault("ADMIN_SECRET", "1234")

import app          # noqa: E402
import ledger       # noqa: E402
import rails        # noqa: E402

ADMIN = {"X-Admin-Pin": "1234"}
GREEN, DIM, BOLD, RESET = "\033[32m", "\033[2m", "\033[1m", "\033[0m"


# ---------------------------------------------------------------- rail stub
class StubPaystack(rails.PaystackRail):
    """Same class, same signature checks — only the network is faked."""

    def __init__(self):
        super().__init__(secret_key="sk_test_moneyloop", public_key="pk_test_moneyloop")
        self.paid = {}
        self.transfers = []
        self.pool = 0.0

    def topup_init(self, email, amount, reference, callback_url=""):
        self.paid[reference] = amount          # pretend the payer completed checkout
        return {"reference": reference,
                "checkout_url": f"https://checkout.paystack.com/{reference}",
                "access_code": "stub"}

    def topup_verify(self, reference):
        if reference not in self.paid:
            return {"status": "failed", "amount": 0.0, "reference": reference}
        self.pool += self.paid[reference]
        return {"status": "success", "amount": self.paid[reference],
                "reference": reference, "paid_at": "2026-01-01T00:00:00Z"}

    def payout(self, amount, bank, reference, reason=""):
        if amount > self.pool:
            raise rails.RailError("insufficient balance on the rail")
        self.pool -= amount
        self.transfers.append({"reference": reference, "amount": amount, "bank": bank})
        return {"transfer_ref": reference, "transfer_code": "TRF_stub",
                "recipient_code": "RCP_stub", "status": "success"}

    def balance(self):
        return round(self.pool, 2)


# ---------------------------------------------------------------- helpers
def money(v):
    return f"R{float(v):>8.2f}"


class MoneyLoop(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.rail = StubPaystack()
        app.rail = cls.rail
        cls.c = app.app.test_client()
        cls.market = app.col("marketplaces").find_one({"slug": "mthatha-town-market"})
        cls.buyer_phone = "27811110001"

    # -- reporting -------------------------------------------------
    def show(self, title):
        print(f"\n{BOLD}{title}{RESET}")
        for a in sorted(app.col("accounts").find({}),
                        key=lambda d: (d["owner_type"], d["owner_key"])):
            b = ledger.balances(app.LD(), a["_id"])
            if b["balance"] == 0 and b["held"] == 0:
                continue
            held = f"  held {money(b['held'])}" if b["held"] else ""
            print(f"  {a['owner_type']:<12} {a['owner_key'][:22]:<24} "
                  f"{money(b['balance'])}{held}")
        print(f"  {DIM}rail pool {money(self.rail.balance())}{RESET}")

    def assert_ledger_sound(self, note=""):
        audit = ledger.audit(app.LD())
        self.assertTrue(audit["balanced"],
                        f"journal does not net to zero{note}: {audit['net']}")
        self.assertEqual(audit["drift"], [],
                         f"cached balances drifted from the journal{note}")

    # -- the loop --------------------------------------------------
    def test_01_topup_twenty_rand(self):
        r = self.c.post("/api/wallet/topup",
                        json={"phone": self.buyer_phone, "amount": 20.0})
        self.assertEqual(r.status_code, 200, r.get_json())
        reference = r.get_json()["reference"]

        # Credit arrives only via the webhook, signed the way Paystack signs it.
        payload = json.dumps({"event": "charge.success",
                              "data": {"reference": reference}}).encode()
        sig = hmac.new(self.rail.secret_key.encode(), payload, hashlib.sha512).hexdigest()
        r = self.c.post("/api/rail/webhook", data=payload,
                        headers={"x-paystack-signature": sig,
                                 "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 200)

        wallet = self.c.get(f"/api/wallet?phone={self.buyer_phone}").get_json()
        self.assertEqual(wallet["balance"], 20.0)
        self.assertEqual(wallet["available"], 20.0)
        self.assert_ledger_sound(" after top-up")
        self.show("1. Topped up R20")

        # Replaying the same webhook must not mint a second R20.
        self.c.post("/api/rail/webhook", data=payload,
                    headers={"x-paystack-signature": sig,
                             "Content-Type": "application/json"})
        again = self.c.get(f"/api/wallet?phone={self.buyer_phone}").get_json()
        self.assertEqual(again["balance"], 20.0, "webhook replay double-credited the wallet")

    def test_02_bad_signature_is_rejected(self):
        r = self.c.post("/api/rail/webhook", data=b'{"event":"charge.success"}',
                        headers={"x-paystack-signature": "nope"})
        self.assertEqual(r.status_code, 401)

    def test_03_order_holds_escrow(self):
        products = self.c.get("/api/products?marketplace=mthatha-town-market").get_json()["products"]
        item = min(products, key=lambda p: p["price"])
        qty = 1
        r = self.c.post("/api/orders", json={
            "marketplace": "mthatha-town-market",
            "buyer": {"name": "Loop Buyer", "phone": self.buyer_phone,
                      "area": "Mthatha", "address": "12 Dirt Road"},
            "items": [{"product_id": item["_id"], "qty": qty}],
            "payment_method": "wallet"})
        # A R20 wallet against goods + R25 delivery is short on purpose: this is
        # the top-up-at-checkout path, and it must be offered, not refused.
        self.assertEqual(r.status_code, 402, r.get_json())
        shortfall = r.get_json()["shortfall"]
        self.assertGreater(shortfall, 0)
        print(f"\n{BOLD}2. Checkout short by {money(shortfall)} — offering a top-up{RESET}")

        top = self.c.post("/api/wallet/topup",
                          json={"phone": self.buyer_phone, "amount": shortfall}).get_json()
        self.c.post("/api/wallet/topup/verify", json={"reference": top["reference"]})

        r = self.c.post("/api/orders", json={
            "marketplace": "mthatha-town-market",
            "buyer": {"name": "Loop Buyer", "phone": self.buyer_phone,
                      "area": "Mthatha", "address": "12 Dirt Road"},
            "items": [{"product_id": item["_id"], "qty": qty}],
            "payment_method": "wallet"})
        self.assertEqual(r.status_code, 200, r.get_json())
        data = r.get_json()
        MoneyLoop.order = data["order"]
        MoneyLoop.otp = data.get("otp_demo") or data["order"]["otp"]

        wallet = self.c.get(f"/api/wallet?phone={self.buyer_phone}").get_json()
        self.assertEqual(wallet["held"], MoneyLoop.order["total"])
        self.assertEqual(wallet["available"], 0.0)
        self.assert_ledger_sound(" after escrow hold")
        self.show(f"3. Ordered {money(MoneyLoop.order['total'])} — held in escrow")

    def test_04_deliver_releases_and_splits(self):
        oid = MoneyLoop.order["_id"]
        self.c.post(f"/api/orders/{oid}/verify-otp", json={"code": MoneyLoop.otp})
        r = self.c.patch(f"/api/orders/{oid}", json={"status": "confirmed"}, headers=ADMIN)
        self.assertEqual(r.status_code, 200, r.get_json())
        pin = r.get_json()["order"]["delivery_pin"]

        r = self.c.patch(f"/api/orders/{oid}", json={"runner_id": "auto"}, headers=ADMIN)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.c.patch(f"/api/orders/{oid}", json={"status": "picked_up"}, headers=ADMIN)

        # Wrong PIN must not release a cent.
        bad = self.c.patch(f"/api/orders/{oid}",
                           json={"status": "delivered", "delivery_pin": "0000"}, headers=ADMIN)
        self.assertEqual(bad.status_code, 409)
        wallet = self.c.get(f"/api/wallet?phone={self.buyer_phone}").get_json()
        self.assertEqual(wallet["held"], MoneyLoop.order["total"],
                         "a wrong PIN released escrow")

        r = self.c.patch(f"/api/orders/{oid}",
                         json={"status": "delivered", "delivery_pin": pin}, headers=ADMIN)
        self.assertEqual(r.status_code, 200, r.get_json())
        settlement = r.get_json()["order"]["settlement"]
        self.assertEqual(settlement["cash_status"], "settled",
                         f"escrow did not release: {settlement.get('release_error', '')}")

        wallet = self.c.get(f"/api/wallet?phone={self.buyer_phone}").get_json()
        self.assertEqual(wallet["held"], 0.0)
        self.assertEqual(wallet["balance"], 0.0, "the buyer's money did not leave")
        self.assert_ledger_sound(" after release")
        self.show("4. Delivered with the PIN — escrow split across the network")

        entry = app.col("ledger").find_one(
            {"idempotency_key": f"escrow_release:{MoneyLoop.order['code']}"})
        credited = round(sum(e["amount"] for e in entry["entries"]
                             if e["direction"] == "credit" and not e.get("hold")), 2)
        self.assertEqual(credited, MoneyLoop.order["total"],
                         "splits do not add up to what was held")

    def test_05_seller_withdraws(self):
        # The seller who actually made the sale, not just any seller.
        sold = MoneyLoop.order["fees"]["sellers"][0]
        acct = ledger.account(app.LD(), "seller",
                              sold.get("seller_id") or sold["seller_name"],
                              self.market["_id"])
        owed = ledger.balances(app.LD(), acct["_id"])["available"]
        self.assertGreater(owed, 0, "the seller earned nothing from the delivery")

        # Withdrawals are per-account; the seller's is raised by the operator
        # who owes them, then approved by the platform.
        wd = {"_id": app._new_id(), "account_id": acct["_id"],
              "marketplace_id": self.market["_id"], "amount": owed,
              "bank": {"holder": "Nomsa Dlamini", "account_no": "1234567890",
                       "bank_code": "470010"},
              "status": "requested", "hold_reason": "", "transfer_ref": "",
              "created_at": app._now(), "resolved_at": ""}
        app.col("withdrawals").insert_one(wd)

        r = self.c.post(f"/api/withdrawals/{wd['_id']}/approve", headers=ADMIN)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["withdrawal"]["status"], "paid")
        self.assertEqual(ledger.balances(app.LD(), acct["_id"])["balance"], 0.0)
        self.assertEqual(self.rail.transfers[-1]["amount"], owed)
        self.assert_ledger_sound(" after withdrawal")
        self.show(f"5. Seller withdrew {money(owed)} to their bank")

    def test_06_failed_cod_never_costs_the_runner(self):
        """The point of the waterfall: a buyer who no-shows does not make the
        runner work for free."""
        phone = "27811110002"
        products = self.c.get("/api/products?marketplace=mthatha-town-market").get_json()["products"]
        item = next(p for p in products if p["price"] <= 100)
        r = self.c.post("/api/orders", json={
            "marketplace": "mthatha-town-market",
            "buyer": {"name": "No Show", "phone": phone,
                      "area": "Mthatha", "address": "No such place"},
            "items": [{"product_id": item["_id"], "qty": 1}],
            "payment_method": "cod"})
        self.assertEqual(r.status_code, 200, r.get_json())
        order = r.get_json()["order"]
        otp = r.get_json().get("otp_demo") or order["otp"]
        self.c.post(f"/api/orders/{order['_id']}/verify-otp", json={"code": otp})
        self.c.patch(f"/api/orders/{order['_id']}", json={"status": "confirmed"}, headers=ADMIN)
        self.c.patch(f"/api/orders/{order['_id']}", json={"runner_id": "auto"}, headers=ADMIN)
        assigned = app.col("orders").find_one({"_id": order["_id"]})
        runner_acct = ledger.account(app.LD(), "runner", assigned["runner_id"])
        before = ledger.balances(app.LD(), runner_acct["_id"])["balance"]

        r = self.c.post(f"/api/orders/{order['_id']}/incident",
                        json={"reason": "no_show"}, headers=ADMIN)
        self.assertEqual(r.status_code, 200, r.get_json())
        failure = r.get_json()["failure"]

        after = ledger.balances(app.LD(), runner_acct["_id"])["balance"]
        self.assertGreater(after, before, "the runner was not paid for the wasted trip")
        self.assertEqual(round(after - before, 2), failure["attempt_fee"])

        buyer_acct = ledger.buyer_account(app.LD(), phone)
        self.assertLess(ledger.balances(app.LD(), buyer_acct["_id"])["balance"], 0,
                        "an empty-walleted no-show should end up in debt")
        buyer = app.col("buyers").find_one({"phone": phone})
        self.assertTrue(buyer["blocked_until_settled"])
        self.assert_ledger_sound(" after a failed delivery")
        self.show("6. COD no-show — runner paid, buyer in debt, reserve carried it")

        # And the debt follows the phone number onto a different marketplace.
        r = self.c.post("/api/orders", json={
            "marketplace": "kei-fresh-market",
            "buyer": {"name": "No Show", "phone": phone, "area": "Butterworth",
                      "address": "Elsewhere"},
            "items": [{"product_id": self.c.get(
                "/api/products?marketplace=kei-fresh-market").get_json()["products"][0]["_id"],
                "qty": 1}],
            "payment_method": "cod"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("owe", r.get_json()["error"].lower())

    def test_07_final_audit(self):
        audit = ledger.audit(app.LD())
        self.assertTrue(audit["balanced"])
        self.assertEqual(audit["drift"], [])
        owed = ledger.liabilities(app.LD())
        print(f"\n{GREEN}{BOLD}Loop closed.{RESET} journal nets to "
              f"{audit['net']}, no drift across {len(audit['accounts'])} accounts.")
        print(f"  outstanding liabilities {money(owed)} · "
              f"rail pool {money(self.rail.balance())}")


if __name__ == "__main__":
    unittest.main(verbosity=1)
