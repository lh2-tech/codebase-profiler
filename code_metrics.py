"""Aggregate-only PR-shape and code-structure metrics.

Everything returned here is a count or a percentage. No source text, file
contents, comment bodies or file paths leave this module, so the metadata-only
guarantee of the output archive is unchanged.

PR tiers (applied in this order, so the percentages sum to 100):

  rich      linked issue AND at least one substantive human review comment
  simple    1-2 files changed, no human discussion
  standard  3-10 files changed
  other     everything left (0 files, >10 files, or 1-2 files with discussion
            but no linked issue)

Substantive = human author, and either an inline review comment or a review /
note body of at least SUBSTANTIVE_MIN_WORDS words. "Touches tests" is not
required for ``standard``: file paths are only available for GitHub, and
requiring it would push most PRs into ``other``.

Code metrics need tree-sitter. When it is not installed the structure fields
come back as "" (unmeasured), never 0, so a missing parser cannot be mistaken
for a property of the repository.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable

SUBSTANTIVE_MIN_WORDS = 20
SIMPLE_MAX_FILES = 2
STANDARD_MAX_FILES = 10

MAX_FILES = 20_000
MAX_BYTES = 1_000_000

PR_FIELDS = [
    "pr_simple_pct",
    "pr_standard_pct",
    "pr_rich_pct",
    "pr_other_pct",
    "pr_tiers_classified",
]
STRUCTURE_FIELDS = [
    "function_count",
    "class_count",
    "docstring_coverage_pct",
    "comment_docstring_ratio",
    "untested_files",
    "untested_files_pct",
]

# --------------------------------------------------------------------------
# PR tiers
# --------------------------------------------------------------------------


def _words(text: Any) -> int:
    return len(str(text or "").split())


def _github_signals(pr: dict[str, Any]) -> tuple[int | None, bool, bool, bool]:
    """(files, has_issue, has_substantive_review, has_any_human_discussion)."""

    def human(node: dict[str, Any]) -> bool:
        author = node.get("author") or {}
        return not is_bot_login(author.get("login"), author.get("__typename"))

    files = pr.get("changedFiles")
    issue = bool((pr.get("closingIssuesReferences") or {}).get("nodes"))
    substantive = False
    discussion = False
    for review in (pr.get("reviews") or {}).get("nodes") or []:
        if not human(review):
            continue
        body = review.get("bodyText")
        if (body or "").strip():
            discussion = True
            substantive = substantive or _words(body) >= SUBSTANTIVE_MIN_WORDS
    for thread in (pr.get("reviewThreads") or {}).get("nodes") or []:
        for comment in (thread.get("comments") or {}).get("nodes") or []:
            if human(comment):
                discussion = substantive = True
    for comment in (pr.get("comments") or {}).get("nodes") or []:
        if human(comment) and (comment.get("bodyText") or "").strip():
            discussion = True
    return (int(files) if files is not None else None), issue, substantive, discussion


def _gitlab_signals(mr: dict[str, Any]) -> tuple[int | None, bool, bool, bool]:
    raw = str(mr.get("changes_count") or "").rstrip("+").strip()
    files = int(raw) if raw.isdigit() else None
    issue = bool(mr.get("closes_issues"))
    substantive = discussion = False
    for note in mr.get("notes") or []:
        if note.get("system"):
            continue
        author = (note.get("author") or {}).get("username") or ""
        if is_bot_login(author):
            continue
        discussion = True
        if note.get("type") == "DiffNote" or _words(note.get("body")) >= SUBSTANTIVE_MIN_WORDS:
            substantive = True
    return files, issue, substantive, discussion


def is_bot_login(login: str | None, typename: str | None = None) -> bool:
    """Same rule as extract_org_raw_data.is_bot_login, kept local to avoid a cycle."""
    if typename == "Bot":
        return True
    low = (login or "").lower()
    return low.endswith("[bot]") or "bot" in low


def classify_pr(platform: str, pr: dict[str, Any]) -> str | None:
    """Return 'rich' | 'simple' | 'standard' | 'other', or None if unclassifiable."""
    if platform == "github":
        files, issue, substantive, discussion = _github_signals(pr)
    elif platform == "gitlab":
        files, issue, substantive, discussion = _gitlab_signals(pr)
    else:
        return None  # Bitbucket's PR list carries no file or review data.
    if files is None:
        return None
    if issue and substantive:
        return "rich"
    if 1 <= files <= SIMPLE_MAX_FILES and not discussion:
        return "simple"
    if SIMPLE_MAX_FILES < files <= STANDARD_MAX_FILES:
        return "standard"
    return "other"


def pr_tier_metrics(
    platform: str, prs: Iterable[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return (summary fields, per-PR tiers). Fields are "" when nothing classified."""
    tiers: list[dict[str, Any]] = []
    counts = {"simple": 0, "standard": 0, "rich": 0, "other": 0}
    for pr in prs:
        number = pr.get("number") or pr.get("iid") or pr.get("id")
        tier = classify_pr(platform, pr)
        tiers.append({"number": number, "tier": tier})
        if tier:
            counts[tier] += 1
    total = sum(counts.values())
    fields: dict[str, Any] = {k: "" for k in PR_FIELDS}
    if total:
        for tier, n in counts.items():
            fields[f"pr_{tier}_pct"] = round(100 * n / total, 1)
        fields["pr_tiers_classified"] = total
    return fields, tiers


