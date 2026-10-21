"""Shift calendars and finite-capacity time allocation.

Gross available hours  = standard shift hours on working, non-holiday days.
Planned downtime       = maintenance overlapping those shifts.  Unplanned = breakdowns.
Net available hours    = gross - planned - unplanned.   Approved overtime windows are reported
separately and added to the available base for utilisation (see docs/CALCULATIONS.md).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterator, Optional

from .model import CalendarSpec, Resource


@dataclass(frozen=True)
class Segment:
    start: datetime
    end: datetime
    overtime: bool = False

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


def _at(day: date, hour: float) -> datetime:
    return datetime(day.year, day.month, day.day) + timedelta(hours=hour)


def calendar_for(res: Resource, plants: dict) -> CalendarSpec:
    return res.calendar or plants[res.plant].calendar


def intervals_on(spec: CalendarSpec, day: date) -> list[tuple[datetime, datetime, bool]]:
    out = []
    if day.weekday() in spec.working_weekdays and day not in spec.holidays:
        out += [(_at(day, a), _at(day, b), False) for a, b in spec.shifts]
    out += [(_at(day, a), _at(day, b), True) for a, b in spec.overtime.get(day, [])]
    return sorted(out)


def subtract(iv: tuple[datetime, datetime], downs: list) -> Iterator[tuple[datetime, datetime]]:
    s, e = iv
    for d in sorted(downs, key=lambda d: d.start):
        if d.end <= s or d.start >= e:
            continue
        if d.start > s:
            yield (s, d.start)
        s = max(s, d.end)
        if s >= e:
            return
    if s < e:
        yield (s, e)


def allocate(spec: CalendarSpec, downs: list, from_dt: datetime, hours: float,
             limit_days: int = 400) -> Optional[list[Segment]]:
    """Consume `hours` of working time starting at/after from_dt. None if not possible in limit."""
    remaining = hours
    segs: list[Segment] = []
    day = from_dt.date()
    for _ in range(limit_days):
        for s, e, ot in intervals_on(spec, day):
            if e <= from_dt:
                continue
            s = max(s, from_dt)
            for a, b in subtract((s, e), downs):
                use = min(remaining, (b - a).total_seconds() / 3600)
                if use <= 1e-9:
                    continue
                segs.append(Segment(a, a + timedelta(hours=use), ot))
                remaining -= use
                if remaining <= 1e-9:
                    return segs
        day += timedelta(days=1)
    return None


def hours_breakdown(spec: CalendarSpec, downs: list, b0: datetime, b1: datetime) -> dict:
    """Gross/planned/unplanned/net/overtime hours of one resource inside [b0, b1)."""
    gross = planned = unplanned = ot = 0.0
    day = b0.date()
    while _at(day, 0) < b1:
        for s, e, is_ot in intervals_on(spec, day):
            s, e = max(s, b0), min(e, b1)
            if s >= e:
                continue
            h = (e - s).total_seconds() / 3600
            if is_ot:
                ot += h
                continue
            gross += h
            for d in downs:
                o = (min(e, d.end) - max(s, d.start)).total_seconds() / 3600
                if o > 0:
                    if d.kind == "maintenance":
                        planned += o
                    else:
                        unplanned += o
        day += timedelta(days=1)
    return {"gross": gross, "planned_downtime": planned, "unplanned_downtime": unplanned,
            "net": gross - planned - unplanned, "overtime_available": ot}
