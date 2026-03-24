# Constraint Style Q&A

This document captures the design decisions from the constraint style review session
(2026-03-24). It preserves the WHY behind each rule in `rules.md` so future sessions
can understand the intent without re-asking.

## Background

The constraint code in `air/src/constraints/` enforces zero-knowledge proof soundness.
Incorrect transformations can silently break security — a malicious prover could forge
proofs if a constraint is weakened. All transformations must be semantics-preserving.

A previous developer applied `rules.md` too aggressively: hoisting flags to function
tops, reordering code, scoping `when_transition()` (banned), and changing function
signatures. The result was semantically correct but hard to review — the diff was too
large and structural to verify by inspection.

## Core Principle: Separation of "when" and "what"

**Q: Should assertions contain ONLY the constrained expression, with all flags in `when()`?**

A: Yes. The assertion body is the "what" (the value that must be zero). All gates,
flags, and conditions are the "when" (conditions under which the check applies).
This makes every constraint read as: "when [conditions], [expression] must be zero."

**Why:** Pre-multiplying flags into the assertion body hides which factors are
conditions vs. constrained values. For security-critical code, this ambiguity is
dangerous — reviewers must be able to see at a glance what's being checked vs. what
activates the check.

## when() Decomposition

**Q: Should compound gates be pre-multiplied or decomposed into when() chains?**

A: Always decompose. `.when(f1).when(f2)` not `.when(f1 * f2)`. Each condition is
visually separate.

**Why:** Pre-multiplied gates hide which conditions are independent. Decomposed chains
read as "when f1 AND when f2" — each condition is explicit.

**Q: Even for single-use compound gates?**

A: Yes. The readability benefit outweighs the minimal code change.

**Q: What about scoping for reuse?**

A: When a compound condition (2+ flags) is shared by 2+ assertions, pre-multiply
into a gate variable and scope the builder:
```rust
let gate = f1 * f2;
let builder = &mut builder.when(gate);
```
This caches the compound multiplication. Never chain `.when()` in a scoped builder
binding — only one `let builder = &mut builder.when(...)` per scope level. Chaining
`.when(f1).when(f2)` is for single-assertion chains only. For scoped builders, use
a gate variable. Use nested `{}` blocks for additional scope levels.

## Multiplication Cost Model

**Q: What is the performance cost of when() chaining?**

A: The first `when()` is free — it just stores the condition expression. Each additional
chained `when()` adds one field multiplication (computing the compound condition).
Then `assert_zero(x)` on the `FilteredAirBuilder` adds one more multiplication
(`compound_condition * x`).

**Q: When does scoping save multiplications?**

A: Only when a compound condition is reused by 2+ assertions. For a single simple
flag used by 2 assertions, repeating `.when(flag)` twice costs the same as scoping
(1 mult each, no compound to cache). So don't scope single flags.

**Q: What about `when_transition().when(flag)` vs `when(is_transition * flag)`?**

A: Both have the same cost (2 mults). But `when_transition().when(flag)` is preferred
because it decomposes into two separate conditions. Note: `when_transition()` is
exactly `when(builder.is_transition())`.

## Gate vs Formula (DANGER)

**Q: How do you tell if a factor is a gate (safe to extract) or intrinsic (unsafe)?**

A: Symmetry test. Set the candidate factor to 0:
- If the constraint becomes trivially satisfied → it's a **gate** (controls activation)
- If the mathematical meaning changes → it's **intrinsic** (participates in the formula)

**Concrete example of the danger:**
```
assert_zero(actual * flag_sum - expected)
```
Setting `flag_sum = 0` gives `0 - 0 = 0` (trivially true). But this HIDES the case
where `expected ≠ 0` — the constraint is supposed to verify `actual * flag_sum = expected`.
Factoring to `when(flag_sum).assert_eq(actual, expected)` gives a DIFFERENT polynomial:
`flag_sum * (actual - expected) ≠ actual * flag_sum - expected` (unless `flag_sum` is
always 0 or 1).

**Why this matters:** If `flag_sum` could ever be 2 (e.g., non-exclusive flags), the
factored form would accept inputs the original rejects. This is a soundness bug.

## Transition Constraints

**Q: What style for transition constraints with per-constraint flags?**

A: `builder.when_transition().when(flag).assert_eq(...)` — chain directly.

