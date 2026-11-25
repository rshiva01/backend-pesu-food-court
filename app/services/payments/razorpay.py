"""Razorpay over plain HTTPS (no SDK). Docs: razorpay.com/docs/api."""

import logging
from typing import Any

import httpx

from app.services.payments.base import (
    GatewayError,
    GatewayOrder,
    GatewayPayment,
    hmac_hex,
    same,
)

log = logging.getLogger("payments.razorpay")
API = "https://api.razorpay.com/v1"


class RazorpayProvider:
    name = "razorpay"

    def __init__(self, key_id: str, key_secret: str, webhook_secret: str) -> None:
        if not (key_id and key_secret and webhook_secret):
            raise RuntimeError("Razorpay keys and webhook secret are required")
        self.key_id = key_id
        self._secret = key_secret
        self._webhook_secret = webhook_secret

    async def _call(self, method: str, path: str, json: dict[str, Any] | None = None) -> Any:
        try:
            async with httpx.AsyncClient(
                base_url=API, auth=(self.key_id, self._secret), timeout=10
            ) as c:
                r = await c.request(method, path, json=json)
        except httpx.HTTPError as e:
            log.warning("razorpay unreachable", extra={"ctx": {"path": path}})
            raise GatewayError("gateway unreachable") from e
        if r.status_code >= 400:
            # body may describe the error; never log keys
            log.warning("razorpay error", extra={"ctx": {"path": path, "status": r.status_code}})
            raise GatewayError(f"gateway returned {r.status_code}")
        return r.json()

    async def create_order(
        self, amount_paise: int, receipt: str, notes: dict[str, str]
    ) -> GatewayOrder:
        j = await self._call(
            "POST",
            "/orders",
            {
                "amount": amount_paise,
                "currency": "INR",
                "receipt": receipt[:40],
                "notes": notes,
                "payment_capture": 1,
            },
        )
        return GatewayOrder(id=j["id"], amount_paise=j["amount"], currency=j["currency"])

    def verify_checkout_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        return same(hmac_hex(self._secret, f"{order_id}|{payment_id}".encode()), signature)

    def verify_webhook(self, body: bytes, signature: str) -> bool:
        return same(hmac_hex(self._webhook_secret, body), signature)

    async def fetch_payment(self, payment_id: str) -> GatewayPayment:
        j = await self._call("GET", f"/payments/{payment_id}")
        return payment_from_entity(j)

    async def refund(self, payment_id: str, amount_paise: int) -> str:
        j = await self._call("POST", f"/payments/{payment_id}/refund", {"amount": amount_paise})
        return str(j["id"])


def payment_from_entity(e: dict[str, Any]) -> GatewayPayment:
    return GatewayPayment(
        id=str(e["id"]),
        order_id=str(e.get("order_id") or ""),
        amount_paise=int(e["amount"]),
        currency=str(e["currency"]),
        status=str(e["status"]),
    )
