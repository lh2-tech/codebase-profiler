"""Azure DevOps (and its TFS/VSTS predecessors) pull request merges recovered from Git history.

Azure DevOps completes a pull request with a commit whose subject is "Merged PR <id>: <title>", for a merge
commit, a squash and the merge commit of a semi-linear merge alike. TFS/VSTS wrote
"Merge pull request <id> from <source> into <target>" (no "#", unlike GitHub)."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import extract_org_raw_data as ex
import integrity

ENV = {**os.environ, "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@x", "GIT_COMMITTER_NAME": "Dev",
       "GIT_COMMITTER_EMAIL": "dev@x", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, env=ENV, check=True, capture_output=True, text=True).stdout


def commit(repo: Path, name: str, message: str) -> None:
    (repo / name).write_text(name + "\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def merge(repo: Path, branch: str, message: str) -> None:
    git(repo, "checkout", "-q", "-b", branch)
    commit(repo, f"{branch.replace('/', '_')}.txt", f"work on {branch}")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", branch, "-m", message)


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "a.txt", "init")
    # Azure DevOps merge commit (no fast-forward), with the PR description and linked work items in the body
    merge(repo, "feature/login", "Merged PR 42: Add login page\n\nAdds the page.\n\nRelated work items: #1001")
    # Azure DevOps squash merge: one parent, same subject shape
    commit(repo, "b.txt", "Merged PR 43: Fix typo in header")
    # TFS / VSTS wording
    merge(repo, "feature/old", "Merge pull request 7 from feature/old into main")
    # GitHub still recognised, and kept apart from the TFS wording by its "#"
    merge(repo, "gh", "Merge pull request #9 from org/gh\n\nGitHub title")
    return repo


class AzureDevOpsMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def records(self):
        return {r["pr_number"]: r for r in ex.detect_merged_prs_from_git(self.repo) if r["pr_number"]}

    def test_merged_pr_subject_is_a_numbered_pr(self):
        rec = self.records()["42"]
        self.assertEqual(rec["method"], "merge-commit")
        self.assertEqual(rec["title"], "Add login page")
        self.assertEqual(rec["platform"], "azure-devops")

    def test_a_squash_merge_is_told_apart_by_its_single_parent(self):
        rec = self.records()["43"]
        self.assertEqual((rec["method"], rec["title"], rec["platform"]), ("squash", "Fix typo in header", "azure-devops"))

    def test_tfs_wording_keeps_source_branch(self):
        rec = self.records()["7"]
        self.assertEqual((rec["method"], rec["source_branch"], rec["platform"]),
                         ("merge-commit", "feature/old", "azure-devops"))

    def test_github_marker_still_wins_for_hash_numbers(self):
        rec = self.records()["9"]
        self.assertEqual((rec["source_branch"], rec["platform"]), ("org/gh", "github"))

    def test_azure_merges_are_not_double_counted_as_unnumbered(self):
        records = ex.detect_merged_prs_from_git(self.repo)
        self.assertEqual(sorted(r["pr_number"] for r in records), ["42", "43", "7", "9"])

    def test_the_marker_needs_the_whole_shape(self):
        for subject in ("Merged PR: nothing", "Merged PRs 12: x", "Revert \"Merged PR 42: Add login page\""):
            self.assertIsNone(ex.AZURE_DEVOPS_MERGE_RE.match(subject), subject)

    def test_integrity_counts_azure_pr_numbers(self):
        facts = integrity.repo_facts(self.repo, "main")
        self.assertEqual(facts["pr_merge_numbers"], 4)
        self.assertEqual(facts["pr_number_max"], 43)


if __name__ == "__main__":
    unittest.main()
