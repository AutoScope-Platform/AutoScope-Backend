"""Orders business logic: cart, checkout, vehicle purchase and the order lifecycle.

Lifecycle of a parts order (status lives on the transaction):
    pending --paid--> confirmed --seller ships--> shipped --buyer confirms--> delivered
Cancelling is possible before shipping; a paid order is refunded from escrow.
A vehicle purchase skips shipping: pending -> confirmed -> completed (buyer confirms
handover) and the listing goes reserved -> sold.

Stock is taken at checkout (under a row lock so two buyers cannot take the last unit)
and given back on cancellation.

Delivery (FR-4.3): each seller's order adds that seller's delivery fee, worked out from
their `DeliverySettings` and the buyer's delivery city. The fee is part of the order's
transaction amount, so it is paid, held in escrow and refunded with the parts.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import db as db_module
from app.core.db import utcnow
from app.core.errors import BadRequest, NotFound, UnprocessableEntity
from app.core.events import Event, publish, subscribe
from app.core.permissions import ADMIN_ROLES
from app.modules.catalog import service as catalog
from app.modules.catalog.models import (
    InquiryStatus,
    Listing,
    ListingInquiry,
    ListingStatus,
    ListingType,
)
from app.modules.identity.models import User
from app.modules.orders.models import CartItem, DeliverySettings, Order, OrderItem
from app.modules.payments import service as payments
from app.modules.payments.models import FINAL_STATUSES, Transaction, TransactionKind, TxStatus

# Shipped orders the buyer never confirmed are completed automatically after this long.
AUTO_CONFIRM_AFTER = timedelta(days=7)


# ======================================================================================
# Cart
# ======================================================================================
def _cart_listing(db: Session, buyer: User, listing_id: uuid.UUID) -> Listing:
    listing = db.get(Listing, listing_id)
    if listing is None or listing.status != ListingStatus.LIVE or listing.type != ListingType.PART:
        raise NotFound("This part is not available", code="listing_unavailable")
    if listing.seller_id == buyer.id:
        raise UnprocessableEntity("You cannot buy your own listing", code="own_listing")
    return listing


def set_cart_item(db: Session, buyer: User, listing_id: uuid.UUID, quantity: int) -> None:
    """Add a part, or change its quantity. Quantity 0 removes it."""
    item = db.scalar(
        select(CartItem).where(CartItem.user_id == buyer.id, CartItem.listing_id == listing_id)
    )
    if quantity <= 0:
        if item:
            db.delete(item)
        db.commit()
        return
    listing = _cart_listing(db, buyer, listing_id)
    if listing.stock is not None and quantity > listing.stock:
        raise UnprocessableEntity(
            f"Only {listing.stock} in stock",
            code="insufficient_stock",
            details={"stock": listing.stock},
        )
    if item is None:
        item = CartItem(user_id=buyer.id, listing_id=listing_id)
        db.add(item)
    item.quantity = quantity
    db.commit()


def get_cart(db: Session, buyer: User) -> list[tuple[CartItem, Listing]]:
    rows = db.execute(
        select(CartItem, Listing)
        .join(Listing, Listing.id == CartItem.listing_id)
        .where(CartItem.user_id == buyer.id)
        .order_by(CartItem.created_at)
    ).all()
    return [(c, listing) for c, listing in rows]


def clear_cart(db: Session, buyer: User) -> None:
    for item in db.scalars(select(CartItem).where(CartItem.user_id == buyer.id)):
        db.delete(item)
    db.commit()


# ======================================================================================
# Delivery fees (FR-4.3)
# ======================================================================================
def get_delivery_settings(db: Session, seller_id: uuid.UUID) -> DeliverySettings | None:
    return db.scalar(select(DeliverySettings).where(DeliverySettings.seller_id == seller_id))


def set_delivery_settings(
    db: Session,
    seller: User,
    *,
    same_city_fee_minor: int,
    other_city_fee_minor: int,
    free_over_minor: int | None,
) -> DeliverySettings:
    settings = get_delivery_settings(db, seller.id) or DeliverySettings(seller_id=seller.id)
    settings.same_city_fee_minor = same_city_fee_minor
    settings.other_city_fee_minor = other_city_fee_minor
    settings.free_over_minor = free_over_minor
    db.add(settings)
    db.commit()
    return settings


def _same_city(a: str | None, b: str | None) -> bool:
    return bool(a and b and a.strip().casefold() == b.strip().casefold())


def delivery_fee(
    settings: DeliverySettings | None,
    *,
    seller_city: str | None,
    delivery_city: str | None,
    subtotal_minor: int,
) -> int:
    """The fee for one seller's part of an order. Without a delivery city the other-city
    fee applies, so the buyer is never charged less than the seller expects."""
    if settings is None:
        return 0
    if settings.free_over_minor is not None and subtotal_minor >= settings.free_over_minor:
        return 0
    if _same_city(seller_city, delivery_city):
        return settings.same_city_fee_minor
    return settings.other_city_fee_minor


def _seller_city(seller: User | None, listings: list[Listing]) -> str | None:
    """Where the seller ships from: their profile city, else the city on their listing."""
    if seller and seller.city:
        return seller.city
    return next((listing.city for listing in listings if listing.city), None)


def cart_by_seller(db: Session, buyer: User, delivery_city: str | None) -> list[dict]:
    """The cart grouped per seller with each seller's delivery fee, for the cart screen."""
    groups: dict[uuid.UUID, list[tuple[CartItem, Listing]]] = {}
    for item, listing in get_cart(db, buyer):
        groups.setdefault(listing.seller_id, []).append((item, listing))
    out = []
    for seller_id, rows in groups.items():
        seller = db.get(User, seller_id)
        settings = get_delivery_settings(db, seller_id)
        subtotal = sum(listing.price_minor * item.quantity for item, listing in rows)
        out.append(
            {
                "seller": seller,
                "subtotal_minor": subtotal,
                "delivery_fee_minor": delivery_fee(
                    settings,
                    seller_city=_seller_city(seller, [listing for _, listing in rows]),
                    delivery_city=delivery_city,
                    subtotal_minor=subtotal,
                ),
                "free_delivery_over_minor": settings.free_over_minor if settings else None,
            }
        )
    return out


