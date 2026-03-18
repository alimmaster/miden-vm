#!/usr/bin/env python3
"""
Cross-commit constraint equivalence diffing tool.

Runs the ConstraintRecorder test across a range of git commits and diffs
the fingerprints to surface exactly which constraints changed, with the
actual source expression for each.

The constraint_recorder.rs file is injected into each commit automatically
(no stash needed), so this works even on commits that don't have it.

Usage:
    python3 scripts/diff_constraints.py f7c294c44 HEAD
    python3 scripts/diff_constraints.py HEAD~3 HEAD
    python3 scripts/diff_constraints.py f7c294c44 HEAD --pr-comment 123
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

# Paths relative to repo root
RECORDER_REL = "air/src/constraint_recorder.rs"
LIB_RS_REL = "air/src/lib.rs"
DUMP_DIR_REL = "air/constraint_dumps"

REPO_ROOT = Path(
    subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], text=True
    ).strip()
)

# Injection marker — the two lines we add to lib.rs
MOD_DECLARATION = '#[cfg(feature = "std")]\npub mod constraint_recorder;\n'

# Hidden marker to identify our PR comment
COMMENT_MARKER = "<!-- constraint-equivalence-report -->"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ConstraintEntry:
    kind: str           # "base" or "ext"
    fingerprint: str    # hex string
    count: int
    file: str = ""      # e.g. "./src/constraints/stack/ops/mod.rs"
    line: int = 0       # source line number
    function: str = ""  # e.g. "miden_air::constraints::stack::ops::enforce_main"
    source_expr: str = ""  # extracted source line(s)


@dataclass
class CommitDump:
    commit_hash: str
    commit_msg: str
    index: int = 0
    constraints: dict = field(default_factory=dict)  # key -> ConstraintEntry
    build_failed: bool = False
    error: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd, **kwargs):
    kwargs.setdefault("cwd", REPO_ROOT)
    kwargs.setdefault("text", True)
    kwargs.setdefault("capture_output", True)
    return subprocess.run(cmd, **kwargs)


def short_hash(full_hash):
    return full_hash[:10]


def get_commit_list(start, end):
    """Return list of full commit hashes from start to end (both inclusive)."""
    result = run(["git", "rev-list", "--reverse", f"{start}~1..{end}"])
    if result.returncode != 0:
        start_full = run(["git", "rev-parse", start]).stdout.strip()
        result = run(["git", "rev-list", "--reverse", f"{start}..{end}"])
        hashes = [h for h in result.stdout.strip().split("\n") if h]
        hashes.insert(0, start_full)
        return hashes
    return [h for h in result.stdout.strip().split("\n") if h]


def get_commit_msg(commit_hash):
    return run(["git", "log", "--format=%s", "-1", commit_hash]).stdout.strip()


def get_repo_slug():
    """Get owner/repo from git remote."""
    result = run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
    if result.returncode == 0:
        return result.stdout.strip()
    return None


# ---------------------------------------------------------------------------
# File injection / cleanup
# ---------------------------------------------------------------------------

def inject_recorder(recorder_content):
    """Write constraint_recorder.rs and ensure lib.rs declares the module."""
    (REPO_ROOT / RECORDER_REL).write_text(recorder_content)

    lib_path = REPO_ROOT / LIB_RS_REL
    lib_text = lib_path.read_text()
    if "constraint_recorder" not in lib_text:
        lib_text = lib_text.replace(
            "mod constraints;\n",
            "mod constraints;\n\n" + MOD_DECLARATION,
            1,
        )
        lib_path.write_text(lib_text)


def revert_injection():
    """Remove injected file and restore lib.rs."""
    recorder = REPO_ROOT / RECORDER_REL
    if recorder.exists():
        recorder.unlink()
    run(["git", "checkout", "--", LIB_RS_REL])


# ---------------------------------------------------------------------------
# Build & run
# ---------------------------------------------------------------------------

def run_recorder(dump_path):
    """Run the constraint recorder test. Returns (success, combined_output)."""
    env = os.environ.copy()
    env["CONSTRAINT_DUMP_PATH"] = str(dump_path)
    env["RUST_BACKTRACE"] = "1"

    result = run(
        [
            "cargo", "test", "-p", "miden-air",
            "constraint_recorder", "--", "--nocapture",
        ],
        env=env,
        timeout=600,
    )
    return result.returncode == 0, result.stderr + result.stdout


# ---------------------------------------------------------------------------
# Dump parsing
# ---------------------------------------------------------------------------

def parse_dump(dump_path):
    """Parse a full constraint dump file into {key: ConstraintEntry}."""
    constraints = {}
    if not dump_path.exists():
        return constraints

    text = dump_path.read_text()
    in_body = False
    current_key = None
    current_entry = None

    for raw_line in text.split("\n"):
        line = raw_line.rstrip()

        if line == "---":
            in_body = True
            continue
        if not in_body:
            continue

        m = re.match(r"^(base|ext):(\S+)\s+x(\d+)", line)
        if m:
            if current_key and current_entry:
                constraints[current_key] = current_entry

            kind, fp, count = m.group(1), m.group(2), int(m.group(3))
            current_key = f"{kind}:{fp}"
            current_entry = ConstraintEntry(kind=kind, fingerprint=fp, count=count)
            continue

        if current_entry is None:
            continue

        stripped = line.strip()
        if not stripped:
            if current_key and current_entry:
                constraints[current_key] = current_entry
            current_key = current_entry = None
            continue

        if re.match(r"^\[\d+\]:$", stripped):
            continue

        fm = re.match(r"^\d+:\s+(.+)$", stripped)
        if fm and not current_entry.function:
            current_entry.function = fm.group(1)

        am = re.match(r"^at\s+(\S+):(\d+):\d+$", stripped)
        if am and not current_entry.file:
            current_entry.file = am.group(1)
            current_entry.line = int(am.group(2))

    if current_key and current_entry:
        constraints[current_key] = current_entry

    return constraints


# ---------------------------------------------------------------------------
# Source expression extraction
# ---------------------------------------------------------------------------

def extract_source_expr(file_rel, line_num):
    """Read the source file and extract the assert expression at the given line."""
    clean = file_rel.lstrip("./")
    full_path = REPO_ROOT / "air" / clean
    if not full_path.exists():
        return None

    try:
        lines = full_path.read_text().split("\n")
    except Exception:
        return None

    if line_num < 1 or line_num > len(lines):
        return None

    search_start = max(0, line_num - 4)
    search_end = min(len(lines), line_num + 1)

    expr_start = None
    for idx in range(search_end - 1, search_start - 1, -1):
        l = lines[idx]
        if re.search(r"builder\b", l):
            expr_start = idx
            break
        if re.search(r"\.assert_\w+\s*\(", l) and expr_start is None:
            expr_start = idx

    if expr_start is None:
        return lines[line_num - 1].strip()

    collected = []
    depth = 0
    for idx in range(expr_start, min(len(lines), expr_start + 20)):
        l = lines[idx]
        collected.append(l.strip())
        depth += l.count("(") - l.count(")")
        if ";" in l:
            break
        # Only stop on balanced parens if we've already seen an assert call
        if depth <= 0 and len(collected) > 1 and re.search(r"\.assert_\w+", " ".join(collected)):
            break

    result = " ".join(collected)
    if len(result) > 300:
        result = result[:300] + "..."
    return result


def enrich_with_source(constraints):
    """Fill in source_expr for each constraint (must be on the right commit)."""
    for entry in constraints.values():
        if entry.file and entry.line:
            expr = extract_source_expr(entry.file, entry.line)
            if expr:
                entry.source_expr = expr


def save_sources(dump_dir, idx, h, constraints):
    """Write a JSON sidecar with source expressions alongside the dump file."""
    data = {}
    for key, entry in constraints.items():
        if entry.source_expr or entry.file:
            data[key] = {
                "source_expr": entry.source_expr,
                "file": entry.file,
                "line": entry.line,
                "function": entry.function,
            }
    path = dump_dir / f"{idx:02d}_{h}_sources.json"
    path.write_text(json.dumps(data, indent=2) + "\n")


def load_sources(dump_dir, idx, h):
    """Read a JSON sidecar and return {key: {source_expr, file, line, function}}."""
    path = dump_dir / f"{idx:02d}_{h}_sources.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def enrich_from_sidecar(constraints, dump_dir, idx, h):
    """Populate source_expr from a JSON sidecar (for regeneration without source checkout)."""
    sources = load_sources(dump_dir, idx, h)
    for key, entry in constraints.items():
        info = sources.get(key)
        if info and not entry.source_expr:
            entry.source_expr = info.get("source_expr", "")
            if not entry.file:
                entry.file = info.get("file", "")
            if not entry.line:
                entry.line = info.get("line", 0)
            if not entry.function:
                entry.function = info.get("function", "")


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------

def diff_dumps(prev, curr):
    """Compare two CommitDumps. Returns (removed, added, count_changed, unchanged_count)."""
    pk = set(prev.constraints)
    ck = set(curr.constraints)

    removed = {k: prev.constraints[k] for k in pk - ck}
    added = {k: curr.constraints[k] for k in ck - pk}

    count_changed = {}
    unchanged = 0
    for k in pk & ck:
        if prev.constraints[k].count != curr.constraints[k].count:
            count_changed[k] = (prev.constraints[k], curr.constraints[k])
        else:
            unchanged += 1

    return removed, added, count_changed, unchanged


def match_updates(removed, added):
    """Match removed+added constraints into update pairs.

    Groups by function name (strong signal), then matches within each group
    using string similarity on the source expression. Even low similarity is
    accepted because sharing a function is already a strong indicator.

    Returns (updates, unmatched_removed, unmatched_added) where:
      updates: list of (old_entry, new_entry) pairs
      unmatched_removed/added: dicts of truly removed/added constraints
    """
    removed_by_func = {}
    for k, e in removed.items():
        removed_by_func.setdefault(e.function or "", []).append((k, e))

    added_by_func = {}
    for k, e in added.items():
        added_by_func.setdefault(e.function or "", []).append((k, e))

    updates = []
    unmatched_removed = {}
    unmatched_added = {}

    for func in set(removed_by_func) | set(added_by_func):
        r_list = removed_by_func.get(func, [])
        a_list = added_by_func.get(func, [])

        if not r_list:
            for k, e in a_list:
                unmatched_added[k] = e
            continue
        if not a_list:
            for k, e in r_list:
                unmatched_removed[k] = e
            continue

        # Sort by line for stable ordering
        r_list.sort(key=lambda x: x[1].line)
        a_list.sort(key=lambda x: x[1].line)

        # Build similarity scores and greedy-match best pairs
        pairs = []
        for ri, (_, re_) in enumerate(r_list):
            for ai, (_, ae) in enumerate(a_list):
                ratio = SequenceMatcher(
                    None, re_.source_expr, ae.source_expr
                ).ratio()
                pairs.append((ratio, ri, ai))

        pairs.sort(reverse=True)
        used_r, used_a = set(), set()
        for _ratio, ri, ai in pairs:
            if ri in used_r or ai in used_a:
                continue
            updates.append((r_list[ri][1], a_list[ai][1]))
            used_r.add(ri)
            used_a.add(ai)

        for ri, (k, e) in enumerate(r_list):
            if ri not in used_r:
                unmatched_removed[k] = e
        for ai, (k, e) in enumerate(a_list):
            if ai not in used_a:
                unmatched_added[k] = e

    # Second pass: match remaining unmatched across functions using similarity.
    # This catches constraints that moved between functions (e.g., inlining).
    if unmatched_removed and unmatched_added:
        r_items = list(unmatched_removed.items())
        a_items = list(unmatched_added.items())

        pairs2 = []
        for ri, (_, re_) in enumerate(r_items):
            for ai, (_, ae) in enumerate(a_items):
                # Use source expression if available, fall back to function name
                r_text = re_.source_expr or re_.function
                a_text = ae.source_expr or ae.function
                if not r_text or not a_text:
                    continue
                ratio = SequenceMatcher(None, r_text, a_text).ratio()
                if ratio > 0.25:
                    pairs2.append((ratio, ri, ai))

        pairs2.sort(reverse=True)
        used_r2, used_a2 = set(), set()
        for _ratio, ri, ai in pairs2:
            if ri in used_r2 or ai in used_a2:
                continue
            updates.append((r_items[ri][1], a_items[ai][1]))
            used_r2.add(ri)
            used_a2.add(ai)

        unmatched_removed = {k: e for i, (k, e) in enumerate(r_items) if i not in used_r2}
        unmatched_added = {k: e for i, (k, e) in enumerate(a_items) if i not in used_a2}

    # Sort updates by file/line of the old entry
    updates.sort(key=lambda pair: (pair[0].file, pair[0].line))
    return updates, unmatched_removed, unmatched_added


# ---------------------------------------------------------------------------
# Terminal report
# ---------------------------------------------------------------------------

def fmt_entry(entry):
    loc = ""
    if entry.file and entry.line:
        loc = f" | {entry.file.lstrip('./')}:{entry.line}"
    line = f"    {entry.kind}:{entry.fingerprint} x{entry.count}{loc}"
    if entry.source_expr:
        line += f"\n      {entry.source_expr}"
    return line


def fmt_loc(entry):
    if entry.file and entry.line:
        return f"{entry.file.lstrip('./')}:{entry.line}"
    return ""


def print_report(dumps):
    total_updates = total_removed = total_added = commits_with_changes = 0
    failed = sum(1 for d in dumps if d.build_failed)

    print()
    print("=" * 80)
    print("CONSTRAINT EQUIVALENCE REPORT")
    print("=" * 80)

    for i in range(1, len(dumps)):
        prev, curr = dumps[i - 1], dumps[i]

        if prev.build_failed or curr.build_failed:
            skip_reason = curr.error if curr.build_failed else "prev failed"
            print(f"\n--- {prev.commit_hash} -> {curr.commit_hash}: SKIPPED ({skip_reason}) ---")
            continue

        removed, added, changed, unchanged = diff_dumps(prev, curr)

        if not removed and not added and not changed:
            continue

        updates, only_removed, only_added = match_updates(removed, added)

        commits_with_changes += 1
        total_updates += len(updates)
        total_removed += len(only_removed)
        total_added += len(only_added)

        parts = [f"Unchanged: {unchanged}"]
        if updates:
            parts.append(f"Updated: {len(updates)}")
        if only_removed:
            parts.append(f"Removed: {len(only_removed)}")
        if only_added:
            parts.append(f"Added: {len(only_added)}")
        if changed:
            parts.append(f"Count-changed: {len(changed)}")

        print(f"\n=== {prev.commit_hash} -> {curr.commit_hash}: {curr.commit_msg} ===")
        print(f"  {' | '.join(parts)}")

        if updates:
            print("\n  UPDATED:")
            for old, new in updates:
                print(f"    {fmt_loc(old)} -> {fmt_loc(new)}")
                if old.source_expr:
                    print(f"      - {old.source_expr}")
                if new.source_expr:
                    print(f"      + {new.source_expr}")

        if only_removed:
            print("\n  REMOVED:")
            for e in sorted(only_removed.values(), key=lambda e: (e.file, e.line)):
                print(fmt_entry(e))

        if only_added:
            print("\n  ADDED:")
            for e in sorted(only_added.values(), key=lambda e: (e.file, e.line)):
                print(fmt_entry(e))

        if changed:
            print("\n  COUNT CHANGED:")
            for key in sorted(changed):
                old, new = changed[key]
                print(f"    {key}: x{old.count} -> x{new.count}")

    ok_commits = len(dumps) - failed
    equivalent = ok_commits - commits_with_changes - 1
    if equivalent < 0:
        equivalent = 0

    print(f"\n{'=' * 80}")
    print("SUMMARY")
    print(f"{'=' * 80}")
    print(f"  {len(dumps)} commits analyzed, {failed} failed to build")
    print(f"  {commits_with_changes} commits had constraint changes")
    print(f"  {equivalent} consecutive pairs verified equivalent (zero changes)")
    print(f"  Total: {total_updates} updated, {total_removed} removed, {total_added} added")


# ---------------------------------------------------------------------------
# Regeneration from existing dumps
# ---------------------------------------------------------------------------

def load_dumps_from_dir(dump_dir):
    """Scan dump_dir for existing dump files and reconstruct CommitDump objects."""
    import glob as glob_mod
    pattern = str(dump_dir / "*_dump.txt")
    dump_files = sorted(glob_mod.glob(pattern))

    dumps = []
    for path in dump_files:
        fname = Path(path).name  # e.g. "22_94b25974d5_dump.txt"
        m = re.match(r"^(\d+)_([a-f0-9]+)_dump\.txt$", fname)
        if not m:
            continue
        idx = int(m.group(1))
        h = m.group(2)
        msg = get_commit_msg(h)

        constraints = parse_dump(Path(path))
        enrich_from_sidecar(constraints, dump_dir, idx, h)

        dumps.append(CommitDump(h, msg, index=idx, constraints=constraints))

    return dumps


# ---------------------------------------------------------------------------
# Interpretations
# ---------------------------------------------------------------------------

def load_interpretations(dump_dir):
    """Load interpretations.json from dump_dir. Returns {commit_hash: [entries]}."""
    path = dump_dir / "interpretations.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def lookup_remark(interps_for_commit, old_key, new_key):
    """Look up a remark for a specific constraint update.

    Checks per-constraint entries first (matching old_fp/new_fp),
    then falls back to group remarks (applies_to: "all").
    """
    if not interps_for_commit:
        return ""
    for entry in interps_for_commit:
        if entry.get("applies_to") == "all":
            continue
        if entry.get("old_fp") == old_key and entry.get("new_fp") == new_key:
            return entry.get("remark", "")
    # Fall back to group remark
    for entry in interps_for_commit:
        if entry.get("applies_to") == "all":
            return entry.get("group_remark", "")
    return ""


# ---------------------------------------------------------------------------
# Markdown PR comment generation
# ---------------------------------------------------------------------------

def gh_url(slug, commit_hash, file_rel, line):
    """Build a GitHub permalink to a specific line in a commit."""
    if not slug or not file_rel:
        return None
    repo_path = "air/" + file_rel.lstrip("./")
    url = f"https://github.com/{slug}/blob/{commit_hash}/{repo_path}"
    if line:
        url += f"#L{line}"
    return url


_full_hash_cache = {}

def resolve_full_hash(short_hash):
    """Resolve a short hash to a full 40-char hash (cached)."""
    if short_hash not in _full_hash_cache:
        _full_hash_cache[short_hash] = run(
            ["git", "rev-parse", short_hash]
        ).stdout.strip()
    return _full_hash_cache[short_hash]


def pr_diff_url(slug, pr_number, commit_hash, file_rel, line):
    """Build a GitHub PR per-commit diff URL pointing to a specific file+line."""
    if not slug or not pr_number or not file_rel:
        return None
    repo_path = "air/" + file_rel.lstrip("./")
    file_hash = hashlib.sha256(repo_path.encode()).hexdigest()
    full_hash = resolve_full_hash(commit_hash)
    url = f"https://github.com/{slug}/pull/{pr_number}/changes/{full_hash}#diff-{file_hash}"
    if line:
        url += f"R{line}"
    return url


def md_loc(entry, slug=None, commit_hash=None):
    """Short location string for markdown, optionally as a GitHub link."""
    if not entry.file or not entry.line:
        return ""
    short = f"{entry.file.lstrip('./')}:{entry.line}"
    url = gh_url(slug, commit_hash, entry.file, entry.line) if slug else None
    if url:
        return f"[`{short}`]({url})"
    return f"`{short}`"


def md_expr(entry):
    """Source expression formatted for markdown table cell."""
    if entry.source_expr:
        escaped = entry.source_expr.replace("|", "\\|")
        return f"`{escaped}`"
    return ""


def md_short_file(entry, slug=None, commit_hash=None):
    """Short file link: path after 'constraints/' as a GitHub blob link."""
    if not entry.file or not entry.line:
        return ""
    # ./src/constraints/chiplets/ace.rs → chiplets/ace.rs
    path = entry.file.lstrip("./")
    short = re.sub(r"^src/constraints/", "", path)
    url = gh_url(slug, commit_hash, entry.file, entry.line) if slug else None
    if url:
        return f"[`{short}`]({url})"
    return f"`{short}`"


def md_cell(entry, slug=None, commit_hash=None):
    """Expression-primary cell: show expression, fall back to location if no expression."""
    expr = md_expr(entry)
    if expr:
        return expr
    return md_loc(entry, slug, commit_hash)


def generate_pr_comment(dumps, start_commit, end_commit, slug=None, dump_dir=None, pr_number=None):
    """Generate a GitHub-flavored markdown comment body."""
    interps = load_interpretations(dump_dir) if dump_dir else {}
    lines = []
    lines.append(COMMENT_MARKER)
    lines.append("")
    lines.append("## Constraint Equivalence Report")
    lines.append("")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"**Last updated:** {now}  ")
    lines.append(f"**Range:** `{start_commit}..{end_commit}` ({len(dumps)} commits)")
    lines.append("")

    # Compute all diffs first
    failed = sum(1 for d in dumps if d.build_failed)
    total_updates = total_removed = total_added = commits_with_changes = 0
    equivalent_pairs = 0
    change_sections = []

    for i in range(1, len(dumps)):
        prev, curr = dumps[i - 1], dumps[i]
        if prev.build_failed or curr.build_failed:
            continue

        removed, added, count_changed, unchanged = diff_dumps(prev, curr)
        if not removed and not added and not count_changed:
            equivalent_pairs += 1
            continue

        updates, only_removed, only_added = match_updates(removed, added)

        commits_with_changes += 1
        total_updates += len(updates)
        total_removed += len(only_removed)
        total_added += len(only_added)

        change_sections.append(
            (prev, curr, updates, only_removed, only_added, count_changed, unchanged)
        )

    # Summary table
    lines.append("### Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Commits analyzed | {len(dumps)} |")
    if failed:
        lines.append(f"| Build failures | {failed} |")
    lines.append(f"| Pairs with changes | **{commits_with_changes}** |")
    lines.append(f"| Pairs verified equivalent | {equivalent_pairs} |")
    if total_updates:
        lines.append(f"| Total constraints updated | {total_updates} |")
    if total_removed:
        lines.append(f"| Total constraints removed | {total_removed} |")
    if total_added:
        lines.append(f"| Total constraints added | {total_added} |")
    lines.append("")

    if not change_sections:
        lines.append("> All consecutive commit pairs produce identical constraint fingerprints.")
        return "\n".join(lines)

    lines.append("### Changes")
    lines.append("")

    for prev, curr, updates, only_removed, only_added, count_changed, unchanged in change_sections:
        n_total = len(updates) + len(only_removed) + len(only_added) + len(count_changed)

        # Header with optional compare link
        header = f"#### `{curr.commit_hash}` {curr.commit_msg}"
        if slug:
            compare_url = f"https://github.com/{slug}/compare/{prev.commit_hash}...{curr.commit_hash}"
            header += f" ([diff]({compare_url}))"
        lines.append(header)
        lines.append("")

        parts = []
        if updates:
            parts.append(f"{len(updates)} updated")
        if only_removed:
            parts.append(f"{len(only_removed)} removed")
        if only_added:
            parts.append(f"{len(only_added)} added")
        if count_changed:
            parts.append(f"{len(count_changed)} count-changed")
        lines.append(f"> {', '.join(parts)} | {unchanged} unchanged")
        lines.append("")

        use_details = n_total > 12
        if use_details:
            lines.append("<details>")
            lines.append(f"<summary>Show {n_total} changes</summary>")
            lines.append("")

        # Updated constraints — side-by-side table with links + remarks
        interps_for_commit = interps.get(curr.commit_hash, [])
        if updates:
            has_remarks = any(
                lookup_remark(interps_for_commit,
                              f"{old.kind}:{old.fingerprint}",
                              f"{new.kind}:{new.fingerprint}")
                for old, new in updates
            )
            lines.append("**Updated:**")
            lines.append("")
            if has_remarks:
                lines.append("| Expression | Remark |")
                lines.append("|------------|--------|")
            else:
                lines.append("| Expression |")
                lines.append("|------------|")
            for old, new in updates:
                file_link = md_short_file(new, slug, curr.commit_hash)
                cell = md_cell(new, slug, curr.commit_hash)
                diff_url = pr_diff_url(slug, pr_number, curr.commit_hash, new.file, new.line)
                if diff_url:
                    cell = f"[{cell}]({diff_url})"
                entry_text = f"{file_link} {cell}" if file_link else cell
                if has_remarks:
                    old_key = f"{old.kind}:{old.fingerprint}"
                    new_key = f"{new.kind}:{new.fingerprint}"
                    remark = lookup_remark(interps_for_commit, old_key, new_key)
                    lines.append(f"| {entry_text} | {remark} |")
                else:
                    lines.append(f"| {entry_text} |")
            lines.append("")

        # Truly removed (linked to prev commit)
        if only_removed:
            lines.append("**Removed:**")
            lines.append("")
            lines.append("| Expression |")
            lines.append("|------------|")
            for e in sorted(only_removed.values(), key=lambda e: (e.file, e.line)):
                file_link = md_short_file(e, slug, prev.commit_hash)
                cell = md_cell(e, slug, prev.commit_hash)
                diff_url = pr_diff_url(slug, pr_number, prev.commit_hash, e.file, e.line)
                if diff_url:
                    cell = f"[{cell}]({diff_url})"
                entry_text = f"{file_link} {cell}" if file_link else cell
                lines.append(f"| {entry_text} |")
            lines.append("")

        # Truly added (linked to curr commit)
        if only_added:
            lines.append("**Added:**")
            lines.append("")
            lines.append("| Expression |")
            lines.append("|------------|")
            for e in sorted(only_added.values(), key=lambda e: (e.file, e.line)):
                file_link = md_short_file(e, slug, curr.commit_hash)
                cell = md_cell(e, slug, curr.commit_hash)
                diff_url = pr_diff_url(slug, pr_number, curr.commit_hash, e.file, e.line)
                if diff_url:
                    cell = f"[{cell}]({diff_url})"
                entry_text = f"{file_link} {cell}" if file_link else cell
                lines.append(f"| {entry_text} |")
            lines.append("")

        if count_changed:
            lines.append("**Count changed:**")
            lines.append("")
            lines.append("| Fingerprint | Old | New |")
            lines.append("|-------------|-----|-----|")
            for key in sorted(count_changed):
                old, new = count_changed[key]
                lines.append(f"| `{key}` | x{old.count} | x{new.count} |")
            lines.append("")

        if use_details:
            lines.append("</details>")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# GitHub PR comment management
# ---------------------------------------------------------------------------

def find_existing_comment(pr_number):
    """Find our existing comment on the PR by marker. Returns comment ID or None."""
    result = run([
        "gh", "api",
        f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments",
        "--paginate", "-q",
        f'[.[] | select(.body | contains("{COMMENT_MARKER}"))][0].id',
    ])
    if result.returncode == 0 and result.stdout.strip():
        try:
            return int(result.stdout.strip())
        except ValueError:
            pass
    return None


def post_or_update_comment(pr_number, body):
    """Post a new comment or update existing one on the PR."""
    comment_id = find_existing_comment(pr_number)

    if comment_id:
        print(f"Updating existing comment {comment_id}...")
        result = run([
            "gh", "api",
            f"repos/{{owner}}/{{repo}}/issues/comments/{comment_id}",
            "-X", "PATCH", "-f", f"body={body}",
        ])
        if result.returncode == 0:
            print("Comment updated.")
        else:
            print(f"Failed to update comment: {result.stderr}", file=sys.stderr)
    else:
        print("Posting new comment...")
        result = run([
            "gh", "pr", "comment", str(pr_number), "--body", body,
        ])
        if result.returncode == 0:
            print("Comment posted.")
        else:
            print(f"Failed to post comment: {result.stderr}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Diff constraint fingerprints across git commits",
    )
    parser.add_argument("start_commit", nargs="?", help="Start commit (inclusive)")
    parser.add_argument("end_commit", nargs="?", help="End commit (inclusive)")
    parser.add_argument(
        "--no-source", action="store_true",
        help="Skip source expression extraction",
    )
    parser.add_argument(
        "--dump-dir", type=Path, default=None,
        help=f"Directory to store dump files (default: {DUMP_DIR_REL})",
    )
    parser.add_argument(
        "--pr-comment", type=int, default=None, metavar="PR",
        help="Post/update a constraint report comment on this PR number",
    )
    parser.add_argument(
        "--regenerate", action="store_true",
        help="Regenerate PR comment from existing dumps (skip build loop)",
    )
    parser.add_argument(
        "--extract-sources", action="store_true",
        help="Generate JSON source sidecars from existing dumps (checkout + read, no build)",
    )
    args = parser.parse_args()

    os.chdir(REPO_ROOT)

    dump_dir = args.dump_dir or (REPO_ROOT / DUMP_DIR_REL)
    dump_dir.mkdir(parents=True, exist_ok=True)

    # --- Regeneration path: load from existing dumps, skip build loop ---
    if args.regenerate:
        print("Regenerating from existing dumps...")
        dumps = load_dumps_from_dir(dump_dir)
        if not dumps:
            print("No dump files found.", file=sys.stderr)
            sys.exit(1)

        start_short = dumps[0].commit_hash
        end_short = dumps[-1].commit_hash
        print(f"Loaded {len(dumps)} dumps ({start_short}..{end_short})")

        print_report(dumps)

        slug = get_repo_slug()
        comment_body = generate_pr_comment(
            dumps, start_short, end_short, slug=slug, dump_dir=dump_dir,
            pr_number=args.pr_comment,
        )
        comment_file = dump_dir / "pr_comment.md"
        comment_file.write_text(comment_body)
        print(f"\nPR comment saved to: {comment_file}")

        if args.pr_comment:
            post_or_update_comment(args.pr_comment, comment_body)

        print(f"Dumps in: {dump_dir}")
        return

    # --- Extract sources path: checkout each commit, read source files, save sidecars ---
    if args.extract_sources:
        import glob as glob_mod

        print("Extracting source expressions from existing dumps...")
        pattern = str(dump_dir / "*_dump.txt")
        dump_files = sorted(glob_mod.glob(pattern))
        if not dump_files:
            print("No dump files found.", file=sys.stderr)
            sys.exit(1)

        # Pre-parse all dumps before any checkout (dump files are tracked and
        # would be removed by git checkout of older commits).
        work_items = []
        for path in dump_files:
            fname = Path(path).name
            m = re.match(r"^(\d+)_([a-f0-9]+)_dump\.txt$", fname)
            if not m:
                continue
            idx = int(m.group(1))
            h = m.group(2)

            sidecar = dump_dir / f"{idx:02d}_{h}_sources.json"
            if sidecar.exists():
                print(f"  [{idx:02d}] {h} — sidecar exists, skipping")
                continue

            constraints = parse_dump(Path(path))
            if not constraints:
                print(f"  [{idx:02d}] {h} — empty dump, skipping")
                continue

            work_items.append((idx, h, constraints))

        if not work_items:
            print("All sidecars already exist.")
            return

        original_ref = run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
        if original_ref == "HEAD":
            original_ref = run(["git", "rev-parse", "HEAD"]).stdout.strip()

        print("Saving working tree...")
        stash_result = run(["git", "stash", "--include-untracked", "-m", "extract-sources: auto-save"])
        had_stash = "No local changes" not in stash_result.stdout

        try:
            for idx, h, constraints in work_items:
                co = run(["git", "checkout", "--force", h])
                if co.returncode != 0:
                    print(f"  [{idx:02d}] {h} — checkout failed, skipping")
                    continue

                enrich_with_source(constraints)
                # Re-create dump_dir (checkout may have removed it)
                dump_dir.mkdir(parents=True, exist_ok=True)
                save_sources(dump_dir, idx, h, constraints)

                n_expr = sum(1 for e in constraints.values() if e.source_expr)
                print(f"  [{idx:02d}] {h} — {n_expr}/{len(constraints)} expressions extracted")
        finally:
            print("\nRestoring working tree...")
            run(["git", "checkout", "--force", original_ref])
            if had_stash:
                pop = run(["git", "stash", "pop"])
                if pop.returncode != 0:
                    print(f"WARNING: git stash pop failed:\n{pop.stderr}", file=sys.stderr)

        print(f"Source sidecars saved in: {dump_dir}")
        return

    # --- Validate args for build mode ---
    if not args.start_commit or not args.end_commit:
        parser.error("start_commit and end_commit are required (unless --regenerate or --extract-sources)")

    # --- Save current state ---
    original_ref = run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if original_ref == "HEAD":
        original_ref = run(["git", "rev-parse", "HEAD"]).stdout.strip()

    recorder_path = REPO_ROOT / RECORDER_REL
    if not recorder_path.exists():
        print(f"Error: {RECORDER_REL} not found. Run from the branch that has it.", file=sys.stderr)
        sys.exit(1)
    recorder_content = recorder_path.read_text()

    # Stash uncommitted work (including untracked files)
    print("Saving working tree...")
    stash_result = run(["git", "stash", "--include-untracked", "-m", "diff_constraints: auto-save"])
    had_stash = "No local changes" not in stash_result.stdout

    # --- Commit list ---
    commits = get_commit_list(args.start_commit, args.end_commit)
    if not commits:
        print("No commits in range.", file=sys.stderr)
        if had_stash:
            run(["git", "stash", "pop"])
        sys.exit(1)
    print(f"Processing {len(commits)} commits...\n")

    # Resolve start/end for the comment (short hashes)
    start_short = short_hash(commits[0])
    end_short = short_hash(commits[-1])

    # --- Main loop ---
    dumps = []
    try:
        for idx, full_hash in enumerate(commits):
            h = short_hash(full_hash)
            msg = get_commit_msg(full_hash)
            label = msg[:65] if len(msg) > 65 else msg
            print(f"  [{idx + 1}/{len(commits)}] {h} {label}", end="  ", flush=True)

            co = run(["git", "checkout", "--force", full_hash])
            if co.returncode != 0:
                print("CHECKOUT FAILED")
                dumps.append(CommitDump(h, msg, index=idx, build_failed=True, error="checkout failed"))
                continue

            try:
                inject_recorder(recorder_content)
            except Exception as e:
                print(f"INJECT FAILED ({e})")
                revert_injection()
                dumps.append(CommitDump(h, msg, index=idx, build_failed=True, error=str(e)))
                continue

            dump_file = dump_dir / f"{idx:02d}_{h}_dump.txt"
            ok, output = run_recorder(dump_file)

            if not ok:
                print("BUILD/TEST FAILED")
                err_file = dump_dir / f"{idx:02d}_{h}_error.txt"
                err_file.write_text(output)
                revert_injection()
                dumps.append(CommitDump(h, msg, index=idx, build_failed=True, error="build failed"))
                continue

            constraints = parse_dump(dump_file)

            if not args.no_source:
                enrich_with_source(constraints)
                save_sources(dump_dir, idx, h, constraints)

            base_n = sum(1 for k in constraints if k.startswith("base:"))
            ext_n = sum(1 for k in constraints if k.startswith("ext:"))
            print(f"OK  {base_n} base + {ext_n} ext")

            revert_injection()
            dumps.append(CommitDump(h, msg, index=idx, constraints=constraints))

    finally:
        print("\nRestoring working tree...")
        run(["git", "checkout", "--force", original_ref])
        if had_stash:
            pop = run(["git", "stash", "pop"])
            if pop.returncode != 0:
                print(f"WARNING: git stash pop failed:\n{pop.stderr}", file=sys.stderr)

    # --- Terminal report ---
    print_report(dumps)

    # --- Write diff files ---
    for i in range(1, len(dumps)):
        prev, curr = dumps[i - 1], dumps[i]
        if prev.build_failed or curr.build_failed:
            continue

        removed, added, changed, unchanged = diff_dumps(prev, curr)
        if not removed and not added and not changed:
            continue

        updates, only_removed, only_added = match_updates(removed, added)

        diff_file = dump_dir / f"{prev.index:02d}_{curr.index:02d}_diff.txt"
        dl = []
        dl.append(f"{prev.commit_hash} -> {curr.commit_hash}: {curr.commit_msg}")
        dl.append(f"Unchanged: {unchanged} | Updated: {len(updates)}"
                  f" | Removed: {len(only_removed)} | Added: {len(only_added)}")
        dl.append("")

        if updates:
            dl.append("UPDATED:")
            for old, new in updates:
                dl.append(f"    {fmt_loc(old)} -> {fmt_loc(new)}")
                if old.source_expr:
                    dl.append(f"      - {old.source_expr}")
                if new.source_expr:
                    dl.append(f"      + {new.source_expr}")
            dl.append("")

        if only_removed:
            dl.append("REMOVED:")
            for e in sorted(only_removed.values(), key=lambda e: (e.file, e.line)):
                dl.append(fmt_entry(e))
            dl.append("")

        if only_added:
            dl.append("ADDED:")
            for e in sorted(only_added.values(), key=lambda e: (e.file, e.line)):
                dl.append(fmt_entry(e))
            dl.append("")

        diff_file.write_text("\n".join(dl))

    # --- Generate PR comment markdown ---
    slug = get_repo_slug()
    comment_body = generate_pr_comment(
        dumps, start_short, end_short, slug=slug, dump_dir=dump_dir,
        pr_number=args.pr_comment,
    )
    comment_file = dump_dir / "pr_comment.md"
    comment_file.write_text(comment_body)
    print(f"\nPR comment saved to: {comment_file}")

    # --- Post to GitHub if requested ---
    if args.pr_comment:
        post_or_update_comment(args.pr_comment, comment_body)

    print(f"Dumps saved in: {dump_dir}")


if __name__ == "__main__":
    main()
