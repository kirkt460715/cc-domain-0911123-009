from fractions import Fraction
from ..values import (
    key,
    period,
    integer,
    fixed,
    fraction,
    rational,
    round_even,
    fingerprint,
    reject,
    items,
)
from ..calendar import boundaries, split, coverage_summary


STATUS_OPTIMAL = "optimal"
STATUS_INFEASIBLE = "infeasible"
STATUS_OVER_SCALE = "over_scale"


class PlanEngine:
    """Coincidence analysis and discrete movable-load plan search.

    All calculations are read-only.  Registered tasks live in the planning
    collection and never alter readings, allocations or frozen closures.
    """

    def __init__(self, reconstructor, demand, tariffs, tasks, catalog):
        self.reconstructor = reconstructor
        self.demand = demand
        self.tariffs = tariffs
        self.tasks = tasks
        self.catalog = catalog

    # ------------------------------------------------------------------ grid

    def _windows(self, meter, start, end, width, origin):
        report = self.reconstructor.reconstruct(meter, start, end)
        points = boundaries(start, end, width, origin)
        rows = []
        for left, right in zip(points, points[1:]):
            energy, covered, evidence = self.demand.integrate_rows(
                report["intervals"], left, right
            )
            span = right - left
            complete = covered == span
            power = energy * Fraction(3600, span) if complete else None
            rows.append(
                {
                    "start": left,
                    "end": right,
                    "seconds": span,
                    "partial": span != width,
                    "complete": complete,
                    "microkwh": rational(energy) if complete else None,
                    "microkw": rational(power) if complete else None,
                    "observed_microkwh": rational(energy),
                    "evidence": evidence,
                }
            )
        return report, rows

    def _topology(self, root, topology):
        if not isinstance(topology, dict):
            reject("input", "meter topology must be an adjacency object")
        children = {}
        owners = {}
        visited = set()
        active = set()
        order = []

        def visit(meter):
            definition = self.catalog.get(meter)
            if meter in active:
                reject("cycle", "meter containment graph contains a cycle")
            if meter in visited:
                return
            active.add(meter)
            values = topology.get(meter, [])
            items(values, 0, 1000)
            if len(values) != len(set(values)):
                reject("duplicate", "duplicate child meter")
            children[meter] = sorted(values)
            for child in values:
                self.catalog.get(child)
                if child in owners and owners[child] != meter:
                    reject("topology", "child meter cannot have two parents")
                owners[child] = meter
                visit(child)
            active.remove(meter)
            visited.add(meter)
            order.append(meter)
            return definition

        root_definition = visit(root)
        if set(topology) - visited:
            reject("topology", "topology includes unreachable parent nodes")
        for meter in visited:
            definition = self.catalog.get(meter)
            if (definition["medium"], definition["direction"]) != (
                root_definition["medium"],
                root_definition["direction"],
            ):
                reject("unit", "coincidence requires one medium and direction")
        return {
            "order": order,
            "children": children,
            "meters": sorted(visited),
            "root_definition": root_definition,
        }

    def _peak(self, rows):
        eligible = [
            (index, row)
            for index, row in enumerate(rows)
            if row["complete"] and not row["partial"]
        ]
        if not eligible:
            return None
        index, row = max(
            eligible,
            key=lambda pair: (fraction(pair[1]["microkw"]), -pair[1]["start"]),
        )
        return {
            "index": index,
            "start": row["start"],
            "end": row["end"],
            "microkw": row["microkw"],
            "microkwh": row["microkwh"],
            "evidence": row["evidence"],
        }

    @staticmethod
    def _row_residual(parent_row, child_rows, tolerance):
        if parent_row["microkw"] is None:
            return {
                "complete": False,
                "missing": ["parent"],
                "parent_microkwh": None,
                "children_microkwh": None,
                "residual_microkwh": None,
                "residual_microkw": None,
                "status": "incomplete",
            }
        missing = [
            name
            for name, row in child_rows.items()
            if row["microkw"] is None
        ]
        if missing:
            return {
                "complete": False,
                "missing": sorted(missing),
                "parent_microkwh": parent_row["microkwh"],
                "children_microkwh": None,
                "residual_microkwh": None,
                "residual_microkw": None,
                "status": "incomplete",
            }
        parent = fraction(parent_row["microkwh"])
        child = sum(
            (fraction(row["microkwh"]) for row in child_rows.values()),
            Fraction(0),
        )
        residual = parent - child
        power = residual * Fraction(3600, parent_row["seconds"])
        return {
            "complete": True,
            "missing": [],
            "parent_microkwh": rational(parent),
            "children_microkwh": rational(child),
            "residual_microkwh": rational(residual),
            "residual_microkw": rational(power),
            "status": "ok" if residual >= -tolerance else "child_exceeds_parent",
        }

    def coincidence(
        self, parent, topology, start, end, width, origin=0, tolerance_microkwh=0
    ):
        start, end = period(start, end)
        width = integer(width, 1, 86400)
        integer(origin, 0, 4133980800)
        tolerance = integer(tolerance_microkwh, 0, 10**15)
        tree = self._topology(key(parent), topology)
        reconstructed = {}
        windows = {}
        for meter in tree["order"]:
            report, rows = self._windows(meter, start, end, width, origin)
            reconstructed[meter] = report
            windows[meter] = rows
        cells = len(windows[parent])
        grid = []
        peaks = {}
        missing_windows = []
        for meter in tree["meters"]:
            peaks[meter] = self._peak(windows[meter])
            for row in windows[meter]:
                if not row["complete"] or row["partial"]:
                    missing_windows.append(
                        {
                            "meter": meter,
                            "start": row["start"],
                            "end": row["end"],
                            "partial": row["partial"],
                            "complete": row["complete"],
                        }
                    )
        root_children = tree["children"].get(parent, [])
        for index in range(cells):
            entries = {}
            for meter in tree["meters"]:
                row = windows[meter][index]
                entries[meter] = {
                    "microkwh": row["microkwh"],
                    "microkw": row["microkw"],
                    "complete": row["complete"] and not row["partial"],
                    "partial": row["partial"],
                    "evidence": row["evidence"],
                }
            parent_row = windows[parent][index]
            child_rows = {name: windows[name][index] for name in root_children}
            residual = self._row_residual(parent_row, child_rows, tolerance)
            residual["provenance"] = {
                "parent_evidence": parent_row["evidence"],
                "children_evidence": {
                    name: row["evidence"] for name, row in child_rows.items()
                },
                "missing": residual["missing"],
            }
            grid.append(
                {
                    "index": index,
                    "start": parent_row["start"],
                    "end": parent_row["end"],
                    "partial": parent_row["partial"],
                    "meters": entries,
                    "residual": residual,
                }
            )
        plant_peak = peaks.get(parent)
        at_plant_peak = None
        if plant_peak is not None:
            index = plant_peak["index"]
            child_values = {}
            for child in root_children:
                row = windows[child][index]
                child_values[child] = {
                    "microkwh": row["microkwh"],
                    "microkw": row["microkw"],
                    "complete": row["complete"] and not row["partial"],
                    "evidence": row["evidence"],
                    "independent_peak": peaks.get(child),
                    "share_of_plant_peak": rational(
                        fraction(row["microkw"]) / fraction(plant_peak["microkw"])
                    )
                    if row["microkw"] is not None
                    and fraction(plant_peak["microkw"]) != 0
                    else None,
                }
            at_plant_peak = {
                "index": index,
                "start": plant_peak["start"],
                "end": plant_peak["end"],
                "parent_microkw": plant_peak["microkw"],
                "parent_microkwh": plant_peak["microkwh"],
                "children": child_values,
                "residual": grid[index]["residual"],
            }
        root_report_digests = {
            meter: reconstructed[meter]["digest"] for meter in tree["meters"]
        }
        result = {
            "parent": parent,
            "topology": {meter: tree["children"].get(meter, []) for meter in tree["meters"]},
            "children": root_children,
            "meters": tree["meters"],
            "start": start,
            "end": end,
            "grid_seconds": width,
            "origin": origin,
            "cells": cells,
            "grid": grid,
            "peaks": {
                "plant": plant_peak,
                "meters": peaks,
                "missing_windows": missing_windows,
            },
            "at_plant_peak": at_plant_peak,
            "complete": all(
                reconstructed[meter]["complete"] for meter in tree["meters"]
            )
            and not any(row["partial"] for row in windows[parent]),
            "input_digests": root_report_digests,
            "basis": "uniform_counter_rate_per_demand_window",
        }
        result["digest"] = fingerprint(result)
        return result

    # ---------------------------------------------------------------- planning

    def _require_complete(self, meter, rows, role):
        incomplete = [
            {"start": row["start"], "end": row["end"], "partial": row["partial"]}
            for row in rows
            if not row["complete"] or row["partial"]
        ]
        if incomplete:
            reject(
                "coverage",
                "planning requires complete demand windows",
                meter=meter,
                role=role,
                windows=incomplete,
            )

    def _select_tasks(self, parent, tree, start, end, width, selected):
        registered = self.tasks.list(period_start=start, period_end=end, width=width)
        known = {row["id"]: row for row in registered}
        if selected is None:
            chosen = [
                row
                for row in registered
                if row["meter"] in tree["meters"] and row["meter"] != parent
            ]
        else:
            items(selected, 0, 64)
            identifiers = []
            for value in selected:
                key(value)
                identifiers.append(value)
            if len(identifiers) != len(set(identifiers)):
                reject("duplicate", "duplicate planning task selection")
            chosen = []
            for identifier in identifiers:
                row = known.get(identifier)
                if row is None:
                    row = self.tasks.get(identifier)
                    if (
                        row["period_start"] != start
                        or row["period_end"] != end
                        or row["width"] != width
                    ):
                        reject(
                            "state",
                            "task belongs to another planning grid",
                            task=identifier,
                        )
                if row["meter"] == parent or row["meter"] not in tree["meters"]:
                    reject(
                        "state",
                        "movable task must belong to a selected submeter",
                        task=identifier,
                    )
                chosen.append(row)
        chosen.sort(key=lambda row: row["id"])
        if len(chosen) > 64:
            reject("capacity", "at most 64 movable tasks are supported")
        identifiers = {row["id"] for row in chosen}
        if len(identifiers) != len(chosen):
            reject("duplicate", "duplicate planning task identity")
        for row in chosen:
            for name in ["after", "before"]:
                for other in row[name]:
                    if other not in identifiers:
                        reject(
                            "dependency",
                            "task dependency is not part of the selected plan",
                            task=row["id"],
                            dependency=other,
                        )
        constraints = []
        seen = set()
        for row in chosen:
            for predecessor in row["after"]:
                pair = (predecessor, row["id"])
                if pair not in seen:
                    seen.add(pair)
                    constraints.append(pair)
            for successor in row["before"]:
                pair = (row["id"], successor)
                if pair not in seen:
                    seen.add(pair)
                    constraints.append(pair)
        edges = {row["id"]: set() for row in chosen}
        for left, right in constraints:
            edges[left].add(right)
        visited = set()
        stack = []
        for node in edges:
            if node in stack:
                reject("cycle", "task dependencies contain a cycle", task=node)
            if node in visited:
                continue
            pending = [(node, False)]
            while pending:
                current, expanded = pending.pop()
                if expanded:
                    stack.remove(current)
                    visited.add(current)
                    continue
                if current in stack:
                    reject("cycle", "task dependencies contain a cycle", task=current)
                if current in visited:
                    continue
                stack.append(current)
                pending.append((current, True))
                for other in sorted(edges[current]):
                    if other in stack:
                        reject(
                            "cycle", "task dependencies contain a cycle", task=other
                        )
                    if other not in visited:
                        pending.append((other, False))
        return chosen, constraints, identifiers

    def _candidates(self, task, cells):
        available = set(task["available"])
        duration = task["duration"]
        return [
            start
            for start in range(task["earliest"], task["latest_end"] - duration + 1)
            if all(index in available for index in range(start, start + duration))
        ]

    def _rate_coefficients(self, meter, start, end, cells, width):
        rates = self.tariffs.list(meter, start, end)
        coverage = coverage_summary(
            start, end, [(row["start"], row["end"]) for row in rates]
        )
        if not coverage["complete"]:
            reject(
                "coverage",
                "time-of-use tariff does not cover the planning period",
                meter=meter,
                gaps=coverage["gaps"],
            )
        currencies = sorted({row["currency"] for row in rates})
        if len(currencies) != 1:
            reject(
                "currency",
                "plan cost comparison requires one settlement currency",
                currencies=currencies,
            )
        cuts = {point for row in rates for point in [row["start"], row["end"]]}
        coefficients = []
        for left, right in cells:
            total = Fraction(0)
            for piece_left, piece_right in split(left, right, cuts):
                rate = next(
                    (
                        row
                        for row in rates
                        if row["start"] <= piece_left and row["end"] >= piece_right
                    ),
                    None,
                )
                if rate is None:
                    reject("coverage", "tariff gap inside planning window")
                total += (
                    Fraction(piece_right - piece_left)
                    * Fraction(rate["microcurrency_per_kwh"], 10**10)
                    / 3600
                )
            coefficients.append(total)
        return coefficients, currencies[0], fingerprint(rates)

    @staticmethod
    def _cost(power, coefficients):
        return sum(
            (
                Fraction(value) * coefficient
                for value, coefficient in zip(power, coefficients)
            ),
            Fraction(0),
        )

    def plan(
        self,
        parent,
        topology,
        start,
        end,
        width,
        capacity_kw,
        tasks=None,
        origin=0,
        tariff_meter=None,
        max_search=100000,
    ):
        start, end = period(start, end)
        width = integer(width, 1, 86400)
        integer(origin, 0, 4133980800)
        max_search = integer(max_search, 1, 50_000_000)
        capacity = fixed(capacity_kw)
        if capacity <= 0:
            reject("input", "plant capacity must be positive")
        if (start - origin) % width or (end - start) % width:
            reject("time", "planning period must align to whole demand windows")
        tree = self._topology(key(parent), topology)
        tariff_meter = parent if tariff_meter is None else key(tariff_meter)
        if tariff_meter not in tree["meters"]:
            reject("state", "tariff meter is not part of the selected tree")
        coincidence = self.coincidence(
            parent, topology, start, end, width, origin
        )
        chosen, constraints, _identifiers = self._select_tasks(
            parent, tree, start, end, width, tasks
        )
        cells = (end - start) // width
        intervals = [
            (start + index * width, start + (index + 1) * width)
            for index in range(cells)
        ]
        involved = sorted(tree["meters"])
        original = {}
        reports = {}
        for meter in involved:
            report, rows = self._windows(meter, start, end, width, origin)
            self._require_complete(meter, rows, "planning")
            reports[meter] = report["digest"]
            original[meter] = [fraction(row["microkw"]) for row in rows]
        coefficients, currency, tariff_digest = self._rate_coefficients(
            tariff_meter, start, end, intervals, width
        )
        # A movable task is a slice of its submeter curve, and every containing
        # meter measures it too.  The task is removed from each ancestor curve
        # at its registered baseline and re-added once at the new placement.
        # Parent curves remain independent measurements; no residual is spread.
        parents_of = {}
        for owner, values in tree["children"].items():
            for child in values:
                parents_of[child] = owner
        paths = {}
        for meter in tree["meters"]:
            chain = [meter]
            while chain[-1] != parent:
                chain.append(parents_of[chain[-1]])
            paths[meter] = list(reversed(chain))
        base = {meter: list(values) for meter, values in original.items()}
        baseline_cells = {}
        task_power = {task["id"]: Fraction(task["power_microkw"]) for task in chosen}
        for task in chosen:
            span = list(
                range(
                    task["original_start"],
                    task["original_start"] + task["duration"],
                )
            )
            baseline_cells[task["id"]] = span
            power = task_power[task["id"]]
            for meter in paths[task["meter"]]:
                for index in span:
                    remaining = base[meter][index] - power
                    if remaining < 0:
                        reject(
                            "baseline",
                            "task baseline power exceeds the meter curve",
                            task=task["id"],
                            meter=meter,
                            index=index,
                            available_microkw=rational(base[meter][index]),
                            task_microkw=rational(power),
                        )
                    base[meter][index] = remaining
        before_peak = max(
            range(cells), key=lambda index: (original[parent][index], -index)
        )
        base_peak = max(range(cells), key=lambda index: (base[parent][index], -index))
        common = {
            "parent": parent,
            "topology": coincidence["topology"],
            "children": coincidence["children"],
            "start": start,
            "end": end,
            "grid_seconds": width,
            "origin": origin,
            "cells": cells,
            "capacity_microkw": capacity,
            "tariff_meter": tariff_meter,
            "currency": currency,
            "coincidence": coincidence,
            "tasks_registered": [
                {
                    "id": task["id"],
                    "meter": task["meter"],
                    "digest": task["digest"],
                    "revision": task["revision"],
                }
                for task in chosen
            ],
            "baseline": {
                "plant": {
                    "peak_index": base_peak,
                    "peak_start": intervals[base_peak][0],
                    "peak_microkw": rational(base[parent][base_peak]),
                },
                "meters": {
                    meter: [rational(value) for value in base[meter]]
                    for meter in involved
                },
            },
            "before": {
                "peak_index": before_peak,
                "peak_start": intervals[before_peak][0],
                "peak_microkw": rational(original[parent][before_peak]),
                "cost_exact_minor": rational(
                    self._cost(original[tariff_meter], coefficients)
                ),
            },
            "input_digest": fingerprint(
                {
                    "reports": reports,
                    "tasks": [
                        {"id": task["id"], "digest": task["digest"]}
                        for task in chosen
                    ],
                    "topology": coincidence["topology"],
                    "start": start,
                    "end": end,
                    "width": width,
                    "origin": origin,
                    "capacity_microkw": capacity,
                    "tariff_meter": tariff_meter,
                    "tariff_digest": tariff_digest,
                    "max_search": max_search,
                }
            ),
        }
        candidate_rows = []
        for task in chosen:
            values = self._candidates(task, cells)
            candidate_rows.append(
                {
                    "task": task["id"],
                    "meter": task["meter"],
                    "candidates": len(values),
                    "starts": values,
                }
            )
        if any(not row["candidates"] for row in candidate_rows):
            empty = [row["task"] for row in candidate_rows if not row["candidates"]]
            return {
                **common,
                "status": STATUS_INFEASIBLE,
                "reason": "no_available_placement",
                "infeasible_tasks": empty,
                "candidates": candidate_rows,
                "search": {"space": 0, "limit": max_search, "evaluated": 0},
                "after": None,
                "schedule": None,
                "cost": None,
            }
        if any(value > capacity for value in base[parent]):
            index = next(
                index for index, value in enumerate(base[parent]) if value > capacity
            )
            return {
                **common,
                "status": STATUS_INFEASIBLE,
                "reason": "capacity_impossible_before_schedule",
                "capacity_violation": {
                    "index": index,
                    "start": intervals[index][0],
                    "microkw": rational(base[parent][index]),
                    "capacity_microkw": capacity,
                },
                "candidates": candidate_rows,
                "search": {"space": 0, "limit": max_search, "evaluated": 0},
                "after": None,
                "schedule": None,
                "cost": None,
            }
        space = 1
        for row in candidate_rows:
            space *= row["candidates"]
        if space > max_search:
            return {
                **common,
                "status": STATUS_OVER_SCALE,
                "reason": "declared_search_limit_exceeded",
                "candidates": candidate_rows,
                "search": {
                    "space": space,
                    "limit": max_search,
                    "evaluated": 0,
                },
                "after": None,
                "schedule": None,
                "cost": None,
                "note": "No greedy result is reported for an over-scale search space.",
            }
        ordered = chosen
        duration_of = {task["id"]: task["duration"] for task in chosen}
        predecessor = {task["id"]: [] for task in chosen}
        successor = {task["id"]: [] for task in chosen}
        for left, right in constraints:
            predecessor[right].append(left)
            successor[left].append(right)
        starts = {}
        load = list(base[parent])
        best = {"objective": None, "starts": None, "load": None}
        evaluated = 0
        pruned_capacity = 0
        pruned_dependency = 0

        def assign(index):
            nonlocal evaluated, pruned_capacity, pruned_dependency
            if index == len(ordered):
                evaluated += 1
                maximum = max(load)
                cost = self._cost(load, coefficients)
                distance = sum(
                    abs(starts[task["id"]] - task["original_start"])
                    for task in ordered
                )
                vector = tuple(starts[task["id"]] for task in ordered)
                objective = (maximum, cost, distance, vector)
                if best["objective"] is None or objective < best["objective"]:
                    best["objective"] = objective
                    best["starts"] = {name: value for name, value in starts.items()}
                    best["load"] = list(load)
                return
            task = ordered[index]
            duration = task["duration"]
            power = task_power[task["id"]]
            candidates = self._candidates(task, cells)
            for start_at in candidates:
                starts[task["id"]] = start_at
                end_at = start_at + duration
                ok = True
                for other in predecessor[task["id"]]:
                    if other in starts and starts[other] + duration_of[other] > start_at:
                        ok = False
                        break
                if not ok:
                    pruned_dependency += 1
                    continue
                for other in successor[task["id"]]:
                    if other in starts and end_at > starts[other]:
                        ok = False
                        break
                if not ok:
                    pruned_dependency += 1
                    continue
                for cell in range(start_at, end_at):
                    load[cell] += power
                if max(load) > capacity:
                    pruned_capacity += 1
                    for cell in range(start_at, end_at):
                        load[cell] -= power
                    continue
                assign(index + 1)
                for cell in range(start_at, end_at):
                    load[cell] -= power
            starts.pop(task["id"], None)

        assign(0)
        search = {
            "space": space,
            "limit": max_search,
            "method": "exhaustive_discrete_enumeration",
            "complete": True,
            "evaluated": evaluated,
            "pruned_capacity": pruned_capacity,
            "pruned_dependency": pruned_dependency,
            "tie_break": "lexicographic_start_vector",
        }
        if best["starts"] is None:
            return {
                **common,
                "status": STATUS_INFEASIBLE,
                "reason": "no_feasible_schedule",
                "constraints": [
                    {"before": left, "after": right} for left, right in constraints
                ],
                "candidates": candidate_rows,
                "search": search,
                "after": None,
                "schedule": None,
                "cost": None,
            }
        scheduled = best["starts"]
        after = {meter: list(values) for meter, values in base.items()}
        for task in ordered:
            later = scheduled[task["id"]]
            power = task_power[task["id"]]
            for meter in paths[task["meter"]]:
                for cell in range(later, later + task["duration"]):
                    after[meter][cell] += power
        after_peak_index = max(
            range(cells), key=lambda index: (after[parent][index], -index)
        )
        before_cost = self._cost(original[tariff_meter], coefficients)
        after_cost = self._cost(after[tariff_meter], coefficients)
        arrangement = []
        for task in ordered:
            later = scheduled[task["id"]]
            arrangement.append(
                {
                    "id": task["id"],
                    "meter": task["meter"],
                    "original_start": task["original_start"],
                    "start": later,
                    "end": later + task["duration"],
                    "start_epoch": intervals[later][0],
                    "end_epoch": intervals[later + task["duration"] - 1][1],
                    "duration_cells": task["duration"],
                    "power_microkw": task["power_microkw"],
                    "energy_microkwh": task["energy_microkwh"],
                    "movement_cells": abs(later - task["original_start"]),
                    "baseline_cells": baseline_cells[task["id"]],
                    "scheduled_cells": list(range(later, later + task["duration"])),
                    "after": successor[task["id"]],
                    "before": predecessor[task["id"]],
                }
            )
        dependency_checks = []
        for left, right in constraints:
            left_end = scheduled[left] + duration_of[left]
            dependency_checks.append(
                {
                    "before": left,
                    "after": right,
                    "before_end": left_end,
                    "after_start": scheduled[right],
                    "ok": left_end <= scheduled[right],
                }
            )
        capacity_rows = []
        for index in range(cells):
            capacity_rows.append(
                {
                    "index": index,
                    "start": intervals[index][0],
                    "end": intervals[index][1],
                    "plant_microkw": rational(after[parent][index]),
                    "capacity_microkw": capacity,
                    "ok": after[parent][index] <= capacity,
                }
            )
        conservation = []
        for meter in sorted(involved):
            before_total = sum(original[meter], Fraction(0))
            after_total = sum(after[meter], Fraction(0))
            conservation.append(
                {
                    "meter": meter,
                    "before_microkwh": rational(before_total * Fraction(width, 3600)),
                    "after_microkwh": rational(after_total * Fraction(width, 3600)),
                    "ok": before_total == after_total,
                }
            )
        cell_checks = []
        for index in range(cells):
            cell_tasks = {}
            total = Fraction(0)
            for task in ordered:
                if scheduled[task["id"]] <= index < scheduled[task["id"]] + task["duration"]:
                    cell_tasks[task["id"]] = task_power[task["id"]]
                    total += task_power[task["id"]]
            okay = base[parent][index] + total == after[parent][index]
            cell_checks.append(
                {
                    "index": index,
                    "plant_base_microkw": rational(base[parent][index]),
                    "scheduled_microkw": rational(total),
                    "plant_after_microkw": rational(after[parent][index]),
                    "ok": okay,
                    "tasks": sorted(cell_tasks),
                }
            )
        meter_peaks = {}
        for meter in sorted(involved):
            before_index = max(
                range(cells), key=lambda index: (original[meter][index], -index)
            )
            after_index = max(
                range(cells), key=lambda index: (after[meter][index], -index)
            )
            meter_peaks[meter] = {
                "before": {
                    "index": before_index,
                    "start": intervals[before_index][0],
                    "microkw": rational(original[meter][before_index]),
                },
                "after": {
                    "index": after_index,
                    "start": intervals[after_index][0],
                    "microkw": rational(after[meter][after_index]),
                },
            }
        # Each meter verified independently: base plus every scheduled task
        # attributable to that meter must equal its after curve cell by cell.
        for meter in sorted(involved):
            for index in range(cells):
                added = Fraction(0)
                for task in ordered:
                    if meter in paths[task["meter"]] and (
                        scheduled[task["id"]]
                        <= index
                        < scheduled[task["id"]] + task["duration"]
                    ):
                        added += task_power[task["id"]]
                if base[meter][index] + added != after[meter][index]:
                    reject(
                        "integrity",
                        "meter curve does not balance after rescheduling",
                        meter=meter,
                        index=index,
                    )
        cell_checks = []
        for index in range(cells):
            cell_tasks = {}
            total = Fraction(0)
            for task in ordered:
                if (
                    scheduled[task["id"]]
                    <= index
                    < scheduled[task["id"]] + task["duration"]
                ):
                    cell_tasks[task["id"]] = task_power[task["id"]]
                    total += task_power[task["id"]]
            okay = base[parent][index] + total == after[parent][index]
            cell_checks.append(
                {
                    "index": index,
                    "plant_base_microkw": rational(base[parent][index]),
                    "scheduled_microkw": rational(total),
                    "plant_after_microkw": rational(after[parent][index]),
                    "ok": okay,
                    "tasks": sorted(cell_tasks),
                }
            )
        curves = []
        for index in range(cells):
            curves.append(
                {
                    "index": index,
                    "start": intervals[index][0],
                    "end": intervals[index][1],
                    "before_microkw": {
                        meter: rational(original[meter][index])
                        for meter in sorted(involved)
                    },
                    "base_microkw": {
                        meter: rational(base[meter][index])
                        for meter in sorted(involved)
                    },
                    "after_microkw": {
                        meter: rational(after[meter][index])
                        for meter in sorted(involved)
                    },
                    "capacity_microkw": capacity,
                    "capacity_ok": after[parent][index] <= capacity,
                    "scheduled_tasks": sorted(
                        task["id"]
                        for task in ordered
                        if scheduled[task["id"]]
                        <= index
                        < scheduled[task["id"]] + task["duration"]
                    ),
                }
            )
        cost = {
            "currency": currency,
            "before_exact_minor": rational(before_cost),
            "before_minor": round_even(before_cost),
            "after_exact_minor": rational(after_cost),
            "after_minor": round_even(after_cost),
            "delta_exact_minor": rational(after_cost - before_cost),
            "delta_minor": round_even(after_cost) - round_even(before_cost),
        }
        result = {
            **common,
            "status": STATUS_OPTIMAL,
            "candidates": candidate_rows,
            "constraints": [
                {"before": left, "after": right} for left, right in constraints
            ],
            "search": search,
            "schedule": {
                "starts": {task["id"]: scheduled[task["id"]] for task in ordered},
                "max_demand_microkw": rational(best["objective"][0]),
                "max_demand_index": after_peak_index,
                "max_demand_start": intervals[after_peak_index][0],
                "cost_exact_minor": rational(after_cost),
                "distance_cells": sum(
                    row["movement_cells"] for row in arrangement
                ),
            },
            "tasks": arrangement,
            "after": {
                "peak_index": after_peak_index,
                "peak_start": intervals[after_peak_index][0],
                "peak_microkw": rational(after[parent][after_peak_index]),
            },
            "curves": curves,
            "demand": {
                "before_peak": {
                    "index": before_peak,
                    "start": intervals[before_peak][0],
                    "microkw": rational(original[parent][before_peak]),
                },
                "after_peak": {
                    "index": after_peak_index,
                    "start": intervals[after_peak_index][0],
                    "microkw": rational(after[parent][after_peak_index]),
                },
                "meter_peaks": meter_peaks,
                "key_windows": [
                    {
                        "kind": "before_peak",
                        "index": before_peak,
                        "start": intervals[before_peak][0],
                        "microkw": rational(original[parent][before_peak]),
                    },
                    {
                        "kind": "after_peak",
                        "index": after_peak_index,
                        "start": intervals[after_peak_index][0],
                        "microkw": rational(after[parent][after_peak_index]),
                    },
                ],
                "missing_plant_windows": [],
            },
            "cost": cost,
            "checks": {
                "energy_conservation": conservation,
                "capacity": {
                    "ok": all(row["ok"] for row in capacity_rows),
                    "rows": capacity_rows,
                },
                "dependencies": {
                    "ok": all(row["ok"] for row in dependency_checks),
                    "rows": dependency_checks,
                },
                "cell_balance": {
                    "ok": all(row["ok"] for row in cell_checks),
                    "rows": cell_checks,
                },
            },
            "formal_records": {
                "simulation_read_only": True,
                "readings_changed": False,
                "closures_changed": False,
            },
            "calculation_version": 1,
        }
        result["result_digest"] = fingerprint(result)
        return result
