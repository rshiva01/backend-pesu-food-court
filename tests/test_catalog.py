from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.cli import _add_staff
from app.db.models import AuditLog, MenuItem, Vendor
from app.db.models.enums import Role
from app.db.session import get_session
from tests.helpers import FakeSms, code_at, login_student, make_vendor_staff, staff_login

ITEM = {
    "name": "Masala Dosa",
    "category": "Dosa",
    "description": "Crisp",
    "price_paise": 6000,
    "is_veg": True,
    "is_available": True,
}


@asynccontextmanager
async def vendor_client(
    make_client: Callable[[], AsyncClient], username: str, prefix: str
) -> AsyncIterator[AsyncClient]:
    await make_vendor_staff(username, f"{username}-pass-123", prefix, f"{username.title()} Hut")
    async with make_client() as c:
        assert await staff_login(c, username, f"{username}-pass-123", code_at()) == 200
        yield c


async def add_item(c: AsyncClient, **over: Any) -> dict[str, Any]:
    r = await c.post("/api/v1/vendor/menu", json={**ITEM, **over})
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


async def audit_actions() -> list[str]:
    async for db in get_session():
        return list(await db.scalars(select(AuditLog.action).order_by(AuditLog.at)))
    return []


# ------------------------------------------------------------------ public
async def test_public_list_hides_inactive_and_internal_fields(client: AsyncClient) -> None:
    await make_vendor_staff("dosa", "dosa-pass-123", "DOS", "Dosa Corner")
    await make_vendor_staff("gone", "gone-pass-123", "GON", "Gone Cafe")
    async for db in get_session():
        v = await db.scalar(select(Vendor).where(Vendor.slug == "gone-corner"))
        assert v is not None
        v.is_active = False
        await db.commit()
    r = await client.get("/api/v1/vendors")
    assert r.status_code == 200
    body = r.json()
    assert [v["slug"] for v in body] == ["dosa-corner"]
    assert set(body[0]) == {
        "id", "slug", "name", "image_url", "is_open", "tagline", "prep_minutes",
    }  # fmt: skip
    assert "DOS" not in r.text
    assert (await client.get("/api/v1/vendors/gone-corner/menu")).status_code == 404


