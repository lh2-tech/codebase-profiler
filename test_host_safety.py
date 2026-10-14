#!/usr/bin/env python3
"""Hosts that receive the API token: reject malformed ones, never forward credentials across hosts."""

from __future__ import annotations

import unittest
import urllib.request

import count_merged_prs as cm


class ValidateHostTests(unittest.TestCase):
    def test_accepts_bare_hosts(self):
        self.assertEqual(cm.validate_host("github.com"), "github.com")
        self.assertEqual(cm.validate_host("ghe.example.com:8443"), "ghe.example.com:8443")
        self.assertEqual(cm.validate_host("https://gitlab.example.com/", allow_scheme=True), "https://gitlab.example.com")

    def test_rejects_hosts_that_redirect_the_token(self):
        for bad in ("evil.com/x", "a.com@evil.com", "a.com evil.com", "", "evil.com?x=1", "https://a.com", "a.com\\b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cm.validate_host(bad)

    def test_http_scheme_warns_about_cleartext(self):
        with self.assertLogs(cm.log, "WARNING"):
            cm.validate_host("http://gitlab.local", allow_scheme=True)


class RedirectTests(unittest.TestCase):
    def _redirect(self, target):
        handler = cm._DropAuthOnCrossHostRedirect()
        req = urllib.request.Request(
            "https://api.example.com/x", headers={"Authorization": "Bearer T", "PRIVATE-TOKEN": "T", "Accept": "a"}
        )
        return handler.redirect_request(req, None, 302, "Found", {}, target)

    def test_cross_host_redirect_drops_credentials(self):
        new = self._redirect("https://other.example.org/y")
        names = {k.lower() for k in {**new.headers, **new.unredirected_hdrs}}
        self.assertNotIn("authorization", names)
        self.assertNotIn("private-token", names)
        self.assertIn("accept", names)

    def test_same_host_redirect_keeps_credentials(self):
        new = self._redirect("https://api.example.com/z")
        self.assertIn("authorization", {k.lower() for k in {**new.headers, **new.unredirected_hdrs}})


if __name__ == "__main__":
    unittest.main()
