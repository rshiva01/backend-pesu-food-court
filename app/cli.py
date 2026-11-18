"""Admin commands (run with: uv run python -m app.cli <command>).

create-admin <username>  prompts for password, prints TOTP setup URI once
seed-dev                 dev only: mock vendors + menus + staff (dosa/dosa1234, admin/admin1234)
totp <username>          dev only: current TOTP code for a seeded account
"""

import argparse
import asyncio
import getpass
import sys

import pyotp
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import decrypt_secret, encrypt_secret, hash_password
from app.db.models import MenuItem, StaffAccount, User, Vendor
from app.db.models.enums import Role
from app.db.session import dispose_engine, get_engine, get_session

# Same items as frontend/mocks/fixtures.ts (rupees; converted to paise on insert).
# name, rupees, category, description, is_veg, is_available
DEV_MENUS: dict[str, list[tuple[str, int, str, str | None, bool, bool]]] = {
    "dosa-corner": [
        ("Masala Dosa", 60, "Dosa", "Crisp dosa, potato palya, chutney & sambar", True, True),
        ("Plain Dosa", 45, "Dosa", None, True, True),
        ("Onion Uttapam", 65, "Dosa", None, True, True),
        ("Idli Vada", 50, "Tiffin", "2 idli + 1 vada", True, True),
        ("Kesari Bath", 35, "Tiffin", None, True, False),
        ("Filter Coffee", 20, "Drinks", None, True, True),
        ("Masala Chai", 15, "Drinks", None, True, True),
    ],
    "slice-house": [
        ("Margherita", 120, "Pizza", None, True, True),
        ("Paneer Tikka Pizza", 160, "Pizza", None, True, True),
        ("Chicken Pepperoni", 180, "Pizza", None, False, True),
        ("Garlic Bread", 70, "Sides", None, True, True),
        ("Cold Coffee", 60, "Drinks", None, True, True),
    ],
    "chaat-street": [
        ("Pani Puri", 30, "Chaat", None, True, True),
        ("Masala Puri", 40, "Chaat", None, True, True),
        ("Bhel Puri", 40, "Chaat", None, True, True),
        ("Sev Puri", 45, "Chaat", None, True, True),
    ],
    "juice-bar": [
        ("Watermelon Juice", 40, "Juices", None, True, True),
        ("Oreo Shake", 80, "Shakes", None, True, True),
    ],
}

# Shared, publicly known secret: dev databases only (seed-dev refuses prod).
DEV_TOTP_SECRET = "PFCDEVTOTPSECRETAAAAAAAAAAAAAAAA"  # noqa: S105  # nosec B105

DEV_VENDORS = [
    # slug, name, prefix, tagline, prep, is_open, image, staff username/password
    (
        "dosa-corner",
        "Dosa Corner",
        "DOS",
        "Dosas, idli, filter coffee",
        8,
        True,
        "/images/foodImage.png",
        "dosa",
    ),
    (
        "slice-house",
        "Slice House",
        "SLC",
        "Pizzas & garlic bread",
        12,
        True,
        "/images/pizza.png",
        "slice",
    ),
    (
        "chaat-street",
        "Chaat Street",
        "CHT",
        "Pani puri, bhel, masala puri",
        5,
        True,
        "/images/foodImage.png",
        "chaat",
    ),
    (
        "juice-bar",
        "Juice Bar",
        "JCE",
        "Fresh juices & shakes",
        4,
        False,
        "/images/foodImage.png",
        "juice",
    ),
]


def _dev_only() -> None:
    if get_settings().env == "production":
        sys.exit("refusing: dev-only command in production")


async def _add_staff(
    username: str, password: str, role: Role, name: str, totp_secret: str
) -> User | None:
    async for db in get_session():
        if await db.scalar(select(StaffAccount).where(StaffAccount.username == username)):
            return None
        user = User(role=role, name=name)
        db.add(user)
        await db.flush()
        db.add(
            StaffAccount(
                user_id=user.id,
                username=username,
                password_hash=hash_password(password),
                totp_secret_enc=encrypt_secret(totp_secret),
                must_change_password=False,
            )
        )
        await db.commit()
        return user
    return None


async def create_admin(username: str) -> None:
    username = username.strip().lower()
    pw = getpass.getpass("New admin password (min 12 chars): ")
    if len(pw) < 12 or pw != getpass.getpass("Repeat: "):
        sys.exit("passwords too short or don't match")
    secret = pyotp.random_base32()
    if await _add_staff(username, pw, Role.admin, "Food court admin", secret) is None:
        sys.exit(f"username {username!r} already exists")
    uri = pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name="PESU Food Court")
    print("Admin created. Add this to an authenticator app now (shown once):")
    print(uri)


async def seed_dev() -> None:
    _dev_only()
    await _add_staff("admin", "admin1234", Role.admin, "Food court admin", DEV_TOTP_SECRET)
    for slug, name, prefix, tagline, prep, is_open, img, uname in DEV_VENDORS:
        owner = await _add_staff(
            uname, f"{uname}1234", Role.vendor, f"{name} staff", DEV_TOTP_SECRET
        )
        async for db in get_session():
            if await db.scalar(select(Vendor).where(Vendor.slug == slug)):
                break
            db.add(
                Vendor(
                    slug=slug,
                    name=name,
                    token_prefix=prefix,
                    tagline=tagline,
                    prep_minutes=prep,
                    is_open=is_open,
                    image_url=img,
                    owner_user_id=owner.id if owner else None,
                )
            )
            await db.commit()
    await _seed_menus()
    print("Seeded. Staff passwords: admin/admin1234, dosa/dosa1234, slice/slice1234 …")
    print("TOTP code: uv run python -m app.cli totp dosa")


async def _seed_menus() -> None:
    async for db in get_session():
        for slug, rows in DEV_MENUS.items():
            v = await db.scalar(select(Vendor).where(Vendor.slug == slug))
            if v is None or await db.scalar(select(MenuItem.id).where(MenuItem.vendor_id == v.id)):
                continue  # idempotent: never touch a menu that already has items
            for name, rupees, cat, desc, veg, avail in rows:
                db.add(
                    MenuItem(
                        vendor_id=v.id,
                        name=name,
                        price_paise=rupees * 100,
                        category=cat,
                        description=desc,
                        is_veg=veg,
                        is_available=avail,
                    )
                )
        await db.commit()


async def totp(username: str) -> None:
    _dev_only()
    async for db in get_session():
        acct = await db.scalar(select(StaffAccount).where(StaffAccount.username == username))
        if acct is None:
            sys.exit("no such user")
        print(pyotp.TOTP(decrypt_secret(acct.totp_secret_enc)).now())


def main() -> None:
    p = argparse.ArgumentParser(prog="app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("create-admin").add_argument("username")
    sub.add_parser("seed-dev")
    sub.add_parser("totp").add_argument("username")
    a = p.parse_args()

    async def run() -> None:
        get_engine()
        try:
            if a.cmd == "create-admin":
                await create_admin(a.username)
            elif a.cmd == "seed-dev":
                await seed_dev()
            else:
                await totp(a.username)
        finally:
            await dispose_engine()

    asyncio.run(run())


if __name__ == "__main__":
    main()