async def test_menu_unknown_and_bad_slug(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/vendors/nope/menu")).status_code == 404
    assert (await client.get("/api/v1/vendors/Bad%20Slug/menu")).status_code == 422
    assert (await client.get("/api/v1/vendors/" + "a" * 41 + "/menu")).status_code == 422


async def test_menu_shows_items_including_sold_out(
    make_client: Callable[[], AsyncClient], client: AsyncClient
) -> None:
    async with vendor_client(make_client, "dosa", "DOS") as v:
        await add_item(v, name="Plain Dosa", price_paise=4500)
        await add_item(v, name="Kesari Bath", category="Tiffin", is_available=False)
    r = await client.get("/api/v1/vendors/dosa-corner/menu")
    assert r.status_code == 200
    body = r.json()
    assert body["vendor"]["slug"] == "dosa-corner" and "token_prefix" not in body["vendor"]
    assert [(i["name"], i["is_available"]) for i in body["items"]] == [
        ("Plain Dosa", True),
        ("Kesari Bath", False),
    ]


# ------------------------------------------------------------------ vendor CRUD
async def test_vendor_menu_crud_and_audit(make_client: Callable[[], AsyncClient]) -> None:
    async with vendor_client(make_client, "dosa", "DOS") as v:
        item = await add_item(v, description="   ")
        assert item["description"] is None and item["price_paise"] == 6000
        r = await v.patch(f"/api/v1/vendor/menu/{item['id']}", json={"price_paise": 6500})
        assert r.status_code == 200 and r.json()["price_paise"] == 6500
        r = await v.patch(f"/api/v1/vendor/menu/{item['id']}", json={"is_available": False})
        assert r.json()["is_available"] is False and r.json()["name"] == "Masala Dosa"
        assert [i["id"] for i in (await v.get("/api/v1/vendor/menu")).json()] == [item["id"]]
        assert (await v.delete(f"/api/v1/vendor/menu/{item['id']}")).status_code == 204
        assert (await v.get("/api/v1/vendor/menu")).json() == []
        assert (await v.delete(f"/api/v1/vendor/menu/{item['id']}")).status_code == 404
    assert [a for a in await audit_actions() if a.startswith("menu.")] == [
        "menu.create",
        "menu.update",
        "menu.update",
        "menu.delete",
    ]


async def test_vendor_cannot_touch_another_vendors_items(
    make_client: Callable[[], AsyncClient],
) -> None:
    async with (
        vendor_client(make_client, "dosa", "DOS") as a,
        vendor_client(make_client, "slice", "SLC") as b,
    ):
        theirs = await add_item(b, name="Margherita", price_paise=12000)
        url = f"/api/v1/vendor/menu/{theirs['id']}"
        assert (await a.patch(url, json={"price_paise": 100})).status_code == 404
        assert (await a.delete(url)).status_code == 404
        assert (await a.get("/api/v1/vendor/menu")).json() == []
        # vendor_id in the body can't smuggle an item into another shop
        r = await a.post("/api/v1/vendor/menu", json={**ITEM, "vendor_id": theirs["vendor_id"]})
        assert r.status_code == 422
        still = (await b.get("/api/v1/vendor/menu")).json()
        assert [(i["id"], i["price_paise"]) for i in still] == [(theirs["id"], 12000)]


async def test_role_checks(
    client: AsyncClient, sms: FakeSms, make_client: Callable[[], AsyncClient]
) -> None:
    assert (await client.get("/api/v1/vendor/menu")).status_code == 401
    await login_student(client, sms)
    assert (await client.get("/api/v1/vendor/menu")).status_code == 401
    assert (await client.post("/api/v1/vendor/menu", json=ITEM)).status_code == 401
    await _add_staff("boss", "boss-pass-1234", Role.admin, "Admin", "A" * 32)
    async with make_client() as adm:
        assert await staff_login(adm, "boss", "boss-pass-1234", pyotp.TOTP("A" * 32).now()) == 200
        assert (await adm.get("/api/v1/vendor/menu")).status_code == 403
        assert (await adm.patch("/api/v1/vendor/shop", json={"is_open": True})).status_code == 403


@pytest.mark.parametrize(
    "over",
    [
        {"price_paise": 99},
        {"price_paise": 500_001},
        {"price_paise": 60.5},
        {"price_paise": "6000"},
        {"price_paise": True},
        {"name": "x"},
        {"name": "x" * 61},
        {"category": ""},
        {"description": "d" * 141},
        {"is_veg": "yes"},
        {"extra": 1},
    ],
)
async def test_menu_validation(
    make_client: Callable[[], AsyncClient], over: dict[str, Any]
) -> None:
    async with vendor_client(make_client, "dosa", "DOS") as v:
        r = await v.post("/api/v1/vendor/menu", json={**ITEM, **over})
        assert r.status_code == 422
        assert isinstance(r.json()["detail"], str)
        assert "6000" not in r.text or over.get("price_paise") == "6000"  # no value echo
    async for db in get_session():
        assert await db.scalar(select(func.count()).select_from(MenuItem)) == 0


async def test_patch_rejects_null_and_bad_id(make_client: Callable[[], AsyncClient]) -> None:
    async with vendor_client(make_client, "dosa", "DOS") as v:
        item = await add_item(v)
        url = f"/api/v1/vendor/menu/{item['id']}"
        assert (await v.patch(url, json={"name": None})).status_code == 422
        assert (await v.patch(url, json={"price_paise": None})).status_code == 422
        r = await v.patch(url, json={"price_paise": 50})
        assert r.status_code == 422 and r.json()["detail"].startswith("Price must be")
        r = await v.patch(url, json={"description": None})
        assert r.status_code == 200 and r.json()["description"] is None
        assert (await v.patch("/api/v1/vendor/menu/not-a-uuid", json={})).status_code == 422


async def test_csrf_header_required(make_client: Callable[[], AsyncClient]) -> None:
    async with vendor_client(make_client, "dosa", "DOS") as v:
        r = await v.post("/api/v1/vendor/menu", json=ITEM, headers={"X-Requested-With": "nope"})
        assert r.status_code == 403


# ------------------------------------------------------------------ shop
async def test_shop_patch(make_client: Callable[[], AsyncClient], client: AsyncClient) -> None:
    async with vendor_client(make_client, "dosa", "DOS") as v:
        r = await v.patch(
            "/api/v1/vendor/shop",
            json={"is_open": False, "prep_minutes": 15, "tagline": "  Hot dosas  "},
        )
        assert r.status_code == 200, r.text
        assert r.json()["tagline"] == "Hot dosas" and "token_prefix" not in r.json()
        pub = (await client.get("/api/v1/vendors")).json()[0]
        assert pub["is_open"] is False and pub["prep_minutes"] == 15
        for bad, msg in [
            ({"prep_minutes": 0}, "Prep time"),
            ({"prep_minutes": 91}, "Prep time"),
            ({"tagline": "t" * 61}, "Tagline"),
            ({"is_open": None}, None),
            ({"name": "Hacked"}, None),
            ({"token_prefix": "ZZZ"}, None),
        ]:
            r = await v.patch("/api/v1/vendor/shop", json=bad)
            assert r.status_code == 422, bad
            if msg:
                assert r.json()["detail"].startswith(msg)
    assert "shop.update" in await audit_actions()
