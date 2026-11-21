"""Mutation checks for parent_count / pin state and the markdown pin matcher.

Each mutant is one textual change. The oracle must disagree with every mutant.
"""

from __future__ import annotations

import inspect

import gh_identity as g


def _oracle(ns):
    kind = ns["_commit_kind"]
    state = ns["_pin_result_state"]
    parents = ns["_parent_count_text"]
    pins = ns["_markdown_pins"]
    assert kind(None) == "unknown"
    assert kind(0) == "root"
    assert kind(1) == "single"
    assert kind(1, single="squash_or_single") == "squash_or_single"
    assert kind(2) == "merge" and kind(3) == "merge"
    assert state(present=False, ancestor=True, on_line=True, parent_count=2, require_merge=True, changed=False) == "unmerged"
    assert state(present=True, ancestor=False, on_line=True, parent_count=2, require_merge=True, changed=False) == "unmerged"
    assert state(present=True, ancestor=True, on_line=False, parent_count=2, require_merge=True, changed=False) == "not_final"
    assert state(present=True, ancestor=True, on_line=True, parent_count=1, require_merge=True, changed=True) == "not_merge"
    assert state(present=True, ancestor=True, on_line=True, parent_count=0, require_merge=True, changed=False) == "not_merge"
    assert state(present=True, ancestor=True, on_line=True, parent_count=2, require_merge=True, changed=True) == "drift"
    assert state(present=True, ancestor=True, on_line=True, parent_count=2, require_merge=False, changed=False) == "ok"
    assert state(present=True, ancestor=True, on_line=True, parent_count=None, require_merge=True, changed=False) == "unknown_parents"
    assert state(present=True, ancestor=True, on_line=True, parent_count=None, require_merge=True, changed=True) == "unknown_parents"
    assert state(present=True, ancestor=True, on_line=True, parent_count=None, require_merge=False, changed=True) == "drift"
    assert state(present=True, ancestor=True, on_line=True, parent_count=1, require_merge=False, changed=True) == "drift"
    assert parents("") == 0
    assert parents("tree a\nparenthood\n\nparent in message\n") == 0
    assert parents("tree a\nparent b\nparent c\n\nparent d\n") == 2
    sha = "ab" * 20
    upper = "AB" * 20
    explicit = pins("README.md", f"acme/widgets@{sha}\n")
    assert [(p["repository"], p["commit"]) for p in explicit] == [("acme/widgets", sha)]
    same = pins("README.md", f"zz/far\naa/near {sha} zz/later\n")
    assert [(p["repository"], p["commit"]) for p in same] == [("aa/near", sha)]
    adjacent = pins("docs/a.md", f"https://github.com/only/here\n{sha}\n")
    assert [(p["repository"], p["commit"]) for p in adjacent] == [("only/here", sha)]
    assert pins("README.md", f"acme/widgets\n{sha}\n") == []
    far = pins("README.md", f"https://github.com/aa/near\n{sha}{' ' * 30}zz/far\n")
    assert [(p["repository"], p["commit"]) for p in far] == [("zz/far", sha)]
    bare = pins("docs/a.md", f"prose {sha} with no repository\n")
    assert bare == []
    assert pins("docs/a.md", f"acme/widgets {upper}\n") == []
    assert pins("README.md", f"and/or {sha}\n") == []
    assert pins("README.md", f"docs/guide.md {sha}\n") == []
    assert pins("README.md", f"acme/widgets {'0' * 40}\n") == []
    assert pins("README.md", f"https://github.com/acme/widgets/pull/4 {sha}\n") == []
    left, right = "aa/bb", "cc/dd"
    tied = pins("docs/t.md", f"{left}   {sha}   {right}\n")
    assert [p["repository"] for p in tied] == [left]


def _activate(name, old, new, extra=()):
    source = inspect.getsource(getattr(g, name))
    count = source.count(old)
    if count != 1:
        raise AssertionError(f"{name} has {count} of {old!r}")
    ns = dict(g.__dict__)
    exec(source.replace(old, new, 1), ns)
    for other in extra:
        exec(inspect.getsource(getattr(g, other)), ns)
    return ns


def _uppercase_namespace():
    ns = dict(g.__dict__)
    ns["_MD_SHA"] = __import__("re").compile(g._MD_SHA.pattern.replace("[0-9a-f]", "[0-9a-fA-F]"))
    exec(inspect.getsource(g._markdown_pins), ns)
    return ns


