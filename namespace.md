# Constraint Namespace Inventory & Migration Plan

## Current State

The codebase uses two co-existing tagging mechanisms:

1. **`tagging/` infrastructure** (old) — `TagGroup`, `tagged_assert_zero()`, `tagged_assert_zeros()`, numeric base-ID chains in `tagging/ids.rs`, and per-module `NAMES` arrays indexed by position.
2. **`named.rs` infrastructure** (new) — `Name`, `Namespace`, `Joined`, `NamedAirBuilder`. Functions take `ns: impl Namespace`, names compose via `ns.join("sub")` and `ns.name(|| dynamic_label)`. Production builders discard labels; debug builders evaluate them lazily.

The old system is still dominant across all constraint files. The new `named.rs` traits are defined but not yet used at call sites.

---

## Patterns Observed

### Pattern A: Individual `tagged()` with array indexing (old)
```rust
const NAMES: [&str; N] = ["system.clk.first_row", "system.clk.transition"];
builder.tagged(NAMES[0], |b| { ... });
builder.tagged(NAMES[1], |b| { ... });
```
**Used by:** system, range/main, stack/overflow, stack/general, chiplet/selectors, decoder (via helper)

### Pattern B: `tagged_list()` with namespace string (old)
```rust
builder.tagged_list("system.fn_hash.load", |b| { b.when_transition().assert_zeros(...) });
```
**Used by:** system/fn_hash, bus/boundary, public_inputs

