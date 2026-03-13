# AIR Constraint Style Rules

Rules for writing AIR constraints using the `AirBuilder` API. These produce identical
algebraic constraints as the raw `assert_zero(flag * expr)` form but make intent explicit.

---

## Rule 1: Use semantic assertion methods

Replace manual zero-check patterns with the builder's built-in methods.

| Before | After | Algebraic identity |
|---|---|---|
| `assert_zero(a - b)` | `assert_eq(a, b)` | a - b = 0 |
| `assert_zero(x * (x - 1))` | `assert_bool(x)` | x² - x = 0 |
| `assert_zero(1 - x)` | `assert_one(x)` | 1 - x = 0 |
| `assert_zero_ext(lhs - rhs)` | `assert_eq_ext(lhs, rhs)` | lhs - rhs = 0 (ext field) |
| loop of `assert_bool` on array | `assert_bools(array)` | batch |

**Why:** "these must be equal" reads faster than "their difference must be zero."

## Rule 2: Factor gates out with `when()`

Instead of multiplying a flag into every expression, use `when(flag)`.

**Single constraint** — chain `.when()` calls, one per independent flag/condition.
Never multiply flags together inside a single `when()`:

```rust
// GOOD
builder.when(f_end).when(h5).assert_zero(s0);
builder.when_transition().when(flag).assert_eq(a_next, a);

// BAD — don't multiply independent flags
builder.when(f_end * h5).assert_zero(s0);
```

**Multiple constraints under the same gate** — use a `{}` block:

```rust
{
    let builder = &mut builder.when(flag);
    builder.assert_eq(a, b);
    builder.assert_eq(c, d);
}
```

Re-use existing combined flags (e.g. `op_flags.right_shift()`) rather than rebuilding them.

`when(condition)` returns a filtered builder that multiplies `condition` into every
constraint issued through it. Algebraically identical: `when(g).assert_zero(x)` == `assert_zero(g * x)`.

## Rule 3: Block-scoping and nesting

Blocks group constraints that **share a gate**. Don't group unrelated constraints in a
block just because they use the same variable.

**Flat block** — multiple constraints under one gate:

```rust
{
    let builder = &mut builder.when(gate);
    builder.assert_eq(a, b);
    builder.assert_eq(c, d);
}
```

**Nested blocks** — when sub-groups share an outer gate but differ on an inner selector:

```rust
{
    let gate = builder.is_transition() * hasher_flag;
    let builder = &mut builder.when(gate);

    // Init step (row 0)
    {
        let builder = &mut builder.when(is_init);
        for i in 0..STATE_WIDTH {
            builder.assert_eq(h_next[i].clone(), expected_init[i].clone());
        }
    }

    // External round step (rows 1-4, 27-30)
    {
        let builder = &mut builder.when(is_external);
        for i in 0..STATE_WIDTH {
            builder.assert_eq(h_next[i].clone(), expected_ext[i].clone());
        }
    }
}
```

**Transition blocks** — for multiple transition constraints under a shared flag, materialize
the transition indicator into the gate:

```rust
// Multiple transition constraints → materialize is_transition()
{
    let gate = builder.is_transition() * flag;
    let builder = &mut builder.when(gate);
    builder.assert_eq(a_next, a);
    builder.assert_eq(b_next, b);
}

// Single transition constraint → chain directly
builder.when_transition().when(flag).assert_eq(a_next, a);
```

**Function-level gates** — if every constraint in a function shares the same gate prefix,
compute it once at the top and issue all constraints through it:

```rust
fn enforce_merkle_absorb<AB>(builder: &mut AB, hasher_flag: AB::Expr, ...) {
    let gate = builder.is_transition() * hasher_flag * f_absorb;
    let builder = &mut builder.when(gate);

    // Capacity reset (directly under shared gate)
    for cap in &cap_next {
        builder.assert_zero(cap.clone());
    }

    // b=0: digest → rate0
    {
        let builder = &mut builder.when(one - b.clone());
        ...
    }

    // b=1: digest → rate1
    {
        let builder = &mut builder.when(b);
        ...
    }
}
```

Never create intermediate builders just to work around lifetimes:

```rust
// BAD — intermediate builder
let mut transition = builder.when_transition();
let builder = &mut transition.when(flag);

// GOOD — materialize the flag
let gate = builder.is_transition() * flag;
let builder = &mut builder.when(gate);
```

## Rule 4: Use `QuadFeltExpr` as a unit

Operate on `QuadFeltExpr` directly instead of decomposing into component pairs:

```rust
// BEFORE
let (exp_0, exp_1) = compute(..., v1_0, v1_1, v2_0, v2_1);
builder.assert_zero(gate.clone() * (exp_0 - actual_0));
builder.assert_zero(gate * (exp_1 - actual_1));

// AFTER
let v1 = QuadFeltExpr::new(v1_0, v1_1);
let v2 = QuadFeltExpr::new(v2_0, v2_1);
let expected = compute(..., v1, v2);
let actual = QuadFeltExpr::new(actual_0, actual_1);
builder.when(gate).assert_eq_quad(actual, expected);
```

`assert_eq_quad` (from `QuadFeltAirBuilder`) asserts component-wise equality on both limbs.

## Rule 5: Inline trivial helper functions

Remove helper functions that:
- Call a single constraint function with fields unpacked from a context struct
- Wrap `builder.when_transition().assert_zero(expr)` as `assert_zero(builder, expr)`
- Issue only 1-3 constraints (e.g., a one-line first-row check)

Inline the body at the call site. Keep a helper only when it contains meaningful logic
(computing expected values, managing complex flag combinations, or reused from multiple
call sites — roughly >20 lines of dense algebra).

Use `// SECTION_NAME` comments and `{}` blocks for visual grouping instead of function
boundaries.

