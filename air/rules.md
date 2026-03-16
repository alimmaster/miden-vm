# AIR Constraint Rules

Rules for writing constraint code in `air/src/constraints/`.
All transformations must be semantics-preserving: the resulting constraint polynomials
must be algebraically identical.

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

Clones on `Expr` do not affect runtime performance — constraint evaluation never allocates. However, unnecessary `.clone()` and `.into()` calls add noise and should be eliminated for readability. Since `Var` and `F` are `Copy`, using them directly avoids the noise entirely.

### Assertion Methods

All assertions call `assert_zero` internally:
- `assert_eq(a, b)` = `assert_zero(a - b)`
- `assert_one(x)` = `assert_zero(x - 1)`
- `assert_bool(x)` = `assert_zero(x * (x - 1))`
- `assert_bools(array)` = batch `assert_bool`
- `assert_zero_ext`, `assert_eq_ext`, `assert_one_ext` = extension field equivalents
- `assert_eq_quad(lhs, rhs)` — component-wise equality on `QuadFeltExpr` limbs

### `when()` and `FilteredAirBuilder`

`builder.when(condition)` returns a `FilteredAirBuilder` that caches `condition: AB::Expr`. Conditions are always base-field expressions. The constraint is active (enforced) whenever the condition is nonzero — in practice conditions are binary, but `when()` does not require this and you should not insert a bool check on the condition. `builder.when_transition()` is equivalent to `builder.when(builder.is_transition())`. Same for `when_first_row()` and `when_last_row()`.

The `FilteredAirBuilder` for extension constraints multiplies `ExprEF * Expr` — the base-field condition scales the extension expression. This is cheaper than `ExprEF * ExprEF` because extension field arithmetic costs more. Never promote a condition to extension field.

A gate can be a sum of flags (e.g. `f_a + f_b + f_c`). When the summands are linearly independent, at most one is nonzero at a time, so the sum is still binary. Pay special attention when summing flags to verify mutual exclusivity — if two summands can be nonzero simultaneously the sum is no longer binary and the gate may scale the constraint rather than just activating it.

Chaining `when(a).when(b).assert_zero(x)` produces `a * (b * x)` — same polynomial as `assert_zero(a * b * x)`. When multiple constraints share the same compound condition, use a scoped builder (`let builder = &mut builder.when(gate)`) so the condition is applied once per assertion. Nest scoped builders to express logically separate groups of constraints under a shared outer gate.

---

## Applying Rules

**Mechanical rules (1, 6, 7, 8):** grep/comby for the pattern, edit matches. No need to
read entire files. For bulk application, use the `comby-rust-refactor` agent with the
comby patterns listed below.

**Judgment rules (2, 3, 4, 5):** read the full function before editing.

If subagents are used, paste the "Types" and "Key Source Locations" sections into the
agent prompt — agents cannot read this file automatically. Never run `cargo check/build`
inside agents; do one check after all edits are complete.

---

## Mechanical Rules

### Rule 1. Use semantic assertion methods

**Comby:** `comby 'builder.assert_zero(:[a] - :[b])' 'builder.assert_eq(:[a], :[b])' -matcher .rs`

| Before | After |
|--------|-------|
| `assert_zero(a - b)` | `assert_eq(a, b)` |
| `assert_zero(x * (x - 1))` | `assert_bool(x)` |
| `assert_zero(1 - x)` | `assert_one(x)` |
| `assert_zero_ext(lhs - rhs)` | `assert_eq_ext(lhs, rhs)` |
| loop of `assert_bool` | `assert_bools(array)` |

### Rule 6. Keep trace columns as `AB::Var`

**Grep:** `: AB::Expr.*\.into\(\)` or `\.clone\(\)\.into\(\)`

Column reads return `AB::Var` (`Copy`). Don't convert to `AB::Expr` at binding site.

```rust
// BAD
let s0: AB::Expr = local.chiplets[0].clone().into();
// GOOD
let s0 = local.chiplets[0];
```

**Do NOT change:**
- `PeriodicVar` reads — need `.into()` for arithmetic
- Function args expecting `AB::Expr` — move `.into()` to call site
- `.not()` calls — `BoolNot` needs `PrimeCharacteristicRing`, use `AB::Expr::from(x).not()`

### Rule 7. Inline constants, never bind them

**Grep:** `let.*=.*F_\d\|let.*= F_`

`Felt` constants live in `constants.rs`. Use directly, never bind to local variables.
For `1 - x` patterns, use `.not()` from `BoolNot` (in `utils.rs`).

