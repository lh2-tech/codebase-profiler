#!/usr/bin/env python3
"""Tests for code_metrics: PR tiers and code-structure counts."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import code_metrics as cm

SKIP = {".git", "node_modules"}


def is_test(rel: str) -> bool:
    low = rel.lower()
    return "test" in low or ".spec." in low


def gh_pr(files, *, issue=False, review_words=0, inline=False, bot_review=False, comment=False):
    login, typename = ("dependabot[bot]", "Bot") if bot_review else ("alice", "User")
    reviews = [{"bodyText": "word " * review_words, "author": {"login": login, "__typename": typename}}] if review_words else []
    threads = [{"comments": {"nodes": [{"bodyText": "x", "author": {"login": "bob", "__typename": "User"}}]}}] if inline else []
    comments = [{"bodyText": "lgtm", "author": {"login": "bob", "__typename": "User"}}] if comment else []
    return {
        "number": 1,
        "changedFiles": files,
        "closingIssuesReferences": {"nodes": [{"number": 9}] if issue else []},
        "reviews": {"nodes": reviews},
        "reviewThreads": {"nodes": threads},
        "comments": {"nodes": comments},
    }


class PrTierTests(unittest.TestCase):
    def test_tiers(self):
        c = lambda pr: cm.classify_pr("github", pr)
        self.assertEqual(c(gh_pr(1)), "simple")
        self.assertEqual(c(gh_pr(2, comment=True)), "other")  # discussion, no issue
        self.assertEqual(c(gh_pr(5)), "standard")
        self.assertEqual(c(gh_pr(30)), "other")
        self.assertEqual(c(gh_pr(0)), "other")
        # Rich wins over simple when an issue and a substantive review exist.
        self.assertEqual(c(gh_pr(1, issue=True, review_words=25)), "rich")
        self.assertEqual(c(gh_pr(40, issue=True, inline=True)), "rich")
        # Short review or a bot review is not substantive.
        self.assertNotEqual(c(gh_pr(5, issue=True, review_words=5)), "rich")
        self.assertNotEqual(c(gh_pr(5, issue=True, review_words=40, bot_review=True)), "rich")
        # Issue alone is not rich.
        self.assertEqual(c(gh_pr(5, issue=True)), "standard")

    def test_percentages_sum_to_100(self):
        prs = [gh_pr(1), gh_pr(1), gh_pr(5), gh_pr(1, issue=True, review_words=30)]
        fields, tiers = cm.pr_tier_metrics("github", prs)
        self.assertEqual(fields["pr_simple_pct"], 50.0)
        self.assertEqual(fields["pr_standard_pct"], 25.0)
        self.assertEqual(fields["pr_rich_pct"], 25.0)
        self.assertEqual(fields["pr_other_pct"], 0.0)
        self.assertEqual(fields["pr_tiers_classified"], 4)
        self.assertEqual(len(tiers), 4)

    def test_gitlab_and_unsupported(self):
        mr = {"iid": 3, "changes_count": "4", "closes_issues": [{"iid": 1}],
              "notes": [{"system": False, "type": "DiffNote", "author": {"username": "dev"}, "body": "x"}]}
        self.assertEqual(cm.classify_pr("gitlab", mr), "rich")
        self.assertEqual(cm.classify_pr("gitlab", {"iid": 4, "changes_count": "1000+"}), "other")
        self.assertIsNone(cm.classify_pr("bitbucket", {"id": 1}))
        fields, _ = cm.pr_tier_metrics("bitbucket", [{"id": 1}])
        self.assertEqual(fields["pr_simple_pct"], "")

    def test_no_prs(self):
        fields, tiers = cm.pr_tier_metrics("github", [])
        self.assertEqual(fields["pr_rich_pct"], "")
        self.assertEqual(tiers, [])


class StructureTests(unittest.TestCase):
    def build(self, files: dict[str, str]) -> Path:
        root = Path(tempfile.mkdtemp())
        for rel, text in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        return root

    def run_metrics(self, files):
        return cm.structure_metrics(self.build(files), is_test=is_test, skip_dirs=SKIP)

    def untested(self, files):
        out = self.run_metrics(files)
        return out["untested_files"], out["untested_files_pct"]

    def test_ordinary_words_do_not_mark_files_tested(self):
        n, _ = self.untested({
            "app/user.py": "x = 1\n", "app/order.py": "x = 1\n",
            "tests/test_checkout.py": "def test():\n    user = 1\n    order = 2\n",
        })
        self.assertEqual(n, 2)

    def test_short_stems_resolve_through_imports(self):
        n, _ = self.untested({
            "app/db.py": "x = 1\n", "app/api.py": "x = 1\n", "app/cli.py": "x = 1\n",
            "tests/test_checkout.py": "from app import db\nfrom app.api import handler\n",
        })
        self.assertEqual(n, 1)  # cli.py is the only one nobody imports

    def test_generic_stem_name_match_needs_mirrored_directory(self):
        three = {"billing/models.py": "x=1\n", "users/models.py": "x=1\n", "orders/models.py": "x=1\n"}
        n, _ = self.untested({**three, "tests/test_models.py": "def test(): pass\n"})
        self.assertEqual(n, 3)  # flat tests/ mirrors only a root-level models.py
        n, _ = self.untested({**three, "tests/billing/test_models.py": "def test(): pass\n"})
        self.assertEqual(n, 2)
        n, _ = self.untested({"models.py": "x=1\n", "tests/test_models.py": "def test(): pass\n"})
        self.assertEqual(n, 0)

    def test_non_generic_name_match_and_colocated(self):
        n, _ = self.untested({
            "src/billing.py": "x=1\n", "src/shipping.py": "x=1\n",
            "tests/test_billing.py": "def test(): pass\n",
            "web/cart.ts": "export const a = 1\n", "web/cart.test.ts": "test('x', () => {})\n",
        })
        self.assertEqual(n, 1)  # shipping.py

    def test_python_relative_and_package_imports(self):
        n, _ = self.untested({
            "pkg/__init__.py": "", "pkg/core.py": "x=1\n", "pkg/util.py": "x=1\n", "pkg/extra.py": "x=1\n",
            "pkg/tests/test_a.py": "from .. import core\nfrom ..util import f\n",
        })
        self.assertEqual(n, 1)  # extra.py

    def test_js_ts_imports(self):
        n, _ = self.untested({
            "src/lib/parse.ts": "export const p = 1\n", "src/lib/format.ts": "export const f = 1\n",
            "src/widgets/button.tsx": "export const b = 1\n", "src/other/thing.ts": "export const t = 1\n",
            "src/lib/index.ts": "export const i = 1\n",
            "test/a.spec.ts": "import { p } from '../src/lib/parse'\nconst { f } = require('../src/lib/format')\n"
                              "import { b } from '@/widgets/button'\nimport x from 'react'\n"
                              "import * as lib from '../src/lib'\n",
        })
        self.assertEqual(n, 1)  # thing.ts

    def test_java_imports(self):
        n, _ = self.untested({
            "src/main/java/com/acme/Billing.java": "class Billing {}\n",
            "src/main/java/com/acme/Shipping.java": "class Shipping {}\n",
            "src/test/java/com/acme/AllTests.java": "import com.acme.Billing;\nclass AllTests {}\n",
        })
        self.assertEqual(n, 1)  # Shipping

    def test_comment_ratio_is_production_only(self):
        import os, shutil
        tools = str(Path(__file__).resolve().parent.parent / ".tools" / "bin")
        os.environ["PATH"] = tools + os.pathsep + os.environ["PATH"]
        if not shutil.which("scc"):
            self.skipTest("scc not available")
        prod = "# c1\n# c2\n" + "x = 1\n" * 8           # 2 comment, 8 code -> 0.2
        out = self.run_metrics({
            "app/mod.py": prod,
            "tests/test_mod.py": "y = 1\n" * 200,           # code-only; would dilute
            "README.md": "text\n" * 300,                    # Markdown counted as code by scc
            "ci.yml": "a: 1\n" * 100,
        })
        self.assertEqual(out["comment_docstring_ratio"], 0.2)

    def test_counts_and_docs(self):
        if cm._load_parsers() is None:
            self.skipTest("tree-sitter-language-pack not installed")
        out = self.run_metrics({
            "a.py": 'class A:\n    def f(self):\n        """doc"""\n    def g(self):\n        pass\n',
            "b.ts": "/** doc */\nexport function h() {}\nconst k = () => 1;\nclass C { m() {} }\ninterface I {}\n",
            "c.go": "package c\n// Doc.\nfunc F() {}\nfunc G() {}\ntype S struct{}\ntype N int\n",
            "d.rs": "/// doc\n#[inline]\nfn a() {}\nstruct T;\nenum E {}\n",
            "e.java": "class J {\n  /** d */\n  void m() {}\n  J() {}\n}\n",
        })
        # python 2 funcs/1 class (1 doc); ts 3 funcs/2 classes (1 doc);
        # go 2/1 (1 doc); rust 1/2 (1 doc); java 2/1 (1 doc)
        self.assertEqual(out["function_count"], 10)
        self.assertEqual(out["class_count"], 7)
        self.assertEqual(out["docstring_coverage_pct"], 50.0)


if __name__ == "__main__":
    unittest.main()
