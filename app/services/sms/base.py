from typing import Protocol


class SmsProvider(Protocol):
    async def send_otp(self, phone: str, code: str) -> None: ...
