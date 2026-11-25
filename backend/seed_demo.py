"""Demo data for a local RestaurantOS: one restaurant with every role.

Creates "RestaurantOS Demo Bistro" with seven demo employees (admin,
manager, two cashiers, waiter, kitchen, bar), its kitchen and bar
stations, tables, a menu with modifiers and the station assignments the
kitchen and bar displays need. No orders, bills, business days or cash
shifts: the demo starts from a clean day.

    DEMO_PASSWORD=... python backend/seed_demo.py [--dry-run]

Every account signs in with DEMO_PASSWORD, which is never printed. The
email domain demo.restaurantos.example is reserved (RFC 2606) and can never
belong to a real mailbox.

Safety (every check fails closed, before anything is written):

- The configured database (DATABASE_URL) must be named "<name>_dev" or
  "<name>_demo" (lowercase letters, digits and underscores); "*_test"
  databases and anything else are refused.
- The database the session is actually connected to (current_database())
  must be that same database; if it cannot be read, nothing is done.
- The code must have exactly one Alembic head, and the database must be
  migrated to exactly that head.
- seed_demo() itself runs the connected-database and migration checks
  before it locks or writes, so a direct call is guarded as fully as the
  command line. Only the integration tests may pass
  allow_test_database=True to let it seed their "*_test" database.
- DEMO_PASSWORD must have at least 8 characters, at most 72 bytes
  (bcrypt's limit), and be no placeholder.
- An unexpected error is reported by its type only: SQLAlchemy messages
  carry bound parameters, such as password hashes. Ctrl+C exits with 130
  and no traceback.

Idempotent: missing rows are created; existing rows are never updated or
deleted (differences from the demo definition are only reported), and the
passwords of existing accounts are left as they are. A conflict, e.g. a
demo email that belongs to another restaurant, aborts the whole run with
nothing written. Concurrent runs are serialized by an advisory lock.
"""

import argparse
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import insert, select, text
from sqlalchemy.orm import Session

from app.core.permissions import RoleName
from app.core.security import hash_password
from app.db import models  # noqa: F401  (registers every mapper)
from app.db.models.category import Category
from app.db.models.modifier import Modifier, ModifierGroup, product_modifier_groups
from app.db.models.product import Product
from app.db.models.restaurant import Restaurant
from app.db.models.role import Role
from app.db.models.station import Station
from app.db.models.station_assignment import StationAssignment
from app.db.models.table import Table
from app.db.models.user import User


REPO_ROOT = Path(__file__).resolve().parents[1]

# Whole-name patterns (re.fullmatch). Test databases are only seedable
# through seed_demo(..., allow_test_database=True), used by the
# integration tests.
SEEDABLE_DATABASE_NAME = re.compile(r"[a-z0-9][a-z0-9_]*_(dev|demo)")
TEST_DATABASE_NAME = re.compile(r"[a-z0-9][a-z0-9_]*_test")
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_BYTES = 72  # bcrypt ignores anything longer; hash_password refuses it
PLACEHOLDER_PASSWORDS = frozenset({"change_me", "changeme", "password", "demo_password"})

# pg_advisory_xact_lock key serializing concurrent runs ("RestOSdm").
ADVISORY_LOCK_KEY = 0x526573744F53646D


class SeedRefused(Exception):
    """A safety check failed; nothing was written."""


class SeedConflict(Exception):
    """Existing data contradicts the demo definition; nothing was written."""


# --- Demo definition ------------------------------------------------------

EMAIL_DOMAIN = "demo.restaurantos.example"
ADMIN_EMAIL = f"admin@{EMAIL_DOMAIN}"

RESTAURANT_NAME = "RestaurantOS Demo Bistro"
RESTAURANT_ADDRESS = "Demo data — not a real restaurant"
RESTAURANT_TIMEZONE = "Europe/Madrid"
RESTAURANT_CURRENCY = "EUR"
ROLE_DESCRIPTION = "Demo role"

# Plain strings (roles.name), the RoleName codes of app.core.permissions.
ROLES = tuple(
    role.value
    for role in (
        RoleName.ADMIN,
        RoleName.MANAGER,
        RoleName.CASHIER,
        RoleName.WAITER,
        RoleName.KITCHEN,
        RoleName.BAR,
    )
)


@dataclass(frozen=True)
class DemoUser:
    email: str
    name: str
    role: str  # a RoleName code
    # Name of the station the user is assigned to, if any.
    station: str | None = None