# ======================================================================================
# Checkout
# ======================================================================================
def checkout(
    db: Session,
    buyer: User,
    *,
    delivery_address: str,
    delivery_city: str | None,
    contact_phone: str | None,
) -> list[Order]:
    """Turn the cart into one order per seller. Does NOT commit (see `run_idempotent`)."""
    cart = get_cart(db, buyer)
    if not cart:
        raise BadRequest("Your cart is empty", code="empty_cart")

    # Lock listings in a fixed order so two checkouts cannot deadlock each other.
    locked = {
        listing.id: listing
        for listing in db.scalars(
            select(Listing)
            .where(Listing.id.in_([c.listing_id for c, _ in cart]))
            .order_by(Listing.id)
            .with_for_update()
        )
    }
    by_seller: dict[uuid.UUID, list[tuple[Listing, int]]] = {}
    for item, _ in cart:
        listing = locked[item.listing_id]
        if listing.status != ListingStatus.LIVE or listing.type != ListingType.PART:
            raise UnprocessableEntity(
                f"“{listing.title}” is no longer available", code="listing_unavailable"
            )
        if listing.stock is not None and item.quantity > listing.stock:
            raise UnprocessableEntity(
                f"Only {listing.stock} of “{listing.title}” left", code="insufficient_stock"
            )
        by_seller.setdefault(listing.seller_id, []).append((listing, item.quantity))

    orders: list[Order] = []
    for seller_id, lines in by_seller.items():
        subtotal = sum(listing.price_minor * qty for listing, qty in lines)
        fee = delivery_fee(
            get_delivery_settings(db, seller_id),
            seller_city=_seller_city(db.get(User, seller_id), [listing for listing, _ in lines]),
            delivery_city=delivery_city,
            subtotal_minor=subtotal,
        )
        tx = payments.create_transaction(
            db,
            kind=TransactionKind.PARTS_ORDER,
            customer_id=buyer.id,
            provider_id=seller_id,
            amount_minor=subtotal + fee,
            payment_due=True,
        )
        order = Order(
            transaction_id=tx.id,
            buyer_id=buyer.id,
            seller_id=seller_id,
            delivery_address=delivery_address.strip(),
            delivery_city=delivery_city,
            contact_phone=contact_phone,
            delivery_fee_minor=fee,
        )
        for listing, qty in lines:
            order.items.append(
                OrderItem(
                    listing_id=listing.id,
                    title=listing.title,
                    unit_price_minor=listing.price_minor,
                    quantity=qty,
                )
            )
            _take_stock(db, listing, qty)
        db.add(order)
        orders.append(order)
    for item, _ in cart:
        db.delete(item)
    db.flush()
    return orders


def _take_stock(db: Session, listing: Listing, quantity: int) -> None:
    if listing.stock is not None:
        listing.stock -= quantity
        if listing.stock == 0:
            listing.status = ListingStatus.SOLD  # out of stock
        catalog.note_stock_change(db, listing)  # low-stock alert after the commit (FR-4.5)


