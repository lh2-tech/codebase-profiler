#!/usr/bin/env python3
"""Guards the metadata-only guarantee: no source code in the output archive.

The GitLab MR enrichment used to fetch /merge_requests/:iid/changes, whose
entries each carry a `diff` field holding real source. That landed in
merged_prs.json and shipped inside the deliverable zip. These tests fail if
anything reintroduces it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
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


class LinkedIssuesAreIdsOnlyTests(unittest.TestCase):
    def test_closes_issues_keeps_only_identifiers(self) -> None:
        def fake_paginate(api, path, token, params=None):
            if path.endswith("/merge_requests"):
                return list(MR_LIST)
            if path.endswith("/closes_issues"):
                return [{"iid": 5, "web_url": "https://x/5", "title": "T",
                         "description": "SECRET_KEY = 'live_key_do_not_ship'"}]
            return []

        with mock.patch.object(ex, "paginate_gitlab", side_effect=fake_paginate), \
             mock.patch.object(ex, "http_get_json", return_value=(dict(MR_DETAIL), {})):
            mrs = ex.fetch_gitlab_merged_mrs("token", 123, "gitlab.com")
        self.assertEqual(mrs[0]["closes_issues"], [{"iid": 5, "web_url": "https://x/5"}])
        self.assertNotIn("SECRET_KEY", repr(mrs))


_WS_SCC = Path(__file__).resolve().parent.parent / ".tools" / "bin" / "scc"


@unittest.skipUnless(shutil.which("scc") or _WS_SCC.exists(), "scc not available")
class EndToEndArchiveTests(unittest.TestCase):
    """Run the real extractor on a repo seeded with markers; grep every output file."""

    MARKERS = ("ZZ_SECRET_MARKER_TOKEN", "zz_unique_function_name", "ZZ_UniqueClassName",
               "zz_docstring_body_text")

    def test_no_marker_reaches_any_output_file(self) -> None:
        work = Path(tempfile.mkdtemp())
        repo = work / "in" / "proj"
        (repo / "tests").mkdir(parents=True)
        (repo / "app.py").write_text(
            "ZZ_SECRET_MARKER_TOKEN = 'abc'\n\nclass ZZ_UniqueClassName:\n"
            "    def zz_unique_function_name(self):\n"
            "        \"\"\"zz_docstring_body_text\"\"\"\n        return 1\n"
        )
        (repo / "tests" / "test_other.py").write_text("def test_x():\n    assert True\n")
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        for cmd in (["git", "init", "-q"], ["git", "add", "."], ["git", "commit", "-qm", "init"]):
            subprocess.run(cmd, cwd=repo, env=env, check=True)
        out = work / "out"
        here = Path(__file__).resolve().parent
        tools = here.parent / ".tools" / "bin"
        run_env = {**os.environ, "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}"}
        proc = subprocess.run(
            [sys.executable, str(here / "extract_org_raw_data.py"), "--offline",
             "--local-repos-dir", str(work / "in"), "--output-dir", str(out)],
            capture_output=True, text=True, env=run_env, cwd=here,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        files = [p for p in out.rglob("*") if p.is_file() and p.suffix != ".zip"]
        self.assertTrue(files)
        for path in files:
            text = path.read_text(errors="ignore")
            for marker in self.MARKERS:
                self.assertNotIn(marker, text, f"{marker} leaked into {path.name}")
        # The metrics themselves must still be produced.
        summary = next(p for p in files if p.name == "summary.csv").read_text()
        self.assertIn("function_count", summary)
        shutil.rmtree(work, ignore_errors=True)


class MockedPlatformArchiveTests(unittest.TestCase):
    """Drive a platform repo through process_repo with markers planted in every channel for-check reads
    (ref names, HTTP error bodies, git stderr, file content, the token), then grep the CSV, JSON, the unzipped
    archive and the xlsx inside it."""

    TOKEN = "ZZ_TOKEN_VALUE_1234"
    MARKERS = ("ZZ_BRANCH_MARKER", "ZZ_TAG_MARKER", "ZZ_ERRBODY_MARKER", "ZZ_STDERR_MARKER",
               "ZZ_FILE_CONTENT_MARKER", "ZZ_TOKEN_VALUE_1234")

    def _fake_clone(self, url, dest, **kwargs):
        dest.mkdir(parents=True)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        (dest / "app.py").write_text("ZZ_FILE_CONTENT_MARKER = 1\n")
        for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "."], ["git", "commit", "-qm", "init"]):
            subprocess.run(cmd, cwd=dest, env=env, check=True, capture_output=True)

    def test_no_marker_reaches_for_check_or_archive(self) -> None:
        import json
        import logging
        import types
        import zipfile

        import for_check

        work = Path(tempfile.mkdtemp())
        run_dir = work / "run"
        run_dir.mkdir()
        target = ex.RepoTarget(platform="github", org="o", full_name="o/r",
                               meta={"default_branch": "main", "size": 5, "language": "Python"}, local_path=None)
        ls_calls = []

        def fake_ls_remote(cmd, **kw):
            ls_calls.append(cmd)
            if len(ls_calls) == 1:
                return subprocess.CompletedProcess(cmd, 128, "", f"fatal: ZZ_STDERR_MARKER {self.TOKEN}\n")
            out = ("a1\trefs/heads/feature/ZZ_BRANCH_MARKER\nb2\trefs/tags/ZZ_TAG_MARKER\n"
                   "c3\trefs/pull/7/head\n")
            return subprocess.CompletedProcess(cmd, 0, out, "")

        graphql = {"data": {"repository": {
            "defaultBranchRef": {"name": "main", "target": {"history": {"totalCount": 1}}},
            "pullRequests": {"totalCount": 3}, "merged": {"totalCount": 2}, "issues": {"totalCount": 0},
            "labels": {"totalCount": 0}, "milestones": {"totalCount": 0}, "releases": {"totalCount": 0},
            "branches": {"totalCount": 1}, "tags": {"totalCount": 1}, "diskUsage": 5,
            "hasWikiEnabled": False, "hasIssuesEnabled": True}}}

        def fake_get(url, headers, **kw):
            raise RuntimeError(f"HTTP 500 for {url}: ZZ_ERRBODY_MARKER Authorization: Bearer {self.TOKEN}")

        patches = [
            mock.patch.object(ex, "fetch_github_languages", return_value={"Python": 10}),
            mock.patch.object(ex, "fetch_github_contributors", return_value=[]),
            mock.patch.object(ex, "fetch_github_merged_prs", return_value=[]),
            mock.patch.object(ex, "clone_repo", side_effect=self._fake_clone),
            mock.patch.object(ex, "run_scc", return_value={"loc": 1, "by_language": {"Python": 1}}),
            mock.patch.object(for_check, "http_post_json", return_value=graphql),
            mock.patch.object(for_check, "http_get_json", side_effect=fake_get),
            mock.patch.object(for_check, "subprocess", types.SimpleNamespace(run=fake_ls_remote)),
        ]
        for p in patches:
            p.start()
        try:
            row = ex.process_repo(
                target, tokens={"github_token": self.TOKEN}, github_token_name="github_token",
                gitlab_token_name="gitlab_token", github_token_fn=None, llm_config=None, run_dir=run_dir,
                clones_dir=work / "clones", github_host="github.com", gitlab_host="gitlab.com",
                log=logging.getLogger("t"),
            )
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertEqual(row.get("error"), "")
        json.dump([{"full_name": "o/r"}], (run_dir / "repos.json").open("w"))
        (run_dir / "summary.csv").write_text("org,repo,loc\no,r,1\n")
        csv_path = for_check.write_run_csv(run_dir)
        for_check.write_workbook(run_dir / "summary.csv")
        zip_path = ex.zip_run_dir(run_dir)

        # The probe must actually have exercised the paths it claims to guard.
        text = csv_path.read_text()
        self.assertIn("ref_after", text)
        self.assertIn("HTTP 500", text)
        self.assertIn("git ls-remote failed", text)
        self.assertIn("refs/pull/7/head", text)

        unzipped = work / "unzipped"
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(unzipped)
        blobs: list[tuple[str, str]] = []
        for path in [*run_dir.rglob("*"), *unzipped.rglob("*")]:
            if not path.is_file():
                continue
            if path.name.endswith(".gz"):
                continue  # binary; checked line by line above
            if path.suffix == ".xlsx":
                with zipfile.ZipFile(path) as xz:
                    blobs += [(f"{path.name}:{n}", xz.read(n).decode("utf-8", "ignore")) for n in xz.namelist()]
            blobs.append((str(path), path.read_text(errors="ignore")))
        self.assertTrue(any(name.endswith("for-check.csv") for name, _ in blobs))
        self.assertTrue(any(name.startswith("summary.xlsx:") for name, _ in blobs))
        self.assertTrue(any(name.endswith("for_check.json") for name, _ in blobs))
        # The commit-hash list is the one new per-repo file: it must hold hashes and nothing else (no messages,
        # authors or paths), both in the run folder and inside the zip.
        import gzip
        import re
        hash_files = [*run_dir.rglob("commit_hashes.txt.gz"), *unzipped.rglob("commit_hashes.txt.gz")]
        self.assertEqual(len(hash_files), 2)
        for hash_file in hash_files:
            lines = gzip.open(hash_file, "rt").read().splitlines()
            self.assertEqual(len(lines), 1)
            for line in lines:
                self.assertRegex(line, r"^[0-9a-f]{40}$")
        for name, body in blobs:
            for marker in self.MARKERS:
                if name.endswith(("clone", ".py")):
                    continue
                self.assertNotIn(marker, body, f"{marker} leaked into {name}")
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
