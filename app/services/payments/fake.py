"""Dev/test stand-in for Razorpay with the same signature scheme. Lets the
whole online flow run without keys. Refuses to exist in production."""

import secrets

from app.services.payments.base import (
    GatewayError,
    GatewayOrder,
    GatewayPayment,
    hmac_hex,
    same,
)

FAKE_KEY_SECRET = "fake-key-secret"  # noqa: S105  # nosec B105
FAKE_WEBHOOK_SECRET = "fake-webhook-secret"  # noqa: S105  # nosec B105


class FakeProvider:
    name = "fake"
    key_id = "fake_key"

    def __init__(self, env: str) -> None:
        if env == "production":
            raise RuntimeError("FakeProvider must not be used in production")
        self.orders: dict[str, GatewayOrder] = {}
        self.payments: dict[str, GatewayPayment] = {}
        self.refunds: list[tuple[str, int]] = []
        self.fail_next = False  # tests: simulate the gateway being down

    def _check(self) -> None:
        if self.fail_next:
            self.fail_next = False
            raise GatewayError("fake outage")

    async def create_order(
        self, amount_paise: int, receipt: str, notes: dict[str, str]
    ) -> GatewayOrder:
        self._check()
        o = GatewayOrder(
            id=f"order_fake{secrets.token_hex(7)}", amount_paise=amount_paise, currency="INR"
        )
        self.orders[o.id] = o
        return o

    def complete(self, order_id: str, amount_paise: int | None = None) -> tuple[str, str]:
        """What Razorpay Checkout hands the browser after a successful payment:
        (payment_id, signature). amount_paise lets tests fake a short payment."""
        o = self.orders[order_id]
        pid = f"pay_fake{secrets.token_hex(7)}"
        self.payments[pid] = GatewayPayment(
            id=pid,
            order_id=order_id,
            amount_paise=o.amount_paise if amount_paise is None else amount_paise,
            currency="INR",
            status="captured",
        )
        return pid, self.sign(order_id, pid)

    @staticmethod
    def sign(order_id: str, payment_id: str) -> str:
        return hmac_hex(FAKE_KEY_SECRET, f"{order_id}|{payment_id}".encode())

    @staticmethod
    def sign_webhook(body: bytes) -> str:
        return hmac_hex(FAKE_WEBHOOK_SECRET, body)

    def verify_checkout_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        return same(self.sign(order_id, payment_id), signature)

    def verify_webhook(self, body: bytes, signature: str) -> bool:
        return same(self.sign_webhook(body), signature)

    async def fetch_payment(self, payment_id: str) -> GatewayPayment:
        self._check()
        p = self.payments.get(payment_id)
        if p is None:
            raise GatewayError("no such payment")
        return p

    async def refund(self, payment_id: str, amount_paise: int) -> str:
        self._check()
        self.refunds.append((payment_id, amount_paise))
        return f"rfnd_fake{secrets.token_hex(7)}"
