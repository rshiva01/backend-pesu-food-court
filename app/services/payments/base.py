"""Payment gateway interface. Orders only ever become paid through
app.services.orders.confirm_payment, after the gateway itself confirms the
payment (signature + fetched amount) — never on the browser's word."""

import hashlib
import hmac
from dataclasses import dataclass
from typing import Protocol


class GatewayError(Exception):
    """Gateway unreachable or refused the call."""


@dataclass(frozen=True)
class GatewayOrder:
    id: str
    amount_paise: int
    currency: str


@dataclass(frozen=True)
class GatewayPayment:
    id: str
    order_id: str
    amount_paise: int
    currency: str
    status: str  # created | authorized | captured | refunded | failed


class PaymentProvider(Protocol):
    name: str
    key_id: str  # public; the browser checkout needs it

    async def create_order(
        self, amount_paise: int, receipt: str, notes: dict[str, str]
    ) -> GatewayOrder: ...

    def verify_checkout_signature(self, order_id: str, payment_id: str, signature: str) -> bool: ...

    def verify_webhook(self, body: bytes, signature: str) -> bool: ...

    async def fetch_payment(self, payment_id: str) -> GatewayPayment: ...

    async def refund(self, payment_id: str, amount_paise: int) -> str: ...


def hmac_hex(secret: str, msg: bytes) -> str:
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
