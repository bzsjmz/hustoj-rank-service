from __future__ import annotations

import json
import logging
import math
import os
import queue
import signal
import socketserver
import threading
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from .college import class_display_name, major_display_name
from .config import PROJECT_ROOT, Settings
from .models import RankEntry
from .renderer import LeaderboardRenderer


@dataclass(frozen=True)
class RenderRequest:
    board: str
    entity: str | None
    page: int

    @property
    def key(self) -> tuple[str, str | None, int]:
        return (self.board, self.entity, self.page)


class RenderFuture:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.response: dict[str, Any] | None = None

    def wait(self, timeout: float) -> dict[str, Any]:
        if not self.event.wait(timeout) or self.response is None:
            return {"ok": False, "error": "渲染请求超时"}
        return self.response


class SingleFlightQueue:
    """Merge concurrent requests for the same board, entity and page."""

    def __init__(self) -> None:
        self.queue: queue.Queue[tuple[RenderRequest, RenderFuture]] = queue.Queue()
        self._lock = threading.Lock()
        self._active: dict[tuple[str, str | None, int], RenderFuture] = {}

    def submit(self, request: RenderRequest) -> tuple[RenderFuture, bool]:
        with self._lock:
            existing = self._active.get(request.key)
            if existing is not None:
                return existing, False
            future = RenderFuture()
            self._active[request.key] = future
            self.queue.put((request, future))
            return future, True

    def complete(self, request: RenderRequest, response: dict[str, Any]) -> None:
        with self._lock:
            future = self._active.pop(request.key, None)
        if future is not None:
            future.response = response
            future.event.set()


@dataclass(frozen=True)
class BoardDefinition:
    source: Path
    image_directory: str
    title: str
    page_size: int = 20
    height: int = LeaderboardRenderer.HEIGHT
    column_labels: tuple[str, str, str] | None = None
    entity_type: str | None = None


