#!/usr/bin/env python3
"""Guards the metadata-only guarantee: no source code in the output archive.

The GitLab MR enrichment used to fetch /merge_requests/:iid/changes, whose
entries each carry a `diff` field holding real source. That landed in
merged_prs.json and shipped inside the deliverable zip. These tests fail if
anything reintroduces it.
"""

from __future__ import annotations

import unittest
from unittest import mock

import extract_org_raw_data as ex


MR_LIST = [{"iid": 7, "title": "Add payment retry", "state": "merged"}]
MR_DETAIL = {
    "iid": 7,
    "title": "Add payment retry",
    "author": {"username": "someone"},
    "changes_count": "12",
}
# What the /changes endpoint would return if anyone called it again.
MR_CHANGES = {
    "changes": [
        {
            "old_path": "billing/retry.py",
            "new_path": "billing/retry.py",
            "diff": "@@ -1,3 +1,7 @@\n+SECRET_KEY = 'live_key_do_not_ship'\n",
        }
    ]
}


class NoDiffsCollectedTests(unittest.TestCase):
    def _run(self):
        def fake_paginate(api, path, token, params=None):
            if path.endswith("/merge_requests"):
                return list(MR_LIST)
            if path.endswith("/notes"):
                return [{"body": "lgtm"}]
            return []

        def fake_get_json(url, headers, **kwargs):
            if url.endswith("/changes"):
                return MR_CHANGES, {}
            return dict(MR_DETAIL), {}

        with mock.patch.object(ex, "paginate_gitlab", side_effect=fake_paginate), \
             mock.patch.object(ex, "http_get_json", side_effect=fake_get_json) as get_json:
            mrs = ex.fetch_gitlab_merged_mrs("token", 123, "gitlab.com")
        return mrs, get_json

    def test_changes_endpoint_is_never_requested(self) -> None:
        _, get_json = self._run()
        called = [c.args[0] for c in get_json.call_args_list]
        self.assertFalse(
            any(url.endswith("/changes") for url in called),
            f"the /changes endpoint was requested again: {called}",
        )

    def test_no_diff_content_survives_into_the_record(self) -> None:
        mrs, _ = self._run()
        blob = repr(mrs)
        self.assertNotIn("@@", blob, "a unified diff hunk reached the MR record")
        self.assertNotIn("SECRET_KEY", blob, "file content reached the MR record")
        self.assertNotIn("changes", mrs[0], "the `changes` key is back on the MR record")

    def test_size_signal_is_still_available(self) -> None:
        mrs, _ = self._run()
        self.assertEqual(
            mrs[0].get("changes_count"),
            "12",
            "changes_count is what replaced the diff payload; it must survive",
        )

    def test_metadata_is_still_collected(self) -> None:
        mrs, _ = self._run()
        self.assertEqual(mrs[0]["title"], "Add payment retry")
        self.assertIn("notes", mrs[0])
        self.assertIn("author_is_bot", mrs[0])


if __name__ == "__main__":
    unittest.main()
