# `padzero` — Cross-Language ABI Struct & Database Tuple Layout Analyzer

`padzero` is a zero-dependency static memory-layout analyzer, 64-byte CPU cache-line inspector, and automatic field packer for **C11/C++20**, **Go (`gc` ABI)**, **Rust (`#[repr(C)]`)**, and **PostgreSQL (`HeapTuple` on-disk row layouts)** — paired with a standalone hardware-datasheet browser workbench.

Unlike Linux `pahole` (which requires compiling a C/C++ binary with `-g` DWARF symbols), Go's `fieldalignment` (Go-only), or Rust's nightly `-Zprint-type-sizes`, `padzero` operates directly on raw source definitions across all four ecosystems without invoking a compiler or database server.

## Install

```bash
pip install git+https://github.com/acus9345/padzero.git
```

Or from a local checkout:

```bash
pip install .
```

## Why It Exists


1. **Silent Struct Padding Across Languages**: Interleaving 1-byte booleans, 2-byte integers, and 8-byte pointers/timestamps routinely inflates in-memory structs by **20%–45%**.
2. **Go's Trailing `struct{}` Trap**: Placing a zero-sized `struct{}` (e.g., `noCopy struct{}`) at the *end* of a non-empty Go struct forces the Go runtime to insert 1 guard byte plus up to 7 bytes of tail alignment padding so pointers to the field cannot alias the next heap allocation. Moving it to offset `0` costs `0` bytes.
3. **PostgreSQL "Column Tetris" (`HeapTupleHeaderData`)**: Every PostgreSQL row starts with a 23-byte `HeapTupleHeaderData` padded to `t_hoff = 24` bytes (`MAXALIGN(8)`), followed by columns aligned according to `pg_type.typalign` (`d`=8B, `i`=4B, `s`=2B, `c`=1B). Poor column order wastes 16–24 bytes per row across billions of rows in `shared_buffers` and WAL segments.
4. **64-Byte L1 Cache-Line Splits & Atomic False Sharing**: Fields that straddle a `0x40` (64B) boundary require two L1 cache line loads per access, while two concurrent `_Atomic` / `atomic.Int64` / `AtomicU64` counters on the same 64B cache line cause core-to-core MESI invalidation storms.

## Quickstart (CLI & CI Gate)

```bash
# Run on a built-in preset (c_orderbook, go_session, rust_telemetry, sql_ledger)
python3 -m padzero.cli c_orderbook

# Analyze any source file (.c, .h, .go, .rs, .sql) with fleet instance count
python3 -m padzero.cli examples/session_worker.go --instances 50000000

# Use as a zero-dependency CI gate (exits 1 if bytes can be saved)
python3 -m padzero.cli examples/orderbook_node.c --check

# Emit machine-readable JSON for build pipelines
python3 -m padzero.cli sql_ledger --json
```

## Interactive Hardware Datasheet Workbench (`web/index.html`)

Open `web/index.html` in any browser (zero build step, zero server required) to inspect:
- **Byte-Accurate 64B Cache-Line Memory Grid**: Every single byte (`0x00`..`0x7F`) rendered to scale, with diagonal hatched cells (`////`) marking alignment holes and tail padding.
- **Side-by-Side Current vs. Packed Layout**: Toggle or reorder fields manually (`↑` / `↓`), add custom fields, or run the greedy alignment bin-packer (`[PACK OPTIMAL]`).
- **False-Sharing & Cache-Line Split Telemetry**: Highlights straddling fields and co-located `@hot` / `atomic` fields sharing a 64-byte L1 line.
- **One-Click Refactored Code & Unified Diff**: Generates ready-to-paste C11, Go, Rust, or PostgreSQL DDL with exact offset annotations.

## Running Tests

```bash
python3 -m unittest discover -s tests -v
```
