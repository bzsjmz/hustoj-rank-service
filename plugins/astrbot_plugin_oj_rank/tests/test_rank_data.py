import csv
import io
import ast
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from rank_data import (
    AcademicLabels,
    RankDataError,
    build_statistics_csv,
    change_label,
    find_changes,
    find_users,
    is_allowed_group_command,
    is_class_id,
    is_major_id,
    load_class_image_registry,
    load_major_image_registry,
    load_daily,
    load_academic_labels,
    load_image_manifest,
    load_snapshot,
)


class RankDataTests(unittest.TestCase):
    def _write(self, directory: str, payload: dict) -> Path:
        path = Path(directory) / "latest.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    @staticmethod
    def _payload() -> dict:
        users = [
            {
                "rank": 1,
                "user_id": "20260001",
                "nickname": "小明",
                "accepted": 20,
                "submitted": 40,
                "ratio": "50.00%",
                "level": "2",
            },
            {
                "rank": 2,
                "user_id": "20260002",
                "nickname": "小明同学",
                "accepted": 10,
                "submitted": 25,
                "ratio": "40.00%",
                "level": "1",
            },
        ]
        return {
            "schema_version": 1,
            "snapshot_id": 7,
            "fetched_at": "2026-08-23T09:43:49+08:00",
            "prefix": "2026",
            "user_count": len(users),
            "users": users,
        }

    def test_load_and_find_prefers_exact_user_id(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = load_snapshot(self._write(directory, self._payload()))
            self.assertEqual(snapshot.user_count, 2)
            self.assertEqual(find_users(snapshot, "20260002")[0]["rank"], 2)
            now = datetime.fromisoformat("2026-08-23T10:13:49+08:00")
            self.assertEqual(snapshot.age_minutes(now.astimezone(timezone.utc)), 30)

    def test_exact_nickname_precedes_partial_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = load_snapshot(self._write(directory, self._payload()))
            matches = find_users(snapshot, "小明")
            self.assertEqual([row["user_id"] for row in matches], ["20260001"])

    def test_loads_multi_cohort_lookup_and_preserves_history_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = {
                "schema_version": 1,
                "snapshot_id": 7,
                "fetched_at": "2026-08-23T09:43:49+08:00",
                "prefix": "2023-2026",
                "lookup_prefixes": ["2023", "2024", "2025", "2026"],
                "user_count": 2,
                "users": [
                    {"rank": 1, "user_id": "202300000001", "nickname": "23级", "accepted": 20, "submitted": 40, "ratio": "50.00%", "level": "2"},
                    {"rank": 8, "user_id": "202600000001", "nickname": "历史", "accepted": 10, "submitted": 25, "ratio": "40.00%", "level": "1", "is_historical": True, "last_seen_at": "2026-08-23T08:00:00+08:00"},
                ],
            }
            snapshot = load_snapshot(self._write(directory, payload))
            historical = find_users(snapshot, "202600000001")
            self.assertEqual("历史", historical[0]["nickname"])
            self.assertTrue(historical[0]["is_historical"])

    def test_count_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = self._payload()
            payload["user_count"] = 99
            with self.assertRaises(RankDataError):
                load_snapshot(self._write(directory, payload))

    def test_load_and_query_daily_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = {
                "schema_version": 1,
                "snapshot_id": 8,
                "fetched_at": "2026-08-23T10:00:00+08:00",
                "baseline_snapshot_id": 1,
                "baseline_fetched_at": "2026-08-23T08:00:00+08:00",
                "prefix": "2026",
                "user_count": 2,
                "changes": [
                    {
                        **self._payload()["users"][0],
                        "baseline_rank": 3,
                        "rank_change": 2,
                        "accepted_change": 4,
                        "submitted_change": 6,
                        "is_new": False,
                    },
                    {
                        **self._payload()["users"][1],
                        "baseline_rank": None,
                        "rank_change": None,
                        "accepted_change": None,
                        "submitted_change": None,
                        "is_new": True,
                    },
                ],
            }
            snapshot = load_daily(self._write(directory, payload))
            self.assertEqual(1, len(find_changes(snapshot, "20260001")))
            self.assertEqual("上升 2 名", change_label(snapshot.changes[0]))
            self.assertEqual("今天首次上榜", change_label(snapshot.changes[1]))

    def test_loads_and_resolves_academic_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "academic-labels.json"
            path.write_text(json.dumps({
                "schema_version": 1, "prefix": "2026",
                "major_labels": {"0723": "移软", "0703": "网工"},
            }), encoding="utf-8")
            labels = load_academic_labels(path)
            self.assertEqual("20260723", labels.resolve_major("移软"))
            self.assertEqual("2026072301", labels.resolve_class("移软01班"))
            self.assertEqual("20260703", labels.resolve_major("20260703"))
            self.assertEqual("移软", labels.major_name("20260723"))
            self.assertEqual("移软01班", labels.class_name("2026072301"))
            self.assertIsNone(labels.resolve_major("不存在"))


    def test_builds_stable_statistics_csv_and_filters_inactive_students(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = {
                "schema_version": 1, "snapshot_id": 9,
                "fetched_at": "2026-08-29T10:00:00+08:00", "prefix": "2026",
                "user_count": 3,
                "users": [
                    {"rank": 1, "user_id": "202607230101", "nickname": "甲", "accepted": 5, "submitted": 10, "ratio": "50%", "level": "1"},
                    {"rank": 2, "user_id": "202607230102", "nickname": "乙", "accepted": 0, "submitted": 0, "ratio": "0%", "level": "1"},
                    {"rank": 3, "user_id": "202607030101", "nickname": "丙", "accepted": 0, "submitted": 2, "ratio": "0%", "level": "1"},
                ],
            }
            snapshot = load_snapshot(self._write(directory, payload))
            labels = AcademicLabels("2026", {"0723": "移软", "0703": "网工"})
            rows = list(csv.DictReader(io.StringIO(build_statistics_csv(snapshot, labels))))
            self.assertEqual("snapshot", rows[0]["row_type"])
            self.assertEqual("3", rows[0]["total_students"])
            self.assertEqual("5", rows[0]["total_ac"])
            self.assertEqual("12", rows[0]["total_submissions"])
            self.assertEqual("1", rows[0]["ac_active_students"])
            self.assertEqual("2", rows[0]["submit_active_students"])
            students = [row for row in rows if row["row_type"] == "student"]
            self.assertEqual(["202607230101", "202607030101"], [row["student_id"] for row in students])
            self.assertEqual("移软01班", students[0]["class_name"])
            full_rows = list(csv.DictReader(io.StringIO(build_statistics_csv(snapshot, labels, include_inactive_students=True))))
            self.assertEqual(3, sum(row["row_type"] == "student" for row in full_rows))
            class_rows = list(csv.DictReader(io.StringIO(build_statistics_csv(snapshot, labels, class_id="2026072301"))))
            self.assertEqual("2", class_rows[0]["total_students"])
            self.assertEqual("移软", class_rows[1]["profession_name"])


    def test_statistics_uses_historical_roster_for_counts_and_rates(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = {
                "schema_version": 1, "snapshot_id": 9,
                "fetched_at": "2026-08-29T10:00:00+08:00", "prefix": "2026",
                "user_count": 3,
                "historical_roster": {
                    "user_count": 5,
                    "user_ids": [
                        "202607230101", "202607230102", "202607230103",
                        "202607030101", "202607030102",
                    ],
                },
                "users": [
                    {"rank": 1, "user_id": "202607230101", "nickname": "甲", "accepted": 5, "submitted": 10, "ratio": "50%", "level": "1"},
                    {"rank": 2, "user_id": "202607230102", "nickname": "乙", "accepted": 0, "submitted": 0, "ratio": "0%", "level": "1"},
                    {"rank": 3, "user_id": "202607030101", "nickname": "丙", "accepted": 0, "submitted": 2, "ratio": "0%", "level": "1"},
                ],
            }
            snapshot = load_snapshot(self._write(directory, payload))
            labels = AcademicLabels("2026", {"0723": "移软", "0703": "网工"})
            rows = list(csv.DictReader(io.StringIO(build_statistics_csv(snapshot, labels))))
            summary = rows[0]
            self.assertEqual("5", summary["total_students"])
            self.assertEqual("3", summary["current_ranklist_students"])
            self.assertEqual("2", summary["missing_students"])
            self.assertEqual("60.00%", summary["ranklist_coverage_rate"])
            self.assertEqual("20.00%", summary["ac_participation_rate"])
            class_row = next(
                row for row in rows
                if row["row_type"] == "class" and row["class_code"] == "072301"
            )
            self.assertEqual("3", class_row["total_students"])
            self.assertEqual("2", class_row["current_ranklist_students"])
            self.assertEqual("1", class_row["missing_students"])
            self.assertEqual("66.67%", class_row["ranklist_coverage_rate"])

    def test_group_command_allowlist_requires_slash_and_exact_oj_command(self):
        self.assertTrue(is_allowed_group_command("/榜单 20"))
        self.assertTrue(is_allowed_group_command("/计院榜单 2"))
        self.assertTrue(is_allowed_group_command("/班级榜单 2026072306"))
        self.assertTrue(is_allowed_group_command("/专业榜单 20260723"))
        self.assertTrue(is_allowed_group_command("/软院榜单"))
        self.assertTrue(is_allowed_group_command("/翻页 2"))
        self.assertTrue(is_allowed_group_command("/文字榜单 2"))
        self.assertTrue(is_allowed_group_command(" /查榜 20260001 "))
        self.assertTrue(is_allowed_group_command("/RANK"))
        self.assertTrue(is_allowed_group_command("/帮助"))
        self.assertTrue(is_allowed_group_command("/最卷班级"))
        self.assertTrue(is_allowed_group_command("/最卷专业"))
        self.assertTrue(is_allowed_group_command("/开发者帮助"))
        self.assertTrue(is_allowed_group_command("/统计数据 全量"))
        self.assertFalse(is_allowed_group_command("榜单"))
        self.assertFalse(is_allowed_group_command("/help"))
        self.assertFalse(is_allowed_group_command("/reset"))
        self.assertFalse(is_allowed_group_command("/榜单乱说"))

    def test_load_image_manifest_and_resolve_page(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_dir = root / "snapshot-7"
            image_dir.mkdir()
            image = image_dir / "page-002.png"
            image.write_bytes(b"png")
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "snapshot_id": 7,
                        "fetched_at": "2026-08-23T10:00:00+08:00",
                        "prefix": "2026",
                        "user_count": 41,
                        "page_size": 20,
                        "page_count": 3,
                        "directory": "snapshot-7",
                    }
                ),
                encoding="utf-8",
            )
            manifest = load_image_manifest(manifest_path)
            self.assertEqual(manifest.page_path(2), image)
            with self.assertRaises(RankDataError):
                manifest.page_path(4)

    def test_image_manifest_rejects_unsafe_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = {
                "schema_version": 1,
                "snapshot_id": 7,
                "fetched_at": "2026-08-23T10:00:00+08:00",
                "prefix": "2026",
                "user_count": 20,
                "page_size": 20,
                "page_count": 1,
                "directory": "../snapshot-7",
            }
            with self.assertRaises(RankDataError):
                load_image_manifest(self._write(directory, payload))

    def test_image_loader_is_a_static_method(self):
        source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        plugin = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "OjRankPlugin"
        )
        method = next(
            node for node in plugin.body if isinstance(node, ast.FunctionDef) and node.name == "_load_images"
        )
        self.assertTrue(
            any(isinstance(item, ast.Name) and item.id == "staticmethod" for item in method.decorator_list)
        )

    def test_class_registry_accepts_only_one_page_per_valid_class(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = {
                "schema_version": 1,
                "classes": [
                    {"class_id": "2026072306", "user_count": 60, "page_count": 1}
                ],
            }
            path = self._write(directory, payload)
            self.assertEqual(
                frozenset({"2026072306"}), load_class_image_registry(path)
            )
            self.assertTrue(is_class_id("2026072306"))
            self.assertFalse(is_class_id("20260723"))

    def test_major_registry_accepts_paged_valid_majors(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = {
                "schema_version": 1,
                "majors": [
                    {"major_id": "20260723", "user_count": 163, "page_count": 9}
                ],
            }
            path = self._write(directory, payload)
            self.assertEqual(
                frozenset({"20260723"}), load_major_image_registry(path)
            )
            self.assertTrue(is_major_id("20260723"))
            self.assertFalse(is_major_id("2026072306"))


    def test_custom_student_id_layout_is_loaded_from_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "academic-labels.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "prefix": "UG",
                        "student_id_length": 10,
                        "major_code_length": 3,
                        "class_code_length": 5,
                        "student_id_pattern": "[A-Z0-9]+",
                        "major_labels": {"C01": "示例专业"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            labels = load_academic_labels(path)
            self.assertEqual("UGC01", labels.resolve_major("示例专业"))
            self.assertEqual("UGC0101", labels.resolve_class("示例专业01班"))
            self.assertTrue(labels.is_student_id("UGC0101A01"))
            self.assertFalse(labels.is_student_id("UGC0101A0"))


if __name__ == "__main__":
    unittest.main()
