# Constraint Equivalence Report — Instructions

## Overview

### What the tool does

`scripts/diff_constraints.py` tracks which AIR constraint polynomials changed
across a range of git commits. It works by:

1. Checking out each commit in the range
2. Injecting the `constraint_recorder.rs` test harness (even into old commits
   that don't have it)
3. Building and running `cargo test constraint_recorder` to evaluate every
   constraint polynomial at a **deterministic random point**
4. Recording the resulting field element as a **fingerprint**
5. Diffing consecutive commit fingerprints to surface changes

### What fingerprints represent

Fingerprints are **polynomial evaluations**, not source text comparisons.

- **Same fingerprint** = same polynomial with very high probability
  (Schwartz-Zippel lemma over a ~64-bit field).
- **Different fingerprint** = the constraint polynomial is **algebraically
  different**. The mathematical expression changed, not just formatting or
  variable names.

The "Expression" column in the PR comment shows source code extracted from the
backtrace location — this is a cosmetic label to help locate the constraint, not
the fingerprint itself.

### What the report covers

- Every constraint polynomial that changed between consecutive commits
  (Updated, Added, Removed)
- Constraint multiplicity changes (same polynomial, different count)
- Human-written remarks from `interpretations.json` explaining why each change
  is correct

### What the report does NOT cover

- Non-constraint code changes: struct definitions, module reorganization,
  imports, trait additions, dead code removal, clone cleanup, etc.
- These must be documented separately in the **PR description**.

The report answers "did any constraint polynomials change?" — it does NOT
describe what all the commits did to the codebase.

## File layout

```
air/constraint_dumps/
  XX_<hash>_dump.txt        # fingerprint dump from the recorder test
  XX_<hash>_sources.json    # JSON sidecar: {fp_key: {source_expr, file, line, function}}
  XX_YY_diff.txt            # text diff between consecutive dumps (full build only)
  interpretations.json      # human-written remarks per commit (see guide below)
  pr_comment.md             # generated markdown report
```

- **Dump files** contain one line per constraint: `base:<fingerprint_hex> x<count>`
  followed by the backtrace showing which function produced it.
- **Source sidecars** persist the extracted source expressions so `--regenerate`
  can rebuild the PR comment without checking out old commits.
- **Diff files** are only written during a full build run (not by `--regenerate`).
- **`interpretations.json`** is hand-edited; the tool reads it during PR comment
  generation (see [Writing interpretations](#writing-interpretations)).
- **`pr_comment.md`** is generated; do not edit by hand.

## Workflow

### The golden path

1. **Make constraint changes** in one or more commits. Push/finalize them.
   Do NOT amend them after step 2.

2. **Run the tool** to generate dumps and diffs:
   ```bash
   python3 scripts/diff_constraints.py <start_commit> HEAD --no-post
   ```

3. **Inspect** the generated diff files and `pr_comment.md` for correctness.

4. **Write interpretations** in `interpretations.json` (see guide below).

5. **Regenerate** the PR comment with remarks:
   ```bash
   python3 scripts/diff_constraints.py --regenerate --no-post
   ```

6. **Commit artifacts** (dumps, sources, diffs, interpretations, pr_comment.md)
   in a **SEPARATE commit** from the constraint changes.

7. Optionally **post to GitHub**:
   ```bash
   python3 scripts/diff_constraints.py --regenerate --pr-comment <PR_NUMBER>
   ```

### CRITICAL: never commit dumps inside the commit being analyzed

Dump filenames and PR comment links embed the commit hash. If you amend a
constraint commit to include its own dumps, the hash changes, making every
embedded reference stale.

**What goes wrong:**
1. You commit constraint changes → hash is `abc123`
2. You run the tool → generates `44_abc123_dump.txt` and PR comment linking to
   `abc123`
3. You amend the same commit to include the dump files → hash becomes `def456`
4. Now `44_abc123_dump.txt` references a hash that no longer exists. Every
   GitHub link in the PR comment is broken. `--regenerate` will crash because
   `get_commit_msg('abc123')` fails.

**Prevention:** always commit artifacts in a separate, later commit.

### Post-amend recovery

If a constraint commit was amended after dumps were generated:

1. Delete the stale dump and sources files for the old hash
2. Rename or re-run the tool for the affected commit range
3. Update the hash key in `interpretations.json`
4. Regenerate the PR comment with `--regenerate --no-post`

If the constraint code itself didn't change (e.g., the amend only added
documentation), you can rename the files:
```bash
mv air/constraint_dumps/44_<old_hash>_dump.txt    air/constraint_dumps/44_<new_hash>_dump.txt
mv air/constraint_dumps/44_<old_hash>_sources.json air/constraint_dumps/44_<new_hash>_sources.json
```

If the constraint code DID change, re-run the full build for the affected range.

## Command reference

### Full build (default)

```bash
python3 scripts/diff_constraints.py <start> <end> [options]
```

Checks out each commit from `<start>` to `<end>` (inclusive), injects the
recorder, builds, runs the test, extracts source expressions, and generates
dump files, diff files, source sidecars, and `pr_comment.md`.

### Extract sources only (`--extract-sources`)

```bash
python3 scripts/diff_constraints.py --extract-sources
```

Checks out each commit for which a dump exists but a source sidecar is missing.
Reads source files to extract expressions and writes `*_sources.json` sidecars.
No Rust build is performed.

### Regenerate PR comment (`--regenerate`)

```bash
python3 scripts/diff_constraints.py --regenerate [--pr-comment <PR>] [--no-post]
```

Loads existing dump files and source sidecars from the dump directory.
Recomputes diffs in memory. Generates `pr_comment.md`.

**Note:** `--regenerate` does NOT write `*_diff.txt` files — only the full
build path does. If you need diff files, run a full build.

### Flags

| Flag | Description |
|------|-------------|
| `--pr-comment <PR>` | PR number for generating GitHub diff links; also posts the comment unless `--no-post` |
| `--no-post` | Generate `pr_comment.md` locally without posting to GitHub |
| `--no-source` | Skip source expression extraction during full build |
| `--dump-dir <DIR>` | Custom directory for output files (default: `air/constraint_dumps`) |

## Writing interpretations

### File format

`interpretations.json` maps short commit hashes to arrays of remark entries:

```json
{
  "<commit_hash>": [
    {
      "group_remark": "Explanation for all changes in this commit.",
      "applies_to": "all"
    }
  ],
  "<other_hash>": [
    {
      "old_fp": "base:<old_fingerprint>",
      "new_fp": "base:<new_fingerprint>",
      "remark": "Per-constraint explanation (takes priority over group remark)."
    }
  ]
}
```

### Categories of constraint changes

Every fingerprint change falls into one of these categories. Use the
corresponding template for clarity and consistency.

#### 1. Equivalence-preserving refactors

The polynomial is algebraically different but computes the same value on all
valid traces. Examples: sign flips (`assert_zero(1-x)` → `assert_one(x)`),
`when()` extraction, variable renaming, precomputed selector products.

**Template:**
```
"Expression restructured: <old pattern> → <new pattern>. Polynomial
algebraically different but semantically equivalent."
```

#### 2. Guard removal relying on invariants

A guard factor (like `when_transition()`) was removed because a separately
enforced invariant makes the constraint auto-vanish where the guard was active.

**Template:**
```
"Guard `<guard>` removed. Constraint polynomial lost `<factor>`. Soundness
preserved because <invariant> (established in commit `<hash>`). Polynomially
non-equivalent but semantically equivalent under the invariant."
```

#### 3. Intentional non-equivalent changes

The polynomial IS different by design — new constraints, domain separators,
changed semantics. These MUST be flagged explicitly.

**Template:**
```
"**Intentional non-equivalent change.** <description>."
```

#### 4. Source extraction artifacts

The "Expression" column sometimes shows a `let` binding instead of the actual
assertion (see [Known limitations](#known-limitations)). Note this in the
remark so readers aren't confused.

### Common mistakes to avoid

| Mistake | Why it's wrong |
|---------|---------------|
| "Shifting line numbers" | Line number changes do NOT cause fingerprint changes. If the fingerprint changed, the polynomial changed. |
| Inverted causation | Describe what the commit DID (e.g., "removed guard"), not the opposite (e.g., "added gate directly"). |
| Conflating source text and fingerprint | A `let` binding in the Expression column is a source extraction artifact, not the recorded constraint. |
| Failing to flag intentional changes | If the polynomial IS different by design, say so — otherwise reviewers assume it should be equivalent. |
| Unverified semantic claims | "Flags vanish on the last row" is a correctness claim. Verify the specific flag in question before asserting this. |

## Verification checklist

Before finalizing the report, verify:

- [ ] All commit hashes in dump filenames exist in `git log`
- [ ] All commit hashes in `pr_comment.md` resolve (no broken links)
- [ ] `interpretations.json` hash keys match the dump filenames
- [ ] Interpretations distinguish equivalent vs intentional changes
- [ ] No source extraction artifacts reported as constraints without explanation
- [ ] Remarks describe what the commit did, not the inverse
- [ ] Semantic correctness claims are justified (e.g., verify the specific flag
      vanishes on the last row)
- [ ] Artifacts are committed separately from constraint changes (no circular
      hash dependency)
- [ ] `pr_comment.md` range header start/end match actual first/last dump files

## PR comment format

Each commit section with constraint changes looks like:

```markdown
#### `<hash>` <commit message> ([diff](<compare_url>))

> N updated | M unchanged

**Updated:**

| Expression | Remark |
|------------|--------|
| [`chiplets/ace.rs`](<blob_url>) [`builder.assert_zero(...);`](<diff_url>) | Remark text |
```

Where:
- **`<blob_url>`** links to the source file at that commit
- **`<diff_url>`** links to the per-commit diff in the PR
- **`<compare_url>`** links to the full diff between consecutive commits
- The Remark column only appears when `interpretations.json` has entries

Sections with >12 changes are wrapped in a `<details>` block.

## Known limitations

### Source extraction bug (`extract_source_expr`)

The function searches backward (up to 4 lines) from the backtrace line for a
`builder\b` keyword match. When the assertion uses a scoped builder:

```rust
let mut last = builder.when_last_row();
last.assert_one(local.chiplets[0]);
```

...the search finds `builder.when_last_row()` as the expression start instead
of `last.assert_one(...)`. This produces misleading entries in the PR comment
that show `let` bindings instead of assertions.

**Workaround:** note the artifact in `interpretations.json`. A future fix
should detect `let` bindings and continue searching forward.

### `--regenerate` does not write diff files

Only the full build path writes `XX_YY_diff.txt` files. If you use
`--regenerate`, diffs are computed in memory for the PR comment but not
persisted to disk.

### Merge commit handling

No special filtering for merge commits. If the commit range includes merges,
they are analyzed like any other commit. This may produce large or confusing
diffs.

### Recorder injection on old commits

The `constraint_recorder.rs` file is injected into each checked-out commit.
If the `AirBuilder` trait interface changed significantly across the commit
range, the injection may fail to compile on some commits.
