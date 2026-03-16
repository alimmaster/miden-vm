//! Constraint tagging helpers for stable numeric IDs.
//!
//! This module dispatches to the full tagging implementation in test/`testing` builds
//! and a no-op fallback in production/no-std builds.

use miden_crypto::stark::air::ExtensionBuilder;

pub mod ids;

#[cfg(all(any(test, feature = "testing"), feature = "std"))]
mod enabled;
#[cfg(not(all(any(test, feature = "testing"), feature = "std")))]
mod fallback;

#[cfg(all(test, feature = "std"))]
mod fixtures;
#[cfg(all(test, feature = "std"))]
mod ood_eval;
#[cfg(all(any(test, feature = "testing"), feature = "std"))]
mod state;

#[cfg(all(any(test, feature = "testing"), feature = "std"))]
pub use enabled::*;
#[cfg(not(all(any(test, feature = "testing"), feature = "std")))]
pub use fallback::*;

/// Tag metadata for a constraint group (base ID + ordered names).
#[derive(Clone, Copy)]
pub struct TagGroup {
    pub base: usize,
    pub names: &'static [&'static str],
}

/// Tag and assert a single extension-field constraint, advancing the per-group index.
pub fn tagged_assert_zero_ext<AB: TaggingAirBuilderExt>(
    builder: &mut AB,
    group: &TagGroup,
    idx: &mut usize,
    expr: AB::ExprEF,
) {
    debug_assert!(*idx < group.names.len(), "tag index out of bounds");
    let id = group.base + *idx;
    let name = group.names[*idx];
    builder.tagged(id, name, |builder| {
        builder.when_transition().assert_zero_ext(expr);
    });
    *idx += 1;
}
