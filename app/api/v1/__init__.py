from fastapi import APIRouter

from app.api.v1 import auth, health, staff

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
router.include_router(auth.router)
router.include_router(auth.staff_router)
router.include_router(staff.router)
# milestone 3+: vendors, orders, vendor/*, admin/*, payments, events
