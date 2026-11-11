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


class CartOut(BaseModel):
    items: list[CartLineOut]
    subtotal_minor: int


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
    delivery_address: str
    delivery_city: str | None
    contact_phone: str | None
    tracking_note: str | None
    shipped_at: datetime | None
    created_at: datetime


class CheckoutOut(BaseModel):
    orders: list[OrderOut]


class ShipIn(BaseModel):
    tracking_note: str | None = Field(
        None, max_length=300, examples=["GIG Logistics, waybill 4471"]
    )


class CancelIn(BaseModel):
    reason: str | None = Field(None, max_length=200)