USERS = (
    DemoUser(ADMIN_EMAIL, "Demo Admin", RoleName.ADMIN.value),
    DemoUser(f"manager@{EMAIL_DOMAIN}", "Demo Manager", RoleName.MANAGER.value),
    DemoUser(f"cashier1@{EMAIL_DOMAIN}", "Demo Cashier One", RoleName.CASHIER.value),
    DemoUser(f"cashier2@{EMAIL_DOMAIN}", "Demo Cashier Two", RoleName.CASHIER.value),
    DemoUser(f"waiter@{EMAIL_DOMAIN}", "Demo Waiter", RoleName.WAITER.value),
    DemoUser(f"kitchen@{EMAIL_DOMAIN}", "Demo Kitchen", RoleName.KITCHEN.value, station="Kitchen"),
    DemoUser(f"bar@{EMAIL_DOMAIN}", "Demo Bar", RoleName.BAR.value, station="Bar"),
)

# (name, type)
STATIONS = (("Kitchen", "KITCHEN"), ("Bar", "BAR"))

# (number, capacity)
TABLES = ((1, 2), (2, 2), (3, 4), (4, 4), (5, 4), (6, 6), (7, 6), (8, 8))

# (name, display order)
CATEGORIES = (("Starters", 1), ("Mains", 2), ("Desserts", 3), ("Drinks", 4))


@dataclass(frozen=True)
class DemoModifierGroup:
    name: str
    min_selections: int
    max_selections: int | None
    is_required: bool
    # (name, price adjustment)
    modifiers: tuple[tuple[str, Decimal], ...]


MODIFIER_GROUPS = (
    DemoModifierGroup(
        "Doneness", 1, 1, True,
        (("Rare", Decimal("0.00")), ("Medium", Decimal("0.00")), ("Well done", Decimal("0.00"))),
    ),
    DemoModifierGroup(
        "Burger extras", 0, 3, False,
        (("Cheese", Decimal("1.00")), ("Bacon", Decimal("2.00")), ("Egg", Decimal("1.50"))),
    ),
    DemoModifierGroup(
        "Pizza extras", 0, 2, False,
        (("Extra mozzarella", Decimal("1.50")), ("Prosciutto", Decimal("2.50")), ("Olives", Decimal("1.00"))),
    ),
    DemoModifierGroup(
        "Milk", 0, 1, False,
        (("Oat milk", Decimal("0.40")), ("Lactose-free milk", Decimal("0.00"))),
    ),
)


@dataclass(frozen=True)
class DemoProduct:
    category: str
    name: str
    description: str
    price: Decimal
    station: str
    modifier_groups: tuple[str, ...] = ()
    is_active: bool = True


PRODUCTS = (
    DemoProduct("Starters", "Patatas bravas", "Fried potatoes, spicy tomato sauce, aioli", Decimal("6.50"), "Kitchen"),
    DemoProduct("Starters", "Garlic prawns", "Prawns in garlic and chilli oil", Decimal("9.80"), "Kitchen"),
    DemoProduct("Starters", "Burrata salad", "Burrata, tomatoes, basil", Decimal("8.90"), "Kitchen"),
    DemoProduct(
        "Mains", "Classic burger", "Beef patty, lettuce, tomato, fries", Decimal("13.50"), "Kitchen",
        ("Doneness", "Burger extras"),
    ),
    DemoProduct(
        "Mains", "Pizza Margherita", "Tomato, mozzarella, basil", Decimal("11.00"), "Kitchen",
        ("Pizza extras",),
    ),
    DemoProduct("Mains", "Grilled sea bass", "With seasonal vegetables", Decimal("18.50"), "Kitchen"),
    DemoProduct("Mains", "Mushroom risotto", "Arborio rice, wild mushrooms, parmesan", Decimal("14.00"), "Kitchen"),
    DemoProduct("Desserts", "Crema catalana", "Custard with caramelised sugar", Decimal("5.50"), "Kitchen"),
    DemoProduct("Desserts", "Chocolate fondant", "Warm chocolate cake, vanilla ice cream", Decimal("6.50"), "Kitchen"),
    # Shows how the POS presents a product that is off the menu today.
    DemoProduct(
        "Desserts", "Seasonal fruit tart", "Unavailable in the demo", Decimal("5.00"), "Kitchen",
        is_active=False,
    ),
    DemoProduct("Drinks", "Coca-Cola", "330 ml", Decimal("2.80"), "Bar"),
    DemoProduct("Drinks", "Sparkling water", "500 ml", Decimal("2.20"), "Bar"),
    DemoProduct("Drinks", "House red wine", "Glass", Decimal("4.50"), "Bar"),
    DemoProduct("Drinks", "Craft beer", "330 ml", Decimal("4.80"), "Bar"),
    DemoProduct("Drinks", "Espresso", "Single shot", Decimal("1.60"), "Bar", ("Milk",)),
    DemoProduct("Drinks", "Cappuccino", "Espresso, steamed milk", Decimal("2.40"), "Bar", ("Milk",)),
)


