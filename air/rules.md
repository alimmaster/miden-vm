# AIR Constraint Rules

Rules for writing constraint code in `air/src/constraints/`.
All transformations must be semantics-preserving: the resulting constraint polynomials
must be equivalent (zero on the same inputs). Sign flips (e.g. `1 - x` vs `x - 1`) are
acceptable when using semantic assertions like `assert_one`.

## AirBuilder Background

Constraint functions are generic over `LiftedAirBuilder` (defined in `../p3-miden/p3-miden-lifted-air/src/builder.rs`), a composite super-trait:
`AirBuilder + ExtensionBuilder + PermutationAirBuilder + PeriodicAirBuilder`.
All constraint functions should bound on `LiftedAirBuilder`, not individual sub-traits.

The underlying traits are defined in `../plonky3/air/src/air.rs`. Field algebra traits
(`PrimeCharacteristicRing`, `Field`, `ExtensionField`, `Algebra`) are in `../plonky3/field/src/field.rs`.
All `../` paths in this document are relative to the repository root.

### Key Source Locations (use these paths directly — do not search)
- `LiftedAirBuilder`: `../p3-miden/p3-miden-lifted-air/src/builder.rs`
- `AirBuilder` (`Var`, `Expr`): `../plonky3/air/src/air.rs`
- `PrimeCharacteristicRing`, `Field`, `Algebra`: `../plonky3/field/src/field.rs`
- `BoolNot`: `air/src/constraints/utils.rs`
- `QuadFeltExpr` / `QuadFeltAirBuilder`: `air/src/constraints/ext_field.rs`
- `Felt` constants: `air/src/constraints/constants.rs`

### Types

`AB::F` is the base field. `AB::Var` is a base-field trace value (`Copy`). `AB::Expr` is an expression over `F` (not `Copy`). Arithmetic on `Var` or `F` values automatically produces `Expr`.

`AB::EF` is the extension field. `AB::VarEF` is an extension-field trace value (`Copy`). `AB::ExprEF` is an expression over `EF` (not `Copy`).

Public values are `F`. Challenges are `EF`. Main trace columns yield `Var`. Permutation trace columns yield `VarEF`. Periodic columns yield `PeriodicVar` (`Copy`, converts to `Expr`).

Clones on `Expr` do not affect runtime performance — constraint evaluation never allocates. However, unnecessary `.clone()` and `.into()` calls add noise and should be eliminated for readability. Since `Var` and `F` are `Copy`, never `.clone()` them. Strip `.into()` on `Var` — keep named bindings but omit type annotations and `.into()`:
```rust
// BAD
let s0: AB::Expr = local.chiplets[0].clone().into();
// GOOD
let s0 = local.chiplets[0];
```

Keep `.into()` only when arithmetic requires `Expr` (e.g., `PeriodicVar` in expressions)
or when building `[AB::Expr; N]` arrays used in multiple arithmetic expressions.
If an array is used only once, convert at the use site instead of pre-converting.

### Assertion Methods

All assertions call `assert_zero` internally:
- `assert_eq(a, b)` = `assert_zero(a - b)`
- `assert_one(x)` = `assert_zero(x - 1)`
- `assert_bool(x)` = `assert_zero(x * (x - 1))` — use for any value that must be 0 or 1
- `assert_bools(array)` = batch `assert_bool` — use only when the array already exists
- `assert_zero_ext`, `assert_eq_ext`, `assert_one_ext` = extension field equivalents
- `assert_eq_quad(lhs, rhs)` — component-wise equality on `QuadFeltExpr` limbs

### `when()` and `FilteredAirBuilder`

`builder.when(condition)` returns a `FilteredAirBuilder` that caches `condition: AB::Expr`. Conditions are always base-field expressions. The constraint is active (enforced) whenever the condition is nonzero — in practice conditions are binary, but `when()` does not require this and you should not insert a bool check on the condition. `builder.when_transition()` is equivalent to `builder.when(builder.is_transition())`. Same for `when_first_row()` and `when_last_row()`.

