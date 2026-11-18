from fastapi import APIRouter

from app.api.v1 import auth, catalog, health, staff, vendor_console

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
router.include_router(auth.router)
router.include_router(auth.staff_router)
router.include_router(staff.router)
router.include_router(catalog.router)
router.include_router(vendor_console.router)
# milestone 4+: orders, vendor/*, admin/*, payments, events
