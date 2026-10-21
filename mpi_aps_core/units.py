"""Unit handling.

Canonical base unit: pairs (PR). Dozens (DZ) are a planning/reporting unit (1 DZ = 12 PR by the
standard setting). A material-specific conversion always takes precedence over the standard.
Any other SAP unit (e.g. CAR) is convertible ONLY if the material defines it; otherwise the
conversion is reported as missing. Quantities are Decimal; no float arithmetic is used.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Optional

from .issues import ERROR, INFO, WARNING, Issue, has_errors

PAIR, DOZEN = "PR", "DZ"
_PAIR_ALIASES = {"pr", "pair", "pairs"}
_DOZEN_ALIASES = {"dz", "doz", "dozen", "dozens"}
SUPPORTED_RATE_TIME_BASES = {"hour", "machine-hour", "line-hour"}


def normalise_unit(code: str) -> str:
    c = code.strip().lower()
    if c in _PAIR_ALIASES:
        return PAIR
    if c in _DOZEN_ALIASES:
        return DOZEN
    return code.strip().upper()


@dataclass(frozen=True)
class RateUnit:
    qty_unit: str  # PR or DZ
    time_basis: str  # hour | machine-hour | line-hour


def parse_rate_unit(text: str) -> tuple[Optional[RateUnit], list[Issue]]:
    """Parse e.g. 'dozens/machine-hour'. Unsupported text is an error, never guessed."""
    parts = [p.strip() for p in (text or "").split("/")]
    if len(parts) == 2:
        qty, basis = normalise_unit(parts[0]), parts[1].lower().replace(" ", "-")
        if qty in (PAIR, DOZEN) and basis in SUPPORTED_RATE_TIME_BASES:
            return RateUnit(qty, basis), []
    return None, [Issue("UNSUPPORTED_RATE_UNIT", ERROR,
                        f"Rate unit '{text}' is not supported. Use pairs|dozens per hour|machine-hour|line-hour.",
                        {"rate_unit": text})]


class UomConverter:
    def __init__(self, material_factors: Optional[Mapping[str, Mapping[str, Decimal]]] = None,
                 standard_pairs_per_dozen: Decimal = Decimal(12)):
        self.standard_dozen = Decimal(standard_pairs_per_dozen)
        self._m = {mat: {normalise_unit(u): Decimal(f) for u, f in units.items()}
                   for mat, units in (material_factors or {}).items()}

    def pairs_per_unit(self, material: str, unit: str) -> tuple[Optional[Decimal], list[Issue]]:
        u = normalise_unit(unit)
        rec = {"material": material, "unit": u}
        if u == PAIR:
            return Decimal(1), []
        mat = self._m.get(material, {})
        if u in mat:
            f = mat[u]
            if f <= 0:
                return None, [Issue("INVALID_UOM_FACTOR", ERROR, f"Non-positive factor for {material}/{u}.", rec)]
            if u == DOZEN and f != self.standard_dozen:
                return f, [Issue("NONSTANDARD_DOZEN", WARNING,
                                 f"{material}: 1 DZ = {f} PR differs from standard {self.standard_dozen}. "
                                 "Material-specific factor used; please confirm.", rec)]
            return f, []
        if u == DOZEN:
            return self.standard_dozen, [Issue("STANDARD_DOZEN_USED", INFO,
                                               f"{material}: no material-specific DZ factor; standard "
                                               f"{self.standard_dozen} PR/DZ used.", rec)]
        return None, [Issue("UOM_CONVERSION_MISSING", ERROR,
                            f"No conversion from {u} to pairs for material {material}. Maintain it in the "
                            "material unit-of-measure data; no value is assumed.", rec)]

    def convert(self, material: str, qty: Decimal, from_unit: str, to_unit: str
                ) -> tuple[Optional[Decimal], list[Issue]]:
        qty = Decimal(qty)
        if qty < 0:
            return None, [Issue("NEGATIVE_QUANTITY", ERROR, f"Negative quantity {qty} for {material}.",
                                {"material": material, "qty": str(qty)})]
        f_from, i1 = self.pairs_per_unit(material, from_unit)
        f_to, i2 = self.pairs_per_unit(material, to_unit)
        issues = i1 + i2
        if has_errors(issues) or f_from is None or f_to is None:
            return None, issues
        return qty * f_from / f_to, issues
