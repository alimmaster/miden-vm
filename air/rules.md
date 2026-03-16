# AIR Constraint Rules

Rules for writing constraint code in `air/src/constraints/`.
All transformations must be semantics-preserving: the resulting constraint polynomials
must be algebraically identical. Verify with the checklist at the end.

## AirBuilder Background

Constraint functions are generic over `LiftedAirBuilder` (defined in `../p3-miden/p3-miden-lifted-air/src/builder.rs`), a composite super-trait:
`AirBuilder + ExtensionBuilder + PermutationAirBuilder + PeriodicAirBuilder`.
All constraint functions should bound on `LiftedAirBuilder`, not individual sub-traits.

The underlying traits are defined in `../plonky3/air/src/air.rs`. Field algebra traits
(`PrimeCharacteristicRing`, `Field`, `ExtensionField`, `Algebra`) are in `../plonky3/field/src/field.rs`.
All `../` paths in this document are relative to the repository root.

### Types

`AB::F` is the base field. `AB::Var` is a base-field trace value (`Copy`). `AB::Expr` is an expression over `F` (not `Copy`). Arithmetic on `Var` or `F` values automatically produces `Expr`.

`AB::EF` is the extension field. `AB::VarEF` is an extension-field trace value (`Copy`). `AB::ExprEF` is an expression over `EF` (not `Copy`).

Public values are `F`. Challenges are `EF`. Main trace columns yield `Var`. Permutation trace columns yield `VarEF`. Periodic columns yield `PeriodicVar` (`Copy`, converts to `Expr`).

Clones on `Expr` do not affect runtime performance — constraint evaluation never allocates. However, unnecessary `.clone()` and `.into()` calls add noise and should be eliminated for readability. Since `Var` and `F` are `Copy`, using them directly avoids the noise entirely.

### Assertion Methods

All assertions ultimately call `assert_zero`. The semantic variants exist for readability:
- `assert_eq(a, b)` = `assert_zero(a - b)`
- `assert_one(x)` = `assert_zero(x - 1)`
- `assert_bool(x)` = `assert_zero(x * (x - 1))`
- `assert_bools(array)` = batch `assert_bool`
- `assert_zero_ext`, `assert_eq_ext`, `assert_one_ext` = extension field equivalents

### `when()` and `FilteredAirBuilder`

`builder.when(condition)` returns a `FilteredAirBuilder` that caches `condition: AB::Expr`. Conditions are always base-field expressions. The constraint is active (enforced) whenever the condition is nonzero — in practice conditions are binary, but `when()` does not require this and you should not insert a bool check on the condition. `builder.when_transition()` is equivalent to `builder.when(builder.is_transition())`. Same for `when_first_row()` and `when_last_row()`.

The `FilteredAirBuilder` for extension constraints multiplies `ExprEF * Expr` — the base-field condition scales the extension expression. This is cheaper than `ExprEF * ExprEF` because extension field arithmetic costs more. Never promote a condition to extension field.

A gate can be a sum of flags (e.g. `f_a + f_b + f_c`). When the summands are linearly independent, at most one is nonzero at a time, so the sum is still binary. Pay special attention when summing flags to verify mutual exclusivity — if two summands can be nonzero simultaneously the sum is no longer binary and the gate may scale the constraint rather than just activating it.

Chaining `when(a).when(b).assert_zero(x)` produces `a * (b * x)` — same polynomial as `assert_zero(a * b * x)`. When multiple constraints share the same compound condition, use a scoped builder (`let builder = &mut builder.when(gate)`) so the condition is applied once per assertion. Nest scoped builders to express logically separate groups of constraints under a shared outer gate.

### `QuadFeltExpr`

Defined in `air/src/constraints/ext_field.rs`. Represents a quadratic extension element `(c0, c1)`. Supports arithmetic (`Add`, `Sub`, `Mul`) and scalar multiply.

`QuadFeltAirBuilder` extension trait (blanket-implemented for all `AirBuilder`):
- `assert_eq_quad(lhs, rhs)` — asserts component-wise equality on both limbs.

---

## Rules

### 1. Use semantic assertion methods

Replace manual zero-check patterns:

| Before | After |
|--------|-------|
| `assert_zero(a - b)` | `assert_eq(a, b)` |
| `assert_zero(x * (x - 1))` | `assert_bool(x)` |
| `assert_zero(1 - x)` | `assert_one(x)` |
| `assert_zero_ext(lhs - rhs)` | `assert_eq_ext(lhs, rhs)` |
| loop of `assert_bool` | `assert_bools(array)` |

### 2. Factor gates with `when()`

When `assert_zero(gate * expr)` appears and `gate` is a binary selector/flag,
replace with `when(gate).assert_zero(expr)` (or the appropriate semantic assertion).

```rust
// BEFORE
builder.assert_zero(flag * x * (x - 1));
// AFTER — flag is a selector, x*(x-1) is the bool check
builder.when(flag).assert_bool(x);
```

When a pre-computed combined flag exists (e.g. `op_flags.right_shift()`), use it
directly in a single `when()` rather than chaining its constituent parts.

Only decompose factors that are binary selectors/flags (constrained to {0, 1} elsewhere).
Do NOT decompose intrinsic algebraic products (see "When NOT to apply" below).

### 3. Scoped builders and nesting

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
// GOOD — compound gate, scoped builder
let gate = builder.is_transition() * flag;
let builder = &mut builder.when(gate);
builder.assert_eq(a_next, a);
builder.assert_eq(b_next, b);

