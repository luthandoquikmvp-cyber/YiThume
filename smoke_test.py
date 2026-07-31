#!/usr/bin/env python3
"""smoke_test.py — the things that are easy to break and invisible by clicking.

Tenant isolation above all: one operator must never see or touch another
marketplace's data. Then runner scoping, frozen fees, and the anti-fraud gates.

    python smoke_test.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "api"))

os.environ.setdefault("ADMIN_SECRET", "1234")
os.environ.setdefault("PAYSTACK_SECRET_KEY", "sk_test_smoke")

import app        # noqa: E402
import ledger     # noqa: E402

ADMIN = {"X-Admin-Pin": "1234"}


def op(token):
    return {"X-Console-Token": token}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = app.app.test_client()
        cls.a = app.col("marketplaces").find_one({"slug": "mthatha-town-market"})
        cls.b = app.col("marketplaces").find_one({"slug": "kei-fresh-market"})
        cls.ta = cls.login("27720000001", "1234")
        cls.tb = cls.login("27720000002", "4321")

    @classmethod
    def login(cls, phone, pin):
        r = cls.c.post("/api/console/login", json={"phone": phone, "pin": pin})
        assert r.status_code == 200, r.get_json()
        return r.get_json()["token"]


class TenantIsolation(Base):
    """The whole promise of multi-tenancy. If any of these fail, tenants leak."""

    def test_orders_are_scoped(self):
        b_order = self._place_order(self.b, "kei-fresh-market", "27899990001")
        seen = self.c.get("/api/orders", headers=op(self.ta)).get_json()["orders"]
        self.assertNotIn(b_order["code"], [o["code"] for o in seen],
                         "operator A can see operator B's orders")

        r = self.c.patch(f"/api/orders/{b_order['_id']}",
                         json={"status": "confirmed"}, headers=op(self.ta))
        self.assertEqual(r.status_code, 404, "operator A could modify B's order")

    def test_products_are_scoped(self):
        mine = self.c.get("/api/products?all=1", headers=op(self.ta)).get_json()["products"]
        self.assertTrue(mine)
        self.assertTrue(all(p["marketplace_id"] == self.a["_id"] for p in mine))

        theirs = self.c.get("/api/products?marketplace=kei-fresh-market").get_json()["products"]
        self.assertTrue(theirs)
        self.assertTrue(all(p["marketplace_id"] == self.b["_id"] for p in theirs),
                        "a storefront query returned another market's products")

        victim = theirs[0]
        r = self.c.patch(f"/api/products/{victim['_id']}",
                         json={"price": 1.0}, headers=op(self.ta))
        self.assertEqual(r.status_code, 404, "operator A could reprice B's product")
        r = self.c.delete(f"/api/products/{victim['_id']}", headers=op(self.ta))
        self.assertEqual(r.status_code, 404, "operator A could delete B's product")

    def test_sellers_are_scoped(self):
        mine = self.c.get("/api/sellers", headers=op(self.ta)).get_json()["sellers"]
        self.assertTrue(all(s["marketplace_id"] == self.a["_id"] for s in mine))
        theirs = self.c.get("/api/sellers", headers=op(self.tb)).get_json()["sellers"]
        self.assertTrue(theirs)
        r = self.c.patch(f"/api/sellers/{theirs[0]['_id']}",
                         json={"approved": False}, headers=op(self.ta))
        self.assertEqual(r.status_code, 404)

    def test_settings_do_not_bleed(self):
        self.c.patch("/api/settings", json={"delivery_fee": 44.0}, headers=op(self.ta))
        a = self.c.get("/api/settings", headers=op(self.ta)).get_json()["settings"]
        b = self.c.get("/api/settings", headers=op(self.tb)).get_json()["settings"]
        self.assertEqual(a["delivery_fee"], 44.0)
        self.assertNotEqual(b["delivery_fee"], 44.0, "one tenant's fee changed another's")
        self.c.patch("/api/settings", json={"delivery_fee": 25.0}, headers=op(self.ta))

    def test_operator_cannot_touch_network_rates(self):
        r = self.c.patch("/api/settings", json={"network_rate": 0.0}, headers=op(self.ta))
        self.assertEqual(r.status_code, 400, "operator changed YiThume's own commission")
        s = self.c.get("/api/settings", headers=op(self.ta)).get_json()["settings"]
        self.assertGreater(s["network_rate"], 0)

    def test_operator_cannot_reach_platform_surfaces(self):
        for path in ("/api/buyers", "/api/remittances", "/api/ledger/audit",
                     "/api/rail/status", "/api/runners"):
            r = self.c.get(path, headers=op(self.ta))
            self.assertIn(r.status_code, (401, 403),
                          f"{path} was reachable by an operator ({r.status_code})")

    def test_admin_pin_not_accepted_in_query(self):
        r = self.c.get("/api/orders?pin=1234")
        self.assertEqual(r.status_code, 401, "admin PIN still works as a query param")

    def test_stats_are_scoped(self):
        a = self.c.get("/api/stats", headers=op(self.ta)).get_json()["stats"]
        everything = self.c.get("/api/stats", headers=ADMIN).get_json()["stats"]
        self.assertLessEqual(a["orders_total"], everything["orders_total"])
        self.assertNotIn("network_revenue", a, "operator can see platform revenue")

    def _place_order(self, market, slug, phone):
        products = self.c.get(f"/api/products?marketplace={slug}").get_json()["products"]
        r = self.c.post("/api/orders", json={
            "marketplace": slug,
            "buyer": {"name": "Someone", "phone": phone, "area": "Town", "address": "Somewhere"},
            "items": [{"product_id": products[0]["_id"], "qty": 1}],
            "payment_method": "cod"})
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()["order"]


class Baskets(Base):
    def test_cannot_mix_marketplaces_in_one_order(self):
        a_p = self.c.get("/api/products?marketplace=mthatha-town-market").get_json()["products"][0]
        b_p = self.c.get("/api/products?marketplace=kei-fresh-market").get_json()["products"][0]
        r = self.c.post("/api/orders", json={
            "marketplace": "mthatha-town-market",
            "buyer": {"name": "Mixer", "phone": "27899990002", "area": "Mthatha", "address": "x"},
            "items": [{"product_id": a_p["_id"], "qty": 1}, {"product_id": b_p["_id"], "qty": 1}],
            "payment_method": "cod"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("same marketplace", r.get_json()["error"])


class RunnerScoping(Base):
    def test_shared_pool_sees_both_markets(self):
        for slug, phone in (("mthatha-town-market", "27899990010"),
                            ("kei-fresh-market", "27899990011")):
            o = self._confirmed_order(slug, phone)
            self.assertTrue(o)
        jobs = self.c.get("/api/runner/jobs?phone=27730000002").get_json()
        markets = {j["marketplace_name"] for j in jobs["available"]}
        self.assertGreaterEqual(len(markets), 2,
                                "a shared-pool runner should see jobs from every market")

    def test_exclusive_runner_only_sees_its_own(self):
        jobs = self.c.get("/api/runner/jobs?phone=27730000004").get_json()
        self.assertTrue(all(j["marketplace_id"] == self.a["_id"] for j in jobs["available"]),
                        "a marketplace-exclusive runner saw another market's jobs")

    def test_opting_out_hides_a_market_from_the_pool(self):
        self.c.patch("/api/console/marketplace",
                     json={"use_shared_runners": False}, headers=op(self.tb))
        o = self._confirmed_order("kei-fresh-market", "27899990012")
        jobs = self.c.get("/api/runner/jobs?phone=27730000002").get_json()
        self.assertNotIn(o["code"], [j["code"] for j in jobs["available"]],
                         "shared runner saw a job from a market that opted out")
        self.c.patch("/api/console/marketplace",
                     json={"use_shared_runners": True}, headers=op(self.tb))

    def test_wallet_orders_outrank_cod_in_the_queue(self):
        jobs = self.c.get("/api/runner/jobs?phone=27730000001").get_json()["available"]
        methods = [j["payment_method"] for j in jobs]
        if "wallet" in methods and "cod" in methods:
            self.assertLess(methods.index("wallet"), methods.index("cod"),
                            "COD jobs are being offered ahead of prepaid ones")

    def _confirmed_order(self, slug, phone):
        products = self.c.get(f"/api/products?marketplace={slug}").get_json()["products"]
        r = self.c.post("/api/orders", json={
            "marketplace": slug,
            "buyer": {"name": "Buyer", "phone": phone, "area": "Town", "address": "Place"},
            "items": [{"product_id": products[0]["_id"], "qty": 1}],
            "payment_method": "cod"})
        data = r.get_json()
        if not data.get("ok"):
            return None
        order = data["order"]
        otp = data.get("otp_demo") or order["otp"]
        self.c.post(f"/api/orders/{order['_id']}/verify-otp", json={"code": otp})
        self.c.patch(f"/api/orders/{order['_id']}", json={"status": "confirmed"}, headers=ADMIN)
        return order


class FrozenFees(Base):
    def test_changing_settings_does_not_alter_an_existing_order(self):
        products = self.c.get("/api/products?marketplace=mthatha-town-market").get_json()["products"]
        r = self.c.post("/api/orders", json={
            "marketplace": "mthatha-town-market",
            "buyer": {"name": "Frozen", "phone": "27899990020", "area": "Mthatha", "address": "x"},
            "items": [{"product_id": products[0]["_id"], "qty": 1}],
            "payment_method": "cod"})
        order = r.get_json()["order"]
        before = dict(order["fees"])

        self.c.patch("/api/settings", json={"delivery_fee": 99.0, "marketplace_rate": 0.30},
                     headers=op(self.ta))
        after = app.col("orders").find_one({"_id": order["_id"]})["fees"]
        self.assertEqual(before, after, "an existing order's fees moved when settings changed")
        self.c.patch("/api/settings", json={"delivery_fee": 25.0, "marketplace_rate": 0.10},
                     headers=op(self.ta))

    def test_absurd_rates_are_refused(self):
        r = self.c.patch("/api/settings", json={"marketplace_rate": 0.95}, headers=op(self.ta))
        self.assertEqual(r.status_code, 400)
        r = self.c.patch("/api/settings", json={"runner_rate": 0.9, "node_rate": 0.9},
                         headers=ADMIN)
        self.assertEqual(r.status_code, 400)


class FailSafes(Base):
    def test_one_live_marketplace_per_number(self):
        r = self.c.post("/api/marketplaces", json={
            "name": "Duplicate Market", "phone": "27720000001", "pin": "9999", "areas": "Mthatha"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("already runs", r.get_json()["error"])

    def test_signup_requires_otp_before_going_live(self):
        r = self.c.post("/api/marketplaces", json={
            "name": "Pending Market", "phone": "27788880001", "pin": "1111", "areas": "Ngcobo"})
        self.assertEqual(r.status_code, 200, r.get_json())
        mid = r.get_json()["marketplace_id"]
        self.assertEqual(app.col("marketplaces").find_one({"_id": mid})["status"], "pending")
        # not live yet → invisible publicly, and no sign-in
        slugs = [m["slug"] for m in self.c.get("/api/marketplaces").get_json()["marketplaces"]]
        self.assertNotIn("pending-market", slugs)
        self.assertEqual(self.c.post("/api/console/login",
                                     json={"phone": "27788880001", "pin": "1111"}).status_code, 401)

        code = r.get_json().get("otp_demo") or app.col("marketplaces").find_one({"_id": mid})["otp"]
        v = self.c.post("/api/marketplaces/verify", json={"marketplace_id": mid, "code": code})
        self.assertEqual(v.status_code, 200, v.get_json())
        self.assertEqual(app.col("marketplaces").find_one({"_id": mid})["status"], "live")

    def test_short_pin_refused(self):
        r = self.c.post("/api/marketplaces", json={
            "name": "Weak Pin", "phone": "27788880002", "pin": "12", "areas": "Town"})
        self.assertEqual(r.status_code, 400)

    def test_unverified_market_has_a_low_escrow_cap(self):
        cap = app.TRUST_TIERS["unverified"]["escrow_cap"]
        self.assertLess(cap, app.TRUST_TIERS["verified"]["escrow_cap"])
        market = app.col("marketplaces").find_one({"slug": "kei-fresh-market"})
        self.assertEqual(market["trust"]["tier"], "unverified")

    def test_unverified_withdrawal_is_held(self):
        mid = self.b["_id"]
        acct = ledger.account(app.LD(), "marketplace", mid, mid)
        ledger.topup(app.LD(), acct["_id"], 100.0, "smoke-seed-b")
        self.c.patch("/api/console/marketplace", headers=op(self.tb), json={
            "bank": {"holder": "B", "account_no": "111", "bank_code": "470010"}})
        r = self.c.post("/api/wallet/withdraw", json={"amount": 50.0}, headers=op(self.tb))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["withdrawal"]["status"], "held",
                         "an unverified market could withdraw immediately")

    def test_withdrawal_needs_bank_details(self):
        mid = self.a["_id"]
        acct = ledger.account(app.LD(), "marketplace", mid, mid)
        ledger.topup(app.LD(), acct["_id"], 50.0, "smoke-seed-a")
        app.col("marketplaces").update_one({"_id": mid}, {"$set": {"bank": {}}})
        r = self.c.post("/api/wallet/withdraw", json={"amount": 10.0}, headers=op(self.ta))
        self.assertEqual(r.status_code, 409)

    def test_cannot_withdraw_more_than_available(self):
        r = self.c.post("/api/wallet/withdraw", json={"amount": 999999.0}, headers=op(self.ta))
        self.assertEqual(r.status_code, 409)

    def test_webhook_rejects_bad_signature(self):
        r = self.c.post("/api/rail/webhook", data=b'{"event":"charge.success"}',
                        headers={"x-paystack-signature": "forged"})
        self.assertEqual(r.status_code, 401)


class LedgerIntegrity(Base):
    def test_posts_must_balance(self):
        deps = app.LD()
        a = ledger.account(deps, "buyer", "27000000001")
        b = ledger.account(deps, "buyer", "27000000002")
        with self.assertRaises(ValueError):
            ledger.post(deps, "adjustment", "unbalanced", [
                {"account_id": a["_id"], "direction": "debit", "amount": 10},
                {"account_id": b["_id"], "direction": "credit", "amount": 7}])

    def test_idempotency_blocks_double_posting(self):
        deps = app.LD()
        acct = ledger.account(deps, "buyer", "27000000003")
        _doc, created = ledger.topup(deps, acct["_id"], 10.0, "idem-test")
        self.assertTrue(created)
        _doc, created = ledger.topup(deps, acct["_id"], 10.0, "idem-test")
        self.assertFalse(created, "the same reference credited twice")
        self.assertEqual(ledger.balances(deps, acct["_id"])["balance"], 10.0)

    def test_audit_reports_no_drift(self):
        audit = self.c.get("/api/ledger/audit", headers=ADMIN).get_json()["audit"]
        self.assertTrue(audit["balanced"], f"journal nets to {audit['net']}")
        self.assertEqual(audit["drift"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