## Rule 6: Keep only "why" comments, drop "what" comments

```rust
// BEFORE — redundant formula comment
// sp * delta_gc * (delta_gc - 1) = 0
// This ensures: if sp=1 and delta_gc != 0, then delta_gc must equal 1
builder.assert_zero(sp * delta_gc.clone() * (delta_gc.clone() - AB::Expr::ONE));

// AFTER — the English "why" is enough; the code IS the formula
// Inside a span, gc can only stay the same or decrement by 1.
builder.assert_zero(sp * delta_gc.clone() * (delta_gc.clone() - AB::Expr::ONE));
```

When using semantic assertions (`assert_eq`, `assert_bool`, `assert_one`), the code is
already self-documenting. Comments should explain *why* the constraint exists, not
restate it algebraically.

## Rule 7: Keep trace columns as `AB::Var`, not `AB::Expr`

Trace column reads (`local.stack[i]`, `row.decoder[j]`, etc.) return `AB::Var`, which
is `Copy`. Do **not** eagerly convert them to `AB::Expr`:

```rust
// BEFORE — every column access clones and converts
let s0: AB::Expr = local.stack[0].clone().into();
let s1: AB::Expr = local.stack[1].clone().into();

// AFTER — keep as Var; implicit conversion happens at arithmetic boundaries
let s0 = local.stack[0];
let s1 = local.stack[1];
```

**Why:** `AB::Var` is `Copy` (a thin index/wrapper), so using it directly eliminates
all `.clone()` and `.into()` noise. Conversion to `AB::Expr` happens automatically
when you perform arithmetic (`s0 + s1`, `s0 * flag`, etc.) — there's no need to do it
eagerly at the binding site.

**Column structs too:** When a struct wraps column values (`DecoderColumns<E>`,
`BitwiseColumns<E>`, etc.), parameterize it with `AB::Var` instead of `AB::Expr`:

```rust
// BEFORE
let cols: DecoderColumns<AB::Expr> = DecoderColumns::from_row::<AB>(local);
// from_row does: row.decoder[i].clone().into() for each field

// AFTER
let cols: DecoderColumns<AB::Var> = DecoderColumns::from_row::<AB>(local);
// from_row does: row.decoder[i] for each field (Copy, no clone)
```

Bound the struct's `E` as `Copy` rather than `Clone`. The `from_row` method should
require `AB: LiftedAirBuilder<Var = E>` so it returns plain `Var` copies.

**Where `.into()` is needed:** Only convert at call boundaries that require `AB::Expr`,
such as flag-computing functions (`ace_chiplet_flag(s0.into(), ...)`) or extension-field
constructors (`QuadFeltExpr::new(v0.into(), v1.into())`). Add `.into()` at the point of
use, not at the point of binding.

## Rule 8: Prefer `AB::F` for field constants over `AB::Expr`

When a constant is a simple field element (ONE, ZERO, or a small literal), prefer `AB::F`
over `AB::Expr`:

```rust
// BEFORE
let one: AB::Expr = AB::Expr::ONE;
sstart - one

// AFTER
sstart - AB::F::ONE
```

`AB::F` is the base field type and is `Copy`. Using it avoids allocating an expression
node for a constant. The arithmetic operators handle mixed `Var * F` and `Expr + F`
automatically.

## Rule 9: Row structs with `#[repr(C)]`

For chiplet sub-regions accessed by index, define a `#[repr(C)]` struct that gives named
fields to the column layout:

```rust
/// Kernel ROM columns: 1 selector + 4-element digest.
#[repr(C)]
struct KernelRow<T> {
    s_first: T,
    digest: [T; 4],
}

const KERNEL_ROW_SIZE: usize = size_of::<KernelRow<u8>>();
const SELECTOR_WIDTH: usize = 5;

impl<T: Copy> KernelRow<T> {
    fn from_chiplets(chiplets: &[T]) -> Self {
        let row = &chiplets[SELECTOR_WIDTH..];
        debug_assert!(row.len() >= KERNEL_ROW_SIZE);
        Self {
            s_first: row[0],
            digest: array::from_fn(|i| row[1 + i]),
        }
    }
}
```

Usage replaces manual index constants and helper functions:

```rust
let row = KernelRow::from_chiplets(&local.chiplets);
let row_next = KernelRow::from_chiplets(&next.chiplets);
builder.when(flag).assert_bool(row.s_first);
```

Bound `T: Copy` so `AB::Var` fields are trivially copyable (Rule 7).

## Rule 10: Organize with labeled sections

Use section headers within a single function body:

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

## Semantics Preservation Checklist

When applying these rules, verify:

1. **Gate equivalence:** `when(g).assert_zero(x)` == `assert_zero(g * x)`.
2. **Transition scoping:** `when_transition().when(f)` == `when(is_transition * f)`.
3. **No dropped constraints:** Every `assert_zero` in the old code has a corresponding
   assertion in the new code.
4. **No added constraints:** No new algebraic relations are introduced.
5. **Clone budget:** If the old code cloned a value N times, the new code either clones
   it the same number of times or uses block scoping to avoid the need.

---

## When NOT to apply these rules

- **High-degree combined expressions** like `assert_zero(sp * delta_gc * (delta_gc - 1))`
  where the product is intrinsic to the constraint — don't split this into
  `when(sp).when(delta_gc).assert_zero(delta_gc - 1)` since the factors aren't
  independent gates. Only use `when()` for actual selector/flag conditions.

- **Bus accumulator constraints** like `assert_zero_ext(p_next * req - p_local * resp)`
  where the expression isn't a simple difference — keep as `assert_zero_ext`.

- **Range-check products** like `x * (x-1) * (x-2) * ... * (x-8)` — these are a single
  constraint, not a boolean check.
