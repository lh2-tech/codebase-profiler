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

import os
import re
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


def comment_docstring_ratio(scc_raw: Any) -> float | str:
    """Comment lines / (comment + code lines) from scc output.

    scc counts Python docstrings and block-comment documentation as comment
    lines, so this is the combined comment + docstring ratio.
    """
    if not isinstance(scc_raw, list):
        return ""
    comment = code = 0
    for row in scc_raw:
        comment += int(row.get("Comment") or row.get("comment") or 0)
        code += int(row.get("Code") or row.get("code") or 0)
    if comment + code == 0:
        return ""
    return round(comment / (comment + code), 4)


def structure_metrics(
    repo: Path,
    *,
    is_test: Callable[[str], bool],
    skip_dirs: Iterable[str],
    scc_raw: Any = None,
) -> dict[str, Any]:
    """Function/class counts, docstring coverage and the untested-file heuristic.

    Untested files: a production source file with no test file of the same
    normalised stem (test_foo.py, foo_test.go, foo.spec.ts, FooTests.cs ...), and
    whose stem does not appear as a word in any test file. Generic stems such as
    ``index`` or ``utils`` only count via the first rule. Static name matching,
    not execution coverage.
    """
    out: dict[str, Any] = {k: "" for k in STRUCTURE_FIELDS}
    out["comment_docstring_ratio"] = comment_docstring_ratio(scc_raw)
    skip = set(skip_dirs)

    prod: list[tuple[str, Path]] = []
    test_stems: set[str] = set()
    test_paths: list[Path] = []
    seen = 0
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext not in EXT_LANG:
                continue
            path = Path(root) / name
            rel = path.relative_to(repo).as_posix()
            if _SKIP_FILE.search(rel) or path.is_symlink():
                continue
            seen += 1
            if seen > MAX_FILES:
                break
            if is_test(rel):
                test_stems.add(_test_stem(name))
                test_paths.append(path)
            else:
                prod.append((rel, path))
        if seen > MAX_FILES:
            break

    # Untested-file heuristic needs no parser.
    if prod:
        test_words: set[str] = set()
        for path in test_paths:
            try:
                if path.stat().st_size > MAX_BYTES:
                    continue
                test_words.update(
                    w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", path.read_text(errors="ignore"))
                )
            except OSError:
                continue
        untested = 0
        considered = 0
        for rel, path in prod:
            stem = _norm(path.stem)
            if path.stem == "__init__":
                continue
            considered += 1
            if stem in test_stems:
                continue
            if stem not in _GENERIC_STEMS and len(stem) >= 4 and stem in test_words:
                continue
            untested += 1
        if considered:
            out["untested_files"] = untested
            out["untested_files_pct"] = round(100 * untested / considered, 1)

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
