"""Request and response bodies for the orders module."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.modules.payments.models import EscrowStatus, TransactionKind, TxStatus


class CartItemIn(BaseModel):
    listing_id: uuid.UUID
    quantity: int = Field(ge=0, le=99, description="0 removes the item.")


class CartLineOut(BaseModel):
    listing_id: uuid.UUID
    title: str
    unit_price_minor: int
    quantity: int
    line_total_minor: int
    in_stock: bool = Field(description="False when the listing is gone or has too little stock.")


class CartSellerOut(BaseModel):
    """One seller's part of the cart: each seller becomes a separate order at checkout."""

    seller_id: uuid.UUID
    seller_name: str
    subtotal_minor: int
    delivery_fee_minor: int
    free_delivery_over_minor: int | None = Field(
        None, description="Spend this much with this seller for free delivery."
    )


class CartOut(BaseModel):
    items: list[CartLineOut]
    subtotal_minor: int
    delivery_city: str | None = Field(None, description="The city the fees were worked out for.")
    sellers: list[CartSellerOut] = Field(default_factory=list)
    delivery_minor: int = Field(0, description="All sellers' delivery fees together.")
    total_minor: int = Field(0, description="subtotal_minor + delivery_minor.")


class DeliverySettingsIn(BaseModel):
    same_city_fee_minor: int = Field(ge=0, description="Delivery within your city, in kobo.")
    other_city_fee_minor: int = Field(ge=0, description="Delivery to other cities, in kobo.")
    free_over_minor: int | None = Field(
        None, ge=0, description="Orders at or above this subtotal deliver free. Null = never."
    )


class DeliverySettingsOut(DeliverySettingsIn):
    pass


class CheckoutIn(BaseModel):
    delivery_address: str = Field(min_length=5, max_length=300, examples=["12 Allen Ave, Ikeja"])
    delivery_city: str | None = Field(None, max_length=80)
    contact_phone: str | None = Field(None, max_length=20)


class VehiclePurchaseIn(BaseModel):
    inquiry_id: uuid.UUID | None = Field(None, description="An accepted offer to buy at.")
    contact_phone: str | None = Field(None, max_length=20)


class OrderItemOut(BaseModel):
    listing_id: uuid.UUID | None
    title: str
    unit_price_minor: int
    quantity: int


class OrderOut(BaseModel):
    id: uuid.UUID
    transaction_id: uuid.UUID
    ref: str = Field(examples=["ORD-80312"])
    kind: TransactionKind
    status: TxStatus
    escrow_status: EscrowStatus
    amount_minor: int
    payment_due: bool
    buyer_id: uuid.UUID
    seller_id: uuid.UUID
    items: list[OrderItemOut]
    subtotal_minor: int = Field(0, description="The items, before delivery.")
    delivery_fee_minor: int = 0
    delivery_address: str
    delivery_city: str | None
    contact_phone: str | None
    tracking_note: str | None
    shipped_at: datetime | None
    cancel_until: datetime | None = Field(
        None, description="The buyer can cancel until then (seller's window). Null = until shipped."
    )
    created_at: datetime


class CheckoutOut(BaseModel):
    orders: list[OrderOut]


class ShipIn(BaseModel):
    tracking_note: str | None = Field(
        None, max_length=300, examples=["GIG Logistics, waybill 4471"]
    )


class CancelIn(BaseModel):
    reason: str | None = Field(None, max_length=200)
