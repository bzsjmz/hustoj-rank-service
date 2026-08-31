from __future__ import annotations

import logging
import signal
import threading
from datetime import datetime

from .college import (
    COMPUTER_COLLEGE,
    SOFTWARE_COLLEGE,
    build_class_intensity,
    build_major_intensity,
    class_display_name,
    major_display_name,
    merge_class_history,
    split_and_rerank_classes,
    split_and_rerank_majors,
    split_and_rerank_colleges,
)
from .config import Settings
from .crawler import CrawlError, RankCrawler, WebVPNSessionExpired
from .database import RankDatabase
from .exporter import RankExporter
from .logging_config import configure_logging
from .ranking import exclude_and_rerank, select_prefix_exclude_and_rerank
from .renderer import (
    LeaderboardRenderer,
    publish_class_image_manifest,
    publish_major_image_manifest,
)


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def run() -> int:
    settings = Settings.from_env()
    settings.ensure_directories()
    logger = configure_logging(settings.log_dir)
    database = RankDatabase(settings.database_path)
    database.initialize()
    exporter = RankExporter(settings.share_dir)
    student_id_layout = settings.student_id_layout
    renderer = LeaderboardRenderer(settings.share_dir)
    computer_college_renderer = LeaderboardRenderer(
        settings.share_dir, "college-images/computer"
    )
    software_college_renderer = LeaderboardRenderer(
        settings.share_dir, "college-images/software"
    )
    class_intensity_renderer = LeaderboardRenderer(
        settings.share_dir, "class-intensity-images"
    )
    major_intensity_renderer = LeaderboardRenderer(
        settings.share_dir, "major-intensity-images"
    )
    stop_event = threading.Event()

    def stop(_signum, _frame) -> None:
        logger.info("shutdown requested")
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    crawler = RankCrawler(settings, logger)
    state = "STARTING"

    logger.info(
        "service starting, prefix=%s interval=%ss database=%s",
        settings.prefix,
        settings.scrape_interval_seconds,
        settings.database_path,
    )
    try:
        crawler.start()
        while not stop_event.is_set():
            if state == "WAITING_FOR_AUTH":
                if crawler.validate_after_user_authentication():
                    state = "READY"
                    logger.info(
                        "WebVPN manual authentication accepted; collection resuming"
                    )
                    continue
                stop_event.wait(settings.login_check_seconds)
                continue

            try:
                source_entries = crawler.scrape_all()
                crawler.mark_ready("ranklist_validated")
                entries = exclude_and_rerank(
                    source_entries, settings.excluded_user_ids
                )
                removed_count = len(source_entries) - len(entries)
                if removed_count:
                    logger.info(
                        "excluded %d configured users from ranklist", removed_count
                    )
                state = "READY"
                logger.info("WebVPN OK")
                fetched_at = _timestamp()
                snapshot_id = database.save_complete_snapshot(
                    entries, settings.prefix, fetched_at
                )
                historical_roster_user_ids = database.historical_roster_user_ids(
                    settings.prefix, settings.excluded_user_ids, student_id_layout=student_id_layout
                )
                current_user_ids = {entry.user_id for entry in entries}
                missing_user_count = len(historical_roster_user_ids - current_user_ids)
                coverage_rate = len(current_user_ids) / len(historical_roster_user_ids)
                logger.info(
                    "roster coverage: historical=%d current=%d missing=%d coverage=%.2f%%",
                    len(historical_roster_user_ids),
                    len(current_user_ids),
                    missing_user_count,
                    coverage_rate * 100,
                )
                missing_historical_entries = database.latest_entries_for_user_ids(
                    settings.prefix,
                    historical_roster_user_ids - current_user_ids,
                )
                baseline = database.daily_baseline(
                    settings.prefix,
                    fetched_at,
                    snapshot_id,
                    settings.excluded_user_ids,
                )
                try:
                    exporter.export(
                        entries,
                        settings.prefix,
                        fetched_at,
                        snapshot_id,
                        baseline,
                        historical_roster_user_ids,
                    )
                    exporter.export_academic_labels(student_id_layout)
                    logger.info("bot share export updated, directory=%s", settings.share_dir)
                except Exception:
                    logger.exception(
                        "bot share export failed; SQLite snapshot remains committed"
                    )
                try:
                    lookup_entries = list(entries)
                    for lookup_prefix in settings.lookup_prefixes:
                        if lookup_prefix == settings.prefix:
                            continue
                        lookup_entries.extend(crawler.scrape_all(prefix=lookup_prefix))
                    exporter.export_lookup(
                        lookup_entries,
                        fetched_at,
                        snapshot_id,
                        settings.lookup_prefixes,
                        tuple(missing_historical_entries.values()),
                    )
                    logger.info(
                        "lookup export updated: %d current rows, %d historical fallbacks",
                        len(lookup_entries),
                        len(missing_historical_entries),
                    )
                except WebVPNSessionExpired:
                    raise
                except Exception:
                    logger.exception(
                        "lookup export failed; previous lookup export retained"
                    )

                try:
                    manifest = renderer.render(
                        crawler.context,
                        entries,
                        settings.prefix,
                        fetched_at,
                        snapshot_id,
                    )
                    logger.info(
                        "leaderboard images updated, %d pages, snapshot_id=%d",
                        manifest["page_count"],
                        snapshot_id,
                    )
                except Exception:
                    logger.exception(
                        "leaderboard image render failed; previous images retained"
                    )

                try:
                    computer_user_ids = database.snapshot_user_ids(
                        settings.college_split_snapshot_id, settings.prefix
                    )
                    computer_entries, software_entries = split_and_rerank_colleges(
                        entries, computer_user_ids
                    )
                    exporter.export_college(
                        computer_entries,
                        settings.prefix,
                        fetched_at,
                        snapshot_id,
                        COMPUTER_COLLEGE,
                        settings.college_split_snapshot_id,
                    )
                    exporter.export_college(
                        software_entries,
                        settings.prefix,
                        fetched_at,
                        snapshot_id,
                        SOFTWARE_COLLEGE,
                        settings.college_split_snapshot_id,
                    )
                    computer_manifest = computer_college_renderer.render(
                        crawler.context,
                        computer_entries,
                        settings.prefix,
                        fetched_at,
                        snapshot_id,
                        title="计院榜单",
                    )
                    software_manifest = software_college_renderer.render(
                        crawler.context,
                        software_entries,
                        settings.prefix,
                        fetched_at,
                        snapshot_id,
                        title="软院榜单",
                    )
                    logger.info(
                        "college leaderboards updated: %d computer users (%d pages), "
                        "%d software users (%d pages), split_snapshot_id=%d",
                        len(computer_entries),
                        computer_manifest["page_count"],
                        len(software_entries),
                        software_manifest["page_count"],
                        settings.college_split_snapshot_id,
                    )
                except Exception:
                    logger.exception(
                        "college leaderboard export/render failed; previous college "
                        "outputs retained"
                    )

                try:
                    current_class_ranklists = split_and_rerank_classes(
                        entries, student_id_layout
                    )
                    class_ranklists, class_historical_user_ids = merge_class_history(
                        current_class_ranklists,
                        tuple(missing_historical_entries.values()),
                        student_id_layout,
                    )
                    for class_id, class_entries in class_ranklists.items():
                        exporter.export_class(
                            class_entries,
                            settings.prefix,
                            fetched_at,
                            snapshot_id,
                            class_id,
                            class_historical_user_ids.get(class_id, frozenset()),
                            student_id_layout,
                        )

                    class_manifests = {}
                    for class_id, class_entries in class_ranklists.items():
                        historical_user_ids = class_historical_user_ids.get(
                            class_id, frozenset()
                        )
                        class_renderer = LeaderboardRenderer(
                            settings.share_dir,
                            f"class-images/{class_id}",
                            page_size=60,
                            height=LeaderboardRenderer.class_image_height(
                                len(class_entries), bool(historical_user_ids)
                            ),
                        )
                        manifest = class_renderer.render(
                            crawler.context,
                            class_entries,
                            settings.prefix,
                            fetched_at,
                            snapshot_id,
                            title=f"{class_display_name(class_id, student_id_layout)}榜单",
                            historical_user_ids=historical_user_ids,
                            subtitle=(
                                f"当前 ranklist {len(class_entries) - len(historical_user_ids)} 人"
                                f"｜历史补全 {len(historical_user_ids)} 人"
                                f"｜共 {len(class_entries)} 名选手"
                            ),
                        )
                        if manifest["page_count"] != 1:
                            raise RuntimeError(
                                f"class leaderboard was not rendered as one page: {class_id}"
                            )
                        class_manifests[class_id] = manifest
                    publish_class_image_manifest(
                        settings.share_dir,
                        snapshot_id,
                        fetched_at,
                        settings.prefix,
                        class_manifests,
                        student_id_layout,
                    )
                    logger.info(
                        "class leaderboards updated: %d classes, %d users, %d images",
                        len(class_ranklists),
                        sum(len(class_entries) for class_entries in class_ranklists.values()),
                        len(class_manifests),
                    )
                except Exception:
                    logger.exception(
                        "class leaderboard export/render failed; previous class outputs retained"
                    )

                try:
                    major_ranklists = split_and_rerank_majors(entries, student_id_layout)
                    for major_id, major_entries in major_ranklists.items():
                        exporter.export_major(
                            major_entries,
                            settings.prefix,
                            fetched_at,
                            snapshot_id,
                            major_id,
                            student_id_layout,
                        )

                    major_manifests = {}
                    for major_id, major_entries in major_ranklists.items():
                        major_renderer = LeaderboardRenderer(
                            settings.share_dir, f"major-images/{major_id}"
                        )
                        major_manifests[major_id] = major_renderer.render(
                            crawler.context,
                            major_entries,
                            settings.prefix,
                            fetched_at,
                            snapshot_id,
                            title=f"{major_display_name(major_id, student_id_layout)}专业榜单",
                        )
                    publish_major_image_manifest(
                        settings.share_dir,
                        snapshot_id,
                        fetched_at,
                        settings.prefix,
                        major_manifests,
                        student_id_layout,
                    )
                    logger.info(
                        "major leaderboards updated: %d majors, %d users, %d pages",
                        len(major_ranklists),
                        sum(len(major_entries) for major_entries in major_ranklists.values()),
                        sum(manifest["page_count"] for manifest in major_manifests.values()),
                    )
                except Exception:
                    logger.exception(
                        "major leaderboard export/render failed; previous major outputs retained"
                    )

                try:
                    class_intensity = build_class_intensity(entries, student_id_layout)
                    major_intensity = build_major_intensity(entries, student_id_layout)
                    class_intensity_manifest = class_intensity_renderer.render(
                        crawler.context,
                        class_intensity,
                        settings.prefix,
                        fetched_at,
                        snapshot_id,
                        title="最卷班级",
                        column_labels=("班级", "班级 AC 总量", "班级卷王"),
                        entity_type="班级",
                    )
                    major_intensity_manifest = major_intensity_renderer.render(
                        crawler.context,
                        major_intensity,
                        settings.prefix,
                        fetched_at,
                        snapshot_id,
                        title="最卷专业",
                        column_labels=("专业", "专业 AC 总量", "卷王班级与同学"),
                        entity_type="专业",
                    )
                    logger.info(
                        "intensity leaderboards updated: %d classes (%d pages), "
                        "%d majors (%d pages)",
                        len(class_intensity),
                        class_intensity_manifest["page_count"],
                        len(major_intensity),
                        major_intensity_manifest["page_count"],
                    )
                except Exception:
                    logger.exception(
                        "intensity leaderboard render failed; previous outputs retained"
                    )

                for scope, label in (("w", "weekly"), ("m", "monthly")):
                    try:
                        scoped_source = crawler.scrape_all(scope=scope)
                        scoped_entries = select_prefix_exclude_and_rerank(
                            scoped_source,
                            settings.prefix,
                            settings.excluded_user_ids,
                        )
                        exporter.export_scoped(
                            scoped_entries,
                            settings.prefix,
                            fetched_at,
                            scope,
                        )
                        logger.info(
                            "%s bot share export updated, %d users",
                            label,
                            len(scoped_entries),
                        )
                    except WebVPNSessionExpired:
                        raise
                    except Exception:
                        logger.exception(
                            "%s rank export failed; previous scoped export retained",
                            label,
                        )
                crawler.persist_session_state()
                logger.info(
                    "scrape complete, %d users; database committed, snapshot_id=%d",
                    len(entries),
                    snapshot_id,
                )
                stop_event.wait(settings.scrape_interval_seconds)
            except WebVPNSessionExpired as exc:
                reason = str(exc)
                if state != "WAITING_FOR_AUTH":
                    if crawler.attempt_automatic_recovery(reason):
                        state = "READY"
                        logger.info(
                            "WebVPN session automatically recovered through normal "
                            "login entry; collection resuming"
                        )
                        continue
                    crawler.enter_waiting_for_auth(reason)
                    logger.warning(
                        "WebVPN requires user authentication; state=WAITING_FOR_AUTH. "
                        "The shared browser session will be validated every %d seconds.",
                        settings.login_check_seconds,
                    )
                state = "WAITING_FOR_AUTH"
                stop_event.wait(settings.login_check_seconds)
            except CrawlError as exc:
                logger.error("scrape cycle failed; previous current_rank retained: %s", exc)
                stop_event.wait(settings.error_retry_seconds)
            except Exception:
                logger.exception("unexpected scrape cycle error; service will retry")
                stop_event.wait(settings.error_retry_seconds)
    finally:
        try:
            crawler.close()
        except Exception:
            logging.getLogger("oj_rank").exception("failed to close browser cleanly")
        logger.info("service stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