The `FilteredAirBuilder` for extension constraints multiplies `ExprEF * Expr` — the base-field condition scales the extension expression. This is cheaper than `ExprEF * ExprEF` because extension field arithmetic costs more. Never promote a condition to extension field.

A gate can be a sum of flags (e.g. `f_a + f_b + f_c`). When the summands are mutually
exclusive (at most one nonzero at a time), the sum is still binary and is a valid `when()`
condition meaning "when any of these is active". Document mutual exclusivity when using
flag sums as conditions. `.not()` on a provably-binary sum is valid.

**Multiplication cost:** The first `when()` is free (just stores the condition). Each
additional chained `when()` adds one field multiplication (computes the compound
condition). `assert_zero(x)` on a `FilteredAirBuilder` adds one more multiplication
(`condition * x`). Minimize duplicated multiplications by scoping shared compound
conditions into a builder variable (see Rule 3).

---

## Design Philosophy

These principles guide all constraint transformations.

### P1. Separation of "when" and "what"

Assertions contain ONLY the constrained expression (the "what"). All gates, flags,
and conditions go in `when()` clauses (the "when"). The `when()` reads as: "this
assertion holds when all conditions are true."

```rust
// BAD: flag mixed into assertion body
builder.assert_zero(flag * (a - b));
// GOOD: flag in when(), assertion is just the check
builder.when(flag).assert_eq(a, b);
```

This applies to ALL constraints, including single all-row constraints. The only
expressions that belong in the assertion body are the values being constrained.

### P2. Decompose into `when()` chains (single assertions only)

For **single-assertion chains**, decompose into `.when(f1).when(f2)`:
```rust
// Single assertion — chain decomposes naturally
builder.when(ace_flag).when(f_read).assert_eq(x, y);
```

For **scoped builders** (`let builder = &mut builder.when(...)`), use exactly one
builder binding per scope level. If multiple conditions are needed, either inline
the multiplication (when it fits on one line) or use a gate variable:
```rust
// Inline multiplication — OK when it fits on one line
let builder = &mut builder.when(hasher_flag * f_abp);

// Gate variable — when the expression is long or has a meaningful name
let within_section_gate = ace_transition * f_next * sstart.not();
let builder = &mut builder.when(within_section_gate);
```

When all constraints in a function share a common gate, use a **descriptive name**
for the builder to make the scope clear:
```rust
// All constraints active during Merkle absorb.
let absorb_builder = &mut builder.when(hasher_flag * f_absorb);
absorb_builder.assert_zero(cap);
{
    let builder = &mut absorb_builder.when(b.not());  // nested scope shadows builder
    builder.assert_eq(rate0_next, digest);
}
```

**Never** write consecutive builder re-bindings:
```rust
// BAD: consecutive re-bindings
let builder = &mut builder.when(f1);
let builder = &mut builder.when(f2);   // ← never do this
```

Nesting is fine — each inner `{}` block gets its own builder:
```rust
let builder = &mut builder.when(outer);
{
    let builder = &mut builder.when(inner);  // OK: nested scope
    builder.assert_eq(a, b);
}
```

### P3. Factor aggressively into `when()`

