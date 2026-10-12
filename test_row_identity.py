#!/usr/bin/env python3
"""Two GitLab projects with the same name in different subgroups must stay two rows."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import extract_org_raw_data as ex


def row(full_name: str, loc: int) -> dict:
    org, repo = full_name.split("/")[0], full_name.split("/")[-1]
    r = ex.empty_summary_row(org, repo, full_name)
    r["loc"] = loc
    return r


class RowIdentityTests(unittest.TestCase):
    def test_same_name_in_two_subgroups_survives_every_rewrite(self):
        run_dir = Path(tempfile.mkdtemp())
        job = ex.JobCheckpoint.load_or_create(run_dir, 2)
        job.record("redcliffelabs/airflow", row("redcliffelabs/airflow", 102212))
        job.record("redcliffelabs/backend-core/airflow", row("redcliffelabs/backend-core/airflow", 2285))
        self.assertEqual(len(job.rows()), 2)
        job.replace_rows(job.rows())          # what the end-of-run company-period pass does
        self.assertEqual(len(job.rows()), 2)
        reloaded = ex.JobCheckpoint.load_or_create(run_dir, 2)   # resume path reads summary.csv
        self.assertEqual(
            sorted(r["full_name"] for r in reloaded.rows()),
            ["redcliffelabs/airflow", "redcliffelabs/backend-core/airflow"],
        )
        self.assertEqual(sorted(int(r["loc"]) for r in reloaded.rows()), [2285, 102212])

    def test_old_summary_without_full_name_still_loads(self):
        self.assertEqual(ex.repo_row_key("g", "r"), "g/r")
        self.assertEqual(ex.repo_row_key("g", "r", "g/sub/r"), "g/sub/r")


if __name__ == "__main__":
    unittest.main()
