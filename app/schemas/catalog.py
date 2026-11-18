import uuid
from typing import Annotated, Any

from pydantic import BaseModel, Field, StrictBool, StrictInt, StringConstraints, model_validator

from app.schemas.common import Strict

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=60)]
Category = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=30)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=140)]
# Same bounds as the DB check constraint and the mock: ₹1 to ₹5000, integer paise.
Price = Annotated[StrictInt, Field(ge=100, le=500_000)]
Prep = Annotated[StrictInt, Field(ge=1, le=90)]
Tagline = Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)]


class VendorPublic(BaseModel):
    """What anyone may see about a vendor. Never token_prefix / is_active / owner."""

    id: uuid.UUID
    slug: str
    name: str
    image_url: str | None
    is_open: bool
    tagline: str | None
    prep_minutes: int | None


class MenuItemOut(BaseModel):
    id: uuid.UUID
    vendor_id: uuid.UUID
    name: str
    description: str | None
    price_paise: int
    category: str
    is_veg: bool
    is_available: bool
    image_url: str | None


class VendorMenuOut(BaseModel):
    vendor: VendorPublic
    items: list[MenuItemOut]


class MenuItemIn(Strict):
    name: Name
    category: Category
    description: Description | None = None
    price_paise: Price
    is_veg: StrictBool
    is_available: StrictBool

    @model_validator(mode="after")
    def _blank_description(self) -> "MenuItemIn":
        if not self.description:
            self.description = None
        return self


# Columns that are NOT NULL: a PATCH may omit them but never null them.
_REQUIRED = ("name", "category", "price_paise", "is_veg", "is_available")


class MenuItemPatch(Strict):
    name: Name | None = None
    category: Category | None = None
    description: Description | None = None
    price_paise: Price | None = None
    is_veg: StrictBool | None = None
    is_available: StrictBool | None = None

    @model_validator(mode="after")
    def _no_null_required(self) -> "MenuItemPatch":
        for f in _REQUIRED:
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        if "description" in self.model_fields_set and not self.description:
            self.description = None
        return self

    def changes(self) -> dict[str, Any]:
        return {f: getattr(self, f) for f in self.model_fields_set}


class ShopPatch(Strict):
    is_open: StrictBool | None = None
    prep_minutes: Prep | None = None
    tagline: Tagline | None = None

    @model_validator(mode="after")
    def _no_null_open(self) -> "ShopPatch":
        if "is_open" in self.model_fields_set and self.is_open is None:
            raise ValueError("is_open cannot be null")
        if "tagline" in self.model_fields_set and not self.tagline:
            self.tagline = None
        return self

    def changes(self) -> dict[str, Any]:
        return {f: getattr(self, f) for f in self.model_fields_set}
