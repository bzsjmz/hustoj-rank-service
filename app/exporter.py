from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import AbstractSet, Sequence

from .college import MAJOR_DISPLAY_NAMES
from .student_ids import StudentIdLayout
from .models import HistoricalRankEntry, RankBaseline, RankEntry


class RankExporter:
    def __init__(self, share_dir: Path):
        self.share_dir = share_dir

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.chmod(0o644)
        temporary.replace(path)
        path.chmod(0o644)

    def export_academic_labels(self, layout: StudentIdLayout | str) -> None:
        """Publish ID layout and optional major labels for read-only consumers."""
        if isinstance(layout, str):
            layout = StudentIdLayout.conventional(layout)
        self.share_dir.mkdir(parents=True, exist_ok=True)
        self.share_dir.chmod(0o755)
        document = {
            "schema_version": 1,
            "prefix": layout.prefix,
            "student_id_length": layout.student_id_length,
            "major_code_length": layout.major_code_length,
            "class_code_length": layout.class_code_length,
            "student_id_pattern": layout.student_id_pattern,
            "major_labels": {code: label for code, label in sorted(MAJOR_DISPLAY_NAMES.items()) if len(code) == layout.major_code_length},
        }
        content = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        self._atomic_write(self.share_dir / "academic-labels.json", content + "\n")


    def export(
        self,
        entries: Sequence[RankEntry],
        prefix: str,
        fetched_at: str,
        snapshot_id: int,
        baseline: RankBaseline | None = None,
        historical_roster_user_ids: AbstractSet[str] | None = None,
    ) -> None:
        if not entries:
            raise ValueError("refusing to export an empty ranklist")
        current_user_ids = {entry.user_id for entry in entries}
        roster_user_ids = (
            frozenset(historical_roster_user_ids)
            if historical_roster_user_ids is not None
            else frozenset(current_user_ids)
        )
        if not roster_user_ids:
            raise ValueError("refusing to export an empty historical roster")
        if not current_user_ids.issubset(roster_user_ids):
            raise ValueError("historical roster does not contain the current ranklist")
        self.share_dir.mkdir(parents=True, exist_ok=True)
        self.share_dir.chmod(0o755)

        document = {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "fetched_at": fetched_at,
            "prefix": prefix,
            "user_count": len(entries),
            "historical_roster": {
                "user_count": len(roster_user_ids),
                "user_ids": sorted(roster_user_ids),
            },
            "current_user_count": len(current_user_ids),
            "missing_user_count": len(roster_user_ids - current_user_ids),
            "coverage_rate": len(current_user_ids) / len(roster_user_ids),
            "users": [asdict(entry) for entry in entries],
        }
        json_text = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        self._atomic_write(self.share_dir / "latest.json", json_text + "\n")

        output = io.StringIO(newline="")
        fields = [
            "rank",
            "user_id",
            "nickname",
            "accepted",
            "submitted",
            "ratio",
            "level",
            "fetched_at",
        ]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for entry in entries:
            row = asdict(entry)
            row["fetched_at"] = fetched_at
            writer.writerow(row)
        self._atomic_write(self.share_dir / "latest.csv", output.getvalue())

        if baseline is not None:
            baseline_by_user = {entry.user_id: entry for entry in baseline.entries}
            changes = []
            for entry in entries:
                previous = baseline_by_user.get(entry.user_id)
                changes.append(
                    {
                        **asdict(entry),
                        "baseline_rank": previous.rank if previous else None,
                        "rank_change": previous.rank - entry.rank if previous else None,
                        "accepted_change": (
                            entry.accepted - previous.accepted if previous else None
                        ),
                        "submitted_change": (
                            entry.submitted - previous.submitted if previous else None
                        ),
                        "is_new": previous is None,
                    }
                )

            daily_document = {
                "schema_version": 1,
                "snapshot_id": snapshot_id,
                "fetched_at": fetched_at,
                "baseline_snapshot_id": baseline.snapshot_id,
                "baseline_fetched_at": baseline.fetched_at,
                "prefix": prefix,
                "user_count": len(entries),
                "changes": changes,
            }
            daily_text = json.dumps(
                daily_document, ensure_ascii=False, separators=(",", ":")
            )
            self._atomic_write(self.share_dir / "daily.json", daily_text + "\n")

    def export_lookup(
        self,
        entries: Sequence[RankEntry],
        fetched_at: str,
        snapshot_id: int,
        prefixes: Sequence[str],
        historical_entries: Sequence[HistoricalRankEntry] = (),
    ) -> None:
        """Publish current multi-cohort rows plus marked historical fallbacks."""
        if not entries:
            raise ValueError("refusing to export an empty lookup dataset")
        if not prefixes or any(not re.fullmatch(r"[A-Za-z0-9_-]+", prefix) for prefix in prefixes):
            raise ValueError("invalid lookup prefixes")
        current_user_ids = [entry.user_id for entry in entries]
        if len(current_user_ids) != len(set(current_user_ids)):
            raise ValueError("duplicate user_id in lookup dataset")
        current_user_id_set = set(current_user_ids)
        fallback_entries = [
            historical
            for historical in historical_entries
            if historical.entry.user_id not in current_user_id_set
        ]
        fallback_user_ids = [historical.entry.user_id for historical in fallback_entries]
        if len(fallback_user_ids) != len(set(fallback_user_ids)):
            raise ValueError("duplicate historical user_id in lookup dataset")

        self.share_dir.mkdir(parents=True, exist_ok=True)
        self.share_dir.chmod(0o755)
        users = [asdict(entry) for entry in entries]
        users.extend(
            {
                **asdict(historical.entry),
                "is_historical": True,
                "last_seen_at": historical.fetched_at,
            }
            for historical in sorted(
                fallback_entries, key=lambda item: item.entry.user_id
            )
        )
        document = {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "fetched_at": fetched_at,
            "prefix": "-".join(prefixes),
            "lookup_prefixes": list(prefixes),
            "user_count": len(users),
            "users": users,
        }
        content = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        self._atomic_write(self.share_dir / "lookup.json", content + "\n")

    def export_scoped(
        self,
        entries: Sequence[RankEntry],
        prefix: str,
        fetched_at: str,
        scope: str,
    ) -> None:
        filenames = {"w": "weekly.json", "m": "monthly.json"}
        if scope not in filenames:
            raise ValueError(f"unsupported rank scope: {scope}")
        self.share_dir.mkdir(parents=True, exist_ok=True)
        self.share_dir.chmod(0o755)
        document = {
            "schema_version": 1,
            "snapshot_id": 0,
            "fetched_at": fetched_at,
            "prefix": prefix,
            "scope": scope,
            "user_count": len(entries),
            "users": [asdict(entry) for entry in entries],
        }
        content = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        self._atomic_write(self.share_dir / filenames[scope], content + "\n")

    def export_college(
        self,
        entries: Sequence[RankEntry],
        prefix: str,
        fetched_at: str,
        snapshot_id: int,
        college: str,
        split_snapshot_id: int,
    ) -> None:
        filenames = {"计院": "computer-college.json", "软院": "software-college.json"}
        filename = filenames.get(college)
        if filename is None:
            raise ValueError(f"unsupported college: {college}")
        if not entries:
            raise ValueError(f"refusing to export an empty {college} leaderboard")
        self.share_dir.mkdir(parents=True, exist_ok=True)
        self.share_dir.chmod(0o755)
        document = {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "fetched_at": fetched_at,
            "prefix": prefix,
            "scope": "college",
            "college": college,
            "split_snapshot_id": split_snapshot_id,
            "user_count": len(entries),
            "users": [asdict(entry) for entry in entries],
        }
        content = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        self._atomic_write(self.share_dir / filename, content + "\n")


    def export_class(
        self,
        entries: Sequence[RankEntry],
        prefix: str,
        fetched_at: str,
        snapshot_id: int,
        class_id: str,
        historical_user_ids: AbstractSet[str] = frozenset(),
        layout: StudentIdLayout | None = None,
    ) -> None:
        layout = layout or StudentIdLayout(prefix, len(prefix) + 8, 4, 6)
        if not layout.is_class_id(class_id):
            raise ValueError(f"invalid class ID: {class_id}")
        if not entries:
            raise ValueError(f"refusing to export an empty class leaderboard: {class_id}")
        directory = self.share_dir / "class-ranklists"
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o755)
        document = {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "fetched_at": fetched_at,
            "prefix": prefix,
            "scope": "class",
            "class_id": class_id,
            "user_count": len(entries),
            "current_user_count": len(entries) - len(historical_user_ids),
            "historical_user_count": len(historical_user_ids),
            "users": [
                {
                    **asdict(entry),
                    "is_historical": entry.user_id in historical_user_ids,
                }
                for entry in entries
            ],
        }
        content = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        self._atomic_write(directory / f"{class_id}.json", content + "\n")


    def export_major(
        self,
        entries: Sequence[RankEntry],
        prefix: str,
        fetched_at: str,
        snapshot_id: int,
        major_id: str,
        layout: StudentIdLayout | None = None,
    ) -> None:
        layout = layout or StudentIdLayout(prefix, len(prefix) + 8, 4, 6)
        if not layout.is_major_id(major_id):
            raise ValueError(f"invalid major ID: {major_id}")
        if not entries:
            raise ValueError(f"refusing to export an empty major leaderboard: {major_id}")
        directory = self.share_dir / "major-ranklists"
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o755)
        document = {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "fetched_at": fetched_at,
            "prefix": prefix,
            "scope": "major",
            "major_id": major_id,
            "user_count": len(entries),
            "users": [asdict(entry) for entry in entries],
        }
        content = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        self._atomic_write(directory / f"{major_id}.json", content + "\n")
