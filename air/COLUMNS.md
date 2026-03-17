# Typed Column Structs

Design for replacing column index constants with typed `#[repr(C)]` structs
and a compile-time index map. See [issue #1763](https://github.com/0xMiden/miden-vm/issues/1763)
and the [column layout gist](https://gist.github.com/adr1anh/063d7dfac6bc9dbf3b75c322b2e50c61).

Reference implementation: [Plonky3 keccak-air/src/columns.rs](../Plonky3/keccak-air/src/columns.rs).

## Pattern Overview

Three mechanisms work together:

1. **`#[repr(C)]` generic structs** — named fields for every trace column,
   nested to match the component hierarchy. `repr(C)` guarantees declaration-order
   layout with no padding (all fields are `T` or `[T; N]`).

2. **`Borrow<Struct<T>> for [T]`** — zero-copy cast from a raw trace-row slice
   to a typed struct reference via `align_to`.

3. **Const index map** — a `const` instance of the struct where each field
   holds its column index (0, 1, 2, ...). Created by transmuting
   `[0usize, 1, 2, ..., N-1]` into the struct layout. Replaces all scattered
   column-offset constants.

```rust
pub const NUM_MAIN_COLS: usize = size_of::<MainCols<u8>>();
pub const MAIN_COL_MAP: MainCols<usize> = {
    assert!(NUM_MAIN_COLS == TRACE_WIDTH);
    unsafe { core::mem::transmute(indices_arr::<NUM_MAIN_COLS>()) }
};
```

After this, `MAIN_COL_MAP.decoder.addr == 6`, `MAIN_COL_MAP.stack.top[0] == 30`, etc.

---

## Main Trace (71 columns)

### Top-Level

```
    system (6)      decoder (24)       stack (19)     range (2)   chiplets (20)
  ┌────────────┬──────────────────┬────────────────┬───────────┬────────────────┐
  │ 0       5  │ 6            29  │ 30          48 │ 49    50  │ 51          70 │
  └────────────┴──────────────────┴────────────────┴───────────┴────────────────┘
```

```rust
#[repr(C)]
pub struct MainCols<T> {
    pub system: SystemCols<T>,      // 6 cols  [0..6)
    pub decoder: DecoderCols<T>,    // 24 cols [6..30)
    pub stack: StackCols<T>,        // 19 cols [30..49)
    pub range: RangeCols<T>,        // 2 cols  [49..51)
    chiplets: [T; 20],              // 20 cols [51..71)  — private, see ChipletsView
}
```

Chiplets are private because the 20 columns are a union — their interpretation
depends on which chiplet is active. All chiplet access goes through
[`ChipletsView`](#chipletsview).

### SystemCols (6)

```rust
#[repr(C)]
pub struct SystemCols<T> {
    pub clk: T,                     // clock cycle
    pub ctx: T,                     // context ID
    pub fn_hash: [T; 4],           // function hash (digest)
}
```

### DecoderCols (24)

```rust
#[repr(C)]
pub struct DecoderCols<T> {
    pub addr: T,                    // block address (hasher table row)
    pub op_bits: [T; 7],           // opcode bits b0–b6
    pub hasher_state: [T; 8],      // h0–h7 (shared hasher / decoder state)
    pub in_span: T,                 // sp — 1 inside a basic block
    pub group_count: T,             // gc — remaining op groups
    pub op_index: T,                // ox — position within op group (0–8)
    pub batch_flags: [T; 3],       // c0, c1, c2
    pub extra: [T; 2],             // e0, e1 (degree-reduction columns)
}
```

The `hasher_state` array has sub-views used in different contexts:

| Sub-range | Name | Context |
|-----------|------|---------|
| `[2..8]` | user op helpers | During instruction execution |
| `[4..8]` | end-block flags | During END: `is_loop_body`, `is_loop`, `is_call`, `is_syscall` |

Accessed via helpers that return arrays by value (better codegen than sub-slice
refs since `T: Copy`):

```rust
impl<T: Copy> DecoderCols<T> {
    pub fn user_op_helpers(&self) -> [T; 6] {
        [self.hasher_state[2], self.hasher_state[3],
         self.hasher_state[4], self.hasher_state[5],
         self.hasher_state[6], self.hasher_state[7]]
    }

    pub fn end_block_flags(&self) -> [T; 4] {
        [self.hasher_state[4], self.hasher_state[5],
         self.hasher_state[6], self.hasher_state[7]]
    }
}
```

When named access improves readability, a struct overlay can also be provided:

```rust
#[repr(C)]
pub struct EndBlockFlags<T> {
    pub is_loop_body: T,
    pub is_loop: T,
    pub is_call: T,
    pub is_syscall: T,
}
```

### StackCols (19)

```rust
#[repr(C)]
pub struct StackCols<T> {
    pub top: [T; 16],              // stack elements s0–s15
    pub b0: T,                      // stack depth
    pub b1: T,                      // overflow table parent address
    pub h0: T,                      // helper: 1/(b0 − 16)
}
```

### RangeCols (2)

```rust
#[repr(C)]
pub struct RangeCols<T> {
    pub multiplicity: T,
    pub value: T,
}
```

---

## Chiplets (20 columns)

### Layout

The 20 chiplet columns are a union: the first 1–5 columns are shared selectors,
and the rest are chiplet-specific data. Which chiplet is active determines the
interpretation.

```
  s0  s1  s2  s3  s4  ···  col 19
  ├───┼───┼───┼───┼───┼─────────────┤
  │ Hasher (s0=0)        [1..17]    │  16 cols: 3 sel + 12 state + 1 node_idx
  │    Bitwise (s0=1,s1=0) [2..15]  │  13 cols: 1 op + 2 agg + 8 bits + 2 out
  │       Memory (s0–s1=1,s2=0) [3..18]  │  15 cols
  │          ACE (s0–s2=1,s3=0)  [4..20] │  16 cols
  │             KernelROM (s0–s3=1,s4=0) [5..10] │  5 cols
  └──────────────────────────────────┘
```

### ChipletsView

All chiplet access goes through an encapsulated view that bundles both the
current and next row. This avoids exposing the raw `[T; 20]` array, ties
selector computation to column access, and provides local/next accessors for
each chiplet.

```rust
pub struct ChipletsView<'a, V> {
    local: &'a [V; 20],
    next: &'a [V; 20],
}

impl<'a, V> ChipletsView<'a, V> {
    pub fn new(local: &'a MainCols<V>, next: &'a MainCols<V>) -> Self {
        Self { local: &local.chiplets, next: &next.chiplets }
    }

    // Shared selectors (by value).
    pub fn selectors_local(&self) -> [V; 5] where V: Copy { ... }
    pub fn selectors_next(&self) -> [V; 5] where V: Copy { ... }

    // Per-chiplet zero-copy borrows.
    pub fn hasher_local(&self) -> &HasherCols<V> { borrow_chiplet(&self.local[1..17]) }
    pub fn hasher_next(&self)  -> &HasherCols<V> { borrow_chiplet(&self.next[1..17]) }

    pub fn bitwise_local(&self) -> &BitwiseCols<V> { borrow_chiplet(&self.local[2..15]) }
    pub fn bitwise_next(&self)  -> &BitwiseCols<V> { borrow_chiplet(&self.next[2..15]) }

    pub fn memory_local(&self) -> &MemoryCols<V> { borrow_chiplet(&self.local[3..18]) }
    pub fn memory_next(&self)  -> &MemoryCols<V> { borrow_chiplet(&self.next[3..18]) }

    pub fn ace_local(&self) -> &AceCols<V> { borrow_chiplet(&self.local[4..20]) }
    pub fn ace_next(&self)  -> &AceCols<V> { borrow_chiplet(&self.next[4..20]) }

    pub fn kernel_rom_local(&self) -> &KernelRomCols<V> { borrow_chiplet(&self.local[5..10]) }
    pub fn kernel_rom_next(&self)  -> &KernelRomCols<V> { borrow_chiplet(&self.next[5..10]) }

    // Compute chiplet selector flags (replaces build_chiplet_selectors()).
    pub fn build_selectors<AB>(&self, builder: &mut AB) -> ChipletSelectors<AB::Expr>
    where AB: MidenAirBuilder, V: Into<AB::Expr> + Copy
    { ... }
}
```

### Chiplet Column Structs

Each struct covers the columns **after** that chiplet's selector prefix.

**HasherCols** — 16 cols, viewed from `chiplets[1..17]`:

```rust
#[repr(C)]
pub struct HasherCols<T> {
    pub selectors: [T; 3],         // hs0, hs1, hs2
    pub state: [T; 12],            // Poseidon2 state
    pub node_index: T,
}

impl<T: Copy> HasherCols<T> {
    pub fn rate(&self) -> [T; 8] { ... }         // state[0..8]
    pub fn capacity(&self) -> [T; 4] { ... }     // state[8..12]
    pub fn digest(&self) -> [T; 4] { ... }       // state[0..4]
}
```

**BitwiseCols** — 13 cols, viewed from `chiplets[2..15]`:

```rust
#[repr(C)]
pub struct BitwiseCols<T> {
    pub op_flag: T,                 // 0 = AND, 1 = XOR
    pub a: T,                       // aggregated input a
    pub b: T,                       // aggregated input b
    pub a_bits: [T; 4],            // 4-bit decomposition of a
    pub b_bits: [T; 4],            // 4-bit decomposition of b
    pub prev_output: T,             // previous aggregated output
    pub output: T,                  // current aggregated output
}
```

**MemoryCols** — 15 cols, viewed from `chiplets[3..18]`:

```rust
#[repr(C)]
pub struct MemoryCols<T> {
    pub is_read: T,
    pub is_word: T,
    pub ctx: T,
    pub word_addr: T,
    pub idx0: T,
    pub idx1: T,
    pub clk: T,
    pub values: [T; 4],
    pub d0: T,                      // lower 16 bits of delta
    pub d1: T,                      // upper 16 bits of delta
    pub d_inv: T,                   // inverse of delta
    pub is_same_ctx_and_word: T,
}
```

**AceCols** — 16 cols, viewed from `chiplets[4..20]`:

```rust
#[repr(C)]
pub struct AceCols<T> {
    pub s_start: T,                 // start-of-circuit flag
    pub s_block: T,                 // 0 = READ, 1 = EVAL
    pub ctx: T,
    pub ptr: T,
    pub clk: T,
    pub eval_op: T,
    pub shared: [T; 10],           // dual-mode columns (see below)
}
```

Columns `shared[0..10]` have different meanings depending on mode:

```rust
impl<T> AceCols<T> {
    pub fn read(&self) -> &AceReadCols<T> { borrow_chiplet(&self.shared) }
    pub fn eval(&self) -> &AceEvalCols<T> { borrow_chiplet(&self.shared) }
}

/// READ mode overlay.
#[repr(C)]
pub struct AceReadCols<T> {
    pub id_0: T,
    pub v_0: [T; 2],               // QuadFelt
    pub id_1: T,
    pub v_1: [T; 2],               // QuadFelt
    pub num_eval: T,                // shared[6]
    pub unused: T,                  // shared[7]
    pub m_1: T,                     // shared[8]
    pub m_0: T,                     // shared[9]
}

/// EVAL mode overlay.
#[repr(C)]
pub struct AceEvalCols<T> {
    pub id_0: T,
    pub v_0: [T; 2],
    pub id_1: T,
    pub v_1: [T; 2],
    pub id_2: T,                    // shared[6]
    pub v_2: [T; 2],               // shared[7..9]
    pub m_0: T,                     // shared[9]
}
```

**KernelRomCols** — 5 cols, viewed from `chiplets[5..10]`:

```rust
#[repr(C)]
pub struct KernelRomCols<T> {
    pub s_first: T,
    pub root: [T; 4],
}
```

---

## Auxiliary Trace (8 columns)

```rust
#[repr(C)]
pub struct AuxCols<T> {
    pub p1_block_stack: T,          // decoder: block stack table
    pub p2_block_hash: T,           // decoder: block hash table
    pub p3_op_group: T,             // decoder: op group table
    pub stack_overflow: T,          // stack overflow running product
    pub range_check: T,             // range checker LogUp sum
    pub hash_kernel_vtable: T,      // hash-kernel virtual table bus
    pub chiplets_bus: T,            // chiplets bus running product
    pub ace_wiring: T,              // ACE wiring LogUp sum
}
```

With `Borrow<AuxCols<T>> for [T]` and `AUX_COL_MAP: AuxCols<usize>`.

---

## Borrow Implementation

Same pattern for `MainCols` and `AuxCols`:

```rust
impl<T> Borrow<MainCols<T>> for [T] {
    fn borrow(&self) -> &MainCols<T> {
        debug_assert_eq!(self.len(), TRACE_WIDTH);
        let (prefix, shorts, suffix) = unsafe { self.align_to::<MainCols<T>>() };
        debug_assert!(prefix.is_empty() && suffix.is_empty() && shorts.len() == 1);
        &shorts[0]
    }
}
// + BorrowMut equivalent
```

For chiplet sub-slices, a shared helper:

```rust
fn borrow_chiplet<T, S>(slice: &[T]) -> &S {
    let (prefix, cols, suffix) = unsafe { slice.align_to::<S>() };
    debug_assert!(prefix.is_empty() && suffix.is_empty() && cols.len() == 1);
    &cols[0]
}
```

---

## Const Index Map

```rust
pub const fn indices_arr<const N: usize>() -> [usize; N] {
    let mut arr = [0; N];
    let mut i = 0;
    while i < N { arr[i] = i; i += 1; }
    arr
}

pub const NUM_MAIN_COLS: usize = size_of::<MainCols<u8>>();
pub const MAIN_COL_MAP: MainCols<usize> = unsafe {
    core::mem::transmute(indices_arr::<NUM_MAIN_COLS>())
};

pub const NUM_AUX_COLS: usize = size_of::<AuxCols<u8>>();
pub const AUX_COL_MAP: AuxCols<usize> = unsafe {
    core::mem::transmute(indices_arr::<NUM_AUX_COLS>())
};
```

Per-chiplet local index maps:

```rust
pub const HASHER_COL_MAP: HasherCols<usize> = unsafe {
    core::mem::transmute(indices_arr::<{ size_of::<HasherCols<u8>>() }>())
};
// HASHER_COL_MAP.selectors[0] == 0, HASHER_COL_MAP.state[0] == 3, etc.
// Global index = MAIN_COL_MAP.chiplets[1] + local_index
```

---

## Compile-Time Safety

```rust
const _: () = assert!(size_of::<MainCols<u8>>() == TRACE_WIDTH);
const _: () = assert!(size_of::<AuxCols<u8>>() == AUX_TRACE_WIDTH);
const _: () = assert!(size_of::<SystemCols<u8>>() == 6);
const _: () = assert!(size_of::<DecoderCols<u8>>() == 24);
const _: () = assert!(size_of::<StackCols<u8>>() == 19);
const _: () = assert!(size_of::<RangeCols<u8>>() == 2);
const _: () = assert!(size_of::<HasherCols<u8>>() == 16);
const _: () = assert!(size_of::<BitwiseCols<u8>>() == 13);
const _: () = assert!(size_of::<MemoryCols<u8>>() == 15);
const _: () = assert!(size_of::<AceCols<u8>>() == 16);
const _: () = assert!(size_of::<AceReadCols<u8>>() == 10);
const _: () = assert!(size_of::<AceEvalCols<u8>>() == 10);
const _: () = assert!(size_of::<KernelRomCols<u8>>() == 5);
```

Plus runtime tests asserting `MAIN_COL_MAP` matches all legacy constants.

**Chiplet overlay aliasing**: Different overlay methods return `&T` references
into overlapping regions of `chiplets[0..20]`. This is safe for shared refs but
would be UB for `&mut T`. Overlay methods only return shared references. For
mutable (trace-generation) access, use flat indices via `MAIN_COL_MAP.chiplets[i]`.

---

## File Structure

Column structs live alongside the existing layout constants in each `trace/`
submodule — no separate `columns/` tree. Each file already owns the layout for
its component; the struct goes next to the constants it replaces.

```
air/src/trace/
├── mod.rs                  MainCols, indices_arr, MAIN_COL_MAP, Borrow impls
│                           + existing layout constants (re-derived for compat)
├── system.rs           NEW SystemCols
├── rows.rs                 RowIndex (unchanged)
├── main_trace.rs           MainTrace (ColMatrix wrapper, accessor methods)
├── challenges.rs           (unchanged)
├── aux_trace.rs            + AuxCols, AUX_COL_MAP
│
├── decoder/
│   └── mod.rs              + DecoderCols, EndBlockFlags
│                           (existing constants stay for compat, re-derived)
├── stack/
│   └── mod.rs              + StackCols
├── range.rs                + RangeCols
│
└── chiplets/
    ├── mod.rs              + ChipletsView, borrow_chiplet helper
    ├── hasher.rs           + HasherCols
    ├── bitwise.rs          + BitwiseCols
    ├── memory.rs           + MemoryCols
    ├── ace.rs              + AceCols, AceReadCols, AceEvalCols
    └── kernel_rom.rs       + KernelRomCols
```

The `constraints/` module does **not** define any layout constants or column
structs — it only imports them from `trace/`.

---

## Constants Strategy

### Principle

**`trace/` is the single source of truth for all column layout.** The
`constraints/` module should not define column-offset constants. During
migration, constraints can temporarily re-export from `trace/` if needed, but
the goal is for constraint code to use struct field access instead of constants.

### What happens to each category of constants

**1. Constants replaced by struct fields (delete from constraints):**

These become unnecessary — struct field names replace them entirely.

| Current (in `constraints/`) | Replaced by |
|---|---|
| `decoder/mod.rs: ADDR_OFFSET = 0` | `dec.addr` |
| `decoder/mod.rs: OP_BITS_OFFSET = 1` | `dec.op_bits[i]` |
| `decoder/mod.rs: IN_SPAN_OFFSET = 16` | `dec.in_span` |
| `decoder/mod.rs: GROUP_COUNT_OFFSET = 17` | `dec.group_count` |
| `decoder/mod.rs: OP_INDEX_OFFSET = 18` | `dec.op_index` |
| `decoder/mod.rs: BATCH_FLAGS_OFFSET = 19` | `dec.batch_flags[i]` |
| `decoder/mod.rs: EXTRA_COLS_OFFSET = 22` | `dec.extra[i]` |
| `range/mod.rs: RANGE_V_COL_IDX` | `local.range.value` |
| `range/bus.rs: RANGE_M_COL_IDX` | `local.range.multiplicity` |
| `chiplets/kernel_rom.rs: SFIRST_IDX, R0..R3_IDX` | `krom.s_first`, `krom.root[i]` |
| `chiplets/ace.rs: ACE_OFFSET` | `chiplets.ace_local()` |
| `chiplets/bus/hash_kernel.rs: S_START, H_START, IDX_COL` | `hasher.selectors[i]`, `hasher.state[i]`, `hasher.node_index` |

**2. Constants replaced by col map (move to trace, keep for compat):**

The global column-index constants in `trace/mod.rs` and `trace/chiplets/mod.rs`
are re-derived from the col map but kept exported for other crates.

```rust
// trace/mod.rs — old name, new source
pub const CLK_COL_IDX: usize = MAIN_COL_MAP.system.clk;
pub const DECODER_TRACE_OFFSET: usize = MAIN_COL_MAP.decoder.addr;
```

**3. Constants that stay in constraints (non-layout):**

These are about constraint logic, not column layout — they stay where they are.

- `constants.rs`: `F_1`, `F_2`, `TWO_POW_16`, etc. (field element values)
- `op_flags/mod.rs`: `NUM_DEGREE_*_OPS`, opcode range starts/ends
- `chiplets/hasher/periodic.rs`: periodic column indices (`P_CYCLE_ROW_0`, etc.)
- `chiplets/bitwise.rs`: `P_BITWISE_K_FIRST`, `NUM_BITS_PER_ROW`
- `chiplets/bus/chiplets.rs`: `TRANSITION_*` labels
- `decoder/bus.rs`: `OP_BIT_WEIGHTS`

**4. Chiplet-relative indices in bus constraints (replaced by struct access):**

These compute `GLOBAL_IDX - CHIPLETS_OFFSET` to index into `local.chiplets[...]`.
With `ChipletsView`, they become direct field access on the chiplet struct:

```rust
// Before (constraints/range/bus.rs)
const MEMORY_D0_IDX: usize = chiplets::MEMORY_D0_COL_IDX - CHIPLETS_OFFSET;
let d0 = local.chiplets[MEMORY_D0_IDX];

// After
let mem = chiplets.memory_local();
let d0 = mem.d0;
```

---

## Backwards Compatibility

All existing public constants in `trace/` (`CLK_COL_IDX`, `DECODER_TRACE_OFFSET`,
etc.) remain exported, re-derived from the col map. `MainTraceRow<T>` remains
as `type MainTraceRow<T> = MainCols<T>`.

Other crates (processor, prover, verifier) are unaffected; they can be migrated
separately.

---

## Usage Examples

### Constraint evaluation (entry point)

```rust
fn eval<AB: MidenAirBuilder>(&self, builder: &mut AB) {
    let main = builder.main();
    let local: &MainCols<AB::Var> = (*main.current_slice()).borrow();
    let next: &MainCols<AB::Var> = (*main.next_slice()).borrow();

    let chiplets = ChipletsView::new(local, next);
    let selectors = chiplets.build_selectors(builder);

    constraints::enforce_main(builder, local, next, &chiplets, &selectors);
    constraints::enforce_bus(builder, local, next, &chiplets, &selectors);
}
```

### System / decoder constraints

```rust
fn enforce_system<AB: MidenAirBuilder>(
    builder: &mut AB,
    local: &MainCols<AB::Var>,
    next: &MainCols<AB::Var>,
) {
    // Direct named access, .into() only at expression boundaries
    builder.when_transition().assert_eq(
        local.system.clk + AB::Expr::ONE,
        next.system.clk,
    );
}

fn enforce_decoder<AB: MidenAirBuilder>(
    builder: &mut AB,
    local: &MainCols<AB::Var>,
    next: &MainCols<AB::Var>,
) {
    let dec = &local.decoder;
    let dec_next = &next.decoder;

    builder.assert_bools(dec.op_bits);
    builder.when(flag).assert_eq(dec.addr, dec_next.addr);

    let [is_loop_body, is_loop, is_call, is_syscall] = dec.end_block_flags();
    // ...
}
```

### Chiplet constraints

```rust
fn enforce_bitwise<AB: MidenAirBuilder>(
    builder: &mut AB,
    chiplets: &ChipletsView<AB::Var>,
    flags: &ChipletFlags<AB::Expr>,
) {
    let cols = chiplets.bitwise_local();
    let cols_next = chiplets.bitwise_next();

    builder.when(flags.is_active.clone()).assert_bool(cols.op_flag);
    // ...
}
```

### Auxiliary trace

```rust
fn enforce_bus_boundary<AB: MidenAirBuilder>(builder: &mut AB) {
    let aux: &AuxCols<AB::VarEF> = builder.permutation().current_slice().borrow();

    let mut first = builder.when_first_row();
    first.assert_one_ext(aux.p1_block_stack);
    first.assert_one_ext(aux.p2_block_hash);
    first.assert_one_ext(aux.p3_op_group);
    first.assert_one_ext(aux.stack_overflow);
    first.assert_one_ext(aux.hash_kernel_vtable);
    first.assert_one_ext(aux.chiplets_bus);
    first.assert_zero_ext(aux.range_check);
    first.assert_zero_ext(aux.ace_wiring);
}
```

### Trace generation (column-major, via col map)

```rust
pub fn addr(&self, i: RowIndex) -> Felt {
    self.columns.get_column(MAIN_COL_MAP.decoder.addr)[i]
}
```

### Future row-major trace generation

```rust
let row: &mut MainCols<Felt> = row_slice.borrow_mut();
row.system.clk = Felt::from(step);
row.decoder.addr = block_addr;
row.stack.top[0] = stack_top;
```

---

## Migration Plan

### Phase 1: Struct definitions (additive, no breaking changes)

Add column structs to existing `trace/` modules (see [File Structure](#file-structure)):

- `trace/mod.rs`: `MainCols<T>`, `indices_arr`, `MAIN_COL_MAP`, `Borrow`/`BorrowMut` impls
- `trace/system.rs` (new): `SystemCols<T>`
- `trace/decoder/mod.rs`: `DecoderCols<T>`, `EndBlockFlags<T>`
- `trace/stack/mod.rs`: `StackCols<T>`
- `trace/range.rs`: `RangeCols<T>`
- `trace/chiplets/mod.rs`: `ChipletsView`, `borrow_chiplet`
- `trace/chiplets/{hasher,bitwise,memory,ace,kernel_rom}.rs`: per-chiplet col structs
- `trace/aux_trace.rs`: `AuxCols<T>`, `AUX_COL_MAP`
- Compile-time size assertions everywhere
- `type MainTraceRow<T> = MainCols<T>` compat alias
- Tests asserting col map matches all legacy constants
- Re-derive existing `trace/` constants from the col map

### Phase 2: Migrate constraints (air crate, per-component)

For each component, switch constraint code to use struct field access and delete
the layout constants from `constraints/`:

1. **System**: `local.clk` → `local.system.clk`
2. **Range**: delete `RANGE_V_COL_IDX` from constraints, use `local.range.value`
3. **Decoder**: delete `ADDR_OFFSET`/`OP_BITS_OFFSET`/etc. from `constraints/decoder/mod.rs`, remove `DecoderColumns::from_row`, use `&local.decoder`
4. **Stack**: `local.stack[0]` → `local.stack.top[0]`, `local.stack[B0_COL_IDX]` → `local.stack.b0`
5. **Chiplets**: delete chiplet-relative indices (`S_START`, `H_START`, `MEMORY_D0_IDX`, etc.) from `constraints/chiplets/`, replace `BitwiseColumns::from_row` etc. with `ChipletsView` accessors
6. **Bus**: `aux_local[P1_BLOCK_STACK]` → `aux.p1_block_stack`

### Phase 3: Update MainTrace accessors (air crate)

Replace scattered constants in `MainTrace` methods with `MAIN_COL_MAP.*`.