def _return_stock(db: Session, listing: Listing, quantity: int) -> None:
    if listing.stock is not None:
        listing.stock += quantity
        if listing.status == ListingStatus.SOLD:
            listing.status = ListingStatus.LIVE
        catalog.note_stock_change(db, listing)


def purchase_vehicle(
    db: Session,
    buyer: User,
    listing_id: uuid.UUID,
    *,
    inquiry_id: uuid.UUID | None,
    contact_phone: str | None,
) -> Order:
    """Buy a listed vehicle at its price, or at the price of an offer the seller accepted.

    The listing is reserved so nobody else can buy it while this purchase is pending.
    """
    listing = db.scalar(select(Listing).where(Listing.id == listing_id).with_for_update())
    if (
        listing is None
        or listing.type != ListingType.VEHICLE
        or listing.status != ListingStatus.LIVE
    ):
        raise NotFound("This vehicle is not available", code="listing_unavailable")
    if listing.seller_id == buyer.id:
        raise UnprocessableEntity("You cannot buy your own listing", code="own_listing")
    price = listing.price_minor
    if inquiry_id:
        offer = db.get(ListingInquiry, inquiry_id)
        if (
            offer is None
            or offer.listing_id != listing.id
            or offer.buyer_id != buyer.id
            or offer.status != InquiryStatus.ACCEPTED
            or offer.offer_minor is None
        ):
            raise UnprocessableEntity("That offer was not accepted", code="invalid_offer")
        price = offer.offer_minor
    tx = payments.create_transaction(
        db,
        kind=TransactionKind.VEHICLE_SALE,
        customer_id=buyer.id,
        provider_id=listing.seller_id,
        amount_minor=price,
        payment_due=True,
    )
    order = Order(
        transaction_id=tx.id,
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        delivery_address=listing.city or "Collect from seller",
        delivery_city=listing.city,
        contact_phone=contact_phone,
    )
    order.items.append(
        OrderItem(listing_id=listing.id, title=listing.title, unit_price_minor=price, quantity=1)
    )
    listing.status = ListingStatus.RESERVED
    db.add(order)
    db.commit()
    publish("order.created", order_id=str(order.id), seller_id=str(order.seller_id), ref=tx.ref)
    return order


# ======================================================================================
# Reading
# ======================================================================================
def get_order(db: Session, order_id: uuid.UUID, viewer: User) -> tuple[Order, Transaction]:
    order = db.get(Order, order_id)
    if order is None or not (
        viewer.id in (order.buyer_id, order.seller_id) or viewer.has_role(*ADMIN_ROLES)
    ):
        raise NotFound("Order not found", code="order_not_found")
    return order, payments.get_transaction(db, order.transaction_id)


def _order_and_tx_locked(db: Session, order_id: uuid.UUID) -> tuple[Order, Transaction]:
    order = db.get(Order, order_id)
    if order is None:
        raise NotFound("Order not found", code="order_not_found")
    return order, payments.get_transaction(db, order.transaction_id, lock=True)


def _is_vehicle(tx: Transaction) -> bool:
    return tx.kind == TransactionKind.VEHICLE_SALE


# ======================================================================================
# Lifecycle
# ======================================================================================
@subscribe("payment.succeeded")
def _on_payment_succeeded(event: Event) -> None:
    """Move a paid order from 'pending' to 'confirmed' and tell the seller."""
    if event.payload.get("kind") not in (
        TransactionKind.PARTS_ORDER.value,
        TransactionKind.VEHICLE_SALE.value,
    ):
        return
    db = db_module.new_session()  # looked up at call time so tests can swap it
    try:
        tx = db.get(Transaction, uuid.UUID(event.payload["transaction_id"]))
        if tx and tx.status == TxStatus.PENDING:
            payments.set_status(db, tx, TxStatus.CONFIRMED)
            db.commit()
            order = db.scalar(select(Order).where(Order.transaction_id == tx.id))
            if order:
                publish(
                    "order.paid", order_id=str(order.id), seller_id=str(order.seller_id), ref=tx.ref
                )
    finally:
        db.close()


def ship_order(db: Session, seller: User, order_id: uuid.UUID, tracking_note: str | None) -> Order:
    order, tx = _order_and_tx_locked(db, order_id)
    if order.seller_id != seller.id:
        raise NotFound("Order not found", code="order_not_found")
    if _is_vehicle(tx):
        raise UnprocessableEntity("Vehicle sales are not shipped", code="not_shippable")
    # `paid_at` is the truth; the status may lag a moment behind it.
    if tx.paid_at is None or tx.status not in (TxStatus.PENDING, TxStatus.CONFIRMED):
        raise UnprocessableEntity("This order cannot be shipped now", code="invalid_status")
    order.shipped_at, order.tracking_note = utcnow(), tracking_note
    payments.set_status(db, tx, TxStatus.SHIPPED)
    db.commit()
    publish("order.shipped", order_id=str(order.id), buyer_id=str(order.buyer_id), ref=tx.ref)
    return order


