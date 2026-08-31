from __future__ import annotations

from dataclasses import replace
from typing import AbstractSet, Sequence

from .models import RankEntry


def exclude_and_rerank(
    entries: Sequence[RankEntry], excluded_user_ids: AbstractSet[str]
) -> list[RankEntry]:
    included = [entry for entry in entries if entry.user_id not in excluded_user_ids]
    return [replace(entry, rank=rank) for rank, entry in enumerate(included, start=1)]


def select_prefix_exclude_and_rerank(
    entries: Sequence[RankEntry],
    prefix: str,
    excluded_user_ids: AbstractSet[str],
) -> list[RankEntry]:
    selected = [entry for entry in entries if entry.user_id.startswith(prefix)]
    return exclude_and_rerank(selected, excluded_user_ids)
