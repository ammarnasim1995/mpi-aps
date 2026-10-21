"""Small deterministic fixtures built on the SYNTHETIC sample (no seeded defects, no stock, no orders)."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

from mpi_aps.model import Order
from mpi_aps.sample import build_sample

PD = date(2026, 10, 21)
NOW = datetime(2026, 10, 21, 8, tzinfo=timezone.utc)


def order(so, sku="SKU-NBR13", qty=12000, plant="PK-1", due=30, prio=1, delivered=0, uom="PR", line="10"):
    return Order(so, line, "CUST-T", sku, D(qty), uom, D(delivered), D(qty), PD + timedelta(days=due - 3),
                 PD + timedelta(days=due), prio, 1, "Pakistan", plant)


def clean(*orders):
    inp = build_sample(PD, seed_defects=False, now=NOW)
    inp.orders = list(orders)
    inp.stocks = []
    return inp
