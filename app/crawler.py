from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace

from .config import Settings
from .models import RankEntry
from .parser import (
    RanklistParseError,
    WebVPNPageKind,
    classify_webvpn_page,
    has_ranklist_table,
    parse_ranklist,
)
from .session import SessionDiagnostics, sanitize_url


class WebVPNSessionExpired(RuntimeError):
    pass


class CrawlError(RuntimeError):
    pass


@dataclass(frozen=True)
class NavigationProbe:
    requested_url: str
    final_url: str
    status: int | None
    classification: str
    redirect_count: int
    redirect_chain: list[str]

    def as_dict(self) -> dict:
        return asdict(self)


class RankCrawler:
    def __init__(self, settings: Settings, logger: logging.Logger):
        self.settings = settings
        self.logger = logger
        self._playwright = None
        self.context = None
        self.page = None
        self.diagnostics = SessionDiagnostics(settings.data_dir, logger)

    def start(self) -> None:
        os.environ.setdefault(
            "PLAYWRIGHT_BROWSERS_PATH", str(self.settings.playwright_browsers_path)
        )
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        launch_options: dict[str, object] = {
            "user_data_dir": str(self.settings.profile_dir),
            "headless": self.settings.headless,
            "viewport": {"width": 1365, "height": 768},
            "args": ["--disable-dev-shm-usage"],
        }
        if self.settings.browser_executable:
            launch_options["executable_path"] = self.settings.browser_executable

        self.context = self._playwright.chromium.launch_persistent_context(**launch_options)
        self._restore_session_cookies()
        visible_page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page = self._new_worker_page()
        self._show_browser_window(visible_page)
        self.diagnostics.service_started()
        self.logger.info("persistent Chromium started, profile=%s", self.settings.profile_dir)

    def _new_worker_page(self):
        if self.context is None:
            raise RuntimeError("browser has not been started")
        page = self.context.new_page()
        page.set_default_timeout(self.settings.page_timeout_ms)
        self.page = page
        self.logger.info("dedicated crawler tab created")
        return page

    @staticmethod
    def _page_is_closed(page) -> bool:
        is_closed = getattr(page, "is_closed", None)
        return bool(is_closed()) if callable(is_closed) else False

    def _show_browser_window(self, page=None) -> None:
        page = page or self.page
        if self.context is None or page is None or self.settings.headless:
            return
        try:
            page.bring_to_front()
            session = self.context.new_cdp_session(page)
            try:
                window = session.send("Browser.getWindowForTarget")
                session.send(
                    "Browser.setWindowBounds",
                    {
                        "windowId": window["windowId"],
                        "bounds": {
                            "windowState": "normal",
                            "left": 0,
                            "top": 0,
                            "width": 1365,
                            "height": 768,
                        },
                    },
                )
            finally:
                session.detach()
        except Exception as exc:
            self.logger.warning("could not normalize Chromium window: %s", type(exc).__name__)

    def _restore_session_cookies(self) -> None:
        state_path = self.settings.browser_state_path
        if self.context is None or not state_path.exists():
            return
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            stored_cookies = state.get("cookies", [])
            existing_keys = {
                self._cookie_key(cookie) for cookie in self.context.cookies()
            }
            missing_cookies = [
                cookie
                for cookie in stored_cookies
                if self._cookie_key(cookie) not in existing_keys
            ]
            if missing_cookies:
                self.context.add_cookies(missing_cookies)
                self.logger.info(
                    "restored %d missing browser cookies from protected storage state; "
                    "%d persistent-profile cookies kept",
                    len(missing_cookies),
                    len(existing_keys),
                )
            elif stored_cookies:
                self.logger.info(
                    "persistent browser profile already contains stored cookies; "
                    "profile values kept"
                )
        except Exception as exc:
            self.logger.warning(
                "could not restore browser storage state: %s", type(exc).__name__
            )

    @staticmethod
    def _cookie_key(cookie: dict) -> tuple[str, str, str]:
        return (
            str(cookie.get("name", "")),
            str(cookie.get("domain", "")),
            str(cookie.get("path", "/")),
        )

    def persist_session_state(self) -> None:
        if self.context is None:
            return
        state_path = self.settings.browser_state_path
        temporary_path = state_path.with_suffix(state_path.suffix + ".tmp")
        state = self.context.storage_state()
        temporary_path.write_text(
            json.dumps(state, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary_path.chmod(0o600)
        temporary_path.replace(state_path)
        state_path.chmod(0o600)
        self.diagnostics.record_cookies(state.get("cookies", []), "storage_state_saved")
        self.logger.info("browser storage state saved securely")

    def close(self) -> None:
        if self.context is not None:
            try:
                if self.current_page_kind() == WebVPNPageKind.RANKLIST:
                    self.persist_session_state()
                else:
                    self.logger.info(
                        "browser state not saved during shutdown because the current "
                        "page is not a verified ranklist"
                    )
            except Exception as exc:
                self.logger.warning(
                    "could not save browser state during shutdown: %s",
                    type(exc).__name__,
                )
            self.context.close()
        if self._playwright is not None:
            self._playwright.stop()

    def _require_page(self):
        if self.page is None or self._page_is_closed(self.page):
            self.logger.warning("dedicated crawler tab was closed; creating a replacement")
            return self._new_worker_page()
        return self.page

    def current_page_kind(self) -> WebVPNPageKind:
        page = self._require_page()
        try:
            return classify_webvpn_page(page.url, page.content())
        except Exception as exc:
            self.logger.warning(
                "could not inspect current browser page: %s", type(exc).__name__
            )
            return WebVPNPageKind.LOGIN

    def current_page_is_login(self) -> bool:
        return self.current_page_kind() == WebVPNPageKind.LOGIN

    def _cookies(self) -> list[dict]:
        if self.context is None:
            return []
        try:
            return self.context.cookies()
        except Exception as exc:
            self.logger.warning("could not inspect browser cookies: %s", type(exc).__name__)
            return []

    @staticmethod
    def _redirect_chain(response) -> list[str]:
        if response is None:
            return []
        chain = []
        request = response.request
        while request.redirected_from is not None:
            request = request.redirected_from
            chain.append(sanitize_url(request.url))
        chain.reverse()
        return chain

    def _navigate(
        self,
        url: str,
        purpose: str,
        *,
        force_diagnostic: bool = False,
    ) -> tuple[str, NavigationProbe]:
        page = self._require_page()
        try:
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.settings.page_timeout_ms,
            )
            html = page.content()
        except Exception as exc:
            safe_url = sanitize_url(url)
            self.diagnostics.record_event(
                "navigation_error",
                purpose=purpose,
                requested_url=safe_url,
                error_type=type(exc).__name__,
            )
            raise CrawlError(
                f"page load failed for {safe_url}: {type(exc).__name__}"
            ) from exc

        redirects = self._redirect_chain(response)
        classification = classify_webvpn_page(page.url, html)
        probe = NavigationProbe(
            requested_url=sanitize_url(url),
            final_url=sanitize_url(page.url),
            status=response.status if response is not None else None,
            classification=classification.value,
            redirect_count=len(redirects),
            redirect_chain=redirects,
        )
        self.diagnostics.record_navigation(
            purpose,
            probe.as_dict(),
            force=force_diagnostic,
        )
        return html, probe

    def _load(
        self,
        url: str,
        purpose: str = "ranklist",
        *,
        force_diagnostic: bool = False,
    ) -> str:
        html, probe = self._navigate(
            url,
            purpose,
            force_diagnostic=force_diagnostic,
        )
        if probe.status in {401, 403}:
            raise WebVPNSessionExpired(
                f"WebVPN returned HTTP {probe.status} for {purpose}"
            )
        if probe.classification == WebVPNPageKind.LOGIN.value:
            raise WebVPNSessionExpired(
                f"WebVPN redirected to login during {purpose}"
            )
        if probe.status is None or probe.status >= 400:
            raise CrawlError(
                f"unexpected HTTP status {probe.status} during {purpose}; "
                f"final_url={probe.final_url}"
            )
        if (
            probe.classification == WebVPNPageKind.UNKNOWN.value
            and probe.redirect_count > 0
        ):
            raise WebVPNSessionExpired(
                f"WebVPN ranklist request redirected to an unknown page during {purpose}"
            )
        if probe.classification != WebVPNPageKind.RANKLIST.value:
            raise CrawlError(
                f"refusing non-ranklist page during {purpose}; "
                f"classification={probe.classification} final_url={probe.final_url}"
            )
        return html

    def mark_ready(self, reason: str) -> None:
        self.diagnostics.transition("READY", reason, cookies=self._cookies())

    def enter_waiting_for_auth(self, reason: str) -> None:
        self._show_browser_window()
        self.diagnostics.transition(
            "WAITING_FOR_AUTH",
            reason,
            cookies=self._cookies(),
        )

    def attempt_automatic_recovery(self, reason: str) -> bool:
        self.diagnostics.transition(
            "RECOVERING",
            reason,
            cookies=self._cookies(),
        )
        self._show_browser_window()
        self.logger.warning(
            "WebVPN session invalid; trying normal login entry once: %s",
            sanitize_url(self.settings.login_entry_url),
        )
        try:
            self._navigate(
                self.settings.login_entry_url,
                "automatic_recovery_entry",
                force_diagnostic=True,
            )
            page = self._require_page()
            page.wait_for_timeout(self.settings.auth_recovery_wait_ms)
            settled_html = page.content()
            settled_kind = classify_webvpn_page(page.url, settled_html)
            self.diagnostics.record_event(
                "automatic_recovery_settled",
                final_url=sanitize_url(page.url),
                classification=settled_kind.value,
            )
            if settled_kind == WebVPNPageKind.LOGIN:
                return False

            self._load(
                self.settings.ranklist_url(),
                "automatic_recovery_validation",
                force_diagnostic=True,
            )
            self.persist_session_state()
            self.mark_ready("automatic_recovery_succeeded")
            return True
        except (WebVPNSessionExpired, CrawlError) as exc:
            self.logger.warning(
                "automatic WebVPN recovery did not authenticate: %s",
                exc,
            )
            return False

    def validate_after_user_authentication(self) -> bool:
        self.logger.info("authentication page changed; validating WebVPN session")
        try:
            self._load(
                self.settings.ranklist_url(),
                "manual_auth_validation",
                force_diagnostic=True,
            )
            self.persist_session_state()
            self.mark_ready("manual_authentication_succeeded")
            return True
        except (WebVPNSessionExpired, CrawlError) as exc:
            self.logger.warning("manual authentication not ready: %s", exc)
            return False

    @staticmethod
    def _same_user_payload(left: RankEntry, right: RankEntry) -> bool:
        return (
            left.user_id == right.user_id
            and left.nickname == right.nickname
            and left.accepted == right.accepted
            and left.submitted == right.submitted
            and left.ratio == right.ratio
            and left.level == right.level
        )

    def _finalize_entries(self, entries: list[RankEntry]) -> list[RankEntry]:
        normalized = [replace(entry, rank=index) for index, entry in enumerate(entries, start=1)]
        changed = sum(before.rank != after.rank for before, after in zip(entries, normalized))
        if changed:
            self.logger.warning(
                "normalized %d source ranks after removing duplicate source rows",
                changed,
            )
        return normalized

    def scrape_all(
        self, scope: str | None = None, prefix: str | None = None
    ) -> Sequence[RankEntry]:
        target_prefix = prefix or self.settings.prefix
        self.logger.info(
            "ranklist scrape started, prefix=%s scope=%s",
            target_prefix,
            scope or "all-time",
        )
        collected: list[RankEntry] = []
        seen: dict[str, tuple[int, RankEntry]] = {}

        for page_index in range(self.settings.max_pages):
            start = page_index * self.settings.page_size
            html = self._load(
                self.settings.ranklist_url(start, scope, target_prefix),
                f"ranklist_{target_prefix}_{scope or 'all'}_page_{page_index}",
                force_diagnostic=page_index == 0,
            )
            try:
                entries = parse_ranklist(html)
            except RanklistParseError as exc:
                raise CrawlError(f"ranklist page {page_index} HTML parse failed: {exc}") from exc

            if not entries:
                if page_index == 0:
                    structure = "table present" if has_ranklist_table(html) else "table missing"
                    raise CrawlError(f"ranklist page 0 has no users ({structure})")
                self.logger.info("ranklist page %d is empty; pagination complete", page_index)
                return self._finalize_entries(collected)

            self.logger.info(
                "ranklist page %d loaded, %d rows, ranks %d..%d, final_url=%s",
                page_index,
                len(entries),
                entries[0].rank,
                entries[-1].rank,
                self._require_page().url,
            )

            new_entries: list[RankEntry] = []
            for entry in entries:
                if scope is None and not entry.user_id.startswith(target_prefix):
                    raise CrawlError(
                        f"unexpected user_id {entry.user_id!r} for prefix {target_prefix!r}"
                    )
                if entry.user_id in seen:
                    previous_page, previous_entry = seen[entry.user_id]
                    if self._same_user_payload(entry, previous_entry):
                        self.logger.warning(
                            "ranklist source repeated user %s on pages %d and %d "
                            "with source ranks %d and %d; payload is identical, deduplicated safely",
                            entry.user_id,
                            previous_page,
                            page_index,
                            previous_entry.rank,
                            entry.rank,
                        )
                        continue
                    raise CrawlError(
                        f"conflicting duplicate user_id across pages: {entry.user_id} "
                        f"(page {previous_page}: {previous_entry}; "
                        f"page {page_index}: {entry}; "
                        f"final_url={sanitize_url(self._require_page().url)})"
                    )
                seen[entry.user_id] = (page_index, entry)
                new_entries.append(entry)
            collected.extend(new_entries)
            self.logger.info(
                "ranklist page %d OK, %d new users, %d unique total",
                page_index,
                len(new_entries),
                len(collected),
            )

            if len(entries) < self.settings.page_size:
                return self._finalize_entries(collected)

        raise CrawlError(
            f"reached MAX_PAGES={self.settings.max_pages} while the last page was full; "
            "refusing to save a possibly truncated ranklist"
        )