```rust
delta_gc.clone() - F_1     // direct constant use
s3_next.not()               // BoolNot for 1-x pattern (works on ExprEF too)
```

Cache `.not()` when reused:
```rust
let not_hs1 = hs1.not();
let f_bp = hasher_active * s0 * not_hs1.clone() * not_hs2.clone();
let f_mp = hasher_active * s0 * not_hs1.clone() * s2;
```

### Rule 8. Section headers

Use `// ===...===` section headers within function bodies for visual grouping.

```rust
// =============================================
// Binary constraints
// =============================================
builder.assert_bools(cols.op_bits);
```

---

## Judgment Rules

Read the full function before editing.

### Rule 2. Factor gates with `when()`

When `assert_zero(gate * expr)` and `gate` is a binary selector, replace with
`when(gate).assert_zero(expr)` (or appropriate semantic assertion).

```rust
// BEFORE
builder.assert_zero(flag * x * (x - 1));
// AFTER
builder.when(flag).assert_bool(x);
```

Use pre-computed combined flags (e.g. `op_flags.right_shift()`) directly in `when()`.

**Do NOT apply when the product is the constraint itself (intrinsic, not a gate):**
```rust
assert_zero(eq_diff * s0_next);           // conditional inverse
assert_zero(op * (op - 1) * (op + 1));   // ternary validity
assert_zero(sstart * sblock);             // mutual exclusion
```

**Do NOT apply to bus accumulators** — neither factor is a selector:
```rust
builder.when_transition().assert_eq_ext(p_next * req, p_local * resp);
```

**Gate vs intrinsic:** a factor is a gate if it's a binary selector controlling activation.
It's intrinsic if it participates in the algebraic relationship.
```rust
builder.when(sp).assert_bool(delta_gc);                        // CORRECT: sp is a gate
builder.when(sp).when(delta_gc).assert_zero(delta_gc - 1);    // WRONG: splits the bool check
```

### Rule 3. Scoped builders and nesting

When multiple constraints share a gate, use a scoped builder. Use nested `{}` blocks
to express logically separate groups under a shared outer gate. Only open a new block
when constraints form a distinct logical group — do not nest just because a variable
is reused.

```rust
// Single constraint — chain directly, no block needed
builder.when(flag).assert_eq(a_next, a);

// Multiple constraints under one gate — scoped block
{
    let builder = &mut builder.when(flag);
    builder.assert_eq(a, b);
    builder.assert_eq(c, d);
}
```

Nesting for logically separate sub-groups:

```rust
{
    let builder = &mut builder.when(hasher_flag);

    // Init step
    {
        let builder = &mut builder.when(is_init);
        for i in 0..STATE_WIDTH {
            builder.assert_eq(h_next[i], expected_init[i]);
        }
    }

    // External round step
    {
        let builder = &mut builder.when(is_external);
        for i in 0..STATE_WIDTH {
            builder.assert_eq(h_next[i], expected_ext[i]);
        }
    }
}
```

When a group of constraints all need `is_transition()` combined with a flag, pre-multiply into
a single gate variable. Do not bind `is_transition()` on its own — only as part of a compound gate:
```rust
// GOOD
let gate = builder.is_transition() * flag;
let builder = &mut builder.when(gate);

// BAD — temporary borrow complicates lifetimes
let mut transition = builder.when_transition();
let builder = &mut transition.when(flag);
```

If all constraints in a function share a gate, apply it once at the top:
```rust
fn enforce_foo<AB: LiftedAirBuilder>(builder: &mut AB, flag: AB::Expr, ...) {
    let builder = &mut builder.when(flag);
    // all constraints share the gate
}
```

### Rule 4. Inline small helpers

Inline functions that: forward to one constraint function with unpacked fields,
wrap a single assertion, or are ≤15 lines. Keep functions that issue many constraints
or are called from multiple sites. Use section comments and `{}` blocks for grouping.

### Rule 5. Prefer intent comments, preserve existing formulas

New comments should explain *why*, not restate the formula. Don't delete existing
formula comments.

```rust
// Inside a span, gc can only stay the same or decrement by 1.
builder.when(sp).assert_bool(delta_gc);
```

---

## Semantics Preservation Checklist

After applying judgment rules (2, 3, 4), verify:

1. `when(g).assert_zero(x)` == `assert_zero(g * x)`
2. `when_transition().when(f)` == `when(is_transition * f)`
3. No dropped constraints — every original `assert_zero` has a corresponding assertion
4. No added constraints
5. Degree preserved — `when(a).when(b).assert_zero(x)` has degree `deg(a) + deg(b) + deg(x)`
