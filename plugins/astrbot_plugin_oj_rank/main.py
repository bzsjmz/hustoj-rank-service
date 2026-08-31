from __future__ import annotations

import math
from pathlib import Path
import secrets
from sys import maxsize

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Plain
from astrbot.api.star import Context, Star, register

from .rank_data import (
    RankDataError,
    build_statistics_csv,
    change_label,
    find_changes,
    find_users,
    format_change,
    format_user,
    is_allowed_group_command,
    is_class_id,
    is_major_id,
    load_academic_labels,
    load_class_image_registry,
    load_major_image_registry,
    load_daily,
    load_image_manifest,
    load_snapshot,
    short_iso_time,
    short_time,
)


PLUGIN_NAME = "astrbot_plugin_oj_rank"
DATA_FILE = "/oj-rank-share/latest.json"
LOOKUP_FILE = "/oj-rank-share/lookup.json"
DAILY_FILE = "/oj-rank-share/daily.json"
WEEKLY_FILE = "/oj-rank-share/weekly.json"
MONTHLY_FILE = "/oj-rank-share/monthly.json"
IMAGE_MANIFEST_FILE = "/oj-rank-share/rank-images/manifest.json"
COMPUTER_COLLEGE_FILE = "/oj-rank-share/computer-college.json"
SOFTWARE_COLLEGE_FILE = "/oj-rank-share/software-college.json"
COMPUTER_COLLEGE_IMAGE_MANIFEST_FILE = (
    "/oj-rank-share/college-images/computer/manifest.json"
)
SOFTWARE_COLLEGE_IMAGE_MANIFEST_FILE = (
    "/oj-rank-share/college-images/software/manifest.json"
)
CLASS_IMAGE_REGISTRY_FILE = "/oj-rank-share/class-images/manifest.json"
CLASS_IMAGE_ROOT = "/oj-rank-share/class-images"
CLASS_DATA_ROOT = "/oj-rank-share/class-ranklists"
MAJOR_IMAGE_REGISTRY_FILE = "/oj-rank-share/major-images/manifest.json"
MAJOR_IMAGE_ROOT = "/oj-rank-share/major-images"
MAJOR_DATA_ROOT = "/oj-rank-share/major-ranklists"
STATISTICS_EXPORT_DIR = Path(__file__).parent / "statistics_exports"
CLASS_INTENSITY_IMAGE_MANIFEST_FILE = "/oj-rank-share/class-intensity-images/manifest.json"
ACADEMIC_LABELS_FILE = "/oj-rank-share/academic-labels.json"
MAJOR_INTENSITY_IMAGE_MANIFEST_FILE = "/oj-rank-share/major-intensity-images/manifest.json"
HELP_PRIORITY = 1000
GROUP_GUARD_PRIORITY = maxsize + 100

HELP_TEXT = """OJ 榜单帮助
/榜单 [页码]：查看天梯总榜图片
/班级榜单 专业简称+班号：如 /班级榜单 示例专业01班（旧十位学号格式仍可用）
/专业榜单 专业简称 [页码]：如 /专业榜单 示例专业（旧八位学号格式仍可用）
/翻页 [页码]：继续查看当前榜单
/最卷班级、/最卷专业：查看按 AC 总量排序的统计榜
/查榜 学号或昵称：查询配置范围内的个人成绩（含历史回退）
/随机选手：随机看看一位同学
/帮助：查看本帮助

榜单数据约每 5 分钟更新。"""

DEVELOPER_HELP_TEXT = """开发者帮助
以下命令不会显示在普通 /帮助 中，但仍可直接调用。

周期榜
/周榜 [人数]：本周 OJ 排名
/月榜 [人数]：本月 OJ 排名

今日数据
/变化 [学号或昵称]：今日整体或个人进展
/冲榜 [人数]：今日名次提升榜
/刷题榜 [人数]：今日新增通过题数榜
/提交榜 [人数]：今日新增提交次数榜
/新星榜 [人数]：今天从百名外冲出的同学

备用与维护
/文字榜单 [页码]、/榜单状态、/日榜
统计导出
/统计数据 [全量]：导出当前快照 CSV
/统计数据 班级 <班级>、/统计数据 专业 <专业>：导出指定范围 CSV

/rank、/who、/daily：英文别名"""


