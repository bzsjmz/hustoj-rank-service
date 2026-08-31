from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import AbstractSet, Sequence

from .models import HistoricalRankEntry, RankBaseline, RankEntry
from .ranking import exclude_and_rerank
from .student_ids import StudentIdLayout


SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at TEXT NOT NULL,
    prefix TEXT NOT NULL,
    user_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS current_rank (
    prefix TEXT NOT NULL,
    user_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    nickname TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    submitted INTEGER NOT NULL,
    ratio REAL NOT NULL,
    level TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (prefix, user_id)
);

CREATE TABLE IF NOT EXISTS rank_snapshots (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    nickname TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    submitted INTEGER NOT NULL,
    ratio REAL NOT NULL,
    level TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_prefix_time
ON snapshots(prefix, fetched_at);

CREATE INDEX IF NOT EXISTS idx_rank_snapshots_user
ON rank_snapshots(user_id, snapshot_id);
"""


class RankDatabase:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def save_complete_snapshot(
        self,
        entries: Sequence[RankEntry],
        prefix: str,
        fetched_at: str,
    ) -> int:
        if not entries:
            raise ValueError("refusing to replace current_rank with an empty result")
        user_ids = [entry.user_id for entry in entries]
        if len(user_ids) != len(set(user_ids)):
            raise ValueError("duplicate user_id in complete scrape")

        snapshot_rows = [
            (
                entry.user_id,
                entry.rank,
                entry.nickname,
                entry.accepted,
                entry.submitted,
                entry.ratio,
                entry.level,
            )
            for entry in entries
        ]
        current_rows = [
            (
                prefix,
                entry.user_id,
                entry.rank,
                entry.nickname,
                entry.accepted,
                entry.submitted,
                entry.ratio,
                entry.level,
                fetched_at,
            )
            for entry in entries
        ]

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "INSERT INTO snapshots(fetched_at, prefix, user_count) VALUES (?, ?, ?)",
                (fetched_at, prefix, len(entries)),
            )
            snapshot_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO rank_snapshots(
                    snapshot_id, user_id, rank, nickname, accepted,
                    submitted, ratio, level
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [(snapshot_id, *row) for row in snapshot_rows],
            )
            connection.execute("DELETE FROM current_rank WHERE prefix = ?", (prefix,))
            connection.executemany(
                """
                INSERT INTO current_rank(
                    prefix, user_id, rank, nickname, accepted,
                    submitted, ratio, level, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                current_rows,
            )
        return snapshot_id

    def current_count(self, prefix: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM current_rank WHERE prefix = ?", (prefix,)
            ).fetchone()
        return int(row[0])
    def historical_roster_user_ids(
        self, prefix: str, excluded_user_ids: AbstractSet[str] = frozenset(),
        student_id_length: int | None = None,
        student_id_layout: StudentIdLayout | None = None,
    ) -> frozenset[str]:
        """Return every non-excluded student ever present in a committed snapshot.

        ``rank_snapshots`` is immutable historical data, so this roster is naturally
        append-only: a student missing from a later ranklist remains in the result.
        Only complete IDs matching the configured layout are treated as students;
        configured test/special accounts are also kept out of the user-facing roster.
        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT rank_snapshots.user_id
                FROM rank_snapshots
                INNER JOIN snapshots ON snapshots.id = rank_snapshots.snapshot_id
                WHERE snapshots.prefix = ?
                """,
                (prefix,),
            ).fetchall()
        return frozenset(
            user_id
            for row in rows
            for user_id in (str(row[0]),)
            if (
                user_id not in excluded_user_ids
                and (
                    student_id_layout.is_student_id(user_id)
                    if student_id_layout is not None
                    else (
                        user_id.startswith(prefix)
                        and len(user_id) == (student_id_length or len(prefix) + 8)
                        and user_id.isdigit()
                    )
                )
            )
        )

    def latest_entries_for_user_ids(
        self, prefix: str, user_ids: AbstractSet[str]
    ) -> dict[str, HistoricalRankEntry]:
        """Return the most recent saved row for each requested historical user."""
        if not user_ids:
            return {}

        latest: dict[str, HistoricalRankEntry] = {}
        ordered_user_ids = sorted(user_ids)
        with self._connect() as connection:
            for offset in range(0, len(ordered_user_ids), 900):
                batch = ordered_user_ids[offset : offset + 900]
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    f"""
                    SELECT rank_snapshots.user_id, rank_snapshots.rank,
                           rank_snapshots.nickname, rank_snapshots.accepted,
                           rank_snapshots.submitted, rank_snapshots.ratio,
                           rank_snapshots.level, snapshots.fetched_at
                    FROM rank_snapshots
                    INNER JOIN snapshots ON snapshots.id = rank_snapshots.snapshot_id
                    WHERE snapshots.prefix = ?
                      AND rank_snapshots.user_id IN ({placeholders})
                    ORDER BY rank_snapshots.user_id ASC,
                             snapshots.fetched_at DESC, snapshots.id DESC
                    """,
                    (prefix, *batch),
                ).fetchall()
                for row in rows:
                    user_id = str(row[0])
                    if user_id in latest:
                        continue
                    latest[user_id] = HistoricalRankEntry(
                        entry=RankEntry(
                            rank=int(row[1]),
                            user_id=user_id,
                            nickname=str(row[2]),
                            accepted=int(row[3]),
                            submitted=int(row[4]),
                            ratio=float(row[5]),
                            level=str(row[6]),
                        ),
                        fetched_at=str(row[7]),
                    )
        return latest

    def snapshot_user_ids(self, snapshot_id: int, prefix: str) -> frozenset[str]:
        """Return the complete user roster from a verified historical snapshot."""
        with self._connect() as connection:
            snapshot = connection.execute(
                "SELECT prefix, user_count FROM snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
            if snapshot is None:
                raise ValueError(f"snapshot {snapshot_id} not found")
            if str(snapshot[0]) != prefix:
                raise ValueError(
                    f"snapshot {snapshot_id} prefix does not match configured prefix"
                )
            rows = connection.execute(
                "SELECT user_id FROM rank_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchall()

        user_ids = frozenset(str(row[0]) for row in rows)
        if not user_ids or len(user_ids) != int(snapshot[1]):
            raise ValueError(f"snapshot {snapshot_id} roster is incomplete")
        return user_ids

    def daily_baseline(
        self,
        prefix: str,
        fetched_at: str,
        current_snapshot_id: int,
        excluded_user_ids: AbstractSet[str],
    ) -> RankBaseline:
        current_time = datetime.fromisoformat(fetched_at)
        day_start = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        with self._connect() as connection:
            snapshot = connection.execute(
                """
                SELECT id, fetched_at
                FROM snapshots
                WHERE prefix = ?
                  AND fetched_at >= ?
                  AND fetched_at < ?
                  AND id <= ?
                ORDER BY fetched_at ASC, id ASC
                LIMIT 1
                """,
                (
                    prefix,
                    day_start.isoformat(timespec="seconds"),
                    day_end.isoformat(timespec="seconds"),
                    current_snapshot_id,
                ),
            ).fetchone()
            if snapshot is None:
                raise ValueError("daily baseline snapshot not found")

            rows = connection.execute(
                """
                SELECT rank, user_id, nickname, accepted, submitted, ratio, level
                FROM rank_snapshots
                WHERE snapshot_id = ?
                ORDER BY rank ASC, user_id ASC
                """,
                (int(snapshot[0]),),
            ).fetchall()

        entries = [
            RankEntry(
                rank=int(row[0]),
                user_id=str(row[1]),
                nickname=str(row[2]),
                accepted=int(row[3]),
                submitted=int(row[4]),
                ratio=float(row[5]),
                level=str(row[6]),
            )
            for row in rows
        ]
        normalized = exclude_and_rerank(entries, excluded_user_ids)
        if not normalized:
            raise ValueError("daily baseline is empty after exclusions")
        return RankBaseline(
            snapshot_id=int(snapshot[0]),
            fetched_at=str(snapshot[1]),
            entries=tuple(normalized),
        )
