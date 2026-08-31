from __future__ import annotations

import unittest

from app.parser import (
    RanklistParseError,
    WebVPNPageKind,
    classify_webvpn_page,
    is_webvpn_login_page,
    parse_ranklist,
)


RANK_HTML = """
<html><body><table class="table"><tbody>
<tr class="evenrow">
  <td>1</td><td><a>202613430208</a></td><td>示例昵称</td>
  <td>34</td><td>36</td><td>94.444%</td><td>初学乍练</td>
</tr>
<tr class="oddrow">
  <td>2</td><td>202600000002</td><td></td>
  <td>1</td><td>4</td><td>25%</td><td>初学乍练</td>
</tr>
</tbody></table></body></html>
"""


class ParserTests(unittest.TestCase):
    def test_parses_rank_rows_and_empty_nickname(self) -> None:
        entries = parse_ranklist(RANK_HTML)
        self.assertEqual(2, len(entries))
        self.assertEqual("202613430208", entries[0].user_id)
        self.assertEqual("示例昵称", entries[0].nickname)
        self.assertEqual(34, entries[0].accepted)
        self.assertAlmostEqual(94.444, entries[0].ratio)
        self.assertEqual("", entries[1].nickname)

    def test_login_url_is_detected(self) -> None:
        self.assertTrue(
            is_webvpn_login_page(
                "https://webvpn.example.edu/login", "<html>anything</html>"
            )
        )

    def test_login_html_is_detected_without_login_url(self) -> None:
        html = "<form action='/auth/login'><h1>WebVPN</h1><p>统一身份认证</p></form>"
        self.assertTrue(is_webvpn_login_page("https://webvpn.example.edu/", html))

    def test_rank_page_is_not_mistaken_for_login(self) -> None:
        self.assertFalse(is_webvpn_login_page("https://webvpn.example.edu/http/x", RANK_HTML))

    def test_login_controls_win_over_rank_style_rows(self) -> None:
        html = (
            "<form action='/auth/login'><h1>WebVPN</h1><p>统一身份认证</p>"
            + RANK_HTML
            + "</form>"
        )
        self.assertEqual(
            WebVPNPageKind.LOGIN,
            classify_webvpn_page("https://webvpn.example.edu/", html),
        )

    def test_unknown_page_is_neither_login_nor_ranklist(self) -> None:
        self.assertEqual(
            WebVPNPageKind.UNKNOWN,
            classify_webvpn_page(
                "https://webvpn.example.edu/",
                "<html><body>unexpected portal</body></html>",
            ),
        )

    def test_malformed_row_fails_the_cycle(self) -> None:
        html = RANK_HTML.replace("<td>34</td>", "<td>not-a-number</td>", 1)
        with self.assertRaises(RanklistParseError):
            parse_ranklist(html)


if __name__ == "__main__":
    unittest.main()
