from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RankEntry:
    rank: int
    user_id: str
    nickname: str
    accepted: int
    submitted: int
    ratio: float
    level: str


@dataclass(frozen=True)
class RankBaseline:
    snapshot_id: int
    fetched_at: str
    entries: tuple[RankEntry, ...]


@dataclass(frozen=True)
class HistoricalRankEntry:
    entry: RankEntry
    fetched_at: str
