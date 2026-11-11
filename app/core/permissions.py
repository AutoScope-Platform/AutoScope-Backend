"""Roles and the permissions matrix (FSD section 8).

A user can hold several roles (FR-1.3). Every route that needs a role checks a
*capability* instead of a role name, so the matrix below is the one place that
says who may do what. Change the matrix here and the whole API follows.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class Role(StrEnum):
    VEHICLE_OWNER = "vehicle_owner"
    DEALER = "dealer"
    PARTS_PROVIDER = "parts_provider"
    MECHANIC = "mechanic"
    HAULAGE_PROVIDER = "haulage_provider"
    INSURER = "insurer"
    # AutoScope staff. `admin` is the super admin; `support` cannot change money settings.
    ADMIN = "admin"
    SUPPORT = "support"


# Roles that must pass KYC before their listings go live (FR-1.4).
BUSINESS_ROLES = frozenset(
    {Role.DEALER, Role.PARTS_PROVIDER, Role.MECHANIC, Role.HAULAGE_PROVIDER, Role.INSURER}
)
ADMIN_ROLES = frozenset({Role.ADMIN, Role.SUPPORT})
# Roles a person may pick for themselves at sign-up. Staff roles are granted by an admin.
SELF_SERVICE_ROLES = frozenset({Role.VEHICLE_OWNER}) | BUSINESS_ROLES


class Capability(StrEnum):
    LIST_VEHICLES = "list_vehicles"  # FSD 8: "List vehicles for sale"
    LIST_PARTS = "list_parts"
    OFFER_SERVICES = "offer_services"  # mechanic bookings
    OFFER_HAULAGE = "offer_haulage"
    OFFER_INSURANCE = "offer_insurance"
    PURCHASE = "purchase"  # buy parts / vehicles
    REVIEW = "review"
    VIEW_OWN_ANALYTICS = "view_own_analytics"
    ADMIN_CONSOLE = "admin_console"  # moderation, KYC, disputes, announcements, audit
    ADMIN_FINANCE = "admin_finance"  # commission rates, payout schedule, payout actions
    VIEW_ALL_ANALYTICS = "view_all_analytics"


_CUSTOMERS = {
    Role.VEHICLE_OWNER,
    Role.DEALER,
    Role.PARTS_PROVIDER,
    Role.MECHANIC,
    Role.HAULAGE_PROVIDER,
    Role.INSURER,
}

# capability -> roles that hold it. Mirrors the matrix in FSD section 8.
MATRIX: dict[Capability, frozenset[Role]] = {
    Capability.LIST_VEHICLES: frozenset({Role.VEHICLE_OWNER, Role.DEALER}),
    Capability.LIST_PARTS: frozenset({Role.PARTS_PROVIDER}),
    Capability.OFFER_SERVICES: frozenset({Role.MECHANIC}),
    Capability.OFFER_HAULAGE: frozenset({Role.HAULAGE_PROVIDER}),
    Capability.OFFER_INSURANCE: frozenset({Role.INSURER}),
    Capability.PURCHASE: frozenset(_CUSTOMERS),
    Capability.REVIEW: frozenset(_CUSTOMERS),
    Capability.VIEW_OWN_ANALYTICS: frozenset(BUSINESS_ROLES),
    Capability.ADMIN_CONSOLE: ADMIN_ROLES,
    Capability.ADMIN_FINANCE: frozenset({Role.ADMIN}),
    Capability.VIEW_ALL_ANALYTICS: ADMIN_ROLES,
}


def has_capability(roles: Iterable[Role | str], capability: Capability) -> bool:
    """True if any of `roles` holds `capability`."""
    allowed = MATRIX[capability]
    return any(Role(r) in allowed for r in roles)
