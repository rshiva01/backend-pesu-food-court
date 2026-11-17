from functools import lru_cache

from app.core.config import get_settings
from app.services.sms.base import SmsProvider


@lru_cache
def get_sms() -> SmsProvider:
    # Real provider (MSG91 / Twilio / college gateway) plugs in here.
    from app.services.sms.console import ConsoleSmsProvider

    return ConsoleSmsProvider(get_settings().env)
