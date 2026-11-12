from fractions import Fraction
from ..values import (
    key,
    period,
    integer,
    fixed,
    rational,
    canonical,
    fingerprint,    reject,
    items,
    text,
)


class PlanTasks:
    """Registered movable production tasks.

    A task is expressed on a declared demand-window grid: its index coordinates
    are only meaningful together with ``period_start``, ``period_end`` and
    ``width``.  Keeping that context in the record makes the same registration
    reproducible and prevents a task from silently moving when the analysis
    period changes.
    """

    def __init__(self, store, catalog):
        self.store = store
        self.catalog = catalog

    def normalize(
        self,
        task,
        meter,
        period_start,
        period_end,
        width,
        original_start,
        duration,
        power_kw,
        earliest=0,
        latest_end=None,
        available=None,
        after=(),
        before=(),
        energy_kwh=None,
        note="",
    ):
        task = key(task)
        self.catalog.get(meter)
        start, end = period(period_start, period_end)
        width = integer(width, 1, 86400)
        if (end - start) % width:
            reject("grid", "planning period must contain a whole number of windows")
        cells = (end - start) // width
        original_start = integer(original_start, 0, cells)
        duration = integer(duration, 1, cells)
        if original_start + duration > cells:
            reject("time", "task baseline extends past planning period")
        power = fixed(power_kw)
        if power <= 0:
            reject("input", "task power must be positive")
        earliest = integer(earliest, 0, cells)
        latest_end = cells if latest_end is None else integer(latest_end, 1, cells)
        if earliest + duration > latest_end:
            reject("input", "task cannot fit between earliest start and latest end")
        if available is None:
            available = list(range(cells))
        else:
            items(available, 0, cells)
            normalized_available = []
            for value in available:
                normalized_available.append(integer(value, 0, cells - 1))
            if len(normalized_available) != len(set(normalized_available)):
                reject("duplicate", "duplicate available grid")
            available = sorted(normalized_available)
        if original_start + duration > latest_end:
            reject(
                "availability",
                "registered task baseline lies outside its declared window",
            )
        if not all(
            index in available
            for index in range(original_start, original_start + duration)
        ):
            reject(
                "availability",
                "registered task baseline lies outside its available cells",
            )

        for name, values in [("after", after), ("before", before)]:
            if not isinstance(values, (list, tuple)):
                reject("input", "task dependencies must be lists")
            values = list(values)
            if len(values) != len(set(values)):
                reject("duplicate", "duplicate task dependency")
            for value in values:
                key(value)
        if task in after or task in before:
            reject("cycle", "task cannot depend on itself")
        energy = Fraction(power * width, 3600) * duration
        if energy_kwh is not None:
            declared = fixed(energy_kwh)
            if Fraction(declared) != energy:
                reject(
                    "energy",
                    "declared task energy does not equal fixed power times duration",
                    exact_microkwh=rational(energy),
                )
        note = text(note, 1000, True)
        payload = {
            "id": task,
            "meter": meter,
            "period_start": start,
            "period_end": end,
            "width": width,
            "cells": cells,
            "original_start": original_start,
            "duration": duration,
            "power_microkw": power,
            "energy_microkwh": rational(energy),
            "earliest": earliest,
            "latest_end": latest_end,
            "available": available,
            "after": sorted(after),
            "before": sorted(before),
            "note": note,
        }
        return payload

    def put(
        self,
        task,
        meter,
        period_start,
        period_end,
        width,
        original_start,
        duration,
        power_kw,
        earliest=0,
        latest_end=None,
        available=None,
        after=(),
        before=(),
        energy_kwh=None,
        note="",
        expected_revision=0,
    ):
        payload = self.normalize(
            task,
            meter,
            period_start,
            period_end,
            width,
            original_start,
            duration,
            power_kw,
            earliest,
            latest_end,
            available,
            after,
            before,
            energy_kwh,
            note,
        )
        checksum = fingerprint(payload)
        with self.store.atomic() as con:
            row = self.store.one("SELECT revision FROM plan_tasks WHERE id=?", (task,))
            current = row["revision"] if row else 0
            if current != expected_revision:
                reject("conflict", "planning task revision changed")
            if row:
                old = self.get(task)
                if (
                    old["meter"] != meter
                    or old["period_start"] != payload["period_start"]
                    or old["period_end"] != payload["period_end"]
                    or old["width"] != payload["width"]
                ):
                    reject(
                        "conflict",
                        "task grid identity is immutable; register a new task",
                    )
            revision = current + 1
            con.execute(
                "INSERT INTO plan_tasks VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET meter=excluded.meter,"
                "grid_seconds=excluded.grid_seconds,revision=excluded.revision,"
                "payload=excluded.payload,digest=excluded.digest",
                (
                    task,
                    meter,
                    payload["width"],
                    revision,
                    canonical(payload),
                    checksum,
                ),
            )
            self.store.event(
                "plan_task_saved",
                task,
                {"revision": revision, "digest": checksum},
            )
        return self.get(task)

    def get(self, task):
        row, payload = self.store.payload("plan_tasks", task)
        return {**payload, "revision": row["revision"], "digest": row["digest"]}

    def list(
        self,
        meter=None,
        period_start=None,
        period_end=None,
        width=None,
    ):
        rows = [
            self.get(row["id"])
            for row in self.store.rows("SELECT id FROM plan_tasks ORDER BY id")
        ]
        if meter is not None:
            self.catalog.get(meter)
            rows = [row for row in rows if row["meter"] == meter]
        if period_start is not None or period_end is not None:
            if period_start is None or period_end is None:
                reject("input", "both period bounds are required for task listing")
            start, end = period(period_start, period_end)
            rows = [
                row
                for row in rows
                if row["period_start"] == start and row["period_end"] == end
            ]
        if width is not None:
            width = integer(width, 1, 86400)
            rows = [row for row in rows if row["width"] == width]
        return rows

    def remove(self, task, expected_revision):
        integer(expected_revision)
        with self.store.atomic() as con:
            current = self.get(task)
            if current["revision"] != expected_revision:
                reject("conflict", "planning task revision changed")
            con.execute("DELETE FROM plan_tasks WHERE id=?", (task,))
            self.store.event(
                "plan_task_removed",
                task,
                {"revision": current["revision"], "digest": current["digest"]},
            )
        return {"task": task, "removed": True, "revision": current["revision"]}
