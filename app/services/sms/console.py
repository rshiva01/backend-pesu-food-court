import logging

log = logging.getLogger("sms.console")


class ConsoleSmsProvider:
    """Dev only: prints the OTP to the API log instead of sending an SMS."""

    def __init__(self, env: str) -> None:
        if env == "production":
            raise RuntimeError("ConsoleSmsProvider must not be used in production")

    async def send_otp(self, phone: str, code: str) -> None:
        # print(), not the logger: the JSON log redacts anything named "code".
        print(f"[dev sms] OTP for {phone}: {code}", flush=True)
        log.info("dev sms sent", extra={"ctx": {"phone": phone}})