**Q: Can we scope `when_transition()` into a builder variable?**

A: NO. This is explicitly banned:
```rust
// BANNED
let mut tb = builder.when_transition();
let builder = &mut tb;
```
It shadows `builder` and adds confusing indirection. Use chaining instead.

## Scoped Builders

**Q: How many builder bindings per scope?**

A: Exactly one `let builder = &mut builder.when(...)` per scope level. If you need
multiple conditions, pre-multiply into a gate variable:
```rust
let gate = f1 * f2;
let builder = &mut builder.when(gate);
```
Never write consecutive builder re-bindings. Use nested `{}` blocks for additional
scope levels.

**Q: When to use `{}` blocks around scoped builders?**

A: Always. Every scoped builder gets `{}` with a comment before the brace describing
the condition and what the constraints enforce.

**Q: Shadow `builder` or use descriptive names?**

A: Shadow for inner scopes. Descriptive names (e.g., `hasher`) only for function-wide
hoists in long functions that don't pass builder to sub-functions. This is rare and
must be in its own commit.

**Q: How deep can when() nesting go?**

A: No limit via nested `{}` blocks. Each block gets one builder binding.

## .not() and Constants

**Q: When can `.not()` be used?**

A: Only on known-boolean values (0 or 1). For non-boolean expressions, use
`AB::Expr::ONE - x`. `.not()` on a sum of mutually exclusive flags is OK — document
the exclusivity.

**Q: `F_1` or `AB::Expr::ONE`?**

A: Always `F_1` from `constants.rs`. Exception: `F_1 - expr` may not type-check
when `Felt` is on the left side of a subtraction with `Expr` on the right. Use
`AB::Expr::ONE - expr` in that case only.

**Q: `.double()` or `* F_2`?**

A: Use `.double()`.

## Type Cleanup

**Q: Should we strip `.clone()` on `Var`?**

A: Yes. `Var` is `Copy`, so `.clone()` is noise.

**Q: Should we strip `.into()` on `Var`?**

A: Strip `.into()` and type annotations. Keep named bindings: `let s0 = cols.s0;`
not `let s0: AB::Expr = cols.s0.clone().into();`.

**Q: What about array `.into()` conversions?**

A: Keep `let bits: [AB::Expr; 4] = cols.bits.map(Into::into)` only when the array
is used in 2+ arithmetic expressions. If used once, convert at the use site.

## Comments

**Q: Can comments be removed?**

A: NEVER. Comments can be rewritten but never deleted. Strategy comments
("Use combined gates to share...") become intent comments ("Capacity must reset
to zero during absorb"). Constraint descriptions and section headers are preserved.

**Q: What about comments on scoped builder blocks?**

A: Comment before the `{` brace: describe BOTH the gate condition AND what the
constraints inside enforce.

**Q: Comments on unconditional constraints?**

A: No special comment needed. The absence of `when()` already signals "all rows."

## Patterns

**Q: `assert_zeros(array::from_fn(...))`?**

A: Convert to `for` loop with scoped `when()` builder.

**Q: `for expr in [a-b, c-d] { assert_zero(gate * expr) }`?**

A: Unroll into explicit individual `assert_eq`/`assert_zero` calls.

**Q: Closures that generate constraints?**

A: Convert to explicit `when().assert_*()` calls.

**Q: `assert_bools(array)`?**

A: Use only when the array already exists. Don't construct one just for the check.

**Q: `assert_bool(x)` scope?**

A: Any value that must be 0 or 1, not just flag columns.

**Q: Named derived flags like `let f_read = sblock.not()`?**

A: Keep — named intermediates document the flag's meaning.

**Q: Helper functions that compute formulas?**

A: Keep non-trivial helpers (binary_or, horner_eval_bits). Plan to elevate to a
BoolAlgebra trait (binary_or, binary_and, conditional_select) in a future commit.

**Q: Section helper functions in decoder (5-15 constraints each)?**

A: Inline into parent. Convert docstring to section header. SEPARATE commit.

## Commit Phasing

1. Rules.md update
2. BoolAlgebra trait (at the start, so later commits can use it)
3. Constraint transformations (Rules 1+2+3 combined, all files)
4. Function-wide hoisting (separate)
5. Helper inlining (separate)

## File Scope

ALL constraint files in `air/src/constraints/`, not just the ones in the original
staged diff.