MUTANTS = [
    ("_commit_kind", "parent_count >= 2", "parent_count > 2", ()),
    ("_commit_kind", "parent_count >= 2", "parent_count >= 1", ()),
    ("_commit_kind", "parent_count == 1", "parent_count == 0", ()),
    ("_commit_kind", 'return "unknown"', 'return "root"', ()),
    ("_commit_kind", 'return "root"', 'return "single"', ()),
    ("_commit_kind", "return single", 'return "merge"', ()),
    ("_pin_result_state", "and parent_count < 2:", "and parent_count < 1:", ()),
    ("_pin_result_state", "and parent_count < 2:", "and parent_count <= 2:", ()),
    ("_pin_result_state", "parent_count is None:", "parent_count is not None:", ()),
    ("_pin_result_state", 'return "unknown_parents"', 'return "ok"', ()),
    ("_pin_result_state", "if not on_line:", "if on_line:", ()),
    ("_pin_result_state", "if not present or not ancestor:", "if not present and not ancestor:", ()),
    ("_pin_result_state", "if changed:", "if not changed:", ()),
    ("_pin_result_state", 'return "unmerged"', 'return "ok"', ()),
    ("_pin_result_state", 'return "not_final"', 'return "ok"', ()),
    ("_pin_result_state", 'return "not_merge"', 'return "ok"', ()),
    ("_pin_result_state", 'return "drift"', 'return "ok"', ()),
    ("_pin_result_state", 'return "ok"', 'return "drift"', ()),
    ("_parent_count_text", 'if line == "":', 'if line == "NOBREAK":', ()),
    ("_parent_count_text", 'startswith("parent ")', 'startswith("parent")', ()),
    ("_parent_count_text", "count += 1", "count += 0", ()),
    ("_nearest_markdown_repo", "for delta in (-1, 0, 1):", "for delta in (0,):", ("_markdown_pins",)),
    ("_nearest_markdown_repo", "for delta in (-1, 0, 1):", "for delta in (-1, 1):", ("_markdown_pins",)),
    ("_nearest_markdown_repo", "(abs(delta), gap, other, start, name)", "(0, gap, other, start, name)", ("_markdown_pins",)),
    ("_nearest_markdown_repo", "(abs(delta), gap, other, start, name)", "(abs(delta), gap, other, -start, name)", ("_markdown_pins",)),
    ("_markdown_pins", "if span in consumed:\n                continue", "if False and span in consumed:\n                continue", ()),
    ("_markdown_pins", "name = _nearest_markdown_repo(qualifiers, index, match.start(), match.end())\n            if name is None:\n                continue",
     "name = _nearest_markdown_repo(qualifiers, index, match.start(), match.end())\n            if name is None:\n                name = \"bare/repo\"", ()),
    ("_github_full_name_ok", 'or name in (".","..")', 'or name.startswith(".")', ()),
    ("_pin_repo", "    else:\n        raise ValueError(\"not an OWNER/REPO\")\n    if not _github_full_name_ok(full):",
     "    else:\n        found = re.search(r\"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)$\", value)\n        if found is None:\n            raise ValueError(\"not an OWNER/REPO\")\n        full = found.group(1)\n    if not _github_full_name_ok(full):", ()),
    ("_markdown_qualifiers", "found.append((match.start(), match.end(), name, False))",
     "found.append((match.start(), match.end(), name, True))", ("_markdown_pins", "_nearest_markdown_repo")),
]


def _repo_mutant():
    ns = dict(g.__dict__)
    ns["REPO"] = __import__("re").compile(r"^[\w.-]+/[\w.-]+$")
    exec(inspect.getsource(g.repo), ns)
    return ns


def _name_oracle(ns):
    assert ns["repo"]("o/r") == "o/r"
    assert ns["repo"]("日本/リポ") == "日本/リポ"
    assert ns["repo"]("acme/.github") == "acme/.github"
    for bad in ("../secret", "./secret", "owner/..", "owner/."):
        try:
            ns["repo"](bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    assert ns["_github_full_name_ok"]("acme/.github") is True
    assert ns["_github_full_name_ok"]("owner/.hidden") is True
    assert ns["_github_full_name_ok"]("owner/..") is False
    assert ns["_pin_repo"]("acme/.github") == "acme/.github"
    assert ns["_pin_repo"]("https://github.com/acme/widgets") == "acme/widgets"
    for bad in ("a/b/c", "https://evil.example/acme/widgets", "/tmp/remotes/acme/widgets", "../secret"):
        try:
            ns["_pin_repo"](bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_parent_and_markdown_mutants_are_killed():
    _oracle(g.__dict__)
    _name_oracle(g.__dict__)
    killed = []
    for name, old, new, extra in MUTANTS:
        ns = _activate(name, old, new, extra)
        try:
            _oracle(ns)
            _name_oracle(ns)
        except AssertionError:
            killed.append(name + ":" + old)
            continue
        raise AssertionError(f"mutant survived: {name}: {old!r} -> {new!r}")
    ns = _uppercase_namespace()
    try:
        _oracle(ns)
    except AssertionError:
        killed.append("_MD_SHA:uppercase")
    else:
        raise AssertionError("uppercase sha mutant survived")
    try:
        _name_oracle(_repo_mutant())
    except AssertionError:
        killed.append("repo:dotdot")
    else:
        raise AssertionError("repo traversal mutant survived")
    assert len(killed) == len(MUTANTS) + 2
