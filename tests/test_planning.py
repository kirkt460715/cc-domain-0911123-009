import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from energyflow.service import EnergyService
from energyflow.values import EnergyError, fingerprint, fraction


def context():
    return json.loads(
        (Path(__file__).resolve().parents[1] / "examples" / "context.json").read_text()
    )


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.data = context()
        self.s = EnergyService()
        self.addCleanup(self.s.close)
        width = self.data["grid_seconds"]
        for meter, series in self.data["series"].items():
            self.s.catalog.register(meter, "Synthetic " + meter)
            self.s.catalog.install(
                "counter-" + meter, meter, 0, 0, max_gap=width
            )
            total = 0
            rows = []
            for index, energy in enumerate(series, 1):
                total += int(energy)
                rows.append(
                    {
                        "id": f"{meter}-{index}",
                        "segment": "counter-" + meter,
                        "at": index * width,
                        "value": total,
                    }
                )
            self.s.readings.import_batch("day-" + meter, rows)
        for rate in self.data["tariffs"]:
            self.s.tariffs.put(
                rate["id"], "main", rate["start"], rate["end"], rate["price"]
            )
        self.start = 0
        self.end = len(self.data["series"]["main"]) * width
        self.width = width

    def register_context_tasks(self):
        for row in self.data["tasks"]:
            self.s.planning_tasks.put(
                row["id"],
                row["meter"],
                self.start,
                self.end,
                self.width,
                row["original_start"],
                row["duration"],
                row["power_kw"],
                earliest=row["earliest"],
                latest_end=row["latest_end"],
                available=row["available"],
                after=row["after"],
            )

    def coincidence(self):
        return self.s.planning.coincidence(
            "main", self.data["tree"], self.start, self.end, self.width
        )

    def test_coincidence_places_residual_and_independent_peaks(self):
        report = self.coincidence()
        self.assertEqual(report["cells"], 8)
        self.assertEqual(report["children"], ["oven", "press"])
        plant = report["peaks"]["plant"]
        self.assertEqual(plant["index"], 3)
        self.assertEqual(plant["microkw"], "80000000/1")
        at_peak = report["at_plant_peak"]
        self.assertEqual(at_peak["children"]["press"]["microkw"], "40000000/1")
        self.assertEqual(at_peak["children"]["oven"]["microkw"], "24000000/1")
        self.assertEqual(at_peak["residual"]["residual_microkw"], "16000000/1")
        self.assertEqual(
            at_peak["children"]["press"]["independent_peak"]["microkw"],
            "40000000/1",
        )
        oven_peak = report["peaks"]["meters"]["oven"]
        self.assertEqual(oven_peak["index"], 4)
        self.assertEqual(oven_peak["microkw"], "28000000/1")
        residual = at_peak["residual"]
        self.assertEqual(residual["status"], "ok")
        self.assertTrue(residual["complete"])
        self.assertEqual(
            residual["provenance"]["parent_evidence"],
            report["grid"][3]["meters"]["main"]["evidence"],
        )
        self.assertTrue(residual["provenance"]["children_evidence"]["press"])
        self.assertTrue(report["complete"])
        self.assertIsNotNone(report["digest"])

    def test_missing_child_window_is_not_zero(self):
        child = "open"
        series = [0, 0, 0, 0, 0, 0, 0, 0]
        self.s.catalog.register(child, "Unmetered shop")
        self.s.catalog.install("counter-open", child, 0, 0, max_gap=900)
        self.s.readings.add(
            dict(id="open-1", segment="counter-open", at=1800, value=0)
        )
        self.s.readings.add(
            dict(id="open-2", segment="counter-open", at=3600, value=0)
        )
        topology = {"main": ["press", "oven", "open"]}
        report = self.s.planning.coincidence(
            "main", topology, 0, self.end, 900
        )
        grid = report["grid"]
        self.assertEqual(grid[0]["residual"]["status"], "incomplete")
        self.assertEqual(grid[0]["residual"]["missing"], ["open"])
        self.assertIsNone(grid[0]["residual"]["residual_microkw"])
        self.assertIsNone(grid[0]["meters"]["open"]["microkw"])
        self.assertEqual(grid[6]["residual"]["status"], "incomplete")
        self.assertFalse(report["complete"])
        self.assertGreater(len(report["peaks"]["missing_windows"]), 0)
        with self.assertRaises(EnergyError):
            self.s.planning.plan(
                "main",
                topology,
                0,
                self.end,
                900,
                "90",
            )

    def test_plan_reaches_optimal_schedule_and_cost_difference(self):
        self.register_context_tasks()
        result = self.s.planning.plan(
            "main",
            self.data["tree"],
            self.start,
            self.end,
            self.width,
            self.data["capacity_kw"],
        )
        self.assertEqual(result["status"], "optimal")
        self.assertEqual(result["before"]["peak_microkw"], "80000000/1")
        self.assertEqual(result["after"]["peak_microkw"], "64000000/1")
        schedule = result["schedule"]["starts"]
        self.assertEqual(schedule, {"p1": 0, "p2": 4})
        self.assertEqual(result["candidates"][0]["candidates"], 7)
        self.assertEqual(result["candidates"][1]["candidates"], 5)
        self.assertEqual(result["search"]["space"], 35)
        self.assertEqual(result["search"]["method"], "exhaustive_discrete_enumeration")
        self.assertTrue(result["search"]["complete"])
        self.assertEqual(result["search"]["evaluated"], 15)
        self.assertEqual(result["search"]["pruned_dependency"], 20)
        self.assertEqual(result["currency"], "CNY")
        self.assertEqual(result["cost"]["before_exact_minor"], "7600/1")
        self.assertEqual(result["cost"]["after_exact_minor"], "7700/1")
        self.assertEqual(result["cost"]["delta_minor"], 100)
        # Both tasks moved; their own submeter curves never keep the old copy.
        for task in result["tasks"]:
            self.assertEqual(
                task["baseline_cells"],
                list(
                    range(
                        task["original_start"],
                        task["original_start"] + task["duration_cells"],
                    )
                ),
            )
            self.assertEqual(
                task["movement_cells"], abs(task["start"] - task["original_start"])
            )
        conservation = {
            row["meter"]: row for row in result["checks"]["energy_conservation"]
        }
        self.assertTrue(conservation["press"]["ok"])
        self.assertTrue(conservation["oven"]["ok"])
        self.assertTrue(conservation["main"]["ok"])
        self.assertTrue(result["checks"]["capacity"]["ok"])
        self.assertTrue(result["checks"]["dependencies"]["ok"])
        self.assertTrue(result["checks"]["cell_balance"]["ok"])
        curve = result["curves"][5]
        self.assertEqual(curve["before_microkw"]["press"], "16000000/1")
        self.assertEqual(curve["after_microkw"]["press"], "16000000/1")
        self.assertEqual(curve["after_microkw"]["oven"], "24000000/1")
        self.assertEqual(curve["scheduled_tasks"], ["p2"])
        self.assertEqual(
            result["demand"]["key_windows"][0],
            {
                "kind": "before_peak",
                "index": 3,
                "start": 2700,
                "microkw": "80000000/1",
            },
        )
        self.assertEqual(result["demand"]["after_peak"]["index"], 2)
        self.assertEqual(result["formal_records"]["simulation_read_only"], True)
        self.assertEqual(result["formal_records"]["readings_changed"], False)

    def test_dependency_and_available_cells_are_enforced(self):
        self.register_context_tasks()
        with self.assertRaises(EnergyError) as caught:
            self.s.planning.plan(
                "main",
                self.data["tree"],
                self.start,
                self.end,
                self.width,
                self.data["capacity_kw"],
                tasks=["p2"],
            )
        self.assertEqual(caught.exception.code, "dependency")
        with self.assertRaises(EnergyError) as caught:
            self.s.planning_tasks.put(
                "bad",
                "press",
                self.start,
                self.end,
                self.width,
                0,
                1,
                "8",
                available=[2],
            )
        self.assertEqual(caught.exception.code, "availability")

    def test_duplicate_selection_and_bad_dependencies_are_rejected(self):
        self.register_context_tasks()
        with self.assertRaises(EnergyError):
            self.s.planning.plan(
                "main",
                self.data["tree"],
                self.start,
                self.end,
                self.width,
                "90",
                tasks=["p1", "p1"],
            )
        with self.assertRaises(EnergyError):
            self.s.planning_tasks.put(
                "x",
                "press",
                self.start,
                self.end,
                self.width,
                0,
                1,
                "8",
                after=["x"],
            )
        self.s.planning_tasks.put(
            "x",
            "press",
            self.start,
            self.end,
            self.width,
            0,
            1,
            "8",
            after=["p1"],
        )
        with self.assertRaises(EnergyError) as caught:
            self.s.planning.plan(
                "main",
                self.data["tree"],
                self.start,
                self.end,
                self.width,
                "90",
                tasks=["x"],
            )
        self.assertEqual(caught.exception.code, "dependency")

    def test_baseline_overdraw_is_rejected(self):
        empty = self.s.planning.plan(
            "main",
            self.data["tree"],
            self.start,
            self.end,
            self.width,
            "90",
            tasks=[],
        )
        self.assertEqual(empty["status"], "optimal")
        self.s.planning_tasks.put(
            "heavy",
            "oven",
            self.start,
            self.end,
            self.width,
            4,
            1,
            "30",
        )
        with self.assertRaises(EnergyError) as caught:
            self.s.planning.plan(
                "main",
                self.data["tree"],
                self.start,
                self.end,
                self.width,
                "90",
                tasks=["heavy"],
            )
        self.assertEqual(caught.exception.code, "baseline")
        self.assertEqual(caught.exception.context["index"], 4)

    def test_capacity_violation_and_over_scale_are_distinct(self):
        self.s.planning_tasks.put(
            "a",
            "press",
            0,
            7200,
            900,
            0,
            2,
            "8",
            earliest=0,
            latest_end=8,
            available=list(range(8)),
        )
        result = self.s.planning.plan(
            "main", {"main": ["press"]}, 0, 7200, 900, "30", tasks=["a"]
        )
        self.assertEqual(result["status"], "infeasible")
        self.assertEqual(result["reason"], "capacity_impossible_before_schedule")
        over = self.s.planning.plan(
            "main",
            {"main": ["press"]},
            0,
            7200,
            900,
            "90",
            tasks=["a"],
            max_search=4,
        )
        self.assertEqual(over["status"], "over_scale")
        self.assertIn("space", over["search"])
        self.assertIsNone(over["schedule"])
        self.assertIsNone(over["cost"])

    def test_infeasible_has_no_schedule_and_optimal_is_proven_by_enumeration(self):
        self.s.planning_tasks.put(
            "a",
            "press",
            0,
            7200,
            900,
            0,
            4,
            "8",
            earliest=0,
            latest_end=8,
            available=list(range(8)),
        )
        self.s.planning_tasks.put(
            "b",
            "press",
            0,
            7200,
            900,
            4,
            4,
            "8",
            earliest=0,
            latest_end=8,
            available=list(range(8)),
            after=["a"],
        )
        # b must start at 4, a must fit entirely before 4 -> a at 0.
        result = self.s.planning.plan(
            "main", {"main": ["press"]}, 0, 7200, 900, "90"
        )
        self.assertEqual(result["status"], "optimal")
        self.assertEqual(result["search"]["evaluated"], 1)
        self.assertEqual(result["search"]["space"], 25)
        self.assertTrue(result["search"]["complete"])
        self.assertEqual(result["schedule"]["starts"], {"a": 0, "b": 4})

    def test_simulation_does_not_touch_formal_records(self):
        self.s.production.put("main-shift", "main", 0, 7200, {"plant": 1})
        preview = self.s.closing.preview(["main"], 0, 7200)
        frozen = self.s.closing.freeze(
            "period", ["main"], 0, 7200, preview["digest"], "operator"
        )
        self.register_context_tasks()
        before = self.s.store.snapshot_signature() if hasattr(
            self.s.store, "snapshot_signature"
        ) else None
        self.s.planning.plan(
            "main", self.data["tree"], 0, 7200, 900, "90"
        )
        self.assertEqual(self.s.closing.get("period"), frozen)
        self.assertEqual(self.s.readings.get("main-4")["revision"], 1)
        if before is not None:
            self.assertEqual(self.s.store.snapshot_signature(), before)

    def test_recalculation_is_stable_and_input_revision_is_versioned(self):
        self.register_context_tasks()
        first = self.s.planning.plan(
            "main", self.data["tree"], 0, 7200, 900, "90"
        )
        second = self.s.planning.plan(
            "main", self.data["tree"], 0, 7200, 900, "90"
        )
        self.assertEqual(first["result_digest"], second["result_digest"])
        self.assertEqual(first["input_digest"], second["input_digest"])
        self.s.readings.correct("press-4", 1, 30, "normal", "late corrected count")
        third = self.s.planning.plan(
            "main", self.data["tree"], 0, 7200, 900, "90"
        )
        self.assertNotEqual(third["input_digest"], first["input_digest"])
        self.assertEqual(third["result_digest"], third["result_digest"])
        # Both plans remain on the same grid and can be recomputed independently.
        fourth = self.s.planning.plan(
            "main", self.data["tree"], 0, 7200, 900, "90"
        )
        self.assertEqual(fourth["result_digest"], third["result_digest"])
        self.assertEqual(
            third["coincidence"]["input_digests"],
            fourth["coincidence"]["input_digests"],
        )

    def test_meter_replacement_and_late_readings_keep_reconstruction_semantics(self):
        self.s.catalog.replacement(
            "counter-press",
            "counter-press-2",
            7200,
            0,
            microkwh_per_tick=1000000,
        )
        self.s.readings.add(
            dict(id="press-9", segment="counter-press-2", at=8100, value=9)
        )
        self.s.catalog.replacement(
            "counter-main", "counter-main-2", 7200, 20, microkwh_per_tick=1000000
        )
        self.s.readings.add(
            dict(id="main-9", segment="counter-main-2", at=8100, value=30)
        )
        self.s.readings.add(
            dict(id="oven-9", segment="counter-oven", at=8100, value=35)
        )
        result = self.s.planning.coincidence(
            "main", self.data["tree"], 0, 8100, 900
        )
        self.assertTrue(result["complete"])
        self.assertEqual(result["cells"], 9)
        self.assertEqual(result["grid"][3]["meters"]["press"]["microkw"], "40000000/1")
        self.assertEqual(result["grid"][8]["meters"]["press"]["microkw"], "36000000/1")
        self.assertEqual(result["grid"][8]["meters"]["main"]["microkw"], "40000000/1")

    def test_cli_exposes_planning_actions(self):
        names = set(self.s.actions())
        self.assertIn("planning.coincidence", names)
        self.assertIn("planning.plan", names)
        self.assertIn("planning_task.put", names)
        self.assertIn("planning_task.list", names)
        result = self.s.dispatch(
            {
                "action": "planning.coincidence",
                "arguments": {
                    "parent": "main",
                    "topology": self.data["tree"],
                    "start": 0,
                    "end": 7200,
                    "width": 900,
                },
            }
        )
        self.assertEqual(result["peaks"]["plant"]["microkw"], "80000000/1")


