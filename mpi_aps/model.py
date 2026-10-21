"""Master/transaction data model. Every record carries `origin` (sample|upload|sap_live|estimate|manual).

Identifiers: business keys are explicit and documented in docs/DATA_DICTIONARY.md. Similarly named
resources, materials or recipes are NEVER treated as equivalent; links are by exact id only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

PROCESSES = ("knitting", "dipping", "packing")
ORIGINS = ("sample", "upload", "sap_live", "estimate", "manual")


@dataclass
class CalendarSpec:
    shifts: list                                    # [(start_hour, end_hour)] per working day
    working_weekdays: frozenset = frozenset({0, 1, 2, 3, 4, 5})   # Mon=0
    holidays: frozenset = frozenset()
    overtime: dict = field(default_factory=dict)    # date -> [(start_hour, end_hour)] approved extra windows


@dataclass
class Downtime:
    start: datetime
    end: datetime
    kind: str          # maintenance (planned) | breakdown (unplanned)
    reason: str = ""


@dataclass
class Plant:
    id: str
    country: str
    name: str
    calendar: CalendarSpec
    origin: str = "sample"


@dataclass
class Resource:
    id: str
    plant: str
    process: str
    group: str
    description: str
    gauge: Optional[str] = None      # knitting compatibility
    bed: Optional[str] = None
    active: bool = True
    calendar: Optional[CalendarSpec] = None       # resource-specific exception, else plant calendar
    downtimes: list = field(default_factory=list)
    origin: str = "sample"


@dataclass
class Sku:
    sku: str
    material: str                    # SAP material code (UoM conversions are looked up by this)
    description: str
    family: str
    coating: Optional[str] = None
    gauge: Optional[str] = None
    bed: Optional[str] = None
    customer_pack: Optional[str] = None
    base_uom: str = "PR"
    origin: str = "sample"


@dataclass
class RoutingOp:
    sku: str
    plant: Optional[str]             # None = valid for any plant that has no plant-specific routing
    seq: int
    process: str
    predecessor: Optional[str]       # predecessor PROCESS in this route
    transfer_hours: Decimal = Decimal(0)   # hand-over time to the NEXT operation
    queue_hours: Decimal = Decimal(0)      # wait before this operation can start
    yield_pct: Decimal = Decimal(1)        # good output / units processed (0<y<=1)
    qa_checkpoint_hours: Decimal = Decimal(0)  # time from completion to quality release
    origin: str = "sample"


@dataclass
class ProductionVersion:
    sku: str
    plant: str
    process: str
    recipe: str
    valid_from: date
    valid_to: Optional[date] = None
    origin: str = "sample"


@dataclass
class ChangeoverRule:
    process: str
    sku_change_h: Decimal
    family_change_h: Decimal
    coating_change_h: Decimal = Decimal(0)
    gauge_change_h: Decimal = Decimal(0)
    source: str = "SAMPLE-ASSUMPTION"


@dataclass
class Order:
    so: str
    line: str
    customer: str
    sku: str
    qty: Decimal                     # in `uom`
    uom: str
    delivered_qty: Decimal
    confirmed_qty: Decimal
    req_date: date
    conf_date: Optional[date]
    cust_priority: int               # 1 = highest
    ship_priority: int
    country: str
    plant: str
    status: str = "open"             # open | closed | cancelled
    origin: str = "sample"

    @property
    def key(self) -> str:
        return f"{self.so}-{self.line}"

    @property
    def due(self) -> date:
        return self.conf_date or self.req_date


@dataclass
class Stock:
    sku: str
    plant: str
    stage: str          # FG | DIP_WIP | KNIT_WIP
    qty: Decimal        # pairs
    allocated: Decimal = Decimal(0)
    blocked: Decimal = Decimal(0)     # quality-blocked
    origin: str = "sample"

    @property
    def available(self) -> Decimal:
        return max(Decimal(0), self.qty - self.allocated - self.blocked)


@dataclass
class RawStock:
    material: str
    plant: str
    unit: str
    qty: Decimal
    reserved: Decimal = Decimal(0)
    blocked: Decimal = Decimal(0)
    origin: str = "sample"

    @property
    def available(self) -> Decimal:
        return max(Decimal(0), self.qty - self.reserved - self.blocked)


@dataclass
class Receipt:
    material: str
    plant: str
    on: date
    qty: Decimal
    origin: str = "sample"


@dataclass
class BomLine:
    sku: str
    component: str
    qty_per_pair: Decimal
    unit: str
    origin: str = "sample"


@dataclass
class MaterialRule:
    material: str
    on_shortage: str = "delay"       # delay (wait for receipts) | block (do not schedule)


@dataclass
class PlannerConfig:
    priority_rules: tuple = ("override", "customer_priority", "due_date", "ship_priority", "order_key")
    objective_weights: dict = field(default_factory=lambda: {
        "finish": 1.0, "changeover": 0.5, "balance": 0.1, "overtime": 2.0})
    at_risk_days: int = 2
    rate_kind: str = "demonstrated"
    allow_sap_standard_fallback: bool = False     # explicit planner-approved fallback only
    plan_unconfirmed: bool = True
    horizon_weeks: int = 12
    lots_by_order: dict = field(default_factory=dict)   # order key -> number of lots (production splits)
    shipment_buffer_hours: int = 0


@dataclass
class RefreshInfo:
    source: str
    origin: str
    attempted_at: datetime
    success: bool
    file_name: Optional[str] = None
    errors: tuple = ()
    rows: Optional[int] = None


@dataclass
class PlanInput:
    plan_date: date
    plants: dict
    resources: dict
    skus: dict
    routings: list
    versions: list
    rates: list
    orders: list
    stocks: list
    raw_stocks: list
    receipts: list
    boms: list
    material_rules: dict
    changeovers: dict
    config: PlannerConfig
    settings: object                 # mpi_aps_core.settings.SettingsStore
    converter: object                # mpi_aps_core.units.UomConverter
    refresh_log: list = field(default_factory=list)
    priority_overrides: dict = field(default_factory=dict)  # order key -> (rank, reason, user)

    @property
    def horizon_start(self) -> datetime:
        return datetime(self.plan_date.year, self.plan_date.month, self.plan_date.day)

    @property
    def horizon_end(self) -> datetime:
        from datetime import timedelta
        return self.horizon_start + timedelta(weeks=self.config.horizon_weeks)