class OnDemandRenderer:
    EAGER_BOARDS = (
        RenderRequest("total", None, 1),
        RenderRequest("computer", None, 1),
        RenderRequest("software", None, 1),
        RenderRequest("class-intensity", None, 1),
        RenderRequest("major-intensity", None, 1),
    )

    def __init__(self, settings: Settings, browser_context, logger: logging.Logger):
        self.settings = settings
        self.browser_context = browser_context
        self.logger = logger
        self.layout = settings.student_id_layout
        self.pre_render_pages = max(1, int(os.getenv("PRE_RENDER_PAGES", "5")))

    def definition(self, board: str, entity: str | None = None) -> BoardDefinition:
        share = self.settings.share_dir
        fixed = {
            "total": BoardDefinition(share / "latest.json", "rank-images", "天梯总榜"),
            "computer": BoardDefinition(
                share / "computer-college.json", "college-images/computer", "计院榜单"
            ),
            "software": BoardDefinition(
                share / "software-college.json", "college-images/software", "软院榜单"
            ),
            "weekly": BoardDefinition(share / "weekly.json", "weekly-images", "周榜"),
            "monthly": BoardDefinition(share / "monthly.json", "monthly-images", "月榜"),
            "class-intensity": BoardDefinition(
                share / "class-intensity.json",
                "class-intensity-images",
                "最卷班级",
                column_labels=("班级", "班级 AC 总量", "班级卷王"),
                entity_type="班级",
            ),
            "major-intensity": BoardDefinition(
                share / "major-intensity.json",
                "major-intensity-images",
                "最卷专业",
                column_labels=("专业", "专业 AC 总量", "卷王班级与同学"),
                entity_type="专业",
            ),
        }
        if board in fixed:
            if entity is not None:
                raise ValueError("该榜单不接受实体参数")
            return fixed[board]
        if board == "class":
            if entity is None or not self.layout.is_class_id(entity):
                raise ValueError("班级编号无效")
            source = share / "class-ranklists" / f"{entity}.json"
            payload = self._load_payload(source)
            has_history = int(payload.get("historical_user_count", 0)) > 0
            count = int(payload.get("user_count", 0))
            return BoardDefinition(
                source,
                f"class-images/{entity}",
                f"{class_display_name(entity, self.layout)}榜单",
                page_size=60,
                height=LeaderboardRenderer.class_image_height(count, has_history),
            )
        if board == "major":
            if entity is None or not self.layout.is_major_id(entity):
                raise ValueError("专业编号无效")
            return BoardDefinition(
                share / "major-ranklists" / f"{entity}.json",
                f"major-images/{entity}",
                f"{major_display_name(entity, self.layout)}专业榜单",
            )
        raise ValueError("不支持的榜单类型")

    @staticmethod
    def _load_payload(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError("榜单数据尚未生成") from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("榜单数据暂时无法读取") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("榜单数据格式不兼容")
        users = payload.get("users")
        if not isinstance(users, list) or not users:
            raise ValueError("榜单当前没有可渲染数据")
        if int(payload.get("user_count", -1)) != len(users):
            raise ValueError("榜单人数校验失败")
        return payload

    @staticmethod
    def _entries(payload: dict[str, Any]) -> tuple[list[RankEntry], frozenset[str]]:
        entries = []
        historical = set()
        for row in payload["users"]:
            if not isinstance(row, dict):
                raise ValueError("榜单用户记录无效")
            entry = RankEntry(
                rank=int(row["rank"]),
                user_id=str(row["user_id"]),
                nickname=str(row.get("nickname", "")),
                accepted=int(row["accepted"]),
                submitted=int(row["submitted"]),
                ratio=float(row["ratio"]),
                level=str(row.get("level", "")),
            )
            entries.append(entry)
            if row.get("is_historical"):
                historical.add(entry.user_id)
        return entries, frozenset(historical)

    def render(self, request: RenderRequest, eager: bool = False) -> dict[str, Any]:
        definition = self.definition(request.board, request.entity)
        payload = self._load_payload(definition.source)
        entries, historical = self._entries(payload)
        page_count = math.ceil(len(entries) / definition.page_size)
        if request.page < 1 or request.page > page_count:
            raise ValueError(f"页码超出范围（1-{page_count}）")
        page_numbers = (
            range(1, min(self.pre_render_pages, page_count) + 1)
            if eager
            else (request.page,)
        )
        subtitle = None
        if request.board == "class":
            subtitle = (
                f"当前 ranklist {len(entries) - len(historical)} 人"
                f"｜历史补全 {len(historical)} 人｜共 {len(entries)} 名选手"
            )
        renderer = LeaderboardRenderer(
            self.settings.share_dir,
            definition.image_directory,
            page_size=definition.page_size,
            height=definition.height,
        )
        started = time.monotonic()
        manifest = renderer.render(
            self.browser_context,
            entries,
            str(payload.get("prefix", self.settings.prefix)),
            str(payload.get("fetched_at", "")),
            max(1, int(payload.get("snapshot_id", 0))),
            title=definition.title,
            column_labels=definition.column_labels,
            historical_user_ids=historical,
            subtitle=subtitle,
            entity_type=definition.entity_type,
            page_numbers=page_numbers,
        )
        page = manifest["pages"].get(str(request.page))
        if not isinstance(page, dict) or not isinstance(page.get("file"), str):
            raise RuntimeError("请求页面未发布")
        relative = str(Path(definition.image_directory) / page["file"])
        self.logger.info(
            "render %s entity=%s pages=%s rendered=%d cached=%d elapsed=%.2fs",
            request.board,
            request.entity or "-",
            list(page_numbers),
            manifest["rendered_page_count"],
            manifest["cached_page_count"],
            time.monotonic() - started,
        )
        return {
            "ok": True,
            "relative_path": relative,
            "page_count": page_count,
            "rendered_page_count": manifest["rendered_page_count"],
        }


class RenderSocketHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            raw = self.rfile.readline(8193)
            if not raw or len(raw) > 8192:
                raise ValueError("请求过长")
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("请求格式无效")
            entity_value = payload.get("entity")
            entity = None if entity_value in (None, "") else str(entity_value)
            request = RenderRequest(
                board=str(payload.get("board", "")),
                entity=entity,
                page=int(payload.get("page", 0)),
            )
            future, _ = self.server.coordinator.submit(request)  # type: ignore[attr-defined]
            response = future.wait(self.server.request_timeout)  # type: ignore[attr-defined]
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            response = {"ok": False, "error": str(exc) or "渲染请求无效"}
        self.wfile.write(
            (json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            .encode("utf-8")
        )


class ThreadedUnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def _logger(log_dir: Path) -> logging.Logger:
    logger = logging.getLogger("oj_rank.render")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        log_dir / "renderer.log", maxBytes=10 * 1024 * 1024, backupCount=2,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(stream)
    logger.addHandler(file_handler)
    return logger


def run() -> int:
    settings = Settings.from_env()
    settings.ensure_directories()
    logger = _logger(settings.log_dir)
    run_dir = Path(os.getenv("OJ_RENDER_RUN_DIR", str(PROJECT_ROOT / "run")))
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir.chmod(0o755)
    socket_path = run_dir / "render.sock"
    if socket_path.exists() or socket_path.is_socket():
        socket_path.unlink()

    coordinator = SingleFlightQueue()
    stop_event = threading.Event()

    def stop(_signum, _frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=settings.browser_executable,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context()
        renderer = OnDemandRenderer(settings, context, logger)
        server = ThreadedUnixServer(str(socket_path), RenderSocketHandler)
        server.coordinator = coordinator  # type: ignore[attr-defined]
        server.request_timeout = float(os.getenv("RENDER_REQUEST_TIMEOUT", "90"))  # type: ignore[attr-defined]
        socket_path.chmod(0o666)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        logger.info("render service ready, socket=%s", socket_path)

        eager_mtimes: dict[tuple[str, str | None, int], int] = {}
        poll_seconds = max(1.0, float(os.getenv("RENDER_POLL_SECONDS", "3")))
        next_poll = 0.0
        try:
            while not stop_event.is_set():
                try:
                    request, _future = coordinator.queue.get(timeout=0.5)
                except queue.Empty:
                    request = None
                if request is not None:
                    try:
                        response = renderer.render(request)
                    except Exception as exc:
                        logger.exception(
                            "on-demand render failed: board=%s entity=%s page=%d",
                            request.board, request.entity, request.page,
                        )
                        response = {"ok": False, "error": str(exc) or "图片渲染失败"}
                    coordinator.complete(request, response)
                    continue

                if time.monotonic() < next_poll:
                    continue
                next_poll = time.monotonic() + poll_seconds
                for eager_request in OnDemandRenderer.EAGER_BOARDS:
                    try:
                        definition = renderer.definition(
                            eager_request.board, eager_request.entity
                        )
                        mtime = definition.source.stat().st_mtime_ns
                    except (OSError, ValueError):
                        continue
                    if eager_mtimes.get(eager_request.key) == mtime:
                        continue
                    try:
                        renderer.render(eager_request, eager=True)
                        eager_mtimes[eager_request.key] = mtime
                    except Exception:
                        logger.exception("eager render failed: %s", eager_request.board)
                    break
        finally:
            server.shutdown()
            server.server_close()
            context.close()
            browser.close()
            socket_path.unlink(missing_ok=True)
            logger.info("render service stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
