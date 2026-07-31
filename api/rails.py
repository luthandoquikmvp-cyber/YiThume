# rails.py — the money rail: real cash in, real cash out.
#
# Paystack, not Stripe. Stripe cannot settle to South African bank accounts —
# it requires a US/EU legal entity — so the withdrawal half of the loop has
# nowhere to land. Stripe's Africa arm is Paystack, which does the whole loop
# in ZAR: card/EFT in, and a Transfers API that pays out to any SA bank.
#
# Test mode is available the moment you create a Paystack account, before any
# compliance review, so this runs end to end today. Going live is a key swap:
#   PAYSTACK_SECRET_KEY=sk_test_...   → sk_live_...
#
# Note: Transfers must be enabled on the Paystack account before payouts work,
# and they settle from the available balance.
#
# The rail is only an on-ramp and off-ramp. Who owns the pooled balance is
# ledger.py's job, not this module's.

import os
import json
import hmac
import hashlib
import urllib.error
import urllib.request

PAYSTACK_BASE = "https://api.paystack.co"

# Paystack works in the smallest currency unit — cents for ZAR.
def to_minor(amount):
    return int(round(float(amount or 0) * 100))


def from_minor(amount):
    return round(int(amount or 0) / 100.0, 2)


class RailError(Exception):
    """The rail refused or could not be reached. Never swallow these — an
    unreported payment failure is a lost customer or a lost payout."""


class PaymentRail:
    """Interface. Implemented once, by Paystack."""

    name = "none"
    currency = "ZAR"

    def enabled(self):
        raise NotImplementedError

    def topup_init(self, email, amount, reference, callback_url=""):
        raise NotImplementedError

    def topup_verify(self, reference):
        raise NotImplementedError

    def payout(self, amount, bank, reference, reason=""):
        raise NotImplementedError

    def balance(self):
        raise NotImplementedError

    def verify_webhook(self, raw_body, signature):
        raise NotImplementedError


class PaystackRail(PaymentRail):
    name = "paystack"

    def __init__(self, secret_key="", public_key=""):
        self.secret_key = (secret_key or "").strip()
        self.public_key = (public_key or "").strip()

    # ---- helpers ----
    def enabled(self):
        return bool(self.secret_key)

    def mode(self):
        if not self.secret_key:
            return "unconfigured"
        return "live" if self.secret_key.startswith("sk_live_") else "test"

    def _call(self, method, path, payload=None):
        if not self.enabled():
            raise RailError("Paystack is not configured — set PAYSTACK_SECRET_KEY")
        url = PAYSTACK_BASE + path
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                parsed = json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode() or "{}").get("message", "")
            except Exception:
                pass
            raise RailError(f"Paystack {path} failed ({exc.code}): {detail or exc.reason}")
        except Exception as exc:
            raise RailError(f"Paystack {path} unreachable: {exc}")
        if not parsed.get("status"):
            raise RailError(parsed.get("message") or f"Paystack {path} rejected the request")
        return parsed.get("data") or {}

    # ---- money in ----
    def topup_init(self, email, amount, reference, callback_url=""):
        """Start a card/EFT payment. Returns the URL to send the payer to."""
        payload = {
            "email": email or "wallet@yithume.app",
            "amount": to_minor(amount),
            "currency": self.currency,
            "reference": reference,
        }
        if callback_url:
            payload["callback_url"] = callback_url
        data = self._call("POST", "/transaction/initialize", payload)
        return {"reference": data.get("reference", reference),
                "checkout_url": data.get("authorization_url", ""),
                "access_code": data.get("access_code", "")}

    def topup_verify(self, reference):
        """Confirm a payment actually succeeded. Never credit a wallet from the
        browser's word — always verify server-side against the rail."""
        data = self._call("GET", f"/transaction/verify/{reference}")
        return {"status": data.get("status", "failed"),
                "amount": from_minor(data.get("amount", 0)),
                "reference": data.get("reference", reference),
                "paid_at": data.get("paid_at", "")}

    # ---- money out ----
    def _recipient(self, bank):
        """Register (or re-register) the destination bank account."""
        data = self._call("POST", "/transferrecipient", {
            "type": "basa",                      # South African bank accounts
            "name": bank.get("holder", ""),
            "account_number": bank.get("account_no", ""),
            "bank_code": bank.get("bank_code", ""),
            "currency": self.currency,
        })
        return data.get("recipient_code", "")

    def payout(self, amount, bank, reference, reason=""):
        recipient = bank.get("recipient_code") or self._recipient(bank)
        data = self._call("POST", "/transfer", {
            "source": "balance",
            "amount": to_minor(amount),
            "recipient": recipient,
            "reference": reference,
            "reason": reason or "YiThume withdrawal",
        })
        return {"transfer_ref": data.get("reference", reference),
                "transfer_code": data.get("transfer_code", ""),
                "recipient_code": recipient,
                "status": data.get("status", "pending")}

    def balance(self):
        data = self._call("GET", "/balance")
        rows = data if isinstance(data, list) else [data]
        for row in rows:
            if str(row.get("currency", "")).upper() == self.currency:
                return from_minor(row.get("balance", 0))
        return 0.0

    def banks(self):
        """Bank list for the withdrawal form — codes are required by Transfers."""
        data = self._call("GET", "/bank?country=south africa")
        rows = data if isinstance(data, list) else []
        return [{"name": b.get("name", ""), "code": b.get("code", "")} for b in rows]

    # ---- webhooks ----
    def verify_webhook(self, raw_body, signature):
        """Paystack signs webhooks with HMAC-SHA512 over the raw body, keyed by
        the secret key. An unverified webhook is an open door to free money."""
        if not self.secret_key or not signature:
            return False
        expected = hmac.new(self.secret_key.encode(), raw_body, hashlib.sha512).hexdigest()
        return hmac.compare_digest(expected, signature)


def get_rail():
    return PaystackRail(
        secret_key=os.environ.get("PAYSTACK_SECRET_KEY", ""),
        public_key=os.environ.get("PAYSTACK_PUBLIC_KEY", ""),
    )