def confirm_received(db: Session, buyer: User, order_id: uuid.UUID) -> Order:
    """The buyer confirms they have the parts (or the car). This starts the dispute window,
    after which the escrow pays the seller."""
    order, tx = _order_and_tx_locked(db, order_id)
    if order.buyer_id != buyer.id:
        raise NotFound("Order not found", code="order_not_found")
    _complete(db, order, tx)
    db.commit()
    publish("order.completed", order_id=str(order.id), seller_id=str(order.seller_id), ref=tx.ref)
    return order


def _complete(db: Session, order: Order, tx: Transaction) -> None:
    """Mark the sale done. Vehicles complete from confirmed; parts must have shipped."""
    ready = (TxStatus.PENDING, TxStatus.CONFIRMED) if _is_vehicle(tx) else (TxStatus.SHIPPED,)
    if tx.paid_at is None or tx.status not in ready:
        raise UnprocessableEntity("This order cannot be confirmed yet", code="invalid_status")
    if _is_vehicle(tx):
        payments.mark_completed(db, tx, TxStatus.COMPLETED)
        for item in order.items:
            listing = db.get(Listing, item.listing_id) if item.listing_id else None
            if listing:
                listing.status = ListingStatus.SOLD
    else:
        payments.mark_completed(db, tx, TxStatus.DELIVERED)


def cancel_order(db: Session, user: User, order_id: uuid.UUID, reason: str | None) -> Order:
    """Cancel before shipping. The buyer or the seller may; a paid order is refunded in full."""
    order, tx = _order_and_tx_locked(db, order_id)
    if user.id not in (order.buyer_id, order.seller_id):
        raise NotFound("Order not found", code="order_not_found")
    if tx.status in FINAL_STATUSES or tx.status not in (TxStatus.PENDING, TxStatus.CONFIRMED):
        raise UnprocessableEntity("This order can no longer be cancelled", code="invalid_status")
    deadline = payments.cancel_deadline(tx)
    if user.id == order.buyer_id and deadline is not None and utcnow() > deadline:
        # The seller's cancellation window (FR-10.3) has passed; the seller can still cancel.
        raise UnprocessableEntity(
            "The seller's cancellation window has passed. Contact the seller or open a dispute.",
            code="cancellation_window_passed",
            details={"deadline": deadline.isoformat()},
        )
    payments.cancel_and_refund(db, tx, reason=reason or "Order cancelled")
    for item in order.items:
        listing = (
            db.scalar(select(Listing).where(Listing.id == item.listing_id).with_for_update())
            if item.listing_id
            else None
        )
        if listing is None:
            continue
        if listing.type == ListingType.VEHICLE:
            if listing.status == ListingStatus.RESERVED:
                listing.status = ListingStatus.LIVE
        else:
            _return_stock(db, listing, item.quantity)
    db.commit()
    other = order.seller_id if user.id == order.buyer_id else order.buyer_id
    publish("order.cancelled", order_id=str(order.id), notify_id=str(other), ref=tx.ref)
    return order


def auto_confirm_shipped(db: Session, *, now=None) -> int:
    """Scheduled job: complete orders shipped more than 7 days ago that nobody disputed."""
    cutoff = (now or utcnow()) - AUTO_CONFIRM_AFTER
    count = 0
    for order in db.scalars(
        select(Order).where(Order.shipped_at.is_not(None), Order.shipped_at <= cutoff)
    ):
        tx = payments.get_transaction(db, order.transaction_id, lock=True)
        if tx.status == TxStatus.SHIPPED:
            payments.mark_completed(db, tx, TxStatus.DELIVERED)
            count += 1
    db.commit()
    return count


@payments.receipt_lines(TransactionKind.PARTS_ORDER, TransactionKind.VEHICLE_SALE)
def _receipt_lines(db: Session, tx: Transaction) -> list[tuple[str, int]]:
    """Itemised receipt (FR-9.3): each item, then delivery."""
    order = db.scalar(select(Order).where(Order.transaction_id == tx.id))
    if order is None:
        return []
    lines = [
        (
            f"{i.title} x {i.quantity}" if i.quantity > 1 else i.title,
            i.unit_price_minor * i.quantity,
        )
        for i in order.items
    ]
    if order.delivery_fee_minor:
        lines.append(("Delivery", order.delivery_fee_minor))
    return lines