# --- Safety checks --------------------------------------------------------

def check_database_name(name: str | None, *, allow_test_database: bool = False) -> str:
    """The database name, if the demo seed may write to it: "<name>_dev" or
    "<name>_demo", or "<name>_test" only with `allow_test_database`."""

    if not isinstance(name, str) or not name:
        raise SeedRefused("the database name is unknown; refusing to seed.")
    if TEST_DATABASE_NAME.fullmatch(name):
        if allow_test_database:
            return name
        raise SeedRefused(f"refusing to seed {name!r}: test databases are never seeded.")
    if not SEEDABLE_DATABASE_NAME.fullmatch(name):
        raise SeedRefused(
            f"refusing to seed {name!r}: the database name must be lowercase letters, digits "
            "and underscores ending in '_dev' or '_demo'."
        )
    return name


def connected_database(session: Session) -> str:
    """current_database() of the session's connection, the one the seed
    writes through."""

    try:
        connected = session.scalar(text("SELECT current_database()"))
    except Exception as error:
        raise SeedRefused(
            f"cannot verify which database is connected ({type(error).__name__}); refusing to seed."
        ) from error

    if not isinstance(connected, str) or not connected:
        raise SeedRefused("cannot verify which database is connected; refusing to seed.")
    return connected


def verify_connected_database(
    session: Session, configured: str | None = None, *, allow_test_database: bool = False
) -> str:
    """The database the session is really connected to, if it is allowed and,
    when given, the `configured` one."""

    connected = connected_database(session)
    if configured is not None and connected != configured:
        raise SeedRefused(f"connected to {connected!r}, but DATABASE_URL names {configured!r}.")
    return check_database_name(connected, allow_test_database=allow_test_database)


def expected_migration_heads() -> frozenset[str]:
    """The code's Alembic head revisions, read from the migration files only."""

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(REPO_ROOT / "alembic.ini"))
    return frozenset(ScriptDirectory.from_config(config).get_heads())


def check_schema_version(session: Session, expected_heads: Iterable[str]) -> None:
    """Refuses unless the code has a single Alembic head and the database is
    migrated to exactly that head."""

    expected = frozenset(expected_heads)
    if not expected:
        raise SeedRefused("the code has no Alembic head; refusing to seed.")
    if len(expected) > 1:
        raise SeedRefused(
            f"the code has several Alembic heads {sorted(expected)}; merge them before seeding."
        )

    try:
        # A savepoint: a missing alembic_version table must not abort the
        # transaction the refusal is reported from.
        with session.begin_nested():
            current = frozenset(session.scalars(text("SELECT version_num FROM alembic_version")).all())
    except Exception as error:
        raise SeedRefused(
            "cannot read the database's migration version; run `alembic upgrade head` first."
        ) from error

    if current != expected:
        raise SeedRefused(
            f"the database is at migration {sorted(current) or 'none'}, the code at "
            f"{sorted(expected)}; run `alembic upgrade head` first."
        )