# --------------------------------------------------------------------------
# Code structure (tree-sitter)
# --------------------------------------------------------------------------

EXT_LANG = {
    ".py": "python", ".pyw": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
    ".java": "java", ".go": "go", ".rs": "rust", ".cs": "c_sharp",
    ".kt": "kotlin", ".kts": "kotlin", ".rb": "ruby", ".php": "php",
    ".swift": "swift", ".scala": "scala",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
}

_FUNC = {
    "python": {"function_definition"},
    "javascript": {"function_declaration", "generator_function_declaration", "method_definition"},
    "typescript": {"function_declaration", "generator_function_declaration", "method_definition"},
    "tsx": {"function_declaration", "generator_function_declaration", "method_definition"},
    "java": {"method_declaration", "constructor_declaration"},
    "go": {"function_declaration", "method_declaration"},
    "rust": {"function_item"},
    "c_sharp": {"method_declaration", "constructor_declaration", "local_function_statement"},
    "kotlin": {"function_declaration"},
    "ruby": {"method", "singleton_method"},
    "php": {"function_definition", "method_declaration"},
    "swift": {"function_declaration", "init_declaration"},
    "scala": {"function_definition"},
    "c": {"function_definition"},
    "cpp": {"function_definition"},
}
_JS_LIKE = {"javascript", "typescript", "tsx"}
_ARROW_VALUES = {"arrow_function", "function_expression", "function"}

_CLASS = {
    "python": {"class_definition"},
    "javascript": {"class_declaration"},
    "typescript": {"class_declaration", "interface_declaration", "enum_declaration"},
    "tsx": {"class_declaration", "interface_declaration", "enum_declaration"},
    "java": {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration"},
    "go": set(),  # type_spec handled specially: only struct / interface types
    "rust": {"struct_item", "enum_item", "trait_item"},
    "c_sharp": {"class_declaration", "interface_declaration", "struct_declaration",
                "enum_declaration", "record_declaration"},
    "kotlin": {"class_declaration", "object_declaration"},
    "ruby": {"class", "module"},
    "php": {"class_declaration", "interface_declaration", "trait_declaration"},
    "swift": {"class_declaration", "protocol_declaration"},
    "scala": {"class_definition", "trait_definition", "object_definition"},
    "c": set(),
    "cpp": {"class_specifier"},
}

# Wrappers between a definition and the comment written above it.
_DOC_WRAPPERS = {
    "export_statement", "decorated_definition", "variable_declarator",
    "lexical_declaration", "variable_declaration", "declaration_list_item",
}
_DOC_SKIP_SIBLINGS = {"attribute_item", "decorator", "annotation", "attribute_list"}

_SKIP_FILE = re.compile(
    r"(\.min\.(js|css)$|\.d\.ts$|_pb2(_grpc)?\.py$|\.pb\.go$|\.generated\.|\.g\.dart$"
    r"|(^|/)(package-lock|yarn\.lock)$)"
)
_TEST_STEM_AFFIX = re.compile(r"^(test_|Test(?=[A-Z]))|(_tests?|_spec|Tests?|Specs?)$")
_TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "unit", "integration", "e2e",
              "functional", "testing"}
