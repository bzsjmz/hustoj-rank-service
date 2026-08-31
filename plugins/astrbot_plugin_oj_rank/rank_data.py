from __future__ import annotations

import csv
import io
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RankDataError(RuntimeError):
    """The exported rank snapshot is unavailable or malformed."""


SAFE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,32}")


ALLOWED_GROUP_COMMANDS = frozenset(
    {
        "榜单",
        "计院榜单",
        "软院榜单",
        "班级榜单",
        "专业榜单",
        "最卷班级",
        "最卷专业",
        "翻页",
        "文字榜单",
        "查榜",
        "榜单状态",
        "周榜",
        "月榜",
        "变化",
        "冲榜",
        "刷题榜",
        "提交榜",
        "新星榜",
        "随机选手",
        "日榜",
        "帮助",
        "统计数据",
        "开发者帮助",
        "rank",
        "who",
        "daily",
    }
)


def is_allowed_group_command(text: str) -> bool:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return False
    command = stripped[1:].split(maxsplit=1)[0].strip().casefold()
    return command in ALLOWED_GROUP_COMMANDS


@dataclass(frozen=True)
class RankSnapshot:
    snapshot_id: int
    fetched_at: datetime
    fetched_at_text: str
    prefix: str
    user_count: int
    users: list[dict[str, Any]]
    historical_roster_user_ids: frozenset[str]

    @property
    def historical_user_count(self) -> int:
        return len(self.historical_roster_user_ids)

    @property
    def missing_user_count(self) -> int:
        return len(self.historical_roster_user_ids - {str(row["user_id"]) for row in self.users})

    @property
    def coverage_rate(self) -> float:
        return self.user_count / self.historical_user_count

    def age_minutes(self, now: datetime | None = None) -> int:
        current = now or datetime.now(timezone.utc)
        fetched = self.fetched_at
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        return max(0, int((current - fetched).total_seconds() // 60))


@dataclass(frozen=True)
class DailySnapshot:
    snapshot_id: int
    fetched_at: datetime
    fetched_at_text: str
    baseline_snapshot_id: int
    baseline_fetched_at: str
    prefix: str
    user_count: int
    changes: list[dict[str, Any]]

    def age_minutes(self, now: datetime | None = None) -> int:
        current = now or datetime.now(timezone.utc)
        fetched = self.fetched_at
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        return max(0, int((current - fetched).total_seconds() // 60))


@dataclass(frozen=True)
class RankImageManifest:
    snapshot_id: int
    fetched_at: str
    prefix: str
    user_count: int
    page_size: int
    page_count: int
    directory: str
    root: Path

    def page_path(self, page_number: int) -> Path:
        if page_number < 1 or page_number > self.page_count:
            raise RankDataError(f"页码超出范围（1-{self.page_count}）")
        snapshot_dir = (self.root / self.directory).resolve()
        if snapshot_dir.parent != self.root.resolve():
            raise RankDataError("榜单图片路径不安全")
        image = snapshot_dir / f"page-{page_number:03d}.png"
        if not image.is_file():
            raise RankDataError("该页榜单图片尚未生成")
        return image


def load_snapshot(path: str | Path, allow_empty: bool = False) -> RankSnapshot:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RankDataError("榜单数据尚未生成") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RankDataError("榜单数据暂时无法读取") from exc

    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RankDataError("榜单数据格式不兼容")

    users = payload.get("users")
    if not isinstance(users, list) or (not users and not allow_empty):
        raise RankDataError("榜单数据为空")

    required = {
        "rank",
        "user_id",
        "nickname",
        "accepted",
        "submitted",
        "ratio",
        "level",
    }
    normalized: list[dict[str, Any]] = []
    for row in users:
        if not isinstance(row, dict) or not required.issubset(row):
            raise RankDataError("榜单用户记录不完整")
        normalized.append(row)

    declared_count = payload.get("user_count")
    if declared_count != len(normalized):
        raise RankDataError("榜单人数校验失败")
    current_user_ids = frozenset(str(row["user_id"]) for row in normalized)
    if len(current_user_ids) != len(normalized):
        raise RankDataError("榜单存在重复学号")

    historical_roster = payload.get("historical_roster")
    if historical_roster is None:
        # Compatibility with snapshots exported before the historical roster field.
        historical_roster_user_ids = current_user_ids
    else:
        if not isinstance(historical_roster, dict):
            raise RankDataError("历史名册格式无效")
        roster_user_ids = historical_roster.get("user_ids")
        if (
            not isinstance(roster_user_ids, list)
            or historical_roster.get("user_count") != len(roster_user_ids)
            or any(not isinstance(user_id, str) or not user_id for user_id in roster_user_ids)
        ):
            raise RankDataError("历史名册字段无效")
        historical_roster_user_ids = frozenset(roster_user_ids)
        if (
            not historical_roster_user_ids
            or len(historical_roster_user_ids) != len(roster_user_ids)
            or not current_user_ids.issubset(historical_roster_user_ids)
        ):
            raise RankDataError("历史名册人数校验失败")

    fetched_at_text = str(payload.get("fetched_at", ""))
    try:
        fetched_at = datetime.fromisoformat(fetched_at_text)
    except ValueError as exc:
        raise RankDataError("榜单更新时间格式错误") from exc

    return RankSnapshot(
        snapshot_id=int(payload.get("snapshot_id", 0)),
        fetched_at=fetched_at,
        fetched_at_text=fetched_at_text,
        prefix=str(payload.get("prefix", "")),
        user_count=len(normalized),
        users=normalized,
        historical_roster_user_ids=historical_roster_user_ids,
    )


def load_daily(path: str | Path) -> DailySnapshot:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RankDataError("每日变化数据尚未生成") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RankDataError("每日变化数据暂时无法读取") from exc

    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RankDataError("每日变化数据格式不兼容")

    changes = payload.get("changes")
    if not isinstance(changes, list) or not changes:
        raise RankDataError("每日变化数据为空")
    required = {
        "rank",
        "user_id",
        "nickname",
        "accepted",
        "submitted",
        "ratio",
        "level",
        "baseline_rank",
        "rank_change",
        "accepted_change",
        "submitted_change",
        "is_new",
    }
    for row in changes:
        if not isinstance(row, dict) or not required.issubset(row):
            raise RankDataError("每日变化用户记录不完整")
    if payload.get("user_count") != len(changes):
        raise RankDataError("每日变化人数校验失败")

    fetched_at_text = str(payload.get("fetched_at", ""))
    try:
        fetched_at = datetime.fromisoformat(fetched_at_text)
    except ValueError as exc:
        raise RankDataError("每日变化更新时间格式错误") from exc

    return DailySnapshot(
        snapshot_id=int(payload.get("snapshot_id", 0)),
        fetched_at=fetched_at,
        fetched_at_text=fetched_at_text,
        baseline_snapshot_id=int(payload.get("baseline_snapshot_id", 0)),
        baseline_fetched_at=str(payload.get("baseline_fetched_at", "")),
        prefix=str(payload.get("prefix", "")),
        user_count=len(changes),
        changes=changes,
    )


def load_image_manifest(path: str | Path) -> RankImageManifest:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RankDataError("榜单图片尚未生成") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RankDataError("榜单图片清单暂时无法读取") from exc

    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RankDataError("榜单图片清单格式不兼容")
    directory = str(payload.get("directory", ""))
    if not re.fullmatch(r"snapshot-\d+", directory):
        raise RankDataError("榜单图片目录无效")
    try:
        snapshot_id = int(payload["snapshot_id"])
        user_count = int(payload["user_count"])
        page_size = int(payload["page_size"])
        page_count = int(payload["page_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RankDataError("榜单图片清单字段无效") from exc
    if snapshot_id <= 0 or user_count <= 0 or page_size <= 0 or page_count <= 0:
        raise RankDataError("榜单图片清单数值无效")
    if page_count != math.ceil(user_count / page_size):
        raise RankDataError("榜单图片页数校验失败")
    if directory != f"snapshot-{snapshot_id}":
        raise RankDataError("榜单图片快照校验失败")

    return RankImageManifest(
        snapshot_id=snapshot_id,
        fetched_at=str(payload.get("fetched_at", "")),
        prefix=str(payload.get("prefix", "")),
        user_count=user_count,
        page_size=page_size,
        page_count=page_count,
        directory=directory,
        root=source.parent,
    )


def find_users(snapshot: RankSnapshot, query: str, limit: int = 10) -> list[dict[str, Any]]:
    needle = query.strip().casefold()
    if not needle:
        return []

    user_id_matches = [
        row for row in snapshot.users if str(row['user_id']).casefold() == needle
    ]
    if user_id_matches:
        return user_id_matches[:limit]

    exact_nickname = [
        row for row in snapshot.users if str(row['nickname']).strip().casefold() == needle
    ]
    if exact_nickname:
        return exact_nickname[:limit]

    return [
        row
        for row in snapshot.users
        if needle in str(row['nickname']).casefold()
        or needle in str(row['user_id']).casefold()
    ][:limit]


def find_changes(
    snapshot: DailySnapshot, query: str, limit: int = 10
) -> list[dict[str, Any]]:
    needle = query.strip().casefold()
    if not needle:
        return []
    exact_id = [
        row for row in snapshot.changes if str(row['user_id']).casefold() == needle
    ]
    if exact_id:
        return exact_id[:limit]
    exact_nickname = [
        row
        for row in snapshot.changes
        if str(row['nickname']).strip().casefold() == needle
    ]
    if exact_nickname:
        return exact_nickname[:limit]
    return [
        row
        for row in snapshot.changes
        if needle in str(row['nickname']).casefold()
        or needle in str(row['user_id']).casefold()
    ][:limit]


def short_time(snapshot: RankSnapshot) -> str:
    return snapshot.fetched_at.strftime("%m-%d %H:%M")


def short_iso_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%m-%d %H:%M")
    except ValueError:
        return value


def format_user(row: dict[str, Any], compact: bool = False) -> str:
    nickname = str(row.get("nickname") or "未设置昵称")
    if compact:
        return (
            f"第 {row['rank']} 名｜{row['user_id']} {nickname}｜"
            f"通过 {row['accepted']} 题"
        )
    return (
        f"{row['user_id']} {nickname}\n"
        f"当前排名：第 {row['rank']} 名\n"
        f"通过 {row['accepted']} 题｜提交 {row['submitted']} 次｜通过率 {row['ratio']}"
    )


def change_label(row: dict[str, Any]) -> str:
    if row.get("is_new"):
        return "今天首次上榜"
    change = int(row.get("rank_change") or 0)
    if change > 0:
        return f"上升 {change} 名"
    if change < 0:
        return f"下降 {abs(change)} 名"
    return "排名不变"


def format_change(row: dict[str, Any], compact: bool = False) -> str:
    nickname = str(row.get("nickname") or "未设置昵称")
    rank = int(row['rank'])
    accepted_change = row.get("accepted_change")
    submitted_change = row.get("submitted_change")
    if compact:
        accepted_text = (
            "今天首次上榜"
            if accepted_change is None
            else f"新增通过 {int(accepted_change)} 题"
        )
        return f"{row['user_id']} {nickname}｜{accepted_text}｜{change_label(row)}"

    baseline_rank = row.get("baseline_rank")
    movement = change_label(row)
    baseline_text = (
        "今天首次上榜"
        if baseline_rank is None
        else f"今天首次记录时第 {int(baseline_rank)} 名"
    )
    accepted_text = (
        "今天新增通过：暂无记录"
        if accepted_change is None
        else f"今天新增通过：{int(accepted_change)} 题"
    )
    submitted_text = (
        "今天新增提交：暂无记录"
        if submitted_change is None
        else f"今天新增提交：{int(submitted_change)} 次"
    )
    return (
        f"{row['user_id']} {nickname}\n"
        f"当前排名：第 {rank} 名\n"
        f"{baseline_text}，{movement}\n"
        f"{accepted_text}｜{submitted_text}"
    )


def is_class_id(value: str, expected_length: int = 10) -> bool:
    return len(value) == expected_length and bool(SAFE_ID_PATTERN.fullmatch(value))


def load_class_image_registry(path: str | Path) -> frozenset[str]:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RankDataError("班级榜单图片尚未生成") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RankDataError("班级榜单图片清单暂时无法读取") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RankDataError("班级榜单图片清单格式不兼容")
    try:
        expected_length = int(payload.get("class_id_length", 10))
    except (TypeError, ValueError) as exc:
        raise RankDataError("班级榜单图片清单字段无效") from exc
    classes = payload.get("classes")
    if not isinstance(classes, list) or not classes:
        raise RankDataError("班级榜单图片清单为空")
    class_ids = set()
    for item in classes:
        if not isinstance(item, dict):
            raise RankDataError("班级榜单图片清单字段无效")
        class_id = str(item.get("class_id", ""))
        try:
            user_count = int(item["user_count"])
            page_count = int(item["page_count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RankDataError("班级榜单图片清单字段无效") from exc
        if not is_class_id(class_id, expected_length) or user_count <= 0 or page_count != 1:
            raise RankDataError("班级榜单图片清单字段无效")
        class_ids.add(class_id)
    if len(class_ids) != len(classes):
        raise RankDataError("班级榜单图片清单存在重复班级")
    return frozenset(class_ids)


def is_major_id(value: str, expected_length: int = 8) -> bool:
    return len(value) == expected_length and bool(SAFE_ID_PATTERN.fullmatch(value))


def load_major_image_registry(path: str | Path) -> frozenset[str]:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RankDataError("专业榜单图片尚未生成") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RankDataError("专业榜单图片清单暂时无法读取") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RankDataError("专业榜单图片清单格式不兼容")
    try:
        expected_length = int(payload.get("major_id_length", 8))
    except (TypeError, ValueError) as exc:
        raise RankDataError("专业榜单图片清单字段无效") from exc
    majors = payload.get("majors")
    if not isinstance(majors, list) or not majors:
        raise RankDataError("专业榜单图片清单为空")
    major_ids = set()
    for item in majors:
        if not isinstance(item, dict):
            raise RankDataError("专业榜单图片清单字段无效")
        major_id = str(item.get("major_id", ""))
        try:
            user_count = int(item["user_count"])
            page_count = int(item["page_count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RankDataError("专业榜单图片清单字段无效") from exc
        if not is_major_id(major_id, expected_length) or user_count <= 0 or page_count <= 0:
            raise RankDataError("专业榜单图片清单字段无效")
        major_ids.add(major_id)
    if len(major_ids) != len(majors):
        raise RankDataError("专业榜单图片清单存在重复专业")
    return frozenset(major_ids)


@dataclass(frozen=True)
class AcademicLabels:
    prefix: str
    major_labels: dict[str, str]
    student_id_length: int = 12
    major_code_length: int = 4
    class_code_length: int = 6
    student_id_pattern: str = r"[0-9]+"

    def __post_init__(self) -> None:
        try:
            re.compile(self.student_id_pattern)
        except re.error as exc:
            raise ValueError("invalid student ID pattern") from exc
        if not SAFE_ID_PATTERN.fullmatch(self.prefix):
            raise ValueError("invalid student ID prefix")
        if not re.fullmatch(self.student_id_pattern, self.prefix):
            raise ValueError("student ID prefix does not match its pattern")
        if not 0 < self.major_code_length < self.class_code_length:
            raise ValueError("invalid major/class code lengths")
        if len(self.prefix) + self.class_code_length >= self.student_id_length:
            raise ValueError("invalid student ID length")

    @property
    def major_id_length(self) -> int:
        return len(self.prefix) + self.major_code_length

    @property
    def class_id_length(self) -> int:
        return len(self.prefix) + self.class_code_length

    def is_student_id(self, value: str) -> bool:
        return len(value) == self.student_id_length and bool(SAFE_ID_PATTERN.fullmatch(value)) and bool(re.fullmatch(self.student_id_pattern, value)) and value.startswith(self.prefix)

    def is_major_id(self, value: str) -> bool:
        return is_major_id(value, self.major_id_length) and bool(re.fullmatch(self.student_id_pattern, value)) and value.startswith(self.prefix)

    def is_class_id(self, value: str) -> bool:
        return is_class_id(value, self.class_id_length) and bool(re.fullmatch(self.student_id_pattern, value)) and value.startswith(self.prefix)

    def resolve_major(self, value: str) -> str | None:
        normalized = value.strip()
        if self.is_major_id(normalized):
            return normalized
        for code, label in self.major_labels.items():
            if normalized == label:
                return self.prefix + code
        return None

    def resolve_class(self, value: str) -> str | None:
        normalized = value.strip()
        if self.is_class_id(normalized):
            return normalized
        suffix_length = self.class_code_length - self.major_code_length
        for code, label in self.major_labels.items():
            match = re.fullmatch(re.escape(label) + rf"([A-Za-z0-9_-]{{{suffix_length}}})班", normalized)
            if match:
                return self.prefix + code + match.group(1)
        return None

    def major_name(self, major_id: str) -> str:
        code = major_id[len(self.prefix):]
        return self.major_labels.get(code, code)

    def class_name(self, class_id: str) -> str:
        major_id = class_id[:self.major_id_length]
        major_name = self.major_name(major_id)
        class_code = class_id[len(self.prefix):]
        major_code = major_id[len(self.prefix):]
        suffix = class_code[self.major_code_length:]
        return class_code if major_name == major_code else f"{major_name}{suffix}班"


def load_academic_labels(path: str | Path) -> AcademicLabels:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RankDataError("专业名称映射尚未生成") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RankDataError("专业名称映射暂时无法读取") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RankDataError("专业名称映射格式不兼容")
    prefix = str(payload.get("prefix", ""))
    labels = payload.get("major_labels")
    try:
        student_id_length = int(payload.get("student_id_length", len(prefix) + 8))
        major_code_length = int(payload.get("major_code_length", 4))
        class_code_length = int(payload.get("class_code_length", 6))
        student_id_pattern = str(payload.get("student_id_pattern", r"[0-9]+"))
        layout = AcademicLabels(prefix, {}, student_id_length, major_code_length, class_code_length, student_id_pattern)
    except (TypeError, ValueError) as exc:
        raise RankDataError("专业名称映射字段无效") from exc
    if not isinstance(labels, dict):
        raise RankDataError("专业名称映射字段无效")
    normalized: dict[str, str] = {}
    for code, label in labels.items():
        code_text = str(code)
        label_text = str(label).strip()
        if not SAFE_ID_PATTERN.fullmatch(code_text) or len(code_text) != layout.major_code_length or not label_text:
            raise RankDataError("专业名称映射字段无效")
        if label_text in normalized.values():
            raise RankDataError("专业名称映射存在重复名称")
        normalized[code_text] = label_text
    return AcademicLabels(prefix, normalized, student_id_length, major_code_length, class_code_length, student_id_pattern)



STATISTICS_CSV_FIELDS = (
    "row_type",
    "snapshot_time",
    "total_students",
    "current_ranklist_students",
    "missing_students",
    "ranklist_coverage_rate",
    "total_ac",
    "total_submissions",
    "ac_active_students",
    "submit_active_students",
    "ac_participation_rate",
    "submit_participation_rate",
    "profession_code",
    "profession_name",
    "class_code",
    "class_name",
    "rank",
    "student_id",
    "nickname",
    "ac",
    "submissions",
)


def build_statistics_csv(
    snapshot: RankSnapshot,
    labels: AcademicLabels,
    *,
    include_inactive_students: bool = False,
    profession_id: str | None = None,
    class_id: str | None = None,
) -> str:
    """Build statistics with historical roster counts and current dynamic metrics."""
    if snapshot.prefix != labels.prefix:
        raise RankDataError("榜单与专业名称映射的学号前缀不一致")
    if profession_id is not None and not labels.is_major_id(profession_id):
        raise RankDataError("统计专业代码无效")
    if class_id is not None and not labels.is_class_id(class_id):
        raise RankDataError("统计班级代码无效")
    if class_id is not None and profession_id is not None:
        if class_id[: labels.major_id_length] != profession_id:
            raise RankDataError("统计班级不属于指定专业")

    def identity_from_student_id(student_id: str) -> tuple[str, str]:
        if not labels.is_student_id(student_id):
            raise RankDataError("统计数据包含无法识别学号的学生")
        row_profession_id = student_id[: labels.major_id_length]
        row_class_id = student_id[: labels.class_id_length]
        if not labels.is_major_id(row_profession_id) or not labels.is_class_id(row_class_id):
            raise RankDataError("统计数据包含无法识别专业或班级的学生")
        return row_profession_id, row_class_id

    def in_scope(student_id: str) -> bool:
        row_profession_id, row_class_id = identity_from_student_id(student_id)
        return (
            (profession_id is None or row_profession_id == profession_id)
            and (class_id is None or row_class_id == class_id)
        )

    scoped_roster_ids = sorted(
        student_id
        for student_id in snapshot.historical_roster_user_ids
        if in_scope(student_id)
    )
    if not scoped_roster_ids:
        raise RankDataError("历史名册中没有该范围的学生")
    scoped_users = [
        row for row in snapshot.users if in_scope(str(row["user_id"]))
    ]

    def number(row: dict[str, Any], field: str) -> int:
        try:
            return int(row[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise RankDataError(f"统计数据字段无效：{field}") from exc

    def summary_values(
        roster_ids: list[str], current_users: list[dict[str, Any]]
    ) -> dict[str, Any]:
        current_ids = {str(row["user_id"]) for row in current_users}
        total_students = len(roster_ids)
        total_ac = sum(number(row, "accepted") for row in current_users)
        total_submissions = sum(number(row, "submitted") for row in current_users)
        ac_active = sum(number(row, "accepted") > 0 for row in current_users)
        submit_active = sum(number(row, "submitted") > 0 for row in current_users)
        return {
            "total_students": total_students,
            "current_ranklist_students": len(current_users),
            "missing_students": len(set(roster_ids) - current_ids),
            "ranklist_coverage_rate": f"{len(current_users) / total_students:.2%}",
            "total_ac": total_ac,
            "total_submissions": total_submissions,
            "ac_active_students": ac_active,
            "submit_active_students": submit_active,
            "ac_participation_rate": f"{ac_active / total_students:.2%}",
            "submit_participation_rate": f"{submit_active / total_students:.2%}",
        }

    grouped_roster_professions: dict[str, list[str]] = {}
    grouped_roster_classes: dict[str, list[str]] = {}
    grouped_current_professions: dict[str, list[dict[str, Any]]] = {}
    grouped_current_classes: dict[str, list[dict[str, Any]]] = {}
    for student_id in scoped_roster_ids:
        row_profession_id, row_class_id = identity_from_student_id(student_id)
        grouped_roster_professions.setdefault(row_profession_id, []).append(student_id)
        grouped_roster_classes.setdefault(row_class_id, []).append(student_id)
    for row in scoped_users:
        row_profession_id, row_class_id = identity_from_student_id(str(row["user_id"]))
        grouped_current_professions.setdefault(row_profession_id, []).append(row)
        grouped_current_classes.setdefault(row_class_id, []).append(row)

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=STATISTICS_CSV_FIELDS)
    writer.writeheader()

    def write(row_type: str, **values: Any) -> None:
        row = {field: "" for field in STATISTICS_CSV_FIELDS}
        row["row_type"] = row_type
        row.update(values)
        writer.writerow(row)

    write(
        "snapshot",
        snapshot_time=snapshot.fetched_at_text,
        **summary_values(scoped_roster_ids, scoped_users),
    )
    for current_profession_id in sorted(grouped_roster_professions):
        write(
            "profession",
            profession_code=current_profession_id[len(labels.prefix) :],
            profession_name=labels.major_name(current_profession_id),
            **summary_values(
                grouped_roster_professions[current_profession_id],
                grouped_current_professions.get(current_profession_id, []),
            ),
        )
    for current_class_id in sorted(grouped_roster_classes):
        current_profession_id = current_class_id[: labels.major_id_length]
        write(
            "class",
            profession_code=current_profession_id[len(labels.prefix) :],
            profession_name=labels.major_name(current_profession_id),
            class_code=current_class_id[len(labels.prefix) :],
            class_name=labels.class_name(current_class_id),
            **summary_values(
                grouped_roster_classes[current_class_id],
                grouped_current_classes.get(current_class_id, []),
            ),
        )
    for row in sorted(scoped_users, key=lambda item: (number(item, "rank"), str(item["user_id"]))):
        ac = number(row, "accepted")
        submissions = number(row, "submitted")
        if not include_inactive_students and ac <= 0 and submissions <= 0:
            continue
        current_profession_id, current_class_id = identity_from_student_id(
            str(row["user_id"])
        )
        write(
            "student",
            profession_code=current_profession_id[len(labels.prefix) :],
            profession_name=labels.major_name(current_profession_id),
            class_code=current_class_id[len(labels.prefix) :],
            class_name=labels.class_name(current_class_id),
            rank=number(row, "rank"),
            student_id=str(row["user_id"]),
            nickname=str(row.get("nickname") or ""),
            ac=ac,
            submissions=submissions,
        )
    return output.getvalue()