def check_password(password: str | SecretStr | None) -> str:
    """DEMO_PASSWORD, if usable. The value is never part of a message."""

    if isinstance(password, SecretStr):
        password = password.get_secret_value()
    if not password or not password.strip():
        raise SeedRefused("set DEMO_PASSWORD (in the environment or .env).")
    if password.strip().lower() in PLACEHOLDER_PASSWORDS:
        raise SeedRefused("DEMO_PASSWORD is a placeholder; choose your own.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SeedRefused(f"DEMO_PASSWORD must have at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise SeedRefused(f"DEMO_PASSWORD must be at most {MAX_PASSWORD_BYTES} bytes (bcrypt's limit).")
    return password


# --- Report ---------------------------------------------------------------

@dataclass
class SeedReport:
    created: dict[str, int] = field(default_factory=dict)
    existing: dict[str, int] = field(default_factory=dict)
    # Human-readable notices: existing rows that differ, duplicates.
    notices: list[str] = field(default_factory=list)

    def count(self, kind: str, created: bool) -> None:
        counts = self.created if created else self.existing
        counts[kind] = counts.get(kind, 0) + 1

    def differs(self, kind: str, label: str, attribute: str) -> None:
        self.notices.append(f"differs  {kind} {label}: {attribute} (left unchanged)")

    def lines(self) -> list[str]:
        kinds = list(dict.fromkeys([*self.created, *self.existing]))
        rows = [
            f"{kind:<22} created {self.created.get(kind, 0):>3}   existing {self.existing.get(kind, 0):>3}"
            for kind in kinds
        ]
        return rows + self.notices


# --- Seeding --------------------------------------------------------------

def seed_demo(
    session: Session,
    password: str,
    *,
    configured_database: str | None = None,
    allow_test_database: bool = False,
) -> SeedReport:
    """Create whatever demo data is missing; never update or delete.

    Refuses, before locking or writing anything, unless the session is
    connected to a "*_dev" or "*_demo" database (`configured_database`, if
    given) that is migrated to the code's single Alembic head.
    `allow_test_database` also permits "*_test"; only the integration
    tests pass it.

    Runs in a savepoint of the session's transaction: on a conflict (or any
    error) every row it wrote is rolled back and the caller's transaction
    is left as it was. The caller commits.
    """

    password = check_password(password)
    verify_connected_database(session, configured_database, allow_test_database=allow_test_database)
    check_schema_version(session, _code_migration_heads())
    report = SeedReport()

    with session.begin_nested():
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ADVISORY_LOCK_KEY})
        _Seeder(session, password, report).run()

    return report


def _code_migration_heads() -> frozenset[str]:
    try:
        return expected_migration_heads()
    except Exception as error:
        raise SeedRefused(
            f"cannot read the code's Alembic heads ({type(error).__name__}); refusing to seed."
        ) from error


