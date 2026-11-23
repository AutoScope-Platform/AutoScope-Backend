"""Orders and the shopping cart (FSD 3.3 and 3.5).

An order is the *details* of a parts order or a vehicle purchase: who ships what where.
The money and the status live on its `Transaction` (payments module); an order never
stores a status of its own, so the two can never disagree.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, Timestamps, UUIDPrimaryKey


class CartItem(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "cart_items"
    __table_args__ = (UniqueConstraint("user_id", "listing_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    listing_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"))
    quantity: Mapped[int] = mapped_column(Integer, default=1)


class DeliverySettings(UUIDPrimaryKey, Timestamps, Base):
    """How a parts seller charges for delivery (FR-4.3). One row per seller; a seller
    without a row delivers free, which is how every order worked before this existed."""

    __tablename__ = "delivery_settings"

    seller_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    same_city_fee_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    other_city_fee_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    # Orders from this seller at or above this subtotal deliver free. None = never free.
    free_over_minor: Mapped[int | None] = mapped_column(BigInteger)


class Order(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "orders"

    # One order per transaction. A checkout with items from several sellers creates one
    # order (and one transaction) per seller, so each seller is paid and disputed separately.
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT"), unique=True
    )
    buyer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    seller_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    delivery_address: Mapped[str] = mapped_column(String(300))
    delivery_city: Mapped[str | None] = mapped_column(String(80))
    contact_phone: Mapped[str | None] = mapped_column(String(20))
    delivery_fee_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    tracking_note: Mapped[str | None] = mapped_column(String(300))
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderItem(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "order_items"

    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    # Kept if the listing is later deleted, so the order history stays readable.
    listing_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("listings.id", ondelete="SET NULL")
    )
    # Snapshot of the listing at purchase time: later edits must not change what was bought.
    title: Mapped[str] = mapped_column(String(160))
    unit_price_minor: Mapped[int] = mapped_column(BigInteger)
    quantity: Mapped[int] = mapped_column(Integer, default=1)

    order: Mapped[Order] = relationship(back_populates="items")