# Directory names that carry no package meaning, dropped before comparing a test's
# directory with the file it might cover (src/test/java/x vs src/main/java/x).
_LAYOUT_DIRS = _TEST_DIRS | {"src", "main", "java", "kotlin", "scala", "python", "lib", "app"}
_GENERIC_STEMS = {
    "index", "main", "utils", "util", "init", "__init__", "app", "mod", "lib", "types",
    "common", "config", "base", "helpers", "helper", "constants", "models", "model",
}


def _load_parsers() -> Callable[[str], Any] | None:
    try:
        from tree_sitter_language_pack import get_parser  # type: ignore
    except Exception:
        return None
    return get_parser


def _walk(root: Any) -> Iterable[Any]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def _is_documented(node: Any, lang: str) -> bool:
    if lang == "python":
        body = node.child_by_field_name("body")
        first = body.named_children[0] if body is not None and body.named_children else None
        # Older grammars wrap the string in expression_statement; newer ones do not.
        if first is not None and first.type == "expression_statement" and first.named_children:
            first = first.named_children[0]
        return first is not None and first.type in {"string", "concatenated_string"}
    outer = node
    while outer.parent is not None and outer.parent.type in _DOC_WRAPPERS:
        outer = outer.parent
    prev = outer.prev_sibling
    while prev is not None and prev.type in _DOC_SKIP_SIBLINGS:
        prev = prev.prev_sibling
    return (
        prev is not None
        and "comment" in prev.type
        and prev.end_point[0] >= outer.start_point[0] - 1
    )


def _count_file(tree: Any, lang: str) -> tuple[int, int, int]:
    funcs = classes = documented = 0
    func_types = _FUNC.get(lang, set())
    class_types = _CLASS.get(lang, set())
    for node in _walk(tree.root_node):
        kind = node.type
        counted = False
        if kind in func_types:
            counted = True
        elif lang in _JS_LIKE and kind == "variable_declarator":
            value = node.child_by_field_name("value")
            counted = value is not None and value.type in _ARROW_VALUES
        if counted:
            funcs += 1
            documented += _is_documented(node, lang)
        if kind in class_types:
            classes += 1
        elif lang == "go" and kind == "type_spec":
            inner = node.child_by_field_name("type")
            classes += inner is not None and inner.type in {"struct_type", "interface_type"}
        elif lang == "c" and kind in {"struct_specifier", "union_specifier"}:
            classes += node.child_by_field_name("body") is not None
        elif lang == "cpp" and kind == "struct_specifier":
            classes += node.child_by_field_name("body") is not None
    return funcs, classes, documented


def _norm(stem: str) -> str:
    return stem.replace("_", "").replace("-", "").lower()


def _test_stem(name: str) -> str:
    """foo.test.ts, test_foo.py, foo_test.go, FooTests.cs -> 'foo'."""
    stem = name.rsplit(".", 1)[0]
    if stem.lower().endswith((".test", ".spec")):
        stem = stem.rsplit(".", 1)[0]
    return _norm(_TEST_STEM_AFFIX.sub("", stem, count=2))


def _mirrors(test_dir: tuple[str, ...], prod_dir: tuple[str, ...]) -> bool:
    """True when a test's directory lines up with the directory of the file it names.

    Colocated tests (foo.test.ts beside foo.ts, foo_test.go beside foo.go) and mirrored
    trees (tests/billing/test_models.py for billing/models.py, src/test/java/x for
    src/main/java/x) both pass. A flat tests/ directory only mirrors the repo root.
    """
    if test_dir == prod_dir:
        return True
    t = tuple(d for d in test_dir if d.lower() not in _LAYOUT_DIRS)
    q = tuple(d for d in prod_dir if d.lower() not in _LAYOUT_DIRS)
    if not t:
        return not q
    return q[-len(t):] == t


# --------------------------------------------------------------------------
# Import resolution: map each test file's imports to the production files it uses
# --------------------------------------------------------------------------

