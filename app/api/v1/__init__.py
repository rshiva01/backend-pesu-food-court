from fastapi import APIRouter

from app.api.v1 import auth, catalog, health, orders, payments, staff, vendor_console

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
router.include_router(auth.router)
router.include_router(auth.staff_router)
router.include_router(staff.router)
router.include_router(catalog.router)
router.include_router(vendor_console.router)
router.include_router(orders.router)
router.include_router(payments.router)
# milestone 6+: admin/*, events (SSE)