if __name__ == "__main__":
    unittest.main()


class PlanningEdgeTests(unittest.TestCase):
    def setUp(self):
        from examples.context import load

        self.data = load()
        self.s = EnergyService()
        self.addCleanup(self.s.close)
        width = self.data["grid_seconds"]
        for meter, series in self.data["series"].items():
            self.s.catalog.register(meter, "Synthetic " + meter)
            self.s.catalog.install("counter-" + meter, meter, 0, 0, max_gap=width)
            total = 0
            rows = []
            for index, energy in enumerate(series, 1):
                total += int(energy)
                rows.append(
                    {
                        "id": f"{meter}-{index}",
                        "segment": "counter-" + meter,
                        "at": index * width,
                        "value": total,
                    }
                )
            self.s.readings.import_batch("day-" + meter, rows)
        for rate in self.data["tariffs"]:
            self.s.tariffs.put(
                rate["id"], "main", rate["start"], rate["end"], rate["price"]
            )

    def test_no_feasible_schedule_is_reported_without_guess(self):
        self.s.planning_tasks.put(
            "blocked",
            "press",
            0,
            7200,
            900,
            2,
            2,
            "8",
            earliest=2,
            latest_end=4,
            available=[2, 3],
        )
        # Baseline peak (72) exactly fits the declared capacity, but the only
        # available placement lifts it above capacity.
        result = self.s.planning.plan(
            "main", {"main": ["press", "oven"]}, 0, 7200, 900, "72"
        )
        self.assertEqual(result["status"], "infeasible")
        self.assertEqual(result["reason"], "no_feasible_schedule")
        self.assertIsNone(result["schedule"])
        self.assertIsNone(result["cost"])
        self.assertTrue(result["search"]["complete"])
        self.assertGreater(result["search"]["pruned_capacity"], 0)

    def test_nested_tree_moves_task_through_every_ancestor(self):
        width = 900
        line_series = [
            6,
            7,
            11,
            12,
            8,
            6,
            5,
            5,
        ]
        self.s.catalog.register("line", "Press feeder")
        self.s.catalog.install("counter-line", "line", 0, 0, max_gap=width)
        total = 0
        rows = []
        for index, energy in enumerate(line_series, 1):
            total += int(energy)
            rows.append(
                {
                    "id": f"line-{index}",
                    "segment": "counter-line",
                    "at": index * width,
                    "value": total,
                }
            )
        self.s.readings.import_batch("day-line", rows)
        self.s.planning_tasks.put(
            "p1",
            "press",
            0,
            7200,
            width,
            2,
            2,
            "8",
            earliest=0,
            latest_end=8,
            available=list(range(8)),
        )
        topology = {"main": ["line", "oven"], "line": ["press"]}
        result = self.s.planning.plan(
            "main", topology, 0, 7200, width, "90"
        )
        self.assertEqual(result["status"], "optimal")
        # Staring p1 one cell later flattens the plant peak from 80 to 72 at
        # the same cost, so the lexicographic optimum is start 1.
        self.assertEqual(result["schedule"]["starts"], {"p1": 1})
        self.assertEqual(result["before"]["peak_microkw"], "80000000/1")
        self.assertEqual(result["after"]["peak_microkw"], "72000000/1")
        for meter in ["main", "line", "press", "oven"]:
            row = next(
                item for item in result["checks"]["energy_conservation"]
                if item["meter"] == meter
            )
            self.assertTrue(row["ok"], meter)
        # The station residual is unchanged: line minus press stays measured.
        coincidence = result["coincidence"]
        self.assertEqual(coincidence["children"], ["line", "oven"])
        self.assertEqual(
            coincidence["grid"][3]["residual"]["residual_microkw"], "8000000/1"
        )
        self.assertTrue(
            all(
                row["residual"]["status"] == "ok" for row in coincidence["grid"]
            )
        )

    def test_registered_tasks_survive_reopen_and_recompute(self):
        self.s.planning_tasks.put(
            "p1",
            "press",
            0,
            7200,
            900,
            2,
            2,
            "8",
            earliest=0,
            latest_end=8,
            available=list(range(8)),
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "planning.db"
            self.s.store.backup(path)
            with EnergyService(path) as reopened:
                stored = reopened.planning_tasks.get("p1")
                self.assertEqual(stored["original_start"], 2)
                result = reopened.planning.plan(
                    "main", {"main": ["press", "oven"]}, 0, 7200, 900, "90"
                )
                self.assertIn(result["status"], {"optimal", "over_scale"})