_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
_JS_SPEC = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*|\bjest\.(?:mock|requireActual)\s*\(\s*"""
    r"""|\bvi\.mock\s*\(\s*|\bimport\s+)['"]([^'"\n]+)['"]"""
)
_JVM_IMPORT = re.compile(r"^\s*import\s+(static\s+)?([\w.]+?)(\.\*)?\s*;?\s*$", re.M)


class _ProdIndex:
    """Suffix index over production files: ('pkg','mod') -> files whose path ends that way."""

    def __init__(self, prod: list[tuple[str, Path]]) -> None:
        self.by_suffix: dict[tuple[str, ...], list[str]] = {}
        self.by_rel: dict[str, str] = {}
        for rel, _ in prod:
            segs = tuple(rel.split("/"))
            stem = segs[-1].rsplit(".", 1)[0]
            parts = segs[:-1] + ((stem,) if stem != "__init__" else ())
            ext = os.path.splitext(rel)[1].lower()
            no_ext = rel.rsplit(".", 1)[0]
            self.by_rel[no_ext] = rel
            if ext in _JS_EXTS and stem == "index":
                self.by_rel.setdefault("/".join(segs[:-1]), rel)
            for i in range(len(parts)):
                self.by_suffix.setdefault(parts[i:], []).append(rel)

    def lookup(self, segs: tuple[str, ...], near: tuple[str, ...]) -> list[str]:
        found = self.by_suffix.get(segs, [])
        if len(found) <= 1:
            return list(found)

        def shared(rel: str) -> int:
            n = 0
            for a, b in zip(rel.split("/")[:-1], near):
                if a != b:
                    break
                n += 1
            return n

        best = max(shared(r) for r in found)
        return [r for r in found if shared(r) == best]


def _python_imports(text: str, test_dir: tuple[str, ...]) -> list[tuple[tuple[str, ...], bool]]:
    """(segments, exact). exact segments are repo-relative; others are matched by suffix."""
    import ast

    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    out: list[tuple[tuple[str, ...], bool]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((tuple(alias.name.split(".")), False))
        elif isinstance(node, ast.ImportFrom):
            mod = tuple((node.module or "").split(".")) if node.module else ()
            if node.level:
                base = test_dir[: max(len(test_dir) - (node.level - 1), 0)]
                out.append((base + mod, True))
                out.extend((base + mod + (a.name,), True) for a in node.names)
            else:
                out.append((mod, False))
                out.extend((mod + (a.name,), False) for a in node.names)
    return [(segs, exact) for segs, exact in out if segs]


def _js_imports(text: str, test_dir: tuple[str, ...]) -> list[tuple[tuple[str, ...], bool]]:
    out: list[tuple[tuple[str, ...], bool]] = []
    for spec in _JS_SPEC.findall(text):
        if spec.startswith("."):
            joined = posixpath.normpath(posixpath.join("/".join(test_dir), spec))
            if not joined.startswith(".."):
                out.append((tuple(joined.split("/")), True))
            continue
        for alias in ("@/", "~/", "#/"):
            if spec.startswith(alias):
                spec = spec[len(alias):]
                break
        segs = tuple(x for x in spec.split("/") if x)
        if len(segs) >= 2:  # a bare name is almost always an npm package
            out.append((segs, False))
    return out


def _jvm_imports(text: str) -> list[tuple[tuple[str, ...], bool]]:
    out = []
    for static, name, wildcard in _JVM_IMPORT.findall(text):
        if wildcard:
            continue
        segs = tuple(name.split("."))
        out.append((segs[:-1] if static else segs, False))
    return out


def _resolve_imports(
    index: _ProdIndex, test_rel: str, text: str, lang: str
) -> set[str]:
    test_dir = tuple(test_rel.split("/")[:-1])
    if lang == "python":
        imports = _python_imports(text, test_dir)
    elif lang in _JS_LIKE:
        imports = _js_imports(text, test_dir)
    elif lang in {"java", "kotlin", "scala"}:
        imports = _jvm_imports(text)
    else:
        return set()
    hit: set[str] = set()
    for segs, exact in imports:
        if exact:
            rel = "/".join(segs)
            if rel in index.by_rel:
                hit.add(index.by_rel[rel])
            if lang == "python" and (init := rel + "/__init__") in index.by_rel:
                hit.add(index.by_rel[init])
        else:
            hit.update(index.lookup(segs, test_dir))
    return hit


def production_comment_ratio(
    repo: Path, prod: list[tuple[str, Path]], skip_dirs: Iterable[str], timeout: int = 600
) -> float | str:
    """Comment lines / (comment + code lines) over the production source files only.

    scc counts docstrings as comment lines, so this is the combined comment + docstring
    ratio. It is computed per file and restricted to the same file set and languages as
    the other structure metrics, so Markdown, JSON, YAML, tests and vendored code cannot
    dilute it.
    """
    if not shutil.which("scc") or not prod:
        return ""
    wanted = {str((repo / rel).resolve()) for rel, _ in prod}
    try:
        proc = subprocess.run(
            ["scc", "--by-file", "--format", "json",
             "--exclude-dir", ",".join(sorted({".git", *skip_dirs})), str(repo.resolve())],
            capture_output=True, text=True, timeout=timeout,
        )
        data = json.loads(proc.stdout or "[]")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return ""
    comment = code = 0
    for lang_row in data if isinstance(data, list) else []:
        for f in lang_row.get("Files") or []:
            if str(Path(f.get("Location", "")).resolve()) in wanted:
                comment += int(f.get("Comment") or 0)
                code += int(f.get("Code") or 0)
    if comment + code == 0:
        return ""
    return round(comment / (comment + code), 4)


def structure_metrics(
    repo: Path,
    *,
    is_test: Callable[[str], bool],
    skip_dirs: Iterable[str],
) -> dict[str, Any]:
    """Function/class counts, docstring coverage, comment ratio and untested files.

    Untested files: a production source file is tested when either
      1. a test file imports it (imports are parsed and resolved to files for Python,
         JS/TS and Java/Kotlin/Scala), or
      2. a test file has the same normalised stem (test_foo.py, foo_test.go, foo.spec.ts,
         FooTests.cs) AND the directories line up. Generic stems (models, utils, index ...)
         need the directories to line up; a flat tests/test_models.py covers only a
         root-level models.py.
    Static analysis, not execution coverage. Languages without an import resolver rely on
    rule 2 alone.
    """
    out: dict[str, Any] = {k: "" for k in STRUCTURE_FIELDS}
    skip = set(skip_dirs)

    prod: list[tuple[str, Path]] = []
    tests: list[tuple[str, Path]] = []
    seen = 0
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            if os.path.splitext(name)[1].lower() not in EXT_LANG:
                continue
            path = Path(root) / name
            rel = path.relative_to(repo).as_posix()
            if _SKIP_FILE.search(rel) or path.is_symlink():
                continue
            seen += 1
            if seen > MAX_FILES:
                break
            (tests if is_test(rel) else prod).append((rel, path))
        if seen > MAX_FILES:
            break

    out["comment_docstring_ratio"] = production_comment_ratio(repo, prod, skip)

    considered = [(rel, path) for rel, path in prod if path.stem != "__init__"]
    if considered:
        tested: set[str] = set()
        index = _ProdIndex(prod)
        by_stem: dict[str, list[tuple[str, ...]]] = {}
        for rel, path in tests:
            parts = rel.split("/")
            by_stem.setdefault(_test_stem(parts[-1]), []).append(tuple(parts[:-1]))
            lang = EXT_LANG[path.suffix.lower()]
            try:
                if path.stat().st_size > MAX_BYTES:
                    continue
                tested |= _resolve_imports(index, rel, path.read_text(errors="ignore"), lang)
            except OSError:
                continue
        untested = 0
        for rel, path in considered:
            if rel in tested:
                continue
            prod_dir = tuple(rel.split("/")[:-1])
            if any(_mirrors(t, prod_dir) for t in by_stem.get(_norm(path.stem), [])):
                continue
            untested += 1
        out["untested_files"] = untested
        out["untested_files_pct"] = round(100 * untested / len(considered), 1)

    get_parser = _load_parsers()
    if get_parser is None:
        return out

    parsers: dict[str, Any] = {}
    funcs = classes = documented = parsed = 0
    for rel, path in prod:
        lang = EXT_LANG[path.suffix.lower()]
        if lang not in parsers:
            try:
                parsers[lang] = get_parser(lang)
            except Exception:
                parsers[lang] = None
        parser = parsers[lang]
        if parser is None:
            continue
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            tree = parser.parse(path.read_bytes())
        except Exception:
            continue
        f, c, d = _count_file(tree, lang)
        funcs += f
        classes += c
        documented += d
        parsed += 1
    if parsed:
        out["function_count"] = funcs
        out["class_count"] = classes
        if funcs:
            out["docstring_coverage_pct"] = round(100 * documented / funcs, 1)
    return out
