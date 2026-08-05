#!/usr/bin/env python3
"""Unit tests for GitHub rate-limit backoff helpers."""

from __future__ import annotations

import io
import json
import unittest
from unittest import mock

import count_merged_prs as cm


class GraphqlRateLimitDetectionTests(unittest.TestCase):
    def test_detects_rate_limit_type(self) -> None:
        errors = [
            {
                "type": "RATE_LIMIT",
                "code": "graphql_rate_limit",
                "message": "API rate limit already exceeded for user ID 7627077.",
            }
        ]
        self.assertTrue(cm.graphql_errors_are_rate_limit(errors))

    def test_detects_rate_limited_type(self) -> None:
        errors = [{"type": "RATE_LIMITED", "message": "API rate limit exceeded for user ID 7627077."}]
        self.assertTrue(cm.graphql_errors_are_rate_limit(errors))

    def test_ignores_non_rate_limit_errors(self) -> None:
        errors = [{"type": "NOT_FOUND", "message": "Could not resolve to a Repository"}]
        self.assertFalse(cm.graphql_errors_are_rate_limit(errors))


class HttpPostJsonRateLimitTests(unittest.TestCase):
    def test_retries_graphql_rate_limit_with_thirty_minute_wait(self) -> None:
        rate_limit_body = json.dumps(
            {
                "errors": [
                    {
                        "type": "RATE_LIMIT",
                        "code": "graphql_rate_limit",
                        "message": "API rate limit already exceeded",
                    }
                ]
            }
        ).encode("utf-8")
        success_body = json.dumps(
            {"data": {"repository": {"pullRequests": {"totalCount": 1}}}}
        ).encode("utf-8")
        responses = [rate_limit_body, success_body]

        def fake_urlopen(_req, timeout=120):
            payload = responses.pop(0)
            response = mock.Mock()
            response.read.return_value = payload
            response.__enter__ = mock.Mock(return_value=response)
            response.__exit__ = mock.Mock(return_value=False)
            return response

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with mock.patch.object(cm, "_schedule_github_rate_limit_cooldown") as schedule:
                data = cm.http_post_json(
                    "https://api.github.com/graphql",
                    {"Authorization": "Bearer test"},
                    {"query": "{}"},
                    retries=2,
                )

        schedule.assert_called_once()
        self.assertEqual(data["data"]["repository"]["pullRequests"]["totalCount"], 1)


class HttpGetJsonRateLimitTests(unittest.TestCase):
    def test_retries_http_429_with_thirty_minute_wait(self) -> None:
        success_body = json.dumps([{"login": "octocat"}]).encode("utf-8")

        def fake_urlopen(_req, timeout=120):
            if not hasattr(fake_urlopen, "called"):
                fake_urlopen.called = True
                raise cm.urllib.error.HTTPError(
                    "https://api.github.com/test",
                    429,
                    "Too Many Requests",
                    {},
                    io.BytesIO(b"rate limit"),
                )
            response = mock.Mock()
            response.read.return_value = success_body
            response.headers = {"Link": ""}
            response.__enter__ = mock.Mock(return_value=response)
            response.__exit__ = mock.Mock(return_value=False)
            return response

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with mock.patch.object(cm, "_schedule_github_rate_limit_cooldown") as schedule:
                data, _headers = cm.http_get_json(
                    "https://api.github.com/test",
                    {"Authorization": "Bearer test"},
                    retries=2,
                )

        schedule.assert_called_once()
        self.assertEqual(data[0]["login"], "octocat")


if __name__ == "__main__":
    unittest.main()