If any factor in `assert_zero(a * b * c)` can be read as a condition ("when a is
true, then b*c must be zero"), factor it into `when()`:

```rust
// "when sstart=1 in current row, sstart_next must be 0"
builder.when(ace_transition).when(sstart).assert_zero(sstart_next);
```

Only keep truly irreducible expressions in the assertion body — those where no single
factor controls activation:
```rust
// ternary validity: op must be -1, 0, or 1. No factor is a condition.
builder.assert_zero(op.clone() * (op.clone() - F_1) * (op + F_1));
```

**DANGER — gate vs formula test:** Use the symmetry test to decide if a factor is a
gate (safe to extract) or participates in the formula (unsafe). Set the candidate
factor to 0: if the constraint becomes trivially satisfied, it's a gate. If the
mathematical meaning changes, it's intrinsic — do NOT extract it.

```rust
// GATE: setting sp=0 → constraint trivially 0 (inactive). Safe to extract.
builder.assert_zero(sp * delta_gc * (delta_gc - F_1));
// → builder.when(sp).assert_bool(delta_gc);

// FORMULA: setting flag_sum=0 → 0 - 0 = 0 (trivially true, but this HIDES
// the case where expected ≠ 0). The factor scales 'actual', it's part of
// the algebraic relationship. Do NOT extract.
builder.assert_zero(actual * flag_sum - expected);
// → KEEP AS IS. flag_sum is intrinsic, not a gate.
```

### P4. Condition ordering

Outermost scope first → module/chiplet flag → specific sub-flag → assertion.
Reads like narrowing from general to specific.

```rust
builder
    .when_transition()          // row scope (most general)
    .when(ace_flag)             // chiplet scope
    .when(f_read)               // specific sub-flag
    .assert_eq(selected, n_eval); // assertion
```

### P5. Minimize multiplications via scoping

When 2+ assertions share a compound condition, pre-multiply into a gate variable
and scope the builder:

```rust
// 2 constraints share is_transition * sp: scope saves 1 mult.
// Group count: within-span rules.
{
    let gate = builder.is_transition() * sp;
    let builder = &mut builder.when(gate);
    builder.assert_bool(delta_gc.clone());
    builder.when(delta_gc).when(is_push.not()).assert_zero(h0);
}
```

For a single simple flag used by only 2 assertions, just repeat `.when(flag)` — no
multiplication is saved by scoping, and standalone chains are clearer.

**One builder binding per scope.** Never write consecutive `let builder = &mut builder.when(...)`.
Use a gate variable to combine conditions for one binding, or use nested `{}` blocks
for separate scope levels.

### P6. Comments: never remove, always preserve

Comments may be rewritten but NEVER deleted. Every constraint description and section
header from the original code must have a corresponding comment in the new code.

- **Strategy comments** (e.g., "Use combined gates to share...") → replace with
  **intent comments** that describe what the constraints enforce.
- **Constraint descriptions** (e.g., "Constraint 1: Index must be 0") → preserve as-is.
- **Section headers** (`// =====...=====` blocks) → preserve and relocate as needed.
- When inlining a function, convert its docstring into a section header + sub-comment.

### P7. Phased commits

Apply rules in phased commits, each independently reviewable:

1. **Rules.md update** — this document
2. **Constraint transformations** — Rules 1+2+3 combined in-place. Small reordering
   (~10 lines) is OK when the transformation is clearly equivalent.
3. **Function-wide hoisting** — separate commit. Where ALL constraints in a function
   share a gate, hoist with descriptive builder name.
4. **Helper inlining** — separate commit. Convert docstrings to section headers.

---

## Rules

Read the full function before editing any rule.

### Rule 1. Use semantic assertion methods

| Before | After |
|--------|-------|
| `assert_zero(a - b)` | `assert_eq(a, b)` |
| `assert_zero(x * (x - 1))` | `when(gate).assert_bool(x)` |
| `assert_zero(1 - x)` | `assert_one(x)` |
| `assert_zero_ext(lhs - rhs)` | `assert_eq_ext(lhs, rhs)` |
| `assert_zeros(array::from_fn(\|i\| gate * (a[i] - b[i])))` | scoped `when(gate)` + `for` loop with `assert_eq` |
| `for expr in [a - b, c - d] { assert_zero(gate * expr) }` | unroll into explicit `assert_eq`/`assert_zero` |
| closure generating constraint exprs | explicit `when().assert_*()` calls |

`assert_bool(x)` is for any value that must be 0 or 1 — not just flag columns.
Use `assert_bools(array)` only when the array already exists; don't construct one
just for the batch check. Extension field constraints follow the same rules.

### Rule 2. Factor gates with `when()`

ALWAYS extract gates into `when()` — even for single, all-row constraints. Decompose
compound gates: `.when(f1).when(f2)` not `.when(f1 * f2)`.

For transition constraints with a per-constraint flag, use `when_transition()` chaining:
```rust
builder.when_transition().when(is_dup).assert_eq(s0_next, s0);
builder.when_transition().when(is_dup1).assert_eq(s0_next, s1);
```

Use pre-computed combined flags (e.g. `op_flags.right_shift()`) directly in `when()`.
Keep named derived flags (e.g., `let f_read = sblock.not()`) for readability.

**Do NOT apply when the product is the constraint itself (intrinsic, not a gate).** Use
the symmetry test from P3: set the candidate factor to 0 — if the constraint becomes
trivially true but hides a real case, the factor is intrinsic.

```rust
// Intrinsic: ternary validity, conditional inverse, formula participation
builder.assert_zero(op * (op - F_1) * (op + F_1));
builder.assert_zero(eq_diff * s0_next);
builder.assert_zero(actual * flag_sum - expected);
```

**Do NOT apply to bus accumulators** — neither factor is a selector:
```rust
builder.when_transition().assert_eq_ext(p_next * req, p_local * resp);
```

### Rule 3. Scoped builders and nesting

When 2+ assertions share a compound condition, use a scoped builder to cache the
multiplication. Always wrap scoped builders in `{}` blocks. Add a comment before the
brace describing BOTH the gate condition AND what the constraints enforce.

**One builder binding per scope level.** If multiple conditions are needed, pre-multiply
into a gate variable. Never write consecutive `let builder = &mut builder.when(...)`.

```rust
// Single constraint — chain directly, no block needed.
builder.when(flag).assert_eq(a_next, a);

// Multiple constraints sharing a compound gate:
// ACE transition constraints for context/clock continuity.
{
    let gate = builder.is_transition() * ace_flag;
    let builder = &mut builder.when(gate);
    builder.assert_eq(ctx_next, ctx);
    builder.assert_eq(clk_next, clk);
}
```

For loops with a shared gate, scope outside the loop to cache the gate:
```rust
// Capacity reset to zero during absorb.
{
    let gate = hasher_flag * f_absorb;
    let builder = &mut builder.when(gate);
    for i in 0..4 {
        builder.assert_zero(cap_next[i]);
    }
}
```

Nesting for logically separate sub-groups — each inner `{}` block gets one builder:
```rust
// Digest placement depends on direction bit b.
{
    let gate = hasher_flag * f_absorb;
    let builder = &mut builder.when(gate);
    // b=0: digest goes to rate0.
    {
        let builder = &mut builder.when(b.not());
        for i in 0..4 {
            builder.assert_eq(rate0_next[i], digest[i]);
        }
    }
    // b=1: digest goes to rate1.
    {
        let builder = &mut builder.when(b);
        for i in 0..4 {
            builder.assert_eq(rate1_next[i], digest[i]);
        }
    }
}
```

**`when_transition()` usage:**
For single assertions, chain directly:
```rust
builder.when_transition().when(flag).assert_eq(a, b);
```

When ALL constraints in a section are transition constraints, scope once at the top:
```rust
// All remaining constraints are transition constraints.
let builder = &mut builder.when_transition();
builder.when(flag_a).assert_eq(x, y);
builder.when(flag_b).assert_eq(a, b);
```

Do NOT use the two-step binding pattern:
```rust
// BAD — two-step binding
let mut tb = builder.when_transition();
let builder = &mut tb;
```

**Naming:** Shadow `builder` for inner scopes. Use descriptive names (e.g., `hasher`)
only for function-wide hoists in long functions that don't pass `builder` to
sub-functions. This should be rare and done in a separate commit.

**Definitions inside scopes:** Expressions that don't depend on `builder` can be
computed inside a `when()` scope — the scope only affects assertions. Keep definitions
near the constraints that use them. Only hoist what requires the un-scoped builder
(e.g., `builder.periodic_values()`).

### Rule 4. Inline small helpers

Inline functions that: forward to one constraint function with unpacked fields,
wrap a single assertion, or are ≤15 lines. Keep functions that issue many constraints
or are called from multiple sites.

When inlining, convert the function's docstring to a section header:
```rust
// ===========================================
// Group count (gc) constraints
// ===========================================
// Enforces that gc stays the same or decrements
// by 1 inside spans.
```

Use section comments and `{}` blocks for grouping inlined code.

### Rule 5. Prefer intent comments, preserve all comments

Comments may be rewritten but NEVER deleted. Every constraint description and section
header must survive refactoring.

New comments should explain *why*, not restate the formula:
```rust
// Inside a span, gc can only stay the same or decrement by 1.
builder.when_transition().when(sp).assert_bool(delta_gc);
```

Replace strategy comments with intent comments:
```rust
// BEFORE: "Use a combined gate to share `hasher_flag * f_abp` across all 4 lanes."
// AFTER:  "ABP capacity must be preserved across permutation."
```

### Rule 6. Keep trace columns as `AB::Var`

Column reads return `AB::Var` (`Copy`). Don't call `.clone()` on `Var` values.
Don't convert to `AB::Expr` at the binding site — strip `.into()` and type annotations:

```rust
// BAD
let s0: AB::Expr = local.chiplets[0].clone().into();
// GOOD
let s0 = local.chiplets[0];
```

**Keep `.into()`:** `PeriodicVar` reads (need `.into()` for arithmetic), function args
expecting `AB::Expr` (move `.into()` to call site), `.not()` calls (`BoolNot` needs
`PrimeCharacteristicRing`, use `AB::Expr::from(x).not()`).

**Array conversions:** Keep `let a_bits: [AB::Expr; 4] = cols.a_bits.map(Into::into)`
only when the array is used in 2+ arithmetic expressions. If used once, convert at the
use site.

### Rule 7. Constants and `.not()`

`Felt` constants live in `constants.rs`. Always use `F_1`, `F_16`, etc. directly —
never bind to local variables, never use `AB::Expr::ONE`.

Exception: when `Felt` is on the left side of a subtraction with an `Expr` on the
right (`F_1 - expr`), the type may not resolve. Use `AB::Expr::ONE - expr` in these
cases only.

`.not()` (from `BoolNot` in `utils.rs`) is ONLY for known-boolean values (0 or 1).
For non-boolean expressions, use `AB::Expr::ONE - x` explicitly.

Use `.double()` for doubling (not `* F_2`). Cache `.not()` when reused:
```rust
let not_hs1 = hs1.not();
```

### Rule 8. Section headers

Use `// ===...===` section headers within function bodies for visual grouping.

```rust
// =============================================
// Binary constraints
// =============================================
```

---

## Semantics Preservation Checklist

After applying rules, verify:

1. `when(g).assert_zero(x)` == `assert_zero(g * x)` — polynomial equivalence
2. `when_transition().when(f)` == `when(is_transition * f)` — transition equivalence
3. No dropped constraints — every original `assert_zero` has a corresponding assertion
4. No added constraints
5. Degree preserved — `when(a).when(b).assert_zero(x)` has degree `deg(a) + deg(b) + deg(x)`
6. No dropped comments — every constraint description and section header survives
7. Gate vs formula verified — no intrinsic factor was extracted into `when()`

---

## Applying Rules

All rules should be applied together per constraint site, combining mechanical and
judgment aspects. Read the full function before editing.

If subagents are used, paste the "Types" and "Key Source Locations" sections into the
agent prompt — agents cannot read this file automatically. Never run `cargo check/build`
inside agents; do one check after all edits are complete.
