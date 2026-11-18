from pydantic import BaseModel, ConfigDict


class Strict(BaseModel):
    """Request bodies: unknown fields rejected, strings trimmed."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
