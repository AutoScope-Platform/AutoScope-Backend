"""Orders endpoints: cart, checkout, vehicle purchase and the order lifecycle."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Query, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import error_responses
from app.core.idempotency import run_idempotent
from app.core.permissions import Capability
from app.modules.identity.deps import CurrentUser, require_capability, user_rate_limit
from app.modules.identity.models import User
from app.modules.orders import service
from app.modules.orders.models import Order
from app.modules.orders.schemas import (
    CancelIn,
    CartItemIn,
    CartLineOut,
    CartOut,
    CartSellerOut,
    CheckoutIn,
    CheckoutOut,
    DeliverySettingsIn,
    DeliverySettingsOut,
    OrderItemOut,
    OrderOut,
    ShipIn,
    VehiclePurchaseIn,
)
from app.modules.payments import service as payments

router = APIRouter(tags=["Orders"])

BuyerUser = Depends(require_capability(Capability.PURCHASE))
PartsSeller = Depends(require_capability(Capability.LIST_PARTS))


def _order_out(db: Session, order: Order, tx=None) -> OrderOut:
    tx = tx or payments.get_transaction(db, order.transaction_id)
    return OrderOut(
        id=order.id, transaction_id=tx.id, ref=tx.ref, kind=tx.kind, status=tx.status,
        escrow_status=tx.escrow_status, amount_minor=tx.amount_minor, payment_due=tx.payment_due,
        buyer_id=order.buyer_id, seller_id=order.seller_id,
        items=[OrderItemOut(**{k: getattr(i, k) for k in OrderItemOut.model_fields}) for i in order.items],
        subtotal_minor=tx.amount_minor - order.delivery_fee_minor,
        delivery_fee_minor=order.delivery_fee_minor,
        delivery_address=order.delivery_address, delivery_city=order.delivery_city,
        contact_phone=order.contact_phone, tracking_note=order.tracking_note,
        shipped_at=order.shipped_at, created_at=order.created_at,
        cancel_until=payments.cancel_deadline(tx),
    )  # fmt: skip


# --- Cart --------------------------------------------------------------------------------
@router.get(
    "/cart",
    response_model=CartOut,
    summary="My cart, with delivery per seller",
    description="Pass `delivery_city` to see each seller's delivery fee for that city "
    "(FR-4.3). Without it the other-city fee is shown. Each seller becomes its own order.",
)
def get_cart(
    user: User = BuyerUser,
    db: Session = Depends(get_db),
    delivery_city: str | None = Query(None, max_length=80, examples=["Lagos"]),
) -> CartOut:
    lines = []
    for item, listing in service.get_cart(db, user):
        ok = listing.status.value == "live" and (
            listing.stock is None or item.quantity <= listing.stock
        )
        lines.append(
            CartLineOut(
                listing_id=listing.id, title=listing.title, unit_price_minor=listing.price_minor,
                quantity=item.quantity, line_total_minor=listing.price_minor * item.quantity,
                in_stock=ok,
            )
        )  # fmt: skip
    sellers = [
        CartSellerOut(
            seller_id=g["seller"].id, seller_name=g["seller"].display_name,
            subtotal_minor=g["subtotal_minor"], delivery_fee_minor=g["delivery_fee_minor"],
            free_delivery_over_minor=g["free_delivery_over_minor"],
        )
        for g in service.cart_by_seller(db, user, delivery_city)
    ]  # fmt: skip
    subtotal = sum(line.line_total_minor for line in lines)
    delivery = sum(s.delivery_fee_minor for s in sellers)
    return CartOut(
        items=lines, subtotal_minor=subtotal, delivery_city=delivery_city, sellers=sellers,
        delivery_minor=delivery, total_minor=subtotal + delivery,
    )  # fmt: skip


@router.get(
    "/me/delivery-settings",
    response_model=DeliverySettingsOut,
    summary="Parts seller: my delivery fees",
)
def my_delivery_settings(user: User = PartsSeller, db: Session = Depends(get_db)):
    settings = service.get_delivery_settings(db, user.id)
    if settings is None:  # never set: delivery is free
        return DeliverySettingsOut(same_city_fee_minor=0, other_city_fee_minor=0)
    return DeliverySettingsOut(
        same_city_fee_minor=settings.same_city_fee_minor,
        other_city_fee_minor=settings.other_city_fee_minor,
        free_over_minor=settings.free_over_minor,
    )


@router.put(
    "/me/delivery-settings",
    response_model=DeliverySettingsOut,
    summary="Parts seller: set my delivery fees (FR-4.3)",
    description="Applies to orders checked out from now on. Your city (profile, else the "
    "listing's) decides what counts as the same city.",
)
def set_delivery_settings(
    body: DeliverySettingsIn, user: User = PartsSeller, db: Session = Depends(get_db)
):
    service.set_delivery_settings(db, user, **body.model_dump())
    return my_delivery_settings(user, db)


@router.put(
    "/cart/items",
    response_model=CartOut,
    summary="Add a part to the cart or change its quantity (0 removes)",
    responses=error_responses(404, 422),
)
def set_cart_item(body: CartItemIn, user: User = BuyerUser, db: Session = Depends(get_db)):
    service.set_cart_item(db, user, body.listing_id, body.quantity)
    return get_cart(user, db, None)


@router.delete("/cart", status_code=status.HTTP_204_NO_CONTENT, summary="Empty my cart")
def clear_cart(user: User = BuyerUser, db: Session = Depends(get_db)) -> None:
    service.clear_cart(db, user)


# --- Checkout and purchase ---------------------------------------------------------------
@router.post(
    "/checkout",
    response_model=CheckoutOut,
    status_code=status.HTTP_201_CREATED,
    summary="Turn the cart into orders (one per seller)",
    description="Requires an `Idempotency-Key` header. Each order then needs paying with "
    "`POST /payments` using its `transaction_id`.",
    responses=error_responses(400, 422, 429),
    dependencies=[Depends(user_rate_limit("checkout", 10, 60))],
)
def do_checkout(
    body: CheckoutIn,
    user: User = BuyerUser,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    def operation() -> dict:
        orders = service.checkout(
            db, user, delivery_address=body.delivery_address,
            delivery_city=body.delivery_city, contact_phone=body.contact_phone,
        )  # fmt: skip
        return CheckoutOut(orders=[_order_out(db, o) for o in orders]).model_dump(mode="json")

    return run_idempotent(
        db, user.id, idempotency_key, "checkout", body.model_dump(mode="json"), operation
    )


@router.post(
    "/listings/{listing_id}/purchase",
    response_model=OrderOut,
    status_code=status.HTTP_201_CREATED,
    summary="Buy a vehicle (at list price or an accepted offer)",
    responses=error_responses(404, 422, 429),
    dependencies=[Depends(user_rate_limit("vehicle-purchase", 10, 60))],
)
def purchase_vehicle(
    listing_id: uuid.UUID,
    body: VehiclePurchaseIn,
    user: User = BuyerUser,
    db: Session = Depends(get_db),
):
    order = service.purchase_vehicle(
        db, user, listing_id, inquiry_id=body.inquiry_id, contact_phone=body.contact_phone
    )
    return _order_out(db, order)


# --- Order lifecycle ---------------------------------------------------------------------
@router.get(
    "/orders/{order_id}",
    response_model=OrderOut,
    summary="One order (buyer, seller or staff)",
    responses=error_responses(404),
)
def get_order(order_id: uuid.UUID, user: CurrentUser, db: Session = Depends(get_db)):
    order, tx = service.get_order(db, order_id, user)
    return _order_out(db, order, tx)


@router.post(
    "/orders/{order_id}/ship",
    response_model=OrderOut,
    summary="Seller: mark a paid order as shipped",
    responses=error_responses(404, 422),
)
def ship(order_id: uuid.UUID, body: ShipIn, user: CurrentUser, db: Session = Depends(get_db)):
    return _order_out(db, service.ship_order(db, user, order_id, body.tracking_note))


@router.post(
    "/orders/{order_id}/confirm-received",
    response_model=OrderOut,
    summary="Buyer: confirm the parts (or vehicle) arrived",
    description="Starts the dispute window; the seller is paid when it ends.",
    responses=error_responses(404, 422),
)
def confirm_received(order_id: uuid.UUID, user: CurrentUser, db: Session = Depends(get_db)):
    return _order_out(db, service.confirm_received(db, user, order_id))


@router.post(
    "/orders/{order_id}/cancel",
    response_model=OrderOut,
    summary="Cancel before shipping (paid orders are refunded)",
    responses=error_responses(404, 422),
)
def cancel(order_id: uuid.UUID, body: CancelIn, user: CurrentUser, db: Session = Depends(get_db)):
    return _order_out(db, service.cancel_order(db, user, order_id, body.reason))