class _Seeder:
    def __init__(self, session: Session, password: str, report: SeedReport):
        self.db = session
        self.password = password
        self.report = report

    def run(self) -> None:
        restaurant = self._restaurant()
        roles = {name: self._role(restaurant, name) for name in ROLES}
        stations = {name: self._station(restaurant, name, type_) for name, type_ in STATIONS}
        for spec in USERS:
            user = self._user(restaurant, roles[spec.role], spec)
            if spec.station is not None:
                self._assignment(user, stations[spec.station], spec)
        for number, capacity in TABLES:
            self._table(restaurant, number, capacity)
        categories = {name: self._category(restaurant, name, order) for name, order in CATEGORIES}
        groups = {spec.name: self._modifier_group(restaurant, spec) for spec in MODIFIER_GROUPS}
        for spec in PRODUCTS:
            product = self._product(categories[spec.category], stations[spec.station], spec)
            for group_name in spec.modifier_groups:
                self._link(product, groups[group_name], spec.name)

    # Restaurant and users

    def _restaurant(self) -> Restaurant:
        self._check_demo_emails_before_writing()

        admin = self.db.scalar(select(User).where(User.email == ADMIN_EMAIL))
        if admin is not None:
            restaurant = self.db.get(Restaurant, admin.restaurant_id)
            if restaurant is None or restaurant.name != RESTAURANT_NAME:
                raise SeedConflict(
                    f"{ADMIN_EMAIL} belongs to a restaurant that is not '{RESTAURANT_NAME}'."
                )
            self.db.scalar(select(Restaurant).where(Restaurant.id == restaurant.id).with_for_update())
            self.report.count("restaurant", created=False)
            self._compare("restaurant", RESTAURANT_NAME, restaurant, {
                "timezone": RESTAURANT_TIMEZONE, "currency": RESTAURANT_CURRENCY,
            })
            return restaurant

        if self.db.scalar(select(Restaurant.id).where(Restaurant.name == RESTAURANT_NAME).limit(1)):
            raise SeedConflict(
                f"a restaurant named '{RESTAURANT_NAME}' exists without the demo admin "
                f"{ADMIN_EMAIL}; refusing to guess whether it is the demo restaurant."
            )

        restaurant = Restaurant(
            name=RESTAURANT_NAME,
            address=RESTAURANT_ADDRESS,
            timezone=RESTAURANT_TIMEZONE,
            currency=RESTAURANT_CURRENCY,
        )
        self.db.add(restaurant)
        self.db.flush()
        self.report.count("restaurant", created=True)
        return restaurant

    def _check_demo_emails_before_writing(self) -> None:
        """Every existing demo account must be in the demo admin's restaurant
        with its demo role; checked before the first write."""

        users = {
            user.email: user
            for user in self.db.scalars(select(User).where(User.email.in_([spec.email for spec in USERS])))
        }
        admin = users.get(ADMIN_EMAIL)
        for spec in USERS:
            user = users.get(spec.email)
            if user is None:
                continue
            if admin is None or user.restaurant_id != admin.restaurant_id:
                raise SeedConflict(f"{spec.email} exists in another restaurant than the demo restaurant.")
            role = self.db.get(Role, user.role_id)
            if role is None or role.restaurant_id != user.restaurant_id or role.name != spec.role:
                raise SeedConflict(f"{spec.email} exists with another role than {spec.role}.")

    def _user(self, restaurant: Restaurant, role: Role, spec: DemoUser) -> User:
        user = self.db.scalar(select(User).where(User.email == spec.email))
        if user is not None:
            # Restaurant and role were checked before writing; the password
            # is never rotated.
            self.report.count("user", created=False)
            self._compare("user", spec.email, user, {"name": spec.name, "is_active": True})
            return user

        user = User(
            restaurant_id=restaurant.id,
            role_id=role.id,
            name=spec.name,
            email=spec.email,
            password_hash=hash_password(self.password),
        )
        self.db.add(user)
        self.db.flush()
        self.report.count("user", created=True)
        return user

    def _role(self, restaurant: Restaurant, name: str) -> Role:
        return self._get_or_create(
            "role", name, Role,
            [Role.restaurant_id == restaurant.id, Role.name == name],
            lambda: Role(restaurant_id=restaurant.id, name=name, description=ROLE_DESCRIPTION),
            {},
        )

    def _assignment(self, user: User, station: Station, spec: DemoUser) -> None:
        # A column query always asks the database (Session.get could answer
        # from the identity map).
        exists = self.db.scalar(
            select(StationAssignment.user_id).where(
                StationAssignment.user_id == user.id, StationAssignment.station_id == station.id
            )
        )
        if exists is None:
            self.db.add(StationAssignment(user_id=user.id, station_id=station.id))
            self.db.flush()
        self.report.count("station assignment", created=exists is None)

    # Floor and menu

    def _station(self, restaurant: Restaurant, name: str, type_: str) -> Station:
        return self._get_or_create(
            "station", name, Station,
            [Station.restaurant_id == restaurant.id, Station.name == name, Station.type == type_],
            lambda: Station(restaurant_id=restaurant.id, name=name, type=type_),
            {"is_active": True},
        )

    def _table(self, restaurant: Restaurant, number: int, capacity: int) -> Table:
        return self._get_or_create(
            "table", f"#{number}", Table,
            [Table.restaurant_id == restaurant.id, Table.number == number],
            lambda: Table(restaurant_id=restaurant.id, number=number, capacity=capacity),
            {"capacity": capacity, "is_active": True},
        )

    def _category(self, restaurant: Restaurant, name: str, display_order: int) -> Category:
        return self._get_or_create(
            "category", name, Category,
            [Category.restaurant_id == restaurant.id, Category.name == name],
            lambda: Category(restaurant_id=restaurant.id, name=name, display_order=display_order),
            {"display_order": display_order, "is_active": True},
        )

    def _modifier_group(self, restaurant: Restaurant, spec: DemoModifierGroup) -> ModifierGroup:
        group = self._get_or_create(
            "modifier group", spec.name, ModifierGroup,
            [ModifierGroup.restaurant_id == restaurant.id, ModifierGroup.name == spec.name],
            lambda: ModifierGroup(
                restaurant_id=restaurant.id,
                name=spec.name,
                min_selections=spec.min_selections,
                max_selections=spec.max_selections,
                is_required=spec.is_required,
            ),
            {
                "min_selections": spec.min_selections,
                "max_selections": spec.max_selections,
                "is_required": spec.is_required,
            },
        )
        for name, adjustment in spec.modifiers:
            self._get_or_create(
                "modifier", f"{spec.name}/{name}", Modifier,
                [Modifier.group_id == group.id, Modifier.name == name],
                lambda name=name, adjustment=adjustment: Modifier(
                    group_id=group.id, name=name, price_adjustment=adjustment
                ),
                {"price_adjustment": adjustment, "is_active": True},
            )
        return group

    def _product(self, category: Category, station: Station, spec: DemoProduct) -> Product:
        return self._get_or_create(
            "product", spec.name, Product,
            [Product.category_id == category.id, Product.name == spec.name],
            lambda: Product(
                category_id=category.id,
                name=spec.name,
                description=spec.description,
                price=spec.price,
                station_id=station.id,
                is_active=spec.is_active,
            ),
            {"price": spec.price, "station_id": station.id, "is_active": spec.is_active},
        )

    def _link(self, product: Product, group: ModifierGroup, product_name: str) -> None:
        link = product_modifier_groups.c
        exists = self.db.scalar(
            select(link.product_id).where(link.product_id == product.id, link.modifier_group_id == group.id)
        )
        if exists is None:
            self.db.execute(insert(product_modifier_groups).values(product_id=product.id, modifier_group_id=group.id))
        self.report.count("product modifier group", created=exists is None)

    # Helpers

    def _get_or_create(self, kind, label, model, criteria, build: Callable, expected: dict):
        rows = self.db.scalars(
            select(model).where(*criteria).order_by(model.created_at, model.id)
        ).all()
        if rows:
            if len(rows) > 1:
                self.report.notices.append(
                    f"notice   {len(rows)} {kind} rows match {label}; the oldest is used"
                )
            self.report.count(kind, created=False)
            self._compare(kind, label, rows[0], expected)
            return rows[0]

        row = build()
        self.db.add(row)
        self.db.flush()
        self.report.count(kind, created=True)
        return row

    def _compare(self, kind: str, label: str, row, expected: dict) -> None:
        for attribute, value in expected.items():
            if getattr(row, attribute) != value:
                self.report.differs(kind, label, attribute)


