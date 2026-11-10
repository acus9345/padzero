"""Command-line interface for padzero."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Sequence

from .engine import PRESETS, ComparisonReport, analyze_source


def render_ascii_report(comp: ComparisonReport) -> str:
    orig = comp.original
    opt = comp.optimized
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append(
        f" PADZERO MEMORY LAYOUT AUDIT :: {comp.struct_name} ({comp.language.upper()})"
    )
    lines.append("=" * 78)
    lines.append(
        f" CURRENT SIZE   : {orig.total_size:3d} B  "
        f"(payload: {orig.payload_bytes:2d}B | padding: {orig.padding_bytes:2d}B | "
        f"efficiency: {orig.efficiency_pct:5.1f}% | L1 lines: {orig.cache_lines_spanned})"
    )
    lines.append(
        f" OPTIMIZED SIZE : {opt.total_size:3d} B  "
        f"(payload: {opt.payload_bytes:2d}B | padding: {opt.padding_bytes:2d}B | "
        f"efficiency: {opt.efficiency_pct:5.1f}% | L1 lines: {opt.cache_lines_spanned})"
    )
    lines.append(
        f" SAVINGS        : -{comp.bytes_saved_per_item} B/item (-{comp.pct_reduction:.1f}%) "
        f"=> {comp.ram_saved_human} RAM saved across {comp.instances:,} instances"
    )
    lines.append("-" * 78)
    lines.append(" CURRENT BYTE MAP:")
    for pf in orig.placed_fields:
        split_tag = " [STRADDLES 64B LINE!]" if pf.straddles_cache_line else ""
        hot_tag = " [HOT/ATOMIC]" if pf.is_atomic_or_hot else ""
        lines.append(
            f"   0x{pf.offset:02X}..0x{max(pf.offset, pf.end_offset - 1):02X} "
            f"({pf.size:2d}B, align {pf.align:2d}B)  {pf.name:<18} : {pf.raw_type}{hot_tag}{split_tag}"
        )
    if orig.padding_holes:
        lines.append("-" * 78)
        lines.append(" DETECTED PADDING HOLES:")
        for hole in orig.padding_holes:
            lines.append(
                f"   [PAD] 0x{hole.offset:02X} (+{hole.size}B) :: {hole.reason}"
            )
    if orig.false_sharing_hazards:
        lines.append("-" * 78)
        lines.append(" CONCURRENT FALSE-SHARING HAZARDS:")
        for haz in orig.false_sharing_hazards:
            lines.append(
                f"   [!] Cache Line #{haz.cache_line_index}: '{haz.field_a}' & '{haz.field_b}' "
                f"are {haz.byte_distance}B apart ({haz.severity})"
            )
    lines.append("-" * 78)
    lines.append(" REFACTORED ZERO-COST DEFINITION:")
    lines.append(comp.refactored_code)
    lines.append("=" * 78)
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="padzero",
        description="Cross-language struct & SQL tuple alignment, cache-line & false-sharing optimizer.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default="c_orderbook",
        help="Path to source file (.c, .h, .go, .rs, .sql) or preset name (c_orderbook, go_session, rust_telemetry, sql_ledger)",
    )
    parser.add_argument(
        "--lang",
        default="auto",
        choices=["auto", "c", "go", "rust", "sql"],
        help="Explicit language override (default: auto)",
    )
    parser.add_argument(
        "--instances",
        type=int,
        default=10_000_000,
        help="Fleet instance count for RAM & cache-line savings calculation",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON report",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit with code 1 if struct can be packed smaller (for CI gates)",
    )
    args = parser.parse_args(argv)

    if args.target in PRESETS:
        preset = PRESETS[args.target]
        source = preset["code"]
        lang = preset["language"] if args.lang == "auto" else args.lang
    elif os.path.exists(args.target):
        with open(args.target, encoding="utf-8") as f:
            source = f.read()
        lang = args.lang
    else:
        print(f"Error: '{args.target}' is neither a file nor a known preset.", file=sys.stderr)
        return 2

    report = analyze_source(source, language=lang, instances=args.instances)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_ascii_report(report))

    if args.check and report.bytes_saved_per_item > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
