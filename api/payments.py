# payments.py — how an order is paid for.
#
# Three methods, in descending order of how much we want them used:
#
#   wallet — prepaid balance, held in escrow until the buyer's delivery PIN
#            proves handover. The only method with scam protection, and the
#            only one that pays the seller instantly.
#   cod    — cash on delivery. Unprotected, carries a surcharge because it
#            genuinely costs more to serve, and a failed trip has to be paid
#            for by someone (see the failed-delivery waterfall in app.py).
#   eft    — manual bank transfer, verified by a human at confirm time.
#
# The escrow hold and release live in ledger.py; this module only decides what
# the payment record on the order looks like and what delivery does to it.


class WalletProvider:
    name = "wallet"
    methods = ("wallet", "cod", "eft")

    def validate_method(self, method):
        return method in self.methods

    def initiate(self, method, order_code, settings):
        """Called at checkout. Returns the payment record stored on the order."""
        if method == "wallet":
            return {
                "method": "wallet",
                "provider": self.name,
                "status": "in_escrow",
                "instructions": "Paid from your YiThume wallet and held until delivery.",
            }
        if method == "eft":
            return {
                "method": "eft",
                "provider": self.name,
                "status": "eft_claimed",  # buyer says they paid; admin verifies at confirm
                "instructions": f"{settings.get('eft_details', '')} Reference: {order_code}",
            }
        return {"method": "cod", "provider": self.name, "status": "cod_pending"}

    def on_delivered(self, payment):
        """Called when the order is delivered. Returns updated payment record."""
        payment = dict(payment)
        payment["status"] = "released" if payment.get("method") == "wallet" else "paid"
        return payment


def get_provider():
    return WalletProvider()
