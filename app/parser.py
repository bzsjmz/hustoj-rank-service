from __future__ import annotations

import re
from enum import Enum
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .models import RankEntry


class RanklistParseError(ValueError):
    pass


class WebVPNPageKind(str, Enum):
    RANKLIST = "ranklist"
    LOGIN = "login"
    UNKNOWN = "unknown"


def _parse_int(value: str, field: str) -> int:
    normalized = value.replace(",", "").strip()
    if not re.fullmatch(r"-?\d+", normalized):
        raise RanklistParseError(f"invalid {field}: {value!r}")
    return int(normalized)


def _parse_ratio(value: str) -> float:
    normalized = value.strip().removesuffix("%").strip()
    try:
        return float(normalized)
    except ValueError as exc:
        raise RanklistParseError(f"invalid ratio: {value!r}") from exc


def has_ranklist_table(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    return soup.select_one("table.table") is not None


def classify_webvpn_page(url: str, html: str) -> WebVPNPageKind:
    path = urlparse(url).path.lower().rstrip("/")
    login_path_markers = (
        "/login",
        "/authserver",
        "/cas/login",
        "/oauth/authorize",
        "/sso/login",
    )
    if any(marker in path for marker in login_path_markers):
        return WebVPNPageKind.LOGIN

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True).lower()
    source = html.lower()
    markers = (
        "webvpn",
        "统一身份认证",
        "扫码登录",
        "验证码",
        "cas login",
        "账号登录",
    )
    marker_count = sum(marker in text or marker in source for marker in markers)
    has_login_control = (
        soup.select_one('input[type="password"]') is not None
        or soup.select_one('form[action*="login"]') is not None
        or soup.select_one('form[action*="auth"]') is not None
        or "qrcode" in source
        or "qr-code" in source
    )
    if marker_count >= 1 and has_login_control:
        return WebVPNPageKind.LOGIN
    if has_login_control and not has_ranklist_table(html):
        return WebVPNPageKind.LOGIN
    if has_ranklist_table(html):
        return WebVPNPageKind.RANKLIST
    if marker_count >= 2:
        return WebVPNPageKind.LOGIN
    return WebVPNPageKind.UNKNOWN


def is_webvpn_login_page(url: str, html: str) -> bool:
    return classify_webvpn_page(url, html) == WebVPNPageKind.LOGIN


def parse_ranklist(html: str) -> list[RankEntry]:
    soup = BeautifulSoup(html, "html.parser")
    entries: list[RankEntry] = []
    errors: list[str] = []

    for row_index, row in enumerate(soup.select("table.table tbody tr"), start=1):
        cells = row.select("td")
        if not cells:
            continue
        if len(cells) < 7:
            errors.append(f"row {row_index}: expected 7 columns, got {len(cells)}")
            continue

        values = [cell.get_text(" ", strip=True) for cell in cells[:7]]
        try:
            if not values[1]:
                raise RanklistParseError("empty user_id")
            entries.append(
                RankEntry(
                    rank=_parse_int(values[0], "rank"),
                    user_id=values[1],
                    nickname=values[2],
                    accepted=_parse_int(values[3], "accepted"),
                    submitted=_parse_int(values[4], "submitted"),
                    ratio=_parse_ratio(values[5]),
                    level=values[6],
                )
            )
        except RanklistParseError as exc:
            errors.append(f"row {row_index}: {exc}")

    if errors:
        preview = "; ".join(errors[:5])
        if len(errors) > 5:
            preview += f"; and {len(errors) - 5} more"
        raise RanklistParseError(preview)
    return entries
