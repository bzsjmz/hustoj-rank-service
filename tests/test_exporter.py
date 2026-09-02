from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from app.exporter import RankExporter
from app.models import HistoricalRankEntry, RankBaseline, RankEntry
from app.student_ids import StudentIdLayout


class ExporterTests(unittest.TestCase):
    def test_exports_json_and_csv_without_authentication_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            share_dir = Path(directory) / "share"
            exporter = RankExporter(share_dir)
            entries = [
                RankEntry(1, "202600000001", "昵称,一", 3, 4, 75.0, "初学乍练"),
                RankEntry(2, "202600000002", "", 1, 2, 50.0, "初学乍练"),
            ]
            exporter.export(entries, "2026", "2026-08-23T10:00:00+08:00", 7)

            document = json.loads((share_dir / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(7, document["snapshot_id"])
            self.assertEqual(2, document["user_count"])
            self.assertEqual("202600000001", document["users"][0]["user_id"])
            self.assertNotIn("cookie", json.dumps(document).lower())
            self.assertNotIn("csrf", json.dumps(document).lower())

            with (share_dir / "latest.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual("昵称,一", rows[0]["nickname"])
            self.assertEqual("2026-08-23T10:00:00+08:00", rows[1]["fetched_at"])
            self.assertEqual(0o644, (share_dir / "latest.json").stat().st_mode & 0o777)

    def test_exports_historical_roster_and_coverage_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            share_dir = Path(directory) / "share"
            exporter = RankExporter(share_dir)
            exporter.export(
                [
                    RankEntry(1, "202600000002", "B", 3, 4, 75.0, "L1"),
                    RankEntry(2, "202600000003", "C", 2, 3, 66.667, "L1"),
                ],
                "2026",
                "2026-08-23T10:00:00+08:00",
                7,
                historical_roster_user_ids={
                    "202600000001", "202600000002", "202600000003"
                },
            )

            document = json.loads((share_dir / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(3, document["historical_roster"]["user_count"])
            self.assertEqual(1, document["missing_user_count"])
            self.assertEqual(2 / 3, document["coverage_rate"])

    def test_exports_multi_cohort_lookup_with_historical_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exporter = RankExporter(Path(directory))
            exporter.export_lookup(
                [RankEntry(1, "202300000001", "23级", 3, 4, 75.0, "L1")],
                "2026-08-23T10:00:00+08:00",
                7,
                ("2023", "2024", "2025", "2026"),
                [
                    HistoricalRankEntry(
                        RankEntry(8, "202600000001", "历史", 2, 3, 66.7, "L1"),
                        "2026-08-23T08:00:00+08:00",
                    )
                ],
            )

            document = json.loads((Path(directory) / "lookup.json").read_text(encoding="utf-8"))
            self.assertEqual(["2023", "2024", "2025", "2026"], document["lookup_prefixes"])
            self.assertEqual(2, document["user_count"])
            self.assertTrue(document["users"][1]["is_historical"])
            self.assertEqual("2026-08-23T08:00:00+08:00", document["users"][1]["last_seen_at"])

    def test_exports_academic_labels_for_read_only_bot_use(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exporter = RankExporter(Path(directory))
            exporter.export_academic_labels("2026")
            document = json.loads((Path(directory) / "academic-labels.json").read_text())
            self.assertEqual("2026", document["prefix"])
            self.assertEqual("移软", document["major_labels"]["0723"])
            self.assertEqual(0o644, (Path(directory) / "academic-labels.json").stat().st_mode & 0o777)


    def test_refuses_empty_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exporter = RankExporter(Path(directory))
            with self.assertRaises(ValueError):
                exporter.export([], "2026", "now", 1)

    def test_exports_daily_rank_and_activity_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exporter = RankExporter(Path(directory))
            baseline = RankBaseline(
                snapshot_id=1,
                fetched_at="2026-08-23T08:00:00+08:00",
                entries=(
                    RankEntry(1, "alice", "Alice", 5, 10, 50.0, "L1"),
                    RankEntry(2, "bob", "Bob", 5, 10, 50.0, "L1"),
                ),
            )
            current = [
                RankEntry(1, "bob", "Bob", 8, 14, 57.14, "L2"),
                RankEntry(2, "alice", "Alice", 5, 11, 45.45, "L1"),
                RankEntry(3, "new", "New", 1, 1, 100.0, "L1"),
            ]

            exporter.export(
                current,
                "2026",
                "2026-08-23T10:00:00+08:00",
                2,
                baseline,
            )

            document = json.loads(
                (Path(directory) / "daily.json").read_text(encoding="utf-8")
            )
            by_user = {row["user_id"]: row for row in document["changes"]}
            self.assertEqual(1, by_user["bob"]["rank_change"])
            self.assertEqual(3, by_user["bob"]["accepted_change"])
            self.assertEqual(-1, by_user["alice"]["rank_change"])
            self.assertTrue(by_user["new"]["is_new"])
            self.assertIsNone(by_user["new"]["baseline_rank"])
            self.assertEqual(0o644, (Path(directory) / "daily.json").stat().st_mode & 0o777)

    def test_exports_weekly_and_monthly_ranklists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exporter = RankExporter(Path(directory))
            entries = [RankEntry(1, "20260001", "A", 3, 4, 75.0, "L1")]

            exporter.export_scoped(
                entries, "2026", "2026-08-23T10:00:00+08:00", "w"
            )
            exporter.export_scoped([], "2026", "2026-08-23T10:00:00+08:00", "m")

            weekly = json.loads(
                (Path(directory) / "weekly.json").read_text(encoding="utf-8")
            )
            monthly = json.loads(
                (Path(directory) / "monthly.json").read_text(encoding="utf-8")
            )
            self.assertEqual("w", weekly["scope"])
            self.assertEqual(1, weekly["user_count"])
            self.assertEqual("m", monthly["scope"])
            self.assertEqual(0, monthly["user_count"])

    def test_exports_college_ranklist_with_split_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            share_dir = Path(directory) / "share"
            exporter = RankExporter(share_dir)
            exporter.export_college(
                [RankEntry(1, "202600000001", "A", 3, 4, 75.0, "L1")],
                "2026",
                "2026-08-23T10:00:00+08:00",
                7,
                "计院",
                253,
            )

            document = json.loads(
                (share_dir / "computer-college.json").read_text(encoding="utf-8")
            )
            self.assertEqual("college", document["scope"])
            self.assertEqual("计院", document["college"])
            self.assertEqual(253, document["split_snapshot_id"])
            self.assertEqual(
                0o644, (share_dir / "computer-college.json").stat().st_mode & 0o777
            )

    def test_exports_class_ranklist_to_a_safe_separate_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            share_dir = Path(directory) / "share"
            exporter = RankExporter(share_dir)
            exporter.export_class(
                [RankEntry(1, "202607230612", "A", 3, 4, 75.0, "L1")],
                "2026",
                "2026-08-23T10:00:00+08:00",
                7,
                "2026072306",
            )

            document = json.loads(
                (share_dir / "class-ranklists" / "2026072306.json").read_text(encoding="utf-8")
            )
            self.assertEqual("class", document["scope"])
            self.assertEqual("2026072306", document["class_id"])
            self.assertEqual(1, document["users"][0]["rank"])
            self.assertFalse(document["users"][0]["is_historical"])

    def test_exports_major_ranklist_to_a_safe_separate_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            share_dir = Path(directory) / "share"
            exporter = RankExporter(share_dir)
            exporter.export_major(
                [RankEntry(1, "202607230612", "A", 3, 4, 75.0, "L1")],
                "2026", "2026-08-23T10:00:00+08:00", 7, "20260723"
            )

            document = json.loads(
                (share_dir / "major-ranklists" / "20260723.json").read_text(encoding="utf-8")
            )
            self.assertEqual("major", document["scope"])
            self.assertEqual("20260723", document["major_id"])

    def test_exports_render_registries_and_intensity_data_without_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            share_dir = Path(directory)
            exporter = RankExporter(share_dir)
            layout = StudentIdLayout.conventional("2026")
            entries = [RankEntry(1, "202607230612", "A", 3, 4, 75.0, "L1")]

            exporter.export_entity_registry(
                "class", {"2026072306": entries}, layout, "now", 7
            )
            exporter.export_entity_registry(
                "major", {"20260723": entries}, layout, "now", 7
            )
            exporter.export_intensity(entries, "2026", "now", 7, "class")

            class_registry = json.loads(
                (share_dir / "class-images" / "manifest.json").read_text()
            )
            major_registry = json.loads(
                (share_dir / "major-images" / "manifest.json").read_text()
            )
            intensity = json.loads(
                (share_dir / "class-intensity.json").read_text()
            )
            self.assertEqual("2026072306", class_registry["classes"][0]["class_id"])
            self.assertEqual(1, major_registry["majors"][0]["page_count"])
            self.assertEqual("class-intensity", intensity["scope"])
            self.assertFalse((share_dir / "class-images" / "2026072306").exists())
