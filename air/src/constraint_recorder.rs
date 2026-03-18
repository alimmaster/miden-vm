//! Debug-only constraint recorder for semantic equivalence checking.
//!
//! Records every `assert_zero` / `assert_zero_ext` constraint as a concrete field element
//! evaluated at a deterministic random point, paired with a stack trace capturing the
//! call site. The resulting set of `(fingerprint, Vec<location>)` pairs can be dumped to a file
//! and diffed across commits to identify which constraints changed.
//!
//! This module is gated behind `#[cfg(feature = "std")]` because it uses `std::backtrace`.

use alloc::string::ToString;
use std::{backtrace::Backtrace, collections::BTreeMap, string::String, vec::Vec};

use miden_core::field::{BasedVectorSpace, QuadFelt};
use miden_crypto::stark::air::{
    AirBuilder, EmptyWindow, ExtensionBuilder, PeriodicAirBuilder, PermutationAirBuilder,
    WindowAccess,
};

use crate::{
    Felt,
    trace::{AUX_TRACE_WIDTH, TRACE_WIDTH},
};

// DETERMINISTIC RANDOM POINT GENERATION
// ================================================================================================

/// Simple deterministic hash for generating pseudo-random field values.
/// Uses a basic Xorshift-style mixing to ensure distinct, non-zero values for each (domain, index).
fn deterministic_felt(domain: u64, index: u64) -> Felt {
    let mut h = domain.wrapping_mul(0x9e3779b97f4a7c15) ^ index.wrapping_mul(0x517cc1b727220a95);
    h = (h ^ (h >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
    h = (h ^ (h >> 27)).wrapping_mul(0x94d049bb133111eb);
    h ^= h >> 31;
    // Ensure non-zero by adding 1
    Felt::new(h.wrapping_add(1))
}

fn deterministic_quad(domain: u64, index: u64) -> QuadFelt {
    let a = deterministic_felt(domain, index * 2);
    let b = deterministic_felt(domain, index * 2 + 1);
    QuadFelt::new([a, b])
}

// CONSTRAINT RECORD
// ================================================================================================

/// A single recorded constraint with its evaluation fingerprint and source location.
#[derive(Debug, Clone)]
pub struct ConstraintRecord {
    /// Condensed backtrace showing only frames within `miden_air::constraints`.
    pub location: String,
}

/// Filters a backtrace to show only frames from `constraints::` or `constraint_recorder`.
fn filter_backtrace(bt: &Backtrace) -> String {
    let full = bt.to_string();
    let mut lines = Vec::new();
    let mut last_was_relevant = false;

    for line in full.lines() {
        let trimmed = line.trim();
        // Keep lines that reference our constraint modules
        let is_relevant = trimmed.contains("miden_air::constraints")
            || trimmed.contains("constraints::")
            || trimmed.contains("air::lib") // the eval() entry point
            || (trimmed.contains("at ") && last_was_relevant);

        if is_relevant
            && !trimmed.contains("constraint_recorder")
            && !trimmed.contains("FilteredAirBuilder")
        {
            lines.push(trimmed.to_string());
            last_was_relevant = true;
        } else if trimmed.starts_with("at ") && last_was_relevant {
            lines.push(format!("  {trimmed}"));
            last_was_relevant = false;
        } else {
            last_was_relevant = false;
        }
    }

    if lines.is_empty() {
        // Fallback: show the first few frames
        full.lines().take(10).collect::<Vec<_>>().join("\n")
    } else {
        lines.join("\n")
    }
}

// MAIN TRACE DATA
// ================================================================================================

/// Holds the random evaluation point data for the recorder.
struct EvalPoint {
    /// Main trace: 2 rows × TRACE_WIDTH random Felt values (row-major).
    main_values: Vec<Felt>,
    /// Aux trace: 2 rows × AUX_TRACE_WIDTH random QuadFelt values (row-major).
    aux_values: Vec<QuadFelt>,
    /// Public values: NUM_PUBLIC_VALUES random Felt values.
    public_values: Vec<Felt>,
    /// Periodic column values: one random Felt per periodic column.
    periodic_values: Vec<Felt>,
    /// Permutation challenges: random QuadFelt values.
    challenges: Vec<QuadFelt>,
    /// Permutation values: random QuadFelt values.
    perm_values: Vec<QuadFelt>,
    /// Random value for is_first_row selector.
    is_first_row: Felt,
    /// Random value for is_last_row selector.
    is_last_row: Felt,
    /// Random value for is_transition selector.
    is_transition: Felt,
}

impl EvalPoint {
    fn new(num_public_values: usize, num_periodic_cols: usize, num_challenges: usize) -> Self {
        // Domain constants for deterministic generation
        const MAIN_DOMAIN: u64 = 0x4d41494e; // "MAIN"
        const AUX_DOMAIN: u64 = 0x41555854; // "AUXT"
        const PUB_DOMAIN: u64 = 0x50554256; // "PUBV"
        const PERIODIC_DOMAIN: u64 = 0x50455249; // "PERI"
        const CHALLENGE_DOMAIN: u64 = 0x4348414c; // "CHAL"
        const SELECTOR_DOMAIN: u64 = 0x53454c45; // "SELE"
        const PERM_VAL_DOMAIN: u64 = 0x5045524d; // "PERM"

        let main_values: Vec<Felt> = (0..2 * TRACE_WIDTH)
            .map(|i| deterministic_felt(MAIN_DOMAIN, i as u64))
            .collect();
        let aux_values: Vec<QuadFelt> = (0..2 * AUX_TRACE_WIDTH)
            .map(|i| deterministic_quad(AUX_DOMAIN, i as u64))
            .collect();
        let public_values: Vec<Felt> = (0..num_public_values)
            .map(|i| deterministic_felt(PUB_DOMAIN, i as u64))
            .collect();
        let periodic_values: Vec<Felt> = (0..num_periodic_cols)
            .map(|i| deterministic_felt(PERIODIC_DOMAIN, i as u64))
            .collect();
        let challenges: Vec<QuadFelt> = (0..num_challenges)
            .map(|i| deterministic_quad(CHALLENGE_DOMAIN, i as u64))
            .collect();
        let perm_values: Vec<QuadFelt> = (0..AUX_TRACE_WIDTH)
            .map(|i| deterministic_quad(PERM_VAL_DOMAIN, i as u64))
            .collect();

        Self {
            main_values,
            aux_values,
            public_values,
            periodic_values,
            challenges,
            perm_values,
            is_first_row: deterministic_felt(SELECTOR_DOMAIN, 0),
            is_last_row: deterministic_felt(SELECTOR_DOMAIN, 1),
            is_transition: deterministic_felt(SELECTOR_DOMAIN, 2),
        }
    }
}

// CONSTRAINT RECORDER
// ================================================================================================

/// A debug AIR builder that evaluates constraints at a deterministic random point
/// and records each constraint's fingerprint along with its source location (backtrace).
///
/// Each fingerprint maps to a vector of stack trace locations. This handles:
/// - Moved constraints: same fingerprint, different stack trace
/// - Duplicate constraints: same polynomial asserted N times → N entries
/// - Reordering: BTreeMap is order-independent
pub struct ConstraintRecorder {
    eval_point: EvalPoint,
    /// Base field constraints: fingerprint → list of source locations.
    pub base_constraints: BTreeMap<u64, Vec<ConstraintRecord>>,
    /// Extension field constraints: fingerprint → list of source locations.
    pub ext_constraints: BTreeMap<(u64, u64), Vec<ConstraintRecord>>,
}

impl ConstraintRecorder {
    pub fn new(num_public_values: usize, num_periodic_cols: usize, num_challenges: usize) -> Self {
        Self {
            eval_point: EvalPoint::new(num_public_values, num_periodic_cols, num_challenges),
            base_constraints: BTreeMap::new(),
            ext_constraints: BTreeMap::new(),
        }
    }

    /// Total number of base constraints (including duplicates).
    pub fn base_count(&self) -> usize {
        self.base_constraints.values().map(|v| v.len()).sum()
    }

    /// Total number of extension constraints (including duplicates).
    pub fn ext_count(&self) -> usize {
        self.ext_constraints.values().map(|v| v.len()).sum()
    }

    /// Dumps the constraint set as sorted lines to a string.
    ///
    /// Format: `base:<fingerprint_hex> x<count> <first_stack_trace_line>`
    ///
    /// This output is deterministic and diff-friendly.
    pub fn dump(&self) -> String {
        let mut lines = Vec::new();

        let base_count = self.base_count();
        let ext_count = self.ext_count();
        lines.push(format!("# base_constraints: {base_count}"));
        lines.push(format!("# ext_constraints: {ext_count}"));
        lines.push(format!("# unique_base: {}", self.base_constraints.len()));
        lines.push(format!("# unique_ext: {}", self.ext_constraints.len()));
        lines.push("---".to_string());

        for (fp, records) in &self.base_constraints {
            let first_line = records[0].location.lines().next().unwrap_or("<unknown>");
            lines.push(format!("base:{fp:016x} x{} {first_line}", records.len()));
        }

        for ((fp0, fp1), records) in &self.ext_constraints {
            let first_line = records[0].location.lines().next().unwrap_or("<unknown>");
            lines.push(format!("ext:{fp0:016x}_{fp1:016x} x{} {first_line}", records.len()));
        }

        lines.join("\n")
    }

    /// Dumps the full constraint set with complete backtraces.
    pub fn dump_full(&self) -> String {
        let mut lines = Vec::new();

        let base_count = self.base_count();
        let ext_count = self.ext_count();
        lines.push(format!("# base_constraints: {base_count}"));
        lines.push(format!("# ext_constraints: {ext_count}"));
        lines.push(format!("# unique_base: {}", self.base_constraints.len()));
        lines.push(format!("# unique_ext: {}", self.ext_constraints.len()));
        lines.push("---".to_string());

        for (fp, records) in &self.base_constraints {
            lines.push(format!("base:{fp:016x} x{}", records.len()));
            for (i, record) in records.iter().enumerate() {
                if i > 0 {
                    lines.push(format!("  [{}]:", i));
                }
                for loc_line in record.location.lines() {
                    lines.push(format!("  {loc_line}"));
                }
            }
            lines.push(String::new());
        }

        for ((fp0, fp1), records) in &self.ext_constraints {
            lines.push(format!("ext:{fp0:016x}_{fp1:016x} x{}", records.len()));
            for (i, record) in records.iter().enumerate() {
                if i > 0 {
                    lines.push(format!("  [{}]:", i));
                }
                for loc_line in record.location.lines() {
                    lines.push(format!("  {loc_line}"));
                }
            }
            lines.push(String::new());
        }

        lines.join("\n")
    }

    fn felt_to_u64(f: Felt) -> u64 {
        f.as_canonical_u64()
    }
}

// AIRBUILDER IMPL
// ================================================================================================

/// Two-row window backed by owned data.
///
/// We need this because `RowWindow<'a, T>` borrows data, but `AirBuilder::main()` returns
/// `Self::MainWindow` by value. We store the data in the recorder and use indices.
#[derive(Clone)]
pub struct OwnedWindow<T: Clone> {
    values: Vec<T>,
    width: usize,
}

impl<T: Clone> WindowAccess<T> for OwnedWindow<T> {
    fn current_slice(&self) -> &[T] {
        &self.values[..self.width]
    }
    fn next_slice(&self) -> &[T] {
        &self.values[self.width..]
    }
}

impl AirBuilder for ConstraintRecorder {
    type F = Felt;
    type Expr = Felt;
    type Var = Felt;
    type PreprocessedWindow = EmptyWindow<Felt>;
    type MainWindow = OwnedWindow<Felt>;
    type PublicVar = Felt;

    fn main(&self) -> Self::MainWindow {
        OwnedWindow {
            values: self.eval_point.main_values.clone(),
            width: TRACE_WIDTH,
        }
    }

    fn preprocessed(&self) -> &Self::PreprocessedWindow {
        EmptyWindow::empty_ref()
    }

    fn is_first_row(&self) -> Self::Expr {
        self.eval_point.is_first_row
    }

    fn is_last_row(&self) -> Self::Expr {
        self.eval_point.is_last_row
    }

    fn is_transition_window(&self, size: usize) -> Self::Expr {
        assert_eq!(size, 2, "only window size 2 is supported");
        self.eval_point.is_transition
    }

    fn assert_zero<I: Into<Self::Expr>>(&mut self, x: I) {
        let val: Felt = x.into();
        let fp = Self::felt_to_u64(val);
        let bt = Backtrace::force_capture();
        let location = filter_backtrace(&bt);

        self.base_constraints.entry(fp).or_default().push(ConstraintRecord { location });
    }

    fn public_values(&self) -> &[Self::PublicVar] {
        &self.eval_point.public_values
    }
}

impl PeriodicAirBuilder for ConstraintRecorder {
    type PeriodicVar = Felt;

    fn periodic_values(&self) -> &[Self::PeriodicVar] {
        &self.eval_point.periodic_values
    }
}

impl ExtensionBuilder for ConstraintRecorder {
    type EF = QuadFelt;
    type ExprEF = QuadFelt;
    type VarEF = QuadFelt;

    fn assert_zero_ext<I>(&mut self, x: I)
    where
        I: Into<Self::ExprEF>,
    {
        let val: QuadFelt = x.into();
        let coeffs = val.as_basis_coefficients_slice();
        let fp = (Self::felt_to_u64(coeffs[0]), Self::felt_to_u64(coeffs[1]));
        let bt = Backtrace::force_capture();
        let location = filter_backtrace(&bt);

        self.ext_constraints.entry(fp).or_default().push(ConstraintRecord { location });
    }
}

impl PermutationAirBuilder for ConstraintRecorder {
    type MP = OwnedWindow<QuadFelt>;
    type RandomVar = QuadFelt;
    type PermutationVar = QuadFelt;

    fn permutation(&self) -> Self::MP {
        OwnedWindow {
            values: self.eval_point.aux_values.clone(),
            width: AUX_TRACE_WIDTH,
        }
    }

    fn permutation_randomness(&self) -> &[Self::RandomVar] {
        &self.eval_point.challenges
    }

    fn permutation_values(&self) -> &[Self::PermutationVar] {
        &self.eval_point.perm_values
    }
}

// This blanket impl means ConstraintRecorder automatically implements LiftedAirBuilder
// and therefore MidenAirBuilder.

// TEST
// ================================================================================================

#[cfg(all(test, feature = "std"))]
pub mod tests {
    use std::eprintln;

    use super::*;
    use crate::{LiftedAir, ProcessorAir, trace::AUX_TRACE_RAND_CHALLENGES};

    #[test]
    fn dump_constraint_fingerprints() {
        let air = ProcessorAir;
        let periodic_cols = <ProcessorAir as LiftedAir<Felt, QuadFelt>>::periodic_columns(&air);
        let num_periodic = periodic_cols.len();

        let mut recorder = ConstraintRecorder::new(
            crate::NUM_PUBLIC_VALUES,
            num_periodic,
            AUX_TRACE_RAND_CHALLENGES,
        );

        <ProcessorAir as LiftedAir<Felt, QuadFelt>>::eval(&air, &mut recorder);

        let summary = recorder.dump();
        let full = recorder.dump_full();

        // Print summary for diffing
        eprintln!("{summary}");

        // Write full output to a file for detailed comparison
        let out_path = std::env::var("CONSTRAINT_DUMP_PATH")
            .unwrap_or_else(|_| "constraint_fingerprints.txt".to_string());
        std::fs::write(&out_path, &full).expect("failed to write constraint dump");
        eprintln!("Full constraint dump written to: {out_path}");

        let base_count = recorder.base_count();
        let ext_count = recorder.ext_count();

        // Basic sanity: we should have a non-trivial number of constraints
        assert!(base_count > 100, "expected >100 base constraints, got {base_count}",);
        assert!(ext_count > 0, "expected >0 extension constraints, got {ext_count}",);

        eprintln!(
            "Total: {} base ({} unique), {} ext ({} unique)",
            base_count,
            recorder.base_constraints.len(),
            ext_count,
            recorder.ext_constraints.len(),
        );
    }
}
