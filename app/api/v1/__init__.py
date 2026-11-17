from fastapi import APIRouter

from app.api.v1 import health

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
# milestone 2+: auth, vendors, orders, vendor/*, admin/*, payments, events
