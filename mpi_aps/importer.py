"""Controlled CSV import for sales orders with row validation, duplicate prevention and reconciliation.

Rejected rows are reported, never silently dropped or repaired. Control totals (row count, total quantity)
supplied by the data owner are reconciled against accepted + rejected.
"""
from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal, InvalidOperation

from .model import Order, PlanInput

ORDER_COLUMNS = ["sales_order", "line", "customer", "sku", "qty", "uom", "delivered_qty", "confirmed_qty", "req_date",
                 "conf_date", "cust_priority", "ship_priority", "country", "plant", "status"]
TEMPLATE = ",".join(ORDER_COLUMNS) + "\nSO9001,10,CUST-A,SKU-NBR13,12000,PR,0,12000,2026-11-20,2026-11-20,1,1,Pakistan,PK-1,open\n"


def _dec(v: str, name: str) -> Decimal:
    try:
        d = Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} '{v}' is not a number")
    if d < 0:
        raise ValueError(f"{name} is negative")
    return d


def parse_orders(text: str, inp: PlanInput, existing: set, origin: str = "upload"):
    rd = csv.DictReader(io.StringIO(text))
    missing = [c for c in ORDER_COLUMNS if c not in (rd.fieldnames or [])]
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")
    accepted, rejected, seen, accepted_pairs = [], [], set(), []
    totals = {"pairs_in_file": Decimal(0), "unconvertible_rows": 0}   # control quantities are compared in PAIRS only
    for n, row in enumerate(rd, start=2):
        errs = []
        pairs = None
        try:
            qty = _dec(row["qty"], "qty")
        except ValueError as e:
            qty = None
            errs.append(str(e))
        try:
            dq, cq = _dec(row["delivered_qty"] or 0, "delivered_qty"), _dec(row["confirmed_qty"] or 0, "confirmed_qty")
        except ValueError as e:
            dq = cq = None
            errs.append(str(e))
        try:
            req = date.fromisoformat(row["req_date"])
            conf = date.fromisoformat(row["conf_date"]) if row["conf_date"].strip() else None
        except ValueError:
            req = conf = None
            errs.append("dates must be ISO yyyy-mm-dd")
        try:
            cp, sp = int(row["cust_priority"]), int(row["ship_priority"])
        except ValueError:
            cp = sp = None
            errs.append("priorities must be integers")
        for c in ("sales_order", "line", "customer", "sku", "uom", "plant"):
            if not (row.get(c) or "").strip():
                errs.append(f"{c} is required")
        if row.get("plant") and row["plant"] not in inp.plants:
            errs.append(f"unknown plant {row['plant']}")
        if row.get("sku") and row["sku"] not in inp.skus:
            errs.append(f"SKU {row['sku']} is not in the material master")
        elif row.get("sku") and row.get("uom") and qty is not None:
            v, iss = inp.converter.convert(inp.skus[row["sku"]].material, qty, row["uom"], "PR")
            if v is None:
                errs.append(iss[0].message)
            else:
                pairs = v
        if (row.get("status") or "open") not in ("open", "closed", "cancelled"):
            errs.append("status must be open|closed|cancelled")
        key = f"{row.get('sales_order')}-{row.get('line')}"
        if key in seen:
            errs.append(f"duplicate key {key} within file")
        elif key in existing:
            errs.append(f"key {key} already exists; duplicates are not overwritten")
        seen.add(key)
        if pairs is not None:
            totals["pairs_in_file"] += pairs
        else:
            totals["unconvertible_rows"] += 1
        if errs:
            rejected.append({"row": n, "key": key, "errors": errs, "qty": str(qty) if qty is not None else None,
                             "pairs": str(pairs) if pairs is not None else None})
            continue
        accepted.append(Order(row["sales_order"], row["line"], row["customer"], row["sku"], qty, row["uom"].strip().upper(), dq, cq,
                              req, conf, cp, sp, row["country"] or inp.plants[row["plant"]].country, row["plant"],
                              row.get("status") or "open", origin))
        accepted_pairs.append(pairs)
    return accepted, rejected, {**totals, "accepted_pairs": sum(accepted_pairs, Decimal(0))}


def reconcile(file_rows: int, accepted: list, rejected: list, totals: dict, control_rows=None, control_qty=None) -> dict:
    """Quantities are reconciled in pairs. Rows whose unit cannot be converted make the quantity control non-comparable."""
    rej_pairs = sum((Decimal(r["pairs"]) for r in rejected if r.get("pairs") is not None), Decimal(0))
    out = {"rows_in_file": file_rows, "accepted": len(accepted), "rejected": len(rejected),
           "pairs_in_file": str(totals["pairs_in_file"]), "pairs_accepted": str(totals["accepted_pairs"]), "pairs_rejected": str(rej_pairs),
           "rows_without_unit_conversion": totals["unconvertible_rows"],
           "internally_consistent": file_rows == len(accepted) + len(rejected)
           and totals["accepted_pairs"] + rej_pairs == totals["pairs_in_file"]}
    ok = out["internally_consistent"]
    if control_rows is not None:
        out["control_rows"], out["rows_difference"] = int(control_rows), file_rows - int(control_rows)
        ok = ok and out["rows_difference"] == 0
    if control_qty is not None:
        out["control_qty_pairs"] = str(control_qty)
        if totals["unconvertible_rows"]:
            out["qty_note"] = (f"{totals['unconvertible_rows']} row(s) have no unit conversion, so the control quantity (pairs) "
                               "is not comparable; fix those rows and re-import.")
            ok = False
        else:
            out["qty_difference_pairs"] = str(totals["pairs_in_file"] - Decimal(str(control_qty)))
            ok = ok and Decimal(out["qty_difference_pairs"]) == 0
    out["reconciled"] = ok
    return out