# --- Command line ---------------------------------------------------------

class DemoSeedSettings(BaseSettings):
    """DEMO_PASSWORD from the environment or the repository's .env."""

    demo_password: SecretStr | None = None

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


def run(session: Session, configured_database: str | None, password: str, *, dry_run: bool = False) -> SeedReport:
    """Every check, then the seed, in one transaction of `session`: committed
    on success (rolled back for a dry run), rolled back on any failure. The
    database checks that need the connection run once, in seed_demo()."""

    try:
        configured = check_database_name(configured_database)
        report = seed_demo(session, password, configured_database=configured)
        if dry_run:
            session.rollback()
        else:
            session.commit()
        report.notices.insert(0, f"database {configured}{' (dry run, rolled back)' if dry_run else ''}")
        return report
    except BaseException:
        session.rollback()
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the RestaurantOS demo data (idempotent).")
    parser.add_argument("--dry-run", action="store_true", help="run every check and the seed, then roll back")
    args = parser.parse_args(argv)

    try:
        # Checked before connecting to anything.
        password = check_password(DemoSeedSettings().demo_password)

        from app.db.database import SessionLocal, engine

        configured = check_database_name(engine.url.database)

        with SessionLocal() as session:
            report = run(session, configured, password, dry_run=args.dry_run)
    except SeedRefused as error:
        print(f"seed_demo: refused: {error}", file=sys.stderr)
        return 2
    except SeedConflict as error:
        print(f"seed_demo: conflict, nothing written: {error}", file=sys.stderr)
        return 3
    except Exception as error:
        # Never the message: a SQLAlchemy error's text includes its bound
        # parameters, e.g. a password hash. The transaction was rolled back.
        print(
            f"seed_demo: failed ({type(error).__name__}); nothing was committed. "
            "Details are withheld because they can contain bound parameters.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        # No traceback: its exception chain could include a database error
        # and its bound parameters. run() rolls back an unfinished run.
        print("seed_demo: interrupted; an unfinished run is rolled back.", file=sys.stderr)
        return 130

    for line in report.lines():
        print(line)
    print(f"Demo accounts: *@{EMAIL_DOMAIN}, password from DEMO_PASSWORD (not shown).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
