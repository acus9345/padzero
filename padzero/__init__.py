"""padzero: Cross-language struct & database tuple memory layout analyzer and optimizer."""

from .engine import (
    FieldSpec,
    PlacedField,
    PaddingHole,
    FalseSharingHazard,
    LayoutReport,
    ComparisonReport,
    detect_language,
    parse_schema,
    compute_layout,
    optimize_fields,
    analyze_source,
    generate_refactored_code,
    PRESETS,
)

__version__ = "0.1.0"
__all__ = [
    "FieldSpec",
    "PlacedField",
    "PaddingHole",
    "FalseSharingHazard",
    "LayoutReport",
    "ComparisonReport",
    "detect_language",
    "parse_schema",
    "compute_layout",
    "optimize_fields",
    "analyze_source",
    "generate_refactored_code",
    "PRESETS",
]
