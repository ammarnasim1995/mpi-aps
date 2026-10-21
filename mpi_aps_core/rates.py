"""Production rates and required machine/line hours.

Formulas (all hours Decimal, rounded half-up to 4 dp at the end only):
  qty_in_rate_unit = convert(planned_qty, qty_unit -> rate.qty_unit)         # material-specific UoM
  NET rate   : effective_rate = rate                                          # no further deductions
  GROSS rate : effective_rate = rate x planning_efficiency_factor             # hierarchy in settings
  UNKNOWN    : blocked (a rate is treated as net ONLY when the source confirms it)
  run_hours  = qty_in_rate_unit / effective_rate
  setup_hours: added separately, unless rate.includes_setup (then NOT added again)
  total_hours = run_hours + setup_hours
Unavailable hours (maintenance, breaks) belong to capacity (available hours), never to this rate.
SAP-standard, demonstrated and planned rates are separate `RateKind`s and are never merged.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Iterable, Optional

from .issues import ERROR, INFO, WARNING, Issue, has_errors
from .settings import SettingsStore
from .units import UomConverter, parse_rate_unit

_Q = Decimal("0.0001")


class RateBasis(str, Enum):
    NET = "net"
    GROSS = "gross"
    UNKNOWN = "unknown"


class RateKind(str, Enum):
    SAP_STANDARD = "sap_standard"
    DEMONSTRATED = "demonstrated"
    PLANNED = "planned"


class RateValidation(str, Enum):
    VALIDATED = "validated"
    UNVALIDATED = "unvalidated"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ProductionRate:
    sku: str
    plant: str
    process: str
    resource: str
    recipe: str
    kind: RateKind
    value: Decimal
    unit: str  # as received, e.g. 'dozens/machine-hour'
    basis: RateBasis
    source: str
    validation_status: RateValidation
    effective_from: date
    effective_to: Optional[date] = None
    includes_setup: bool = False

    def business_key(self):
        return (self.sku, self.plant, self.process, self.resource, self.recipe, self.kind)


def select_rate(rates: Iterable[ProductionRate], *, sku, plant, process, resource, recipe,
                kind: RateKind, as_of: date) -> tuple[Optional[ProductionRate], list[Issue]]:
    ctx = {"sku": sku, "plant": plant, "process": process, "resource": resource,
           "recipe": recipe, "kind": kind.value}
    hit = [r for r in rates if r.business_key() == (sku, plant, process, resource, recipe, kind)
           and r.effective_from <= as_of and (r.effective_to is None or as_of <= r.effective_to)]
    if not hit:
        return None, [Issue("RATE_MISSING", ERROR, f"No {kind.value} rate in effect on {as_of}. "
                            "No rate is substituted from another resource, recipe or plant.", ctx)]
    if len(hit) > 1:
        return None, [Issue("RATE_DUPLICATE", ERROR, f"{len(hit)} overlapping {kind.value} rates in effect; "
                            "fix the duplicate business key.", ctx)]
    return hit[0], []


@dataclass(frozen=True)
class HoursResult:
    ok: bool
    run_hours: Optional[Decimal]
    setup_hours: Decimal
    total_hours: Optional[Decimal]
    effective_rate: Optional[Decimal]
    rate_unit: Optional[str]
    efficiency_factor: Optional[Decimal]
    efficiency_scope: Optional[str]
    issues: tuple[Issue, ...]


def _fail(issues, setup=Decimal(0)) -> HoursResult:
    return HoursResult(False, None, setup, None, None, None, None, None, tuple(issues))


def required_hours(*, qty: Decimal, qty_unit: str, material: str, rate: ProductionRate,
                   settings: SettingsStore, converter: UomConverter, as_of: date,
                   setup_hours: Decimal = Decimal(0), allow_unvalidated: bool = False) -> HoursResult:
    ctx = {"sku": rate.sku, "plant": rate.plant, "resource": rate.resource, "recipe": rate.recipe}
    issues: list[Issue] = []
    setup_hours = Decimal(setup_hours)

    if rate.validation_status == RateValidation.REJECTED:
        return _fail([Issue("RATE_REJECTED", ERROR, "Rate was rejected in validation.", ctx)])
    if rate.validation_status == RateValidation.UNVALIDATED:
        if not allow_unvalidated:
            return _fail([Issue("RATE_UNVALIDATED", ERROR, "Rate is not validated; not used for planning.", ctx)])
        issues.append(Issue("RATE_UNVALIDATED_USED", WARNING, "Unvalidated rate used by explicit override.", ctx))
    if rate.value <= 0:
        return _fail([Issue("RATE_NOT_POSITIVE", ERROR, f"Rate value {rate.value} must be > 0.", ctx)])
    if setup_hours < 0:
        return _fail([Issue("NEGATIVE_SETUP", ERROR, "Setup hours cannot be negative.", ctx)])

    unit, u_issues = parse_rate_unit(rate.unit)
    if unit is None:
        return _fail(u_issues)
    q, c_issues = converter.convert(material, qty, qty_unit, unit.qty_unit)
    issues += c_issues
    if q is None:
        return _fail(issues)

    factor = scope = None
    if rate.basis == RateBasis.NET:
        eff_rate = rate.value
    elif rate.basis == RateBasis.GROSS:
        res = settings.resolve_efficiency(rate.plant, rate.process, rate.resource, as_of)
        issues += list(res.issues)
        if res.value is None:
            return _fail(issues)
        factor, scope = res.value.factor, res.value.scope.value
        eff_rate = rate.value * factor
    else:
        return _fail(issues + [Issue("RATE_BASIS_UNKNOWN", ERROR,
                     "Source does not confirm whether the rate is gross or net; it is not used.", ctx)])

    run = q / eff_rate
    if rate.includes_setup and setup_hours > 0:
        issues.append(Issue("SETUP_ALREADY_IN_RATE", INFO,
                            "Rate includes setup; separate setup hours were not added.", ctx))
        setup_hours = Decimal(0)
    total = run + setup_hours
    return HoursResult(not has_errors(issues), run.quantize(_Q, ROUND_HALF_UP), setup_hours,
                       total.quantize(_Q, ROUND_HALF_UP), eff_rate, f"{unit.qty_unit}/{unit.time_basis}",
                       factor, scope, tuple(issues))