// BAD — functionally identical, but the temporary borrow complicates lifetimes
let mut transition = builder.when_transition();
let builder = &mut transition.when(flag);
```

If every constraint in a function shares the same gate, compute it once at the top:

```rust
fn enforce_foo<AB: LiftedAirBuilder>(builder: &mut AB, flag: AB::Expr, ...) {
    let builder = &mut builder.when(flag);
    // all constraints here share the gate
}
```

### 4. Inline small helpers

Remove helper functions that:
- Forward to a single constraint function with unpacked fields
- Wrap `builder.when_transition().assert_zero(expr)` as `assert_zero(builder, expr)`
- Are 15 lines or fewer (count the function body, not the signature)

Inline the body at the call site. Only extract a function when it issues many
constraints or is reused from multiple call sites.

Use section comments and `{}` blocks for visual grouping instead of function boundaries.

### 5. Prefer intent comments, preserve existing formulas

Prefer comments that explain why a constraint exists over restating the formula. However,
if a formula comment already exists in the code, keep it — do not delete existing comments
that document the algebraic relationship. Only add new comments for intent, not formulas.

```rust
// Inside a span, gc can only stay the same or decrement by 1.
builder.when(sp).assert_bool(delta_gc);
```

### 6. Keep trace columns as `AB::Var`

Trace column reads return `AB::Var`, which is `Copy`. Do not eagerly convert to `AB::Expr`:

```rust
// BAD — unnecessary clone and conversion
let s0: AB::Expr = local.chiplets[0].clone().into();

// GOOD — Var is Copy, implicit conversion on arithmetic
let s0 = local.chiplets[0];
```

Column structs (`DecoderColumns<E>`, etc.) should be parameterized with `AB::Var`:
bound `E: Copy` rather than `E: Clone`. Only convert to `Expr` at the point of use
(e.g. `ace_chiplet_flag(s0.into(), ...)`), not at binding site.

### 7. Inline constants, never bind them

Numeric `Felt` constants live in `constants.rs`. Never bind them to local variables.

```rust
// RHS — use Felt constant directly (auto-coerces)
delta_gc.clone() - F_1
value.clone() * F_7

// LHS — use .not() from BoolNot trait (import utils::BoolNot)
s3_next.not()
flag_sum.not()  // works on AB::ExprEF too
```

When the same `.not()` is used multiple times, store it in a named variable:

```rust
// GOOD — computed once, reused
let not_hs1 = hs1.not();
let f_bp = hasher_active * s0 * not_hs1.clone() * not_hs2.clone();
let f_mp = hasher_active * s0 * not_hs1.clone() * s2;

// BAD — redundant .not() calls
let f_bp = hasher_active * s0 * hs1.not() * hs2.not();
let f_mp = hasher_active * s0 * hs1.not() * s2;
```

### 8. Section headers

Use section headers within a single function body for visual grouping:

```rust
// =============================================
// Binary constraints
// =============================================
builder.assert_bools(cols.op_bits);

// =============================================
// Transition constraints
// =============================================
{
    let builder = &mut builder.when(transition_flag);
    builder.assert_eq(a_next, a);
}
```

---

## When NOT to Apply These Rules

### Intrinsic algebraic products

Do NOT decompose with `when()` when the multiplicative structure IS the constraint:

```rust
// Conditional inverse (EQ/EQZ): if x != 0, h0 = 1/x forces result = 0
assert_zero(eq_diff * s0_next);

// Ternary validity: op in {-1, 0, 1}
assert_zero(op * (op - 1) * (op + 1));

// Mutual exclusion: can't both be 1
assert_zero(sstart * sblock);

// Range-check vanishing polynomial
assert_zero(x * (x-1) * (x-2) * ... * (x-8));
```

A factor is a **gate** if it's a binary selector that controls activation.
A factor is **intrinsic** if it participates in the algebraic relationship being checked.

### Bus accumulator constraints

Running products don't decompose into gate + assertion. Neither factor is a binary
selector — both are accumulator values:

```rust
builder.when_transition().assert_eq_ext(p_next * req, p_local * resp);
```

### Correct decomposition of high-degree products

```rust
// sp IS a gate, delta_gc * (delta_gc - 1) is the bool check
builder.when(sp).assert_bool(delta_gc);  // CORRECT

// delta_gc is NOT a gate — don't split the bool check
builder.when(sp).when(delta_gc).assert_zero(delta_gc - 1);  // WRONG
```

---

## Semantics Preservation Checklist

After every transformation, verify:

1. **Gate equivalence:** `when(g).assert_zero(x)` == `assert_zero(g * x)`.
2. **Transition scoping:** `when_transition().when(f)` == `when(is_transition * f)`.
3. **No dropped constraints:** Every `assert_zero` in old code has a corresponding assertion.
4. **No added constraints:** No new algebraic relations introduced.
5. **Degree preservation:** `when(a).when(b).assert_zero(x)` has degree `deg(a) + deg(b) + deg(x)`.
6. **Semantic equivalence:**
   - `assert_eq(a, b)` == `assert_zero(a - b)` (no degree change)
   - `assert_bool(x)` == `assert_zero(x * (x - 1))` (degree 2 in x)
   - `assert_one(x)` == `assert_zero(x - 1)` (same degree)
