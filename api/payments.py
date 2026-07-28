# payments.py — payment provider interface.
#
# Launch provider is "manual": cash on delivery or manual EFT, verified by
# humans. A real gateway (e.g. Yoco) can be added later as another provider
# implementing the same three methods, without touching the order lifecycle.


class ManualProvider:
    name = "manual"
    methods = ("cod", "eft")

    def validate_method(self, method):
        return method in self.methods

    def initiate(self, method, order_code, settings):
        """Called at checkout. Returns the payment record stored on the order."""
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
        payment["status"] = "paid"
        return payment


def get_provider():
    return ManualProvider()