@register(
    PLUGIN_NAME,
    "bzsjmz",
    "HUSTOJ 榜单只读查询",
    "1.5.0",
    "https://github.com/bzsjmz/hustoj-rank-service",
)
class OjRankPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._last_views: dict[str, tuple[str, str | None, str, int]] = {}

    async def initialize(self):
        logger.info(f"[{PLUGIN_NAME}] 已初始化，只读数据源: {DATA_FILE}")
        logger.info(f"[{PLUGIN_NAME}] 群静默守卫已启用，仅放行 OJ 斜杠命令")

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE,
        priority=GROUP_GUARD_PRIORITY,
    )
    async def group_silence_guard(self, event: AstrMessageEvent):
        raw_text = "".join(
            component.text
            for component in event.get_messages()
            if isinstance(component, Plain)
        )
        if is_allowed_group_command(raw_text):
            return

        # Mark the event as handled without sending anything. stop_event() prevents
        # lower-priority plugin handlers, while _has_send_oper also prevents the
        # default LLM fallback for @bot, replies to bot messages, and unknown /commands.
        event._has_send_oper = True
        event.stop_event()

    @staticmethod
    def _string_set(value) -> set[str]:
        if not isinstance(value, list):
            return set()
        return {str(item).strip() for item in value if str(item).strip()}

    def _allowed(self, event: AstrMessageEvent) -> bool:
        allowed_users = self._string_set(self.config.get("allowed_users", []))
        allowed_groups = self._string_set(self.config.get("allowed_groups", []))
        if not allowed_users and not allowed_groups:
            return True

        sender_id = str(event.get_sender_id() or "")
        group_id = str(event.get_group_id() or "")
        return sender_id in allowed_users or group_id in allowed_groups

    def _limits(self) -> tuple[int, int]:
        try:
            default_limit = int(self.config.get("default_limit", 10))
        except (TypeError, ValueError):
            default_limit = 10
        try:
            max_limit = int(self.config.get("max_limit", 20))
        except (TypeError, ValueError):
            max_limit = 20
        return max(1, default_limit), min(50, max(1, max_limit))

    def _load(self):
        return load_snapshot(DATA_FILE)

    def _load_daily(self):
        return load_daily(DAILY_FILE)

    def _load_lookup(self):
        return load_snapshot(LOOKUP_FILE)


    @staticmethod
    def _load_images(manifest_file: str = IMAGE_MANIFEST_FILE):
        return load_image_manifest(manifest_file)
    @staticmethod
    def _load_academic_labels():
        return load_academic_labels(ACADEMIC_LABELS_FILE)


    @staticmethod
    def _write_statistics_csv(content: str, snapshot_id: int, scope: str) -> Path:
        STATISTICS_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        STATISTICS_EXPORT_DIR.chmod(0o755)
        output = STATISTICS_EXPORT_DIR / f"oj-statistics-{snapshot_id}-{scope}.csv"
        temporary = output.with_suffix(".csv.tmp")
        temporary.write_text("\ufeff" + content, encoding="utf-8")
        temporary.chmod(0o644)
        temporary.replace(output)
        output.chmod(0o644)
        exports = sorted(STATISTICS_EXPORT_DIR.glob("oj-statistics-*.csv"), key=lambda path: path.stat().st_mtime, reverse=True)
        for stale in exports[20:]:
            stale.unlink()
        return output


    @staticmethod
    def _load_scoped(path: str):
        return load_snapshot(path, allow_empty=True)

    def _requested_count(self, limit: int) -> int:
        default_limit, max_limit = self._limits()
        requested = default_limit if limit <= 0 else limit
        return min(max_limit, max(1, requested))

    def _freshness(self, snapshot) -> str:
        try:
            stale_after = int(self.config.get("stale_after_minutes", 15))
        except (TypeError, ValueError):
            stale_after = 15
        age = snapshot.age_minutes()
        return f"（数据已延迟 {age} 分钟）" if age > max(1, stale_after) else ""

    @staticmethod
    def _page_key(event: AstrMessageEvent) -> str:
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        return f"{origin}:{event.get_sender_id() or ''}"

    async def _leaderboard_image(
        self,
        event: AstrMessageEvent,
        page_number: int,
        manifest_file: str = IMAGE_MANIFEST_FILE,
        snapshot_file: str | None = DATA_FILE,
        title: str = "榜单",
    ):
        try:
            manifest = self._load_images(manifest_file)
            image_path = manifest.page_path(page_number)
        except RankDataError as exc:
            logger.warning(f"[{PLUGIN_NAME}] 读取{title}图片失败: {exc}")
            if snapshot_file is None:
                yield event.plain_result(f"暂时无法查询{title}：{exc}")
                return
            async for result in self._leaderboard_text(
                event,
                page_number,
                snapshot_file=snapshot_file,
                title=title,
                prefix=f"图片暂不可用（{exc}），已切换文字榜单\n",
            ):
                yield result
            return
        self._last_views[self._page_key(event)] = (
            manifest_file, snapshot_file, title, page_number
        )
        yield event.image_result(str(image_path))

    async def _leaderboard_text(
        self,
        event: AstrMessageEvent,
        page_number: int,
        snapshot_file: str = DATA_FILE,
        title: str = "榜单",
        prefix: str = "",
    ):
        try:
            snapshot = self._load_scoped(snapshot_file)
        except RankDataError as exc:
            yield event.plain_result(f"暂时无法查询{title}：{exc}")
            return
        page_size = 20
        page_count = math.ceil(snapshot.user_count / page_size)
        if page_number < 1 or page_number > page_count:
            yield event.plain_result(f"页码超出范围（1-{page_count}）")
            return
        start = (page_number - 1) * page_size
        rows = []
        for row in snapshot.users[start : start + page_size]:
            text = format_user(row, compact=True)
            if row.get("is_historical"):
                text += "｜当前 ranklist 暂未收录（历史记录）"
            rows.append(text)
        header = (
            f"OJ {title}文字版 第 {page_number}/{page_count} 页（{snapshot.prefix}）\n"
            f"更新 {short_time(snapshot)}｜共 {snapshot.user_count} 人"
            f"{self._freshness(snapshot)}"
        )
        yield event.plain_result(prefix + "\n".join([header, *rows]))

    @filter.command("榜单")
    async def leaderboard(self, event: AstrMessageEvent, page_number: int = 1):
        if not self._allowed(event):
            yield event.plain_result("当前会话未获准查询榜单。")
            return
        if page_number < 1:
            yield event.plain_result("页码必须从 1 开始，例如：/榜单 2")
            return
        async for result in self._leaderboard_image(event, page_number):
            yield result

    @filter.command("翻页")
    async def turn_page(self, event: AstrMessageEvent, page_number: int = 0):
        if not self._allowed(event):
            yield event.plain_result("当前会话未获准查询榜单。")
            return
        view = self._last_views.get(self._page_key(event))
        manifest_file, snapshot_file, title, previous = view or (
            IMAGE_MANIFEST_FILE,
            DATA_FILE,
            "榜单",
            0,
        )
        if page_number <= 0:
            try:
                manifest = self._load_images(manifest_file)
            except RankDataError as exc:
                yield event.plain_result(f"暂时无法查看{title}图片：{exc}")
                return
            page_number = previous + 1
            if page_number > manifest.page_count:
                page_number = 1
        async for result in self._leaderboard_image(
            event, page_number, manifest_file, snapshot_file, title
        ):
            yield result

    @filter.command("计院榜单")
    async def computer_college_leaderboard(
        self, event: AstrMessageEvent, page_number: int = 1
    ):
        if not self._allowed(event):
            yield event.plain_result("当前会话未获准查询榜单。")
            return
        if page_number < 1:
            yield event.plain_result("页码必须从 1 开始，例如：/计院榜单 2")
            return
        async for result in self._leaderboard_image(
            event,
            page_number,
            COMPUTER_COLLEGE_IMAGE_MANIFEST_FILE,
            COMPUTER_COLLEGE_FILE,
            "计院榜单",
        ):
            yield result

    @filter.command("软院榜单")
    async def software_college_leaderboard(
        self, event: AstrMessageEvent, page_number: int = 1
    ):
        if not self._allowed(event):
            yield event.plain_result("当前会话未获准查询榜单。")
            return
        if page_number < 1:
            yield event.plain_result("页码必须从 1 开始，例如：/软院榜单 2")
            return
        async for result in self._leaderboard_image(
            event,
            page_number,
            SOFTWARE_COLLEGE_IMAGE_MANIFEST_FILE,
            SOFTWARE_COLLEGE_FILE,
            "软院榜单",
        ):
            yield result

    @filter.command("最卷班级")
    async def class_intensity_leaderboard(self, event: AstrMessageEvent):
        if not self._allowed(event):
            yield event.plain_result("当前会话未获准查询榜单。")
            return
        async for result in self._leaderboard_image(
            event, 1, CLASS_INTENSITY_IMAGE_MANIFEST_FILE, None, "最卷班级"
        ):
            yield result

    @filter.command("最卷专业")
    async def major_intensity_leaderboard(self, event: AstrMessageEvent):
        if not self._allowed(event):
            yield event.plain_result("当前会话未获准查询榜单。")
            return
        async for result in self._leaderboard_image(
            event, 1, MAJOR_INTENSITY_IMAGE_MANIFEST_FILE, None, "最卷专业"
        ):
            yield result

    @filter.command("专业榜单")
    async def major_leaderboard(
        self, event: AstrMessageEvent, query: str = "", page_number: int = 1
    ):
        if not self._allowed(event):
            yield event.plain_result("当前会话未获准查询榜单。")
            return
        query = query.strip()
        try:
            labels = self._load_academic_labels()
        except RankDataError as exc:
            yield event.plain_result(f"暂时无法识别专业：{exc}")
            return
        major_id = labels.resolve_major(query)
        if major_id is None or page_number < 1:
            yield event.plain_result(
                "格式：/专业榜单 专业简称 [页码]\n"
                "例如：/专业榜单 示例专业\n"
                "备用格式：/专业榜单 20261001 [页码]\n"
                "输入 /帮助 查询榜单帮助。"
            )
            return
        try:
            major_ids = load_major_image_registry(MAJOR_IMAGE_REGISTRY_FILE)
        except RankDataError as exc:
            yield event.plain_result(f"暂时无法查询专业榜单：{exc}")
            return
        if major_id not in major_ids:
            yield event.plain_result(f"当前榜单中没有该专业：{query}。")
            return
        title = labels.major_name(major_id)
        async for result in self._leaderboard_image(
            event, page_number, f"{MAJOR_IMAGE_ROOT}/{major_id}/manifest.json",
            f"{MAJOR_DATA_ROOT}/{major_id}.json", f"{title}专业榜单"
        ):
            yield result

    @filter.command("班级榜单")
    async def class_leaderboard(
        self, event: AstrMessageEvent, query: str = "", page_number: int = 1
    ):
        if not self._allowed(event):
            yield event.plain_result("当前会话未获准查询榜单。")
            return
        query = query.strip()
        try:
            labels = self._load_academic_labels()
        except RankDataError as exc:
            yield event.plain_result(f"暂时无法识别班级：{exc}")
            return
        class_id = labels.resolve_class(query)
        if class_id is None:
            yield event.plain_result(
                "格式：/班级榜单 专业简称+两位班号\n"
                "例如：/班级榜单 示例专业01班\n"
                "备用格式：/班级榜单 2026100101\n"
                "输入 /帮助 查询榜单帮助。"
            )
            return
        if page_number != 1:
            yield event.plain_result(
                "班级榜单每班仅 1 页。\n"
                "格式：/班级榜单 示例专业01班\n"
                "备用格式：/班级榜单 2026100101\n"
                "输入 /帮助 查询榜单帮助。"
            )
            return
        try:
            class_ids = load_class_image_registry(CLASS_IMAGE_REGISTRY_FILE)
        except RankDataError as exc:
            yield event.plain_result(f"暂时无法查询班级榜单：{exc}")
            return
        if class_id not in class_ids:
            yield event.plain_result(f"当前榜单中没有该班级：{query}。")
            return
        title = labels.class_name(class_id)
        async for result in self._leaderboard_image(
            event, 1, f"{CLASS_IMAGE_ROOT}/{class_id}/manifest.json",
            f"{CLASS_DATA_ROOT}/{class_id}.json", f"{title}榜单"
        ):
            yield result


    @filter.command("统计数据")
    async def statistics_data(
        self, event: AstrMessageEvent, scope: str = "", target: str = ""
    ):
        if not self._allowed(event):
            yield event.plain_result("当前会话未获准查询榜单。")
            return
        scope = scope.strip()
        target = target.strip()
        include_inactive = scope == "全量"
        class_id = None
        profession_id = None
        usage = (
            "格式：/统计数据\n"
            "/统计数据 全量\n"
            "/统计数据 班级 示例专业01班\n"
            "/统计数据 专业 示例专业\n"
            "班级、专业也可使用旧数字格式。\n"
            "输入 /开发者帮助 查看隐藏命令。"
        )
        if scope in ("", "全量"):
            if target:
                yield event.plain_result(usage)
                return
        elif scope in ("班级", "专业") and target:
            try:
                labels = self._load_academic_labels()
            except RankDataError as exc:
                yield event.plain_result(f"暂时无法识别统计范围：{exc}")
                return
            if scope == "班级":
                class_id = labels.resolve_class(target)
            else:
                profession_id = labels.resolve_major(target)
            if (scope == "班级" and class_id is None) or (scope == "专业" and profession_id is None):
                yield event.plain_result(usage)
                return
        else:
            yield event.plain_result(usage)
            return
        try:
            snapshot = self._load()
            labels = self._load_academic_labels()
            content = build_statistics_csv(
                snapshot, labels, include_inactive_students=include_inactive,
                profession_id=profession_id, class_id=class_id
            )
            scope_name = (
                "all" if include_inactive else
                f"class-{class_id[len(labels.prefix):]}" if class_id else
                f"profession-{profession_id[len(labels.prefix):]}" if profession_id else "active"
            )
            output = self._write_statistics_csv(content, snapshot.snapshot_id, scope_name)
        except (OSError, RankDataError) as exc:
            yield event.plain_result(f"暂时无法导出统计数据：{exc}")
            return
        try:
            yield event.chain_result([File(name=output.name, file=str(output))])
        except (OSError, RuntimeError, TypeError) as exc:
            logger.warning(f"[{PLUGIN_NAME}] CSV 文件组件不可用，回退文字输出: {exc}")
            yield event.plain_result(content)


    @filter.command("文字榜单")
    async def text_leaderboard(
        self,
        event: AstrMessageEvent,
        page_number: int = 1,
    ):
        if not self._allowed(event):
            yield event.plain_result("当前会话未获准查询榜单。")
            return
        async for result in self._leaderboard_text(event, page_number):
            yield result

    @filter.command("查榜")
    async def lookup(self, event: AstrMessageEvent, query: str = ""):
        if not self._allowed(event):
            yield event.plain_result("当前会话未获准查询榜单。")
            return
        if not query.strip():
            yield event.plain_result("用法：/查榜 学号或昵称")
            return

        try:
            snapshot = self._load_lookup()
        except RankDataError as exc:
            logger.warning(f"[{PLUGIN_NAME}] 读取查榜扩展数据失败: {exc}")
            yield event.plain_result(f"查榜扩展数据暂时无法读取：{exc}")
            return

        matches = find_users(snapshot, query, limit=10)
        if not matches:
            yield event.plain_result(
                f"没有找到“{query.strip()}”。当前 /查榜 范围为 23 级至 26 级学生。"
            )
            return

        body = []
        for row in matches:
            text = format_user(row)
            if row.get("is_historical"):
                text += (
                    "\n当前 ranklist 暂未收录；以下为最近一次历史记录（"
                    f"{short_iso_time(str(row.get('last_seen_at', '')))}）。"
                )
            body.append(text)
        if len(matches) == 10:
            body.append("结果较多，仅显示前 10 条；用完整学号可精确查询。")
        footer = (
            f"查询范围：23 级至 26 级｜更新 {short_time(snapshot)}"
            f"{self._freshness(snapshot)}"
        )
        yield event.plain_result("\n\n".join([*body, footer]))

    @filter.command("榜单状态")
    async def status(self, event: AstrMessageEvent):
        if not self._allowed(event):
            yield event.plain_result("当前会话未获准查询榜单。")
            return
        try:
            snapshot = self._load()
        except RankDataError as exc:
            yield event.plain_result(f"榜单状态：异常（{exc}）")
            return

        age = snapshot.age_minutes()
        state = "正常" if not self._freshness(snapshot) else "数据延迟"
        yield event.plain_result(
            f"数据状态：{state}\n"
            f"最后更新：{short_time(snapshot)}（{age} 分钟前）\n"
            f"历史名册人数：{snapshot.historical_user_count}\n"
            f"当前 ranklist 人数：{snapshot.user_count}\n"
            f"当前缺失人数：{snapshot.missing_user_count}\n"
            f"覆盖率：{snapshot.coverage_rate:.2%}"
        )

    @filter.command("变化")
    async def changes(self, event: AstrMessageEvent, query: str = ""):
        if not self._allowed(event):
            yield event.plain_result("当前会话未获准查询榜单。")
            return
        try:
            snapshot = self._load_daily()
        except RankDataError as exc:
            yield event.plain_result(f"暂时无法查询每日变化：{exc}")
            return

        if query.strip():
            matches = find_changes(snapshot, query, limit=10)
            if not matches:
                yield event.plain_result(f"每日变化中没有找到“{query.strip()}”。")
                return
            body = [format_change(row) for row in matches]
            footer = (
                f"统计从 {short_iso_time(snapshot.baseline_fetched_at)} 开始，截至 "
                f"{short_iso_time(snapshot.fetched_at_text)}。"
                f"{self._freshness(snapshot)}"
            )
            yield event.plain_result("\n\n".join([*body, footer]))
            return

        new_count = sum(1 for row in snapshot.changes if row.get("is_new"))
        up_count = sum(
            1 for row in snapshot.changes if (row.get("rank_change") or 0) > 0
        )
        down_count = sum(
            1 for row in snapshot.changes if (row.get("rank_change") or 0) < 0
        )
        unchanged = snapshot.user_count - new_count - up_count - down_count
        yield event.plain_result(
            "今日榜单变化\n"
            f"统计从 {short_iso_time(snapshot.baseline_fetched_at)} 开始，截至 "
            f"{short_iso_time(snapshot.fetched_at_text)}。\n"
            f"今天上升 {up_count} 人｜下降 {down_count} 人｜排名不变 {unchanged} 人｜"
            f"首次上榜 {new_count} 人\n"
            "输入 /变化 学号或昵称，可查看个人今天的进展。"
            f"{self._freshness(snapshot)}"
        )

    @filter.command("冲榜")
    async def climbers(self, event: AstrMessageEvent, limit: int = 0):
        if not self._allowed(event):
            yield event.plain_result("当前会话未获准查询榜单。")
            return
        count = self._requested_count(limit)
        try:
            snapshot = self._load_daily()
        except RankDataError as exc:
            yield event.plain_result(f"暂时无法查询冲榜：{exc}")
            return

        candidates = [
            row
            for row in snapshot.changes
            if not row.get("is_new") and (row.get("rank_change") or 0) > 0
        ]
        candidates.sort(
            key=lambda row: (
                -int(row.get("rank_change") or 0),
                -int(row.get("accepted_change") or 0),
                int(row["rank"]),
            )
        )
        rows = candidates[:count]
        if not rows:
            yield event.plain_result("今天暂时还没有名次上升记录。")
            return
        body = [
            f"{index}. {row['user_id']} {str(row.get('nickname') or '未设置昵称')}｜"
            f"第 {int(row['baseline_rank'])} 名 → 第 {int(row['rank'])} 名｜"
            f"提升 {int(row['rank_change'])} 名"
            for index, row in enumerate(rows, 1)
        ]
        header = (
            f"今日名次提升榜（前 {len(rows)} 名）\n"
            f"统计从 {short_iso_time(snapshot.baseline_fetched_at)} 开始，截至 "
            f"{short_iso_time(snapshot.fetched_at_text)}。"
            f"{self._freshness(snapshot)}"
        )
        yield event.plain_result("\n".join([header, *body]))

    async def _scoped_leaderboard(
        self,
        event: AstrMessageEvent,
        path: str,
        title: str,
        limit: int,
    ):
        if not self._allowed(event):
            yield event.plain_result("当前会话未获准查询榜单。")
            return
        try:
            snapshot = self._load_scoped(path)
        except RankDataError as exc:
            yield event.plain_result(f"暂时无法查询{title}：{exc}")
            return
        if not snapshot.users:
            yield event.plain_result(f"{snapshot.prefix} {title}目前还没有数据。")
            return
        count = self._requested_count(limit)
        rows = [
            f"{index}. {format_user(row, compact=True)}"
            for index, row in enumerate(snapshot.users[:count], 1)
        ]
        header = (
            f"{title}（前 {len(rows)} 名）\n"
            f"共 {snapshot.user_count} 人｜更新于 {short_time(snapshot)}"
            f"{self._freshness(snapshot)}"
        )
        yield event.plain_result("\n".join([header, *rows]))

    @filter.command("周榜")
    async def weekly(self, event: AstrMessageEvent, limit: int = 0):
        async for result in self._scoped_leaderboard(
            event, WEEKLY_FILE, "本周 OJ 排名", limit
        ):
            yield result

    @filter.command("月榜")
    async def monthly(self, event: AstrMessageEvent, limit: int = 0):
        async for result in self._scoped_leaderboard(
            event, MONTHLY_FILE, "本月 OJ 排名", limit
        ):
            yield result

    async def _activity_leaderboard(
        self,
        event: AstrMessageEvent,
        field: str,
        label: str,
        limit: int,
    ):
        if not self._allowed(event):
            yield event.plain_result("当前会话未获准查询榜单。")
            return
        try:
            snapshot = self._load_daily()
        except RankDataError as exc:
            yield event.plain_result(f"暂时无法查询{label}：{exc}")
            return
        rows = [
            row
            for row in snapshot.changes
            if not row.get("is_new") and int(row.get(field) or 0) > 0
        ]
        rows.sort(
            key=lambda row: (
                -int(row.get(field) or 0),
                -int(row.get("rank_change") or 0),
                int(row["rank"]),
            )
        )
        selected = rows[: self._requested_count(limit)]
        if not selected:
            yield event.plain_result(f"今天暂时还没有{label}记录。")
            return
        metric_name = "新增通过题数" if field == "accepted_change" else "新增提交次数"
        unit = "题" if field == "accepted_change" else "次"
        body = []
        for index, row in enumerate(selected, 1):
            nickname = str(row.get("nickname") or "未设置昵称")
            body.append(
                f"{index}. {row['user_id']} {nickname}｜"
                f"{metric_name} {int(row[field])} {unit}｜当前第 {int(row['rank'])} 名"
            )
        header = (
            f"今日{metric_name}榜（前 {len(selected)} 名）\n"
            f"统计从 {short_iso_time(snapshot.baseline_fetched_at)} 开始，截至 "
            f"{short_iso_time(snapshot.fetched_at_text)}。"
            f"{self._freshness(snapshot)}"
        )
        yield event.plain_result("\n".join([header, *body]))

    @filter.command("刷题榜")
    async def accepted_activity(self, event: AstrMessageEvent, limit: int = 0):
        async for result in self._activity_leaderboard(
            event, "accepted_change", "刷题", limit
        ):
            yield result

    @filter.command("提交榜")
    async def submitted_activity(self, event: AstrMessageEvent, limit: int = 0):
        async for result in self._activity_leaderboard(
            event, "submitted_change", "提交", limit
        ):
            yield result

    @filter.command("新星榜")
    async def rising_stars(self, event: AstrMessageEvent, limit: int = 0):
        if not self._allowed(event):
            yield event.plain_result("当前会话未获准查询榜单。")
            return
        try:
            snapshot = self._load_daily()
        except RankDataError as exc:
            yield event.plain_result(f"暂时无法查询新星榜：{exc}")
            return
        rows = [
            row
            for row in snapshot.changes
            if row.get("baseline_rank") is not None
            and int(row["baseline_rank"]) > 100
            and int(row.get("accepted_change") or 0) > 0
        ]
        rows.sort(
            key=lambda row: (
                -int(row.get("accepted_change") or 0),
                -int(row.get("rank_change") or 0),
                int(row["rank"]),
            )
        )
        selected = rows[: self._requested_count(limit)]
        if not selected:
            yield event.plain_result("今天暂时还没有符合条件的新星选手。")
            return
        body = []
        for index, row in enumerate(selected, 1):
            nickname = str(row.get("nickname") or "未设置昵称")
            body.append(
                f"{index}. {row['user_id']} {nickname}｜"
                f"今天新增通过 {int(row.get('accepted_change') or 0)} 题｜"
                f"当前第 {int(row['rank'])} 名"
            )
        header = (
            f"今日新星榜（前 {len(selected)} 名）\n"
            "范围：今天首次记录时排在第 100 名以后，并且今天有新增通过。\n"
            f"统计截至 {short_iso_time(snapshot.fetched_at_text)}。"
            f"{self._freshness(snapshot)}"
        )
        yield event.plain_result("\n".join([header, *body]))

    @filter.command("随机选手")
    async def random_player(self, event: AstrMessageEvent):
        if not self._allowed(event):
            yield event.plain_result("当前会话未获准查询榜单。")
            return
        try:
            snapshot = self._load()
        except RankDataError as exc:
            yield event.plain_result(f"暂时无法随机选人：{exc}")
            return
        candidates = [
            row
            for row in snapshot.users
            if int(row.get("accepted") or 0) > 0
            or int(row.get("submitted") or 0) > 0
        ]
        if not candidates:
            yield event.plain_result("当前榜单暂时没有有 AC 或提交记录的同学。")
            return
        row = secrets.choice(candidates)
        yield event.plain_result(
            "随机选手\n" + format_user(row) + f"\n更新 {short_time(snapshot)}"
        )

    @filter.command("日榜")
    async def daily_migration(self, event: AstrMessageEvent):
        if not self._allowed(event):
            yield event.plain_result("当前会话未获准查询榜单。")
            return
        yield event.plain_result(
            "今日数据已分为以下几个榜单：\n"
            "/变化：查看全榜或个人今天的变化\n"
            "/冲榜：查看今天名次提升最多的同学\n"
            "/刷题榜：查看今天通过题目最多的同学\n"
            "/提交榜：查看今天提交次数最多的同学"
        )

    @filter.command("开发者帮助", priority=HELP_PRIORITY)
    async def developer_help(self, event: AstrMessageEvent):
        if self._allowed(event):
            yield event.plain_result(DEVELOPER_HELP_TEXT)

    @filter.command("帮助", priority=HELP_PRIORITY)
    async def help_cn(self, event: AstrMessageEvent):
        if self._allowed(event):
            yield event.plain_result(HELP_TEXT)

    @filter.command("rank")
    async def rank_alias(self, event: AstrMessageEvent, page_number: int = 1):
        async for result in self.leaderboard(event, page_number):
            yield result

    @filter.command("who")
    async def who_alias(self, event: AstrMessageEvent, query: str = ""):
        async for result in self.lookup(event, query):
            yield result

    @filter.command("daily")
    async def daily_alias(self, event: AstrMessageEvent, query: str = ""):
        async for result in self.changes(event, query):
            yield result