### Pattern C: `tagged_assert_zero()` / `tagged_assert_zeros()` via `TagGroup` (oldest)
```rust
const TAGS: TagGroup = TagGroup { names: &NAMES };
tagged_assert_zero(builder, &TAGS, expr);
tagged_assert_zeros(builder, &TAGS, "namespace", exprs);
```
**Used by:** stack/ops, stack/arith, stack/crypto, decoder, all chiplet sub-modules (memory, ace, bitwise, kernel_rom, hasher/*)

### Pattern D: Redundant `NAMESPACE` + `NAMES` constants
```rust
const MEMORY_BINARY_NAMESPACE: &str = "chiplets.memory.binary";
const MEMORY_BINARY_NAMES: [&str; 4] = [MEMORY_BINARY_NAMESPACE; 4];
```
Many chiplet files define a `_NAMESPACE: &str` then broadcast it into a `_NAMES: [&str; N]` array purely because `TagGroup` requires `&[&str]`. The array adds nothing — every element is the same string.

---

## Complete Constraint ID Inventory

### System (13 constraints)
| Tag String | Count | Type | File |
|---|---|---|---|
| `system.clk.first_row` | 1 | boundary | system/mod.rs |
| `system.clk.transition` | 1 | transition | system/mod.rs |
| `system.ctx.call_dyncall` | 1 | transition | system/mod.rs |
| `system.ctx.syscall` | 1 | transition | system/mod.rs |
| `system.ctx.default` | 1 | transition | system/mod.rs |
| `system.fn_hash.load` | 4 | transition (list) | system/mod.rs |
| `system.fn_hash.preserve` | 4 | transition (list) | system/mod.rs |

### Range (4 constraints)
| Tag String | Count | Type | File |
|---|---|---|---|
| `range.main.v.first_row` | 1 | boundary | range/mod.rs |
| `range.main.v.last_row` | 1 | boundary | range/mod.rs |
| `range.main.v.transition` | 1 | transition | range/mod.rs |
| `range.bus.transition` | 1 | bus/transition | range/bus.rs |

### Stack General (16 constraints)
| Tag String | Count | Type | File |
|---|---|---|---|
| `stack.general.transition.{0..15}` | 16 | transition | stack/general/mod.rs |

### Stack Overflow (8 + 1 bus)
| Tag String | Count | Type | File |
|---|---|---|---|
| `stack.overflow.depth.first_row` | 1 | boundary | stack/overflow/mod.rs |
| `stack.overflow.depth.last_row` | 1 | boundary | stack/overflow/mod.rs |
| `stack.overflow.addr.first_row` | 1 | boundary | stack/overflow/mod.rs |
| `stack.overflow.addr.last_row` | 1 | boundary | stack/overflow/mod.rs |
| `stack.overflow.depth.transition` | 1 | transition | stack/overflow/mod.rs |
| `stack.overflow.flag.transition` | 1 | transition | stack/overflow/mod.rs |
| `stack.overflow.addr.transition` | 1 | transition | stack/overflow/mod.rs |
| `stack.overflow.zero_insert.transition` | 1 | transition | stack/overflow/mod.rs |
| `stack.overflow.bus.transition` | 1 | bus/transition | stack/bus.rs |

### Stack Ops (88 constraints)
All use `TagGroup` + `tagged_assert_zero`. Names are repeated for multi-constraint ops:

| Tag String | Count | Type |
|---|---|---|
| `stack.ops.pad` | 1 | transition |
| `stack.ops.dup` | 1 | transition |
| `stack.ops.dup{1,2,3,4,5,6,7,9,11,13,15}` | 11 | transition |
| `stack.ops.clk` | 1 | transition |
| `stack.ops.swap` | 2 | transition |
| `stack.ops.movup{2..8}` | 7 | transition |
| `stack.ops.movdn{2..8}` | 7 | transition |
| `stack.ops.swapw` | 8 | transition |
| `stack.ops.swapw2` | 8 | transition |
| `stack.ops.swapw3` | 8 | transition |
| `stack.ops.swapdw` | 16 | transition |
| `stack.ops.cswap` | 3 | transition |
| `stack.ops.cswapw` | 9 | transition |
| `stack.system.assert` | 1 | transition |
| `stack.system.caller` | 4 | transition |
| `stack.io.sdepth` | 1 | transition |

**Note:** `stack.system.*` and `stack.io.*` break the `stack.ops.*` namespace — these are system/io ops that happen to live in the ops file.

### Stack Crypto (46 constraints)
| Tag String | Count | Type |
|---|---|---|
| `stack.crypto.cryptostream` | 8 | transition |
| `stack.crypto.hornerbase` | 20 | transition |
| `stack.crypto.hornerext` | 18 | transition |

### Stack Arith (42 constraints)
| Tag String | Count | Type |
|---|---|---|
| `stack.arith.add` | 1 | transition |
| `stack.arith.neg` | 1 | transition |
| `stack.arith.mul` | 1 | transition |
| `stack.arith.inv` | 1 | transition |
| `stack.arith.incr` | 1 | transition |
| `stack.arith.not` | 2 | transition |
| `stack.arith.and` | 3 | transition |
| `stack.arith.or` | 3 | transition |
| `stack.arith.eq` | 2 | transition |
| `stack.arith.eqz` | 2 | transition |
| `stack.arith.expacc` | 5 | transition |
| `stack.arith.ext2mul` | 4 | transition |
| `stack.arith.u32.shared` | 1 | transition |
| `stack.arith.u32.output` | 2 | transition |
| `stack.arith.u32.split` | 1 | transition |
| `stack.arith.u32.add` | 1 | transition |
| `stack.arith.u32.add3` | 1 | transition |
| `stack.arith.u32.sub` | 3 | transition |
| `stack.arith.u32.mul` | 1 | transition |
| `stack.arith.u32.madd` | 1 | transition |
| `stack.arith.u32.div` | 3 | transition |
| `stack.arith.u32.assert2` | 2 | integrity |

### Decoder (57 + 3 bus)
| Tag String | Count | Type |
|---|---|---|
| `decoder.in_span.first_row` | 1 | boundary |
| `decoder.in_span.binary` | 1 | integrity |
| `decoder.in_span.span` | 1 | transition |
| `decoder.in_span.respan` | 1 | transition |
| `decoder.op_bits.b{0..6}.binary` | 7 | integrity |
| `decoder.extra.e0` | 1 | integrity |
| `decoder.extra.e1` | 1 | integrity |
| `decoder.op_bits.u32_prefix.b0` | 1 | integrity |
| `decoder.op_bits.very_high.b{0,1}` | 2 | integrity |
| `decoder.batch_flags.c{0..2}.binary` | 3 | integrity |
| `decoder.general.*` (14 names) | 14 | transition/integrity |
| `decoder.group_count.*` (5 names) | 5 | transition |
| `decoder.op_group.*` (2 names) | 2 | transition |
| `decoder.op_index.*` (4 names) | 4 | transition |
| `decoder.batch_flags.*` (9 names) | 9 | transition |
| `decoder.addr.*` (3 names) | 3 | transition |
| `decoder.control_flow.sp_complement` | 1 | transition |
| `decoder.bus.p1.transition` | 1 | bus/transition |
| `decoder.bus.p2.transition` | 1 | bus/transition |
| `decoder.bus.p3.transition` | 1 | bus/transition |

### Chiplets — Selectors (10 constraints)
| Tag String | Count | Type | File |
|---|---|---|---|
| `chiplets.selectors.s{0..4}.binary` | 5 | integrity | selectors.rs |
| `chiplets.selectors.s{0..4}.stability` | 5 | transition | selectors.rs |

### Chiplets — Hasher (~62 constraints)
| Tag String | Count | Type | File |
|---|---|---|---|
| `chiplets.hasher.selectors.binary` | 3 | integrity | hasher/selectors.rs |
| `chiplets.hasher.selectors.stability` | 1 | transition | hasher/selectors.rs |
| `chiplets.hasher.selectors.continuation` | 1 | transition | hasher/selectors.rs |
| `chiplets.hasher.selectors.invalid` | 1 | integrity | hasher/selectors.rs |
| `chiplets.hasher.permutation.init` | 12 | transition | hasher/state.rs |
| `chiplets.hasher.permutation.external` | 12 | transition | hasher/state.rs |
| `chiplets.hasher.permutation.internal` | 12 | transition | hasher/state.rs |
| `chiplets.hasher.abp.capacity` | 4 | transition | hasher/state.rs |
| `chiplets.hasher.output.index` | 1 | transition | hasher/mod.rs + merkle.rs |
| `chiplets.hasher.merkle.index.binary` | 1 | integrity | hasher/merkle.rs |
| `chiplets.hasher.merkle.index.stability` | 1 | transition | hasher/merkle.rs |
| `chiplets.hasher.merkle.capacity` | 4 | transition | hasher/merkle.rs |
| `chiplets.hasher.merkle.digest.rate0` | 4 | transition | hasher/merkle.rs |
| `chiplets.hasher.merkle.digest.rate1` | 4 | transition | hasher/merkle.rs |

### Chiplets — Bitwise (17 constraints)
| Tag String | Count | Type | File |
|---|---|---|---|
| `chiplets.bitwise.op.binary` | 1 | integrity | bitwise.rs |
| `chiplets.bitwise.op.stability` | 1 | transition | bitwise.rs |
| `chiplets.bitwise.a_bits.binary` | 4 | integrity | bitwise.rs |
| `chiplets.bitwise.b_bits.binary` | 4 | integrity | bitwise.rs |
| `chiplets.bitwise.first_row` | 3 | boundary | bitwise.rs |
| `chiplets.bitwise.input.transition` | 2 | transition | bitwise.rs |
| `chiplets.bitwise.output.prev` | 1 | transition | bitwise.rs |
| `chiplets.bitwise.output.aggregate` | 1 | transition | bitwise.rs |

### Chiplets — Memory (21 constraints)
| Tag String | Count | Type | File |
|---|---|---|---|
| `chiplets.memory.binary` | 4 | integrity | memory.rs |
| `chiplets.memory.word_idx.zero` | 2 | boundary | memory.rs |
| `chiplets.memory.first_row.zero` | 4 | boundary | memory.rs |
| `chiplets.memory.delta.inv` | 4 | integrity | memory.rs |
| `chiplets.memory.delta.transition` | 1 | transition | memory.rs |
| `chiplets.memory.scw.flag` | 1 | integrity | memory.rs |
| `chiplets.memory.scw.reads` | 1 | transition | memory.rs |
| `chiplets.memory.value.consistency` | 4 | integrity | memory.rs |

### Chiplets — ACE (21 constraints)
| Tag String | Count | Type | File |
|---|---|---|---|
| `chiplets.ace.selector.binary` | 2 | integrity | ace.rs |
| `chiplets.ace.section.flags` | 5 | integrity | ace.rs |
| `chiplets.ace.section.transition` | 4 | transition | ace.rs |
| `chiplets.ace.read.ids` | 1 | integrity | ace.rs |
| `chiplets.ace.read.to_eval` | 1 | transition | ace.rs |
| `chiplets.ace.eval.op` | 1 | integrity | ace.rs |
| `chiplets.ace.eval.result` | 2 | transition | ace.rs |
| `chiplets.ace.final.zero` | 3 | transition | ace.rs |
| `chiplets.ace.first_row.start` | 1 | boundary | ace.rs |

### Chiplets — Kernel ROM (6 constraints)
| Tag String | Count | Type | File |
|---|---|---|---|
| `chiplets.kernel_rom.sfirst.binary` | 1 | integrity | kernel_rom.rs |
| `chiplets.kernel_rom.digest.contiguity` | 4 | transition | kernel_rom.rs |
| `chiplets.kernel_rom.first_row.start` | 1 | boundary | kernel_rom.rs |

### Chiplets — Bus (3 constraints)
| Tag String | Count | Type | File |
|---|---|---|---|
| `chiplets.bus.chiplets.transition` | 1 | bus/transition | bus/chiplets.rs |
| `chiplets.bus.hash_kernel.transition` | 1 | bus/LogUp | bus/hash_kernel.rs |
| `chiplets.bus.wiring.transition` | 1 | bus/LogUp | bus/wiring.rs |

### Bus Boundary (16 constraints)
| Tag String | Count | Type | File |
|---|---|---|---|
| `bus.boundary.first_row` | 8 | boundary (list) | mod.rs |
| `bus.boundary.last_row` | 8 | boundary (list) | mod.rs |

### Public Inputs (32 constraints)
| Tag String | Count | Type | File |
|---|---|---|---|
| `public_inputs.stack_input` | 16 | boundary (list) | public_inputs.rs |
| `public_inputs.stack_output` | 16 | boundary (list) | public_inputs.rs |

---

## Proposed Namespace Hierarchy (for `named.rs` migration)

The `::` separator in `named.rs` (`Joined` displays as `"outer::inner"`) naturally replaces the `.` separator in the current tag strings. The hierarchy maps directly:

```
system
  ::clk
    ::first_row
    ::transition
  ::ctx
    ::call_dyncall
    ::syscall
    ::default
  ::fn_hash
    ::load::{0..3}
    ::preserve::{0..3}

range
  ::main
    ::v
      ::first_row
      ::last_row
      ::transition
  ::bus
    ::transition

stack
  ::general
    ::transition::{0..15}
  ::overflow
    ::depth::{first_row, last_row, transition}
    ::addr::{first_row, last_row, transition}
    ::flag::transition
    ::zero_insert::transition
    ::bus::transition
  ::ops
    ::pad
    ::dup::{0..15}       (individual per variant)
    ::clk
    ::swap::{0..1}
    ::movup::{2..8}
    ::movdn::{2..8}
    ::swapw::{0..7}
    ::swapw2::{0..7}
    ::swapw3::{0..7}
    ::swapdw::{0..15}
    ::cswap::{0..2}
    ::cswapw::{0..8}
    ::assert               (currently "stack.system.assert")
    ::caller::{0..3}       (currently "stack.system.caller")
    ::sdepth               (currently "stack.io.sdepth")
  ::arith
    ::add, ::neg, ::mul, ::inv, ::incr
    ::not::{0..1}
    ::and::{0..2}
    ::or::{0..2}
    ::eq::{0..1}
    ::eqz::{0..1}
    ::expacc::{0..4}
    ::ext2mul::{0..3}
    ::u32
      ::shared, ::output::{0..1}
      ::split, ::add, ::add3
      ::sub::{0..2}
      ::mul, ::madd
      ::div::{0..2}
      ::assert2::{0..1}
  ::crypto
    ::cryptostream::{0..7}
    ::hornerbase::{0..19}
    ::hornerext::{0..17}

decoder
  ::in_span::{first_row, binary, span, respan}
  ::op_bits
    ::b{0..6}::binary
    ::u32_prefix::b0
    ::very_high::{b0, b1}
  ::extra::{e0, e1}
  ::batch_flags
    ::c{0..2}::binary
    ::span_sum
    ::zero_when_not_span
    ::h{1..7}::zero
  ::general::*            (14 individual names)
  ::group_count::*        (5 individual names)
  ::op_group::*           (2 individual names)
  ::op_index::*           (4 individual names)
  ::addr::*               (3 individual names)
  ::control_flow::sp_complement
  ::bus
    ::p1::transition
    ::p2::transition
    ::p3::transition

chiplets
  ::selectors
    ::s{0..4}::binary
    ::s{0..4}::stability
  ::hasher
    ::selectors::{binary, stability, continuation, invalid}
    ::permutation::{init, external, internal}::{0..11}
    ::abp::capacity::{0..3}
    ::output::index
    ::merkle
      ::index::{binary, stability}
      ::capacity::{0..3}
      ::digest::rate{0,1}::{0..3}
  ::bitwise
    ::op::{binary, stability}
    ::a_bits::binary::{0..3}
    ::b_bits::binary::{0..3}
    ::first_row::{0..2}
    ::input::transition::{0..1}
    ::output::{prev, aggregate}
  ::memory
    ::binary::{0..3}
    ::word_idx::zero::{0..1}
    ::first_row::zero::{0..3}
    ::delta::{inv::{0..3}, transition}
    ::scw::{flag, reads}
    ::value::consistency::{0..3}
  ::ace
    ::selector::binary::{0..1}
    ::section::{flags::{0..4}, transition::{0..3}}
    ::read::{ids, to_eval}
    ::eval::{op, result::{0..1}}
    ::final::zero::{0..2}
    ::first_row::start
  ::kernel_rom
    ::sfirst::binary
    ::digest::contiguity::{0..3}
    ::first_row::start
  ::bus
    ::chiplets::transition
    ::hash_kernel::transition
    ::wiring::transition

bus
  ::boundary
    ::first_row::{0..7}
    ::last_row::{0..7}

public_inputs
  ::stack_input::{0..15}
  ::stack_output::{0..15}
```

### How it maps to code

With `named.rs`, a function receives a namespace and composes within it:

```rust
fn enforce_fn_hash_constraints<AB: NamedAirBuilder>(
    builder: &mut AB,
    ns: impl Namespace,   // caller passes "system" or "system".join("fn_hash")
    local: &MainTraceRow<AB::Var>,
    next: &MainTraceRow<AB::Var>,
) {
    let ns = ns.join("fn_hash");

    // "system::fn_hash::load::0", "system::fn_hash::load::1", ...
    builder.assert_zeros_named(load_exprs, ns.join("load"));

    // "system::fn_hash::preserve::0", ...
    builder.assert_zeros_named(preserve_exprs, ns.join("preserve"));
}
```

For groups with many repeated names (e.g., stack/crypto with 20 identical `"hornerbase"` strings):

```rust
// Old: 20 copies of "stack.crypto.hornerbase" in a const array
// New: just
builder.assert_zeros_named(horner_exprs, ns.join("hornerbase"));
// produces "stack::crypto::hornerbase::0" .. "::19" automatically via assert_zeros_named
```

For individually named constraints (like decoder):

```rust
builder.assert_zero_named(expr, ns.name("first_row"));     // "decoder::in_span::first_row"
builder.assert_zero_named(expr, ns.name("binary"));         // "decoder::in_span::binary"
```

For dynamic indices (closures, evaluated only in debug):

```rust
builder.assert_zero_named(expr, ns.name(|| format!("b{i}")));  // "decoder::op_bits::b3"
```

---

## Are the Constants Still Necessary?

### `tagging/ids.rs` — Numeric base IDs and counts

**Verdict: Can be removed entirely.**

The numeric chain (`TAG_SYSTEM_BASE = 0`, `TAG_RANGE_MAIN_BASE = TAG_SYSTEM_BASE + TAG_SYSTEM_COUNT`, ...) exists to assign sequential integer IDs to each constraint. With `named.rs`, constraints are identified by their hierarchical string path, not by index. The counts (e.g., `TAG_SYSTEM_CLK_COUNT: usize = 2`) are only used to:
1. Size the `NAMES` arrays (which go away)
2. Chain the next base ID (which goes away)
3. In tests, to know the total count (can be derived at test time)

The only remaining use of counts is `NUM_CONSTRAINTS` in some modules for sizing `NAMES` arrays. These can be deleted once the arrays are gone.

### Per-module `_NAMES` arrays and `_NAMESPACE` constants

**Verdict: Inline the strings; drop the constants.**

The pattern of `const FOO_NAMESPACE: &str = "x.y.z"; const FOO_NAMES: [&str; N] = [FOO_NAMESPACE; N];` is pure boilerplate to feed `TagGroup`. With `named.rs`:
- Groups of identical names become a single `ns.join("label")` passed to `assert_zeros_named`.
- Individually named constraints use `ns.name("label")` with inline strings.
- No arrays, no `TagGroup`, no `_NAMES` constants.

The inline strings are perfectly readable — `ns.join("load")` is clearer than `SYSTEM_FN_HASH_LOAD_NAMESPACE`.

### `TagGroup` struct and `tagged_assert_*` helpers

**Verdict: Remove after migration.**

These are the old dispatch layer. Every use site will be replaced by direct `NamedAirBuilder` method calls.

### `TaggingAirBuilderExt` trait (`tagged()`, `tagged_list()`)

**Verdict: Remove after migration.**

The `named.rs` builder methods (`assert_zero_named`, `assert_zeros_named`, etc.) replace these. The `tagged` closure pattern is unnecessary when naming is built into the assertion methods.

---

## Migration Summary

| What | Action |
|---|---|
| `tagging/ids.rs` | Delete entirely |
| `TagGroup` struct | Delete |
| `tagged_assert_zero[s]` helpers | Delete |
| `TaggingAirBuilderExt` trait | Delete |
| Per-module `_NAMES` / `_NAMESPACE` constants | Delete (inline into `ns.join()`/`ns.name()`) |
| `NUM_CONSTRAINTS` constants | Keep only where independently useful (e.g., test assertions) |
| `tagged()` / `tagged_list()` call sites | Replace with `assert_zero_named(expr, ns.name("..."))` / `assert_zeros_named(exprs, ns.join("..."))` |
| Function signatures | Add `ns: impl Namespace` parameter, thread through call chain |
| Separator | Changes from `.` to `::` (from `Joined::fmt`) |

### Naming Convention Changes

The `.` → `::` separator change is a natural consequence of `Joined`. Current names like `"system.clk.first_row"` become `"system::clk::first_row"`. This is fine — the strings are only evaluated in debug mode and the `::` convention is idiomatic Rust.

### Inconsistencies to Fix During Migration

1. **`stack.system.assert`** and **`stack.system.caller`** live in `stack/ops/mod.rs` but break the `stack.ops.*` namespace. Recommend: move under `stack::ops::assert` and `stack::ops::caller`.
2. **`stack.io.sdepth`** similarly lives in ops. Recommend: `stack::ops::sdepth`.
3. **Duplicate names within groups** — e.g., `"stack.ops.swap"` appears twice, `"stack.ops.swapw"` appears 8 times. Currently these are distinguished only by position. With `assert_zeros_named` they get automatic `::0`, `::1` suffixes.
