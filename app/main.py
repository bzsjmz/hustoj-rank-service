from __future__ import annotations

import logging
import signal
import threading
from datetime import datetime, time

from .college import (
    COMPUTER_COLLEGE,
    SOFTWARE_COLLEGE,
    build_class_intensity,
    build_major_intensity,
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


LOOKUP_REFRESH_START = time(0, 20)


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _due_lookup_prefixes(
    prefixes: tuple[str, ...], prefix: str, prefix_fetched_at: dict[str, str], now: datetime
) -> tuple[str, ...]:
    """Return at most one due cohort, so extra full crawls never overlap."""
    local_now = now.astimezone()
    today = local_now.date().isoformat()
    for schedule_index, lookup_prefix in enumerate(
        item for item in prefixes if item != prefix
    ):
        scheduled_at = time(LOOKUP_REFRESH_START.hour + schedule_index, 20)
        if local_now.time() < scheduled_at:
            continue
        previous = prefix_fetched_at.get(lookup_prefix, "")
        if not previous.startswith(today):
            return (lookup_prefix,)
    return ()


def run() -> int:
    settings = Settings.from_env()
    settings.ensure_directories()
    logger = configure_logging(settings.log_dir)
    database = RankDatabase(settings.database_path)
    database.initialize()
    exporter = RankExporter(settings.share_dir)
    student_id_layout = settings.student_id_layout
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
                    cached_lookup_entries, prefix_fetched_at = (
                        exporter.load_lookup_current_entries(settings.lookup_prefixes)
                    )
                    for lookup_prefix in _due_lookup_prefixes(
                        settings.lookup_prefixes,
                        settings.prefix,
                        prefix_fetched_at,
                        datetime.now().astimezone(),
                    ):
                        logger.info(
                            "starting scheduled lookup refresh for prefix=%s", lookup_prefix
                        )
                        cached_lookup_entries[lookup_prefix] = crawler.scrape_all(
                            prefix=lookup_prefix
                        )
                        prefix_fetched_at[lookup_prefix] = fetched_at
                    prefix_fetched_at[settings.prefix] = fetched_at
                    lookup_entries = list(entries)
                    for lookup_prefix in settings.lookup_prefixes:
                        if lookup_prefix != settings.prefix:
                            lookup_entries.extend(
                                cached_lookup_entries.get(lookup_prefix, ())
                            )
                    exporter.export_lookup(
                        lookup_entries,
                        fetched_at,
                        snapshot_id,
                        settings.lookup_prefixes,
                        tuple(missing_historical_entries.values()),
                        prefix_fetched_at,
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
                    logger.info(
                        "college leaderboard data updated: %d computer users, "
                        "%d software users, split_snapshot_id=%d",
                        len(computer_entries),
                        len(software_entries),
                        settings.college_split_snapshot_id,
                    )
                except Exception:
                    logger.exception(
                        "college leaderboard export failed; previous outputs retained"
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

                    exporter.export_entity_registry(
                        "class",
                        class_ranklists,
                        student_id_layout,
                        fetched_at,
                        snapshot_id,
                    )
                    logger.info(
                        "class leaderboard data updated: %d classes, %d users",
                        len(class_ranklists),
                        sum(len(class_entries) for class_entries in class_ranklists.values()),
                    )
                except Exception:
                    logger.exception(
                        "class leaderboard export failed; previous outputs retained"
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

                    exporter.export_entity_registry(
                        "major",
                        major_ranklists,
                        student_id_layout,
                        fetched_at,
                        snapshot_id,
                    )
                    logger.info(
                        "major leaderboard data updated: %d majors, %d users",
                        len(major_ranklists),
                        sum(len(major_entries) for major_entries in major_ranklists.values()),
                    )
                except Exception:
                    logger.exception(
                        "major leaderboard export failed; previous outputs retained"
                    )

                try:
                    class_intensity = build_class_intensity(entries, student_id_layout)
                    major_intensity = build_major_intensity(entries, student_id_layout)
                    exporter.export_intensity(
                        class_intensity,
                        settings.prefix,
                        fetched_at,
                        snapshot_id,
                        "class",
                    )
                    exporter.export_intensity(
                        major_intensity,
                        settings.prefix,
                        fetched_at,
                        snapshot_id,
                        "major",
                    )
                    logger.info(
                        "intensity leaderboard data updated: %d classes, %d majors",
                        len(class_intensity),
                        len(major_intensity),
                    )
                except Exception:
                    logger.exception(
                        "intensity leaderboard export failed; previous outputs retained"
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
                            snapshot_id,
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
