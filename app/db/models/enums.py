import enum


class Role(enum.StrEnum):
    student = "student"
    vendor = "vendor"
    admin = "admin"


class OrderStatus(enum.StrEnum):
    pending_payment = "pending_payment"  # online, waiting for verified webhook
    awaiting_payment = "awaiting_payment"  # pay at counter, token issued
    confirmed = "confirmed"
    preparing = "preparing"
    ready = "ready"
    collected = "collected"
    cancelled = "cancelled"
    refunded = "refunded"


class PaymentMode(enum.StrEnum):
    online = "online"
    counter = "counter"


class PaymentStatus(enum.StrEnum):
    unpaid = "unpaid"
    paid = "paid"
    refunded = "refunded"


class GatewayPaymentStatus(enum.StrEnum):
    created = "created"
    captured = "captured"
    failed = "failed"
    refunded = "refunded"


# Vendor-driven transitions (mark-paid and collect have their own endpoints).
# Mirrors NEXT in frontend/mocks/handlers.ts — keep the two in sync.
VENDOR_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.confirmed: frozenset({OrderStatus.preparing, OrderStatus.cancelled}),
    OrderStatus.preparing: frozenset({OrderStatus.ready, OrderStatus.cancelled}),
    OrderStatus.awaiting_payment: frozenset({OrderStatus.cancelled}),
}
