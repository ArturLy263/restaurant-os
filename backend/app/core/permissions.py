"""Role-based permissions for the POS API.

Roles are the existing per-restaurant `roles` rows; a role's `name` is one
of the RoleName codes below (exact, upper case). Each endpoint requires one
Permission (see `require_permission` in app.core.dependencies), and a user
has the permissions of their role. Anything else is denied: a role name
that is not listed here, or a role that belongs to another restaurant,
grants no permissions at all.

Preparations are additionally scoped by station: a user without
PREPARATIONS_ALL_STATIONS works only the stations assigned to them
(station_assignments) that are active, belong to their restaurant and have
their role's station type (ROLE_STATION_TYPES). Users of any other role
without that permission have no stations.
"""

from enum import StrEnum


class RoleName(StrEnum):
    WAITER = "WAITER"
    KITCHEN = "KITCHEN"
    BAR = "BAR"
    CASHIER = "CASHIER"
    MANAGER = "MANAGER"
    # Pre-existing administrator role (seed.py); has every permission.
    ADMIN = "ADMIN"


class Permission(StrEnum):
    # Orders
    ORDERS_VIEW = "orders:view"
    ORDERS_CREATE = "orders:create"
    ORDERS_ADD_ITEMS = "orders:add_items"
    ORDERS_SEND = "orders:send"
    ORDERS_CANCEL_ITEMS = "orders:cancel_items"
    ORDERS_VOID = "orders:void"
    # Lifts the rule that only an order's creator may modify it.
    ORDERS_MANAGE_ALL = "orders:manage_all"

    # Kitchen and bar
    PREPARATIONS_VIEW = "preparations:view"
    PREPARATIONS_UPDATE = "preparations:update"
    PREPARATIONS_SERVE = "preparations:serve"
    # Not limited to assigned stations.
    PREPARATIONS_ALL_STATIONS = "preparations:all_stations"
    # Cancel an unfinished preparation of a fully paid (CLOSED) order,
    # e.g. the guest left; no financial effect. Management only.
    PREPARATIONS_DISCARD = "preparations:discard"

    # Bills and payments
    BILLS_VIEW = "bills:view"
    BILLS_CREATE = "bills:create"
    # Financial corrections of an issued bill: cancelling a billed item
    # (an ITEM_CANCELLATION adjustment) and voiding an order with a bill.
    BILLS_ADJUST = "bills:adjust"
    PAYMENTS_CREATE = "payments:create"

    # Management
    EVENTS_VIEW = "events:view"
    MENU_MANAGE = "menu:manage"
    TABLES_MANAGE = "tables:manage"


_WAITER = frozenset(
    {
        Permission.ORDERS_VIEW,
        Permission.ORDERS_CREATE,
        Permission.ORDERS_ADD_ITEMS,
        Permission.ORDERS_SEND,
        # Limited by the services to orders the user created and that
        # have no bill yet; billed items are cancelled through the bill.
        Permission.ORDERS_CANCEL_ITEMS,
        Permission.ORDERS_VOID,
        # Waiters take ready dishes to the table (READY -> SERVED only),
        # from whichever station prepared them.
        Permission.PREPARATIONS_SERVE,
        Permission.PREPARATIONS_ALL_STATIONS,
    }
)

_PREPARATION_STATION = frozenset(
    {
        Permission.PREPARATIONS_VIEW,
        Permission.PREPARATIONS_UPDATE,
        Permission.PREPARATIONS_SERVE,
    }
)

_CASHIER = frozenset(
    {
        Permission.ORDERS_VIEW,
        Permission.BILLS_VIEW,
        Permission.BILLS_CREATE,
        Permission.BILLS_ADJUST,
        Permission.PAYMENTS_CREATE,
    }
)

_ALL = frozenset(Permission)

ROLE_PERMISSIONS: dict[RoleName, frozenset[Permission]] = {
    RoleName.WAITER: _WAITER,
    RoleName.KITCHEN: _PREPARATION_STATION,
    RoleName.BAR: _PREPARATION_STATION,
    RoleName.CASHIER: _CASHIER,
    RoleName.MANAGER: _ALL,
    RoleName.ADMIN: _ALL,
}


# The station type (stations.type) a station role may be assigned to.
ROLE_STATION_TYPES: dict[RoleName, str] = {
    RoleName.KITCHEN: "KITCHEN",
    RoleName.BAR: "BAR",
}


def station_type_for_role(role_name: str | None) -> str | None:
    """The station type of a role name; None for every other role."""

    try:
        return ROLE_STATION_TYPES.get(RoleName(role_name))
    except ValueError:
        return None


def permissions_for_role(role_name: str | None) -> frozenset[Permission]:
    """The permissions of a role name; none for unknown names."""

    if role_name is None:
        return frozenset()

    try:
        return ROLE_PERMISSIONS[RoleName(role_name)]
    except ValueError:
        return frozenset()
