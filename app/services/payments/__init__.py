from functools import lru_cache

from app.core.config import get_settings
from app.services.payments.base import PaymentProvider


@lru_cache
def get_payments() -> PaymentProvider:
    s = get_settings()
    if s.payment_provider == "razorpay":
        from app.services.payments.razorpay import RazorpayProvider

        return RazorpayProvider(
            s.razorpay_key_id,
            s.razorpay_key_secret.get_secret_value() if s.razorpay_key_secret else "",
            s.razorpay_webhook_secret.get_secret_value() if s.razorpay_webhook_secret else "",
        )
    from app.services.payments.fake import FakeProvider

    return FakeProvider(s.env)
