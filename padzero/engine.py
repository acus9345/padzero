"""Core cross-language memory layout, cache-line straddling, and tuple packing engine.

Supports:
- C11 / C++20 (System V x86_64 / AAPCS64 LP64 ABI)
- Go (gc compiler amd64/arm64 ABI, including trailing zero-sized struct{} padding)
- Rust (#[repr(C)] deterministic layout & #[repr(Rust)] size/align simulation)
- PostgreSQL (HeapTupleHeaderData 23B + null bitmap + MAXALIGN(8) + typalign column packing)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
import re
from typing import Any


CACHE_LINE_BYTES = 64
POSTGRES_TUPLE_HEADER_BYTES = 24  # MAXALIGN(23-byte HeapTupleHeaderData + 1-byte bitmap)


@dataclass(frozen=True)
class FieldSpec:
    """Normalized specification of a struct field or SQL table column."""

    name: str
    raw_type: str
    size: int
    align: int
    count: int = 1
    is_atomic_or_hot: bool = False
    is_zero_sized_marker: bool = False
    nullable: bool = True
    comment: str = ""

    @property
    def total_size(self) -> int:
        return self.size * self.count


@dataclass(frozen=True)
class PlacedField:
    """A field placed at a concrete byte offset within a struct or tuple."""

    name: str
    raw_type: str
    offset: int
    size: int
    align: int
    count: int
    is_atomic_or_hot: bool
    is_zero_sized_marker: bool
    cache_line_start: int
    cache_line_end: int
    straddles_cache_line: bool
    comment: str = ""

    @property
    def end_offset(self) -> int:
        return self.offset + self.size


@dataclass(frozen=True)
class PaddingHole:
    """Hidden alignment padding inserted before a field or at the tail of a struct."""

    offset: int
    size: int
    reason: str
    kind: str  # "inter-field", "tail", "zero-sized-guard", "tuple-header"


@dataclass(frozen=True)
class FalseSharingHazard:
    """Two concurrent/atomic/hot fields sharing the same 64-byte CPU cache line."""

    cache_line_index: int
    field_a: str
    field_b: str
    byte_distance: int
    severity: str
    remediation: str


@dataclass
class LayoutReport:
    """Complete byte-level layout report for a single field ordering."""

    struct_name: str
    language: str
    header_bytes: int
    payload_bytes: int
    padding_bytes: int
    tail_padding_bytes: int
    total_size: int
    struct_align: int
    cache_lines_spanned: int
    efficiency_pct: float
    placed_fields: list[PlacedField] = field(default_factory=list)
    padding_holes: list[PaddingHole] = field(default_factory=list)
    false_sharing_hazards: list[FalseSharingHazard] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ComparisonReport:
    """Side-by-side comparison between original and optimal packed layout."""

    struct_name: str
    language: str
    instances: int
    original: LayoutReport
    optimized: LayoutReport
    bytes_saved_per_item: int
    pct_reduction: float
    ram_saved_bytes: int
    ram_saved_human: str
    cache_lines_saved_total: int
    refactored_code: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "struct_name": self.struct_name,
            "language": self.language,
            "instances": self.instances,
            "original": self.original.to_dict(),
            "optimized": self.optimized.to_dict(),
            "bytes_saved_per_item": self.bytes_saved_per_item,
            "pct_reduction": self.pct_reduction,
            "ram_saved_bytes": self.ram_saved_bytes,
            "ram_saved_human": self.ram_saved_human,
            "cache_lines_saved_total": self.cache_lines_saved_total,
            "refactored_code": self.refactored_code,
        }


# ---------------------------------------------------------------------------
# Type Catalogs per Language (LP64 / 64-bit targets)
# ---------------------------------------------------------------------------

C_TYPES: dict[str, tuple[int, int]] = {
    "char": (1, 1),
    "signed char": (1, 1),
    "unsigned char": (1, 1),
    "bool": (1, 1),
    "_Bool": (1, 1),
    "int8_t": (1, 1),
    "uint8_t": (1, 1),
    "atomic_bool": (1, 1),
    "short": (2, 2),
    "unsigned short": (2, 2),
    "int16_t": (2, 2),
    "uint16_t": (2, 2),
    "int": (4, 4),
    "unsigned int": (4, 4),
    "int32_t": (4, 4),
    "uint32_t": (4, 4),
    "float": (4, 4),
    "atomic_int": (4, 4),
    "atomic_uint": (4, 4),
    "long": (8, 8),
    "unsigned long": (8, 8),
    "long long": (8, 8),
    "unsigned long long": (8, 8),
    "int64_t": (8, 8),
    "uint64_t": (8, 8),
    "size_t": (8, 8),
    "ssize_t": (8, 8),
    "uintptr_t": (8, 8),
    "ptrdiff_t": (8, 8),
    "double": (8, 8),
    "atomic_llong": (8, 8),
    "atomic_ullong": (8, 8),
    "atomic_size_t": (8, 8),
    "pthread_mutex_t": (40, 8),
    "__int128": (16, 16),
    "unsigned __int128": (16, 16),
    "long double": (16, 16),
}

GO_TYPES: dict[str, tuple[int, int]] = {
    "bool": (1, 1),
    "byte": (1, 1),
    "uint8": (1, 1),
    "int8": (1, 1),
    "atomic.Bool": (1, 1),
    "uint16": (2, 2),
    "int16": (2, 2),
    "uint32": (4, 4),
    "int32": (4, 4),
    "rune": (4, 4),
    "float32": (4, 4),
    "atomic.Int32": (4, 4),
    "atomic.Uint32": (4, 4),
    "sync.Mutex": (8, 4),
    "uint64": (8, 8),
    "int64": (8, 8),
    "int": (8, 8),
    "uint": (8, 8),
    "uintptr": (8, 8),
    "float64": (8, 8),
    "complex64": (8, 4),
    "time.Duration": (8, 8),
    "atomic.Int64": (8, 8),
    "atomic.Uint64": (8, 8),
    "atomic.Pointer": (8, 8),
    "string": (16, 8),
    "error": (16, 8),
    "any": (16, 8),
    "interface{}": (16, 8),
    "complex128": (16, 8),
    "time.Time": (24, 8),
    "sync.RWMutex": (24, 4),
    "struct{}": (0, 1),
}

RUST_TYPES: dict[str, tuple[int, int]] = {
    "bool": (1, 1),
    "u8": (1, 1),
    "i8": (1, 1),
    "AtomicBool": (1, 1),
    "AtomicU8": (1, 1),
    "AtomicI8": (1, 1),
    "u16": (2, 2),
    "i16": (2, 2),
    "AtomicU16": (2, 2),
    "AtomicI16": (2, 2),
    "u32": (4, 4),
    "i32": (4, 4),
    "f32": (4, 4),
    "char": (4, 4),
    "AtomicU32": (4, 4),
    "AtomicI32": (4, 4),
    "u64": (8, 8),
    "i64": (8, 8),
    "f64": (8, 8),
    "usize": (8, 8),
    "isize": (8, 8),
    "AtomicU64": (8, 8),
    "AtomicI64": (8, 8),
    "AtomicUsize": (8, 8),
    "Option<NonNull<u8>>": (8, 8),
    "u128": (16, 16),
    "i128": (16, 16),
    "Option<u64>": (16, 8),
    "Option<u32>": (8, 4),
    "String": (24, 8),
    "Vec<u8>": (24, 8),
    "Vec<u64>": (24, 8),
    "()": (0, 1),
}

SQL_TYPES: dict[str, tuple[int, int]] = {
    "boolean": (1, 1),
    "bool": (1, 1),
    "char": (1, 1),
    '"char"': (1, 1),
    "smallint": (2, 2),
    "int2": (2, 2),
    "smallserial": (2, 2),
    "integer": (4, 4),
    "int": (4, 4),
    "int4": (4, 4),
    "serial": (4, 4),
    "real": (4, 4),
    "float4": (4, 4),
    "date": (4, 4),
    "oid": (4, 4),
    "bigint": (8, 8),
    "int8": (8, 8),
    "bigserial": (8, 8),
    "double precision": (8, 8),
    "float8": (8, 8),
    "timestamp": (8, 8),
    "timestamptz": (8, 8),
    "timestamp with time zone": (8, 8),
    "timestamp without time zone": (8, 8),
    "time": (8, 8),
    "money": (8, 8),
    "uuid": (16, 1),  # Postgres uuid has typalign = 'c' (1-byte align, 16B len!)
    "interval": (16, 8),
    "macaddr8": (8, 4),
    "macaddr": (6, 4),
}


def align_up(offset: int, align: int) -> int:
    if align <= 1:
        return offset
    rem = offset % align
    return offset if rem == 0 else offset + (align - rem)


def format_bytes(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.2f} KB"
    if num_bytes < 1024 * 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.2f} MB"
    return f"{num_bytes / (1024 * 1024 * 1024):.2f} GB"


def _extract_brace_body(source: str) -> str:
    first = source.find("{")
    last = source.rfind("}")
    if first != -1 and last > first:
        return source[first + 1 : last]
    return source


def detect_language(source: str) -> str:
    """Automatically detects whether source is C, Go, Rust, or PostgreSQL DDL."""
    s = source.strip()
    if re.search(r"\bCREATE\s+TABLE\b", s, re.IGNORECASE):
        return "sql"
    if re.search(r"\btype\s+\w+\s+struct\b", s):
        return "go"
    if re.search(r"\b(?:pub\s+)?struct\s+\w+\s*\{", s) and re.search(
        r"\b\w+\s*:\s*[\w<\[\]&]", s
    ):
        return "rust"
    return "c"


def _is_hot_or_atomic(name: str, raw_type: str, comment: str) -> bool:
    combined = f"{name} {raw_type} {comment}".lower()
    keywords = (
        "atomic",
        "_atomic",
        "mutex",
        "rwmutex",
        "spinlock",
        "@hot",
        "@atomic",
        "@contended",
        "seq_lock",
        "write_seq",
        "consumer_head",
        "producer_tail",
        "producer_seq",
        "consumer_seq",
    )
    return any(k in combined for k in keywords)


def parse_c_struct(source: str) -> tuple[str, list[FieldSpec]]:
    name_match = re.search(r"struct\s+(\w+)\s*\{", source)
    tds_match = re.search(r"\}\s*(\w+)\s*;", source)
    struct_name = (
        name_match.group(1)
        if name_match
        else (tds_match.group(1) if tds_match else "AnonymousStruct")
    )
    body = _extract_brace_body(source)
    fields: list[FieldSpec] = []

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//") or line.startswith("/*"):
            continue
        comment = ""
        if "//" in line:
            line, comment = line.split("//", 1)
            line = line.strip()
            comment = comment.strip()
        line = re.sub(r"/\*.*?\*/", "", line).strip()
        if not line.endswith(";"):
            continue
        decl = line[:-1].strip()
        if not decl:
            continue

        arr_match = re.search(r"(\w+)\s*\[(\d+)\]\s*$", decl)
        count = 1
        if arr_match:
            field_name = arr_match.group(1)
            count = max(1, int(arr_match.group(2)))
            type_part = decl[: arr_match.start()].strip()
        else:
            ptr_match = re.search(r"(\*+)\s*(\w+)\s*$", decl)
            if ptr_match:
                field_name = ptr_match.group(2)
                type_part = decl[: ptr_match.end(1)].strip()
            else:
                parts = decl.rsplit(None, 1)
                if len(parts) != 2:
                    continue
                type_part, field_name = parts[0].strip(), parts[1].strip()

        clean_type = re.sub(r"\b(volatile|const|restrict|_Atomic)\b", "", type_part).strip()
        clean_type = " ".join(clean_type.split())

        if "*" in clean_type or "*" in type_part:
            size, align = 8, 8
        elif clean_type in C_TYPES:
            size, align = C_TYPES[clean_type]
        else:
            size, align = 4, 4

        hot = _is_hot_or_atomic(field_name, type_part, comment)
        display_type = f"{type_part}[{count}]" if count > 1 else type_part
        fields.append(
            FieldSpec(
                name=field_name,
                raw_type=display_type,
                size=size,
                align=align,
                count=count,
                is_atomic_or_hot=hot,
                comment=comment,
            )
        )
    return struct_name, fields


def parse_go_struct(source: str) -> tuple[str, list[FieldSpec]]:
    name_match = re.search(r"type\s+(\w+)\s+struct\s*\{", source)
    struct_name = name_match.group(1) if name_match else "GoStruct"
    body = _extract_brace_body(source)
    fields: list[FieldSpec] = []

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue
        comment = ""
        if "//" in line:
            line, comment = line.split("//", 1)
            line = line.strip()
            comment = comment.strip()
        line = re.sub(r"`[^`]*`", "", line).strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        field_name, type_part = parts[0].strip(), parts[1].strip()

        count = 1
        arr_match = re.match(r"^\[(\d+)\](.+)$", type_part)
        base_type = type_part
        if arr_match:
            count = max(1, int(arr_match.group(1)))
            base_type = arr_match.group(2).strip()

        is_zero = False
        if base_type == "struct{}":
            size, align = 0, 1
            is_zero = True
        elif (
            base_type.startswith("*")
            or base_type.startswith("map[")
            or base_type.startswith("chan ")
            or base_type.startswith("func(")
        ):
            size, align = 8, 8
        elif base_type.startswith("[]"):
            size, align = 24, 8
        elif base_type.startswith("atomic.Pointer"):
            size, align = 8, 8
        elif base_type in GO_TYPES:
            size, align = GO_TYPES[base_type]
        else:
            size, align = 8, 8

        hot = _is_hot_or_atomic(field_name, type_part, comment)
        fields.append(
            FieldSpec(
                name=field_name,
                raw_type=type_part,
                size=size,
                align=align,
                count=count,
                is_atomic_or_hot=hot,
                is_zero_sized_marker=is_zero,
                comment=comment,
            )
        )
    return struct_name, fields


def parse_rust_struct(source: str) -> tuple[str, list[FieldSpec]]:
    name_match = re.search(r"struct\s+(\w+)\s*\{", source)
    struct_name = name_match.group(1) if name_match else "RustStruct"
    body = _extract_brace_body(source)
    fields: list[FieldSpec] = []

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//") or line.startswith("#["):
            continue
        comment = ""
        if "//" in line:
            line, comment = line.split("//", 1)
            line = line.strip()
            comment = comment.strip()
        line = line.rstrip(",").strip()
        if ":" not in line:
            continue
        left, right = line.split(":", 1)
        field_name = re.sub(r"^\s*pub(?:\([^)]*\))?\s+", "", left).strip()
        type_part = right.strip()

        count = 1
        arr_match = re.match(r"^\[([^;]+);\s*(\d+)\]$", type_part)
        base_type = type_part
        if arr_match:
            base_type = arr_match.group(1).strip()
            count = max(1, int(arr_match.group(2)))

        is_zero = False
        if base_type == "()" or base_type.startswith("PhantomData"):
            size, align = 0, 1
            is_zero = True
        elif (
            base_type.startswith("&")
            or base_type.startswith("*const")
            or base_type.startswith("*mut")
            or base_type.startswith("Box<")
            or base_type.startswith("Arc<")
        ):
            size, align = 8, 8
        elif base_type.startswith("Vec<"):
            size, align = 24, 8
        elif base_type in RUST_TYPES:
            size, align = RUST_TYPES[base_type]
        else:
            size, align = 8, 8

        hot = _is_hot_or_atomic(field_name, type_part, comment)
        fields.append(
            FieldSpec(
                name=field_name,
                raw_type=type_part,
                size=size,
                align=align,
                count=count,
                is_atomic_or_hot=hot,
                is_zero_sized_marker=is_zero,
                comment=comment,
            )
        )
    return struct_name, fields


def parse_sql_table(source: str) -> tuple[str, list[FieldSpec]]:
    name_match = re.search(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.]+)\s*\(",
        source,
        re.IGNORECASE,
    )
    table_name = name_match.group(1) if name_match else "postgres_table"
    first_paren = source.find("(")
    last_paren = source.rfind(")")
    body = (
        source[first_paren + 1 : last_paren]
        if (first_paren != -1 and last_paren > first_paren)
        else source
    )
    fields: list[FieldSpec] = []

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        comment = ""
        if "--" in line:
            line, comment = line.split("--", 1)
            line = line.strip()
            comment = comment.strip()
        line = line.rstrip(",").strip()
        if not line:
            continue
        upper_head = line.upper()
        if upper_head.startswith(
            ("PRIMARY KEY", "FOREIGN KEY", "CONSTRAINT", "UNIQUE", "CHECK")
        ):
            continue

        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        col_name = parts[0].strip('"')
        rest = parts[1].strip()
        nullable = "NOT NULL" not in rest.upper() and "PRIMARY KEY" not in rest.upper()

        type_clean = re.split(
            r"\b(?:NOT\s+NULL|NULL|DEFAULT|PRIMARY\s+KEY|REFERENCES|UNIQUE|CHECK)\b",
            rest,
            flags=re.IGNORECASE,
        )[0].strip()
        type_lower = " ".join(type_clean.lower().split())

        char_match = re.match(r"^(?:character|char)\s*\((\d+)\)$", type_lower)
        if char_match:
            n = int(char_match.group(1))
            size, align = n + 1, 4 if n > 1 else 1
        elif type_lower in SQL_TYPES:
            size, align = SQL_TYPES[type_lower]
        else:
            size, align = 8, 4

        fields.append(
            FieldSpec(
                name=col_name,
                raw_type=type_clean.upper(),
                size=size,
                align=align,
                count=1,
                is_atomic_or_hot=False,
                nullable=nullable,
                comment=comment,
            )
        )
    return table_name, fields


def parse_schema(source: str, language: str = "auto") -> tuple[str, str, list[FieldSpec]]:
    lang = detect_language(source) if language == "auto" else language.lower()
    if lang == "go":
        name, fields = parse_go_struct(source)
    elif lang == "rust":
        name, fields = parse_rust_struct(source)
    elif lang == "sql":
        name, fields = parse_sql_table(source)
    else:
        lang = "c"
        name, fields = parse_c_struct(source)
    return lang, name, fields


def compute_layout(
    struct_name: str,
    language: str,
    fields: list[FieldSpec],
) -> LayoutReport:
    """Computes exact byte offsets, padding holes, cache lines, and false-sharing hazards."""
    header_bytes = POSTGRES_TUPLE_HEADER_BYTES if language == "sql" else 0
    offset = header_bytes
    max_align = 8 if language == "sql" else 1
    placed: list[PlacedField] = []
    holes: list[PaddingHole] = []
    notes: list[str] = []

    if language == "sql":
        notes.append(
            "PostgreSQL HeapTupleHeaderData occupies 23B + 1B null-bitmap alignment padding (t_hoff = 24B)."
        )

    for idx, f in enumerate(fields):
        is_last = idx == len(fields) - 1
        eff_align = max(1, f.align)
        max_align = max(max_align, eff_align)

        aligned_offset = align_up(offset, eff_align)
        pad = aligned_offset - offset
        if pad > 0:
            holes.append(
                PaddingHole(
                    offset=offset,
                    size=pad,
                    reason=f"Align '{f.name}' ({f.raw_type}) to {eff_align}-byte boundary",
                    kind="inter-field",
                )
            )
        offset = aligned_offset

        f_bytes = f.total_size
        if language == "go" and f.is_zero_sized_marker and is_last and offset > 0:
            holes.append(
                PaddingHole(
                    offset=offset,
                    size=1,
                    reason=(
                        f"Go trailing zero-sized field '{f.name}' (struct{{}}) at end of struct "
                        "forces 1 guard byte + tail alignment padding"
                    ),
                    kind="zero-sized-guard",
                )
            )
            notes.append(
                f"Go ABI Trap: Trailing zero-sized field '{f.name}' at the end of a struct forces 1 extra byte plus tail alignment!"
            )
            cl_start = offset // CACHE_LINE_BYTES
            cl_end = cl_start
            placed.append(
                PlacedField(
                    name=f.name,
                    raw_type=f.raw_type,
                    offset=offset,
                    size=0,
                    align=eff_align,
                    count=f.count,
                    is_atomic_or_hot=f.is_atomic_or_hot,
                    is_zero_sized_marker=True,
                    cache_line_start=cl_start,
                    cache_line_end=cl_end,
                    straddles_cache_line=False,
                    comment=f.comment,
                )
            )
            offset += 1
            continue

        cl_start = offset // CACHE_LINE_BYTES
        cl_end = (
            (offset + max(0, f_bytes - 1)) // CACHE_LINE_BYTES if f_bytes > 0 else cl_start
        )
        straddles = f_bytes > 0 and (cl_start != cl_end)
        if straddles:
            notes.append(
                f"Cache-Line Split: Field '{f.name}' ({f.raw_type}) straddles 64B cache line boundary at 0x{(cl_start + 1) * 64:02X}."
            )

        placed.append(
            PlacedField(
                name=f.name,
                raw_type=f.raw_type,
                offset=offset,
                size=f_bytes,
                align=eff_align,
                count=f.count,
                is_atomic_or_hot=f.is_atomic_or_hot,
                is_zero_sized_marker=f.is_zero_sized_marker,
                cache_line_start=cl_start,
                cache_line_end=cl_end,
                straddles_cache_line=straddles,
                comment=f.comment,
            )
        )
        offset += f_bytes

    total_size = align_up(offset, max_align) if offset > 0 else 0
    tail_pad = total_size - offset
    if tail_pad > 0:
        holes.append(
            PaddingHole(
                offset=offset,
                size=tail_pad,
                reason=f"Tail padding to satisfy struct alignment ({max_align}-byte stride)",
                kind="tail",
            )
        )

    payload_bytes = sum(f.total_size for f in fields)
    padding_bytes = sum(h.size for h in holes)
    cache_lines_spanned = (
        max(1, math.ceil(total_size / CACHE_LINE_BYTES)) if total_size > 0 else 0
    )
    denom = total_size - header_bytes
    efficiency = (payload_bytes / denom * 100.0) if denom > 0 else 100.0

    hazards: list[FalseSharingHazard] = []
    hot_placed = [pf for pf in placed if pf.is_atomic_or_hot]
    for i in range(len(hot_placed)):
        for j in range(i + 1, len(hot_placed)):
            a, b = hot_placed[i], hot_placed[j]
            if (
                a.cache_line_start == b.cache_line_start
                or a.cache_line_end == b.cache_line_start
            ):
                dist = max(0, b.offset - (a.offset + a.size))
                hazards.append(
                    FalseSharingHazard(
                        cache_line_index=a.cache_line_start,
                        field_a=a.name,
                        field_b=b.name,
                        byte_distance=dist,
                        severity="CRITICAL",
                        remediation=(
                            f"Separate '{a.name}' and '{b.name}' across 64B cache lines "
                            "(e.g., insert cache-line pad or alignas(64))."
                        ),
                    )
                )

    return LayoutReport(
        struct_name=struct_name,
        language=language,
        header_bytes=header_bytes,
        payload_bytes=payload_bytes,
        padding_bytes=padding_bytes,
        tail_padding_bytes=tail_pad,
        total_size=total_size,
        struct_align=max_align,
        cache_lines_spanned=cache_lines_spanned,
        efficiency_pct=round(efficiency, 1),
        placed_fields=placed,
        padding_holes=holes,
        false_sharing_hazards=hazards,
        notes=notes,
    )


def optimize_fields(fields: list[FieldSpec], language: str) -> list[FieldSpec]:
    """Computes an optimal field ordering that minimizes alignment and tail padding."""
    if not fields:
        return []

    zero_fields = [f for f in fields if f.is_zero_sized_marker or f.total_size == 0]
    remaining = [f for f in fields if not (f.is_zero_sized_marker or f.total_size == 0)]

    indexed = list(enumerate(remaining))
    indexed.sort(key=lambda pair: (-pair[1].align, -pair[1].total_size, pair[0]))

    ordered: list[FieldSpec] = list(zero_fields)
    offset = POSTGRES_TUPLE_HEADER_BYTES if language == "sql" else 0
    pool = list(indexed)

    while pool:
        zero_pad_idx = None
        for idx, (_, cand) in enumerate(pool):
            if align_up(offset, cand.align) == offset:
                zero_pad_idx = idx
                break
        if zero_pad_idx is not None:
            _, chosen = pool.pop(zero_pad_idx)
        else:
            _, chosen = pool.pop(0)
        offset = align_up(offset, chosen.align) + chosen.total_size
        ordered.append(chosen)

    return ordered


def generate_refactored_code(
    struct_name: str,
    language: str,
    fields: list[FieldSpec],
    report: LayoutReport,
) -> str:
    """Generates clean, copy-pasteable refactored struct/table definition with byte offset comments."""
    lines: list[str] = []

    if language == "c":
        lines.append(
            f"// Optimized by padzero: {report.total_size}B total "
            f"({report.padding_bytes}B padding, {report.efficiency_pct}% density)"
        )
        lines.append(f"struct {struct_name} {{")
        for f, pf in zip(fields, report.placed_fields):
            arr_suffix = ""
            base_t = f.raw_type
            m = re.match(r"^(.+)\[(\d+)\]$", f.raw_type)
            if m:
                base_t = m.group(1)
                arr_suffix = f"[{m.group(2)}]"
            lines.append(
                f"    {base_t} {f.name}{arr_suffix}; "
                f"// offset: {pf.offset:2d}B | size: {pf.size:2d}B | align: {pf.align}B"
            )
        lines.append("};")
    elif language == "go":
        lines.append(
            f"// Optimized by padzero: {report.total_size}B total "
            f"({report.padding_bytes}B padding, {report.efficiency_pct}% density)"
        )
        lines.append(f"type {struct_name} struct {{")
        for f, pf in zip(fields, report.placed_fields):
            lines.append(
                f"\t{f.name:<18} {f.raw_type:<14} "
                f"// offset: {pf.offset:2d}B | size: {pf.size:2d}B | align: {pf.align}B"
            )
        lines.append("}")
    elif language == "rust":
        lines.append(
            f"// Optimized by padzero: {report.total_size}B total "
            f"({report.padding_bytes}B padding, {report.efficiency_pct}% density)"
        )
        lines.append("#[repr(C)]")
        lines.append(f"pub struct {struct_name} {{")
        for f, pf in zip(fields, report.placed_fields):
            lines.append(
                f"    pub {f.name}: {f.raw_type}, "
                f"// offset: {pf.offset:2d}B | size: {pf.size:2d}B | align: {pf.align}B"
            )
        lines.append("}")
    elif language == "sql":
        lines.append(
            f"-- Optimized by padzero (Column Tetris): {report.total_size}B/tuple "
            f"(24B header + {report.payload_bytes}B data + {report.padding_bytes}B pad)"
        )
        lines.append(f"CREATE TABLE {struct_name} (")
        for idx, (f, pf) in enumerate(zip(fields, report.placed_fields)):
            comma = "," if idx < len(fields) - 1 else ""
            null_str = "" if f.nullable else " NOT NULL"
            lines.append(
                f"    {f.name:<18} {f.raw_type}{null_str}{comma:<2} "
                f"-- offset: {pf.offset:2d}B | size: {pf.size:2d}B | typalign: {pf.align}B"
            )
        lines.append(");")
    return "\n".join(lines)


def analyze_source(
    source: str,
    language: str = "auto",
    instances: int = 10_000_000,
) -> ComparisonReport:
    """Parses source, computes original and packed layouts, and returns full comparison metrics."""
    lang, struct_name, fields = parse_schema(source, language)
    original_report = compute_layout(struct_name, lang, fields)
    opt_fields = optimize_fields(fields, lang)
    optimized_report = compute_layout(struct_name, lang, opt_fields)

    if optimized_report.total_size > original_report.total_size:
        opt_fields = list(fields)
        optimized_report = original_report

    saved_per_item = max(0, original_report.total_size - optimized_report.total_size)
    pct_reduction = (
        round(saved_per_item / original_report.total_size * 100.0, 1)
        if original_report.total_size > 0
        else 0.0
    )
    ram_saved = saved_per_item * instances
    cl_saved = max(
        0,
        (original_report.cache_lines_spanned - optimized_report.cache_lines_spanned)
        * instances,
    )
    refactored = generate_refactored_code(struct_name, lang, opt_fields, optimized_report)

    return ComparisonReport(
        struct_name=struct_name,
        language=lang,
        instances=instances,
        original=original_report,
        optimized=optimized_report,
        bytes_saved_per_item=saved_per_item,
        pct_reduction=pct_reduction,
        ram_saved_bytes=ram_saved,
        ram_saved_human=format_bytes(ram_saved),
        cache_lines_saved_total=cl_saved,
        refactored_code=refactored,
    )


PRESETS: dict[str, dict[str, str]] = {
    "c_orderbook": {
        "title": "C11 HFT OrderBook Tick Node",
        "language": "c",
        "description": "Alternating 1B flags and 8B pointers/timestamps waste 32 bytes per node and split the struct across two 64B L1 cache lines.",
        "code": """struct OrderTickNode {
    bool is_aggressive;          // 1B flag
    uint64_t order_id;           // 8B identifier
    uint8_t venue_id;            // 1B exchange code
    double limit_price;          // 8B IEEE-754 price
    char side_flag;              // 1B 'B' or 'S'
    uint64_t match_ns;           // 8B nanosecond timestamp
    uint16_t flags;              // 2B bitfield
    void *next_order;            // 8B pointer
    uint8_t shard_tag[3];        // 3B routing tag
    atomic_ullong producer_seq;  // 8B @hot producer cursor
    atomic_ullong consumer_seq;  // 8B @hot consumer cursor
};""",
    },
    "go_session": {
        "title": "Go Concurrent Worker Session (with struct{} Trap)",
        "language": "go",
        "description": "Includes Go's infamous trailing zero-sized struct{} padding trap plus interleaved bools and 16B/24B headers.",
        "code": """type SessionWorker struct {
    Active       bool           // 1B + 7B hidden pad
    TenantID     uint64         // 8B
    Compression  uint8          // 1B + 7B hidden pad
    Endpoint     string         // 16B string header (ptr+len)
    RetryCount   uint16         // 2B + 6B hidden pad
    PayloadBuf   []byte         // 24B slice header (ptr+len+cap)
    WriteHits    atomic.Int64   // 8B @hot atomic counter
    ReadHits     atomic.Int64   // 8B @hot atomic counter
    noCopy       struct{}       // 0B trailing field forces +8B tail pad!
}""",
    },
    "rust_telemetry": {
        "title": "Rust #[repr(C)] FFI Telemetry Frame",
        "language": "rust",
        "description": "FFI #[repr(C)] disables Rust's compiler reordering, leaving 28 bytes of alignment holes around u128 and String headers.",
        "code": """#[repr(C)]
pub struct TelemetryFrame {
    pub version: u8,             // 1B
    pub trace_id: u128,          // 16B (requires 16B alignment!)
    pub sampled: bool,           // 1B
    pub span_id: u64,            // 8B
    pub status_code: u16,        // 2B
    pub service_name: String,    // 24B (align 8)
    pub cpu_cycles: AtomicU64,   // 8B @hot
    pub drop_count: AtomicU64,   // 8B @hot
    pub region_code: [u8; 3],    // 3B
}""",
    },
    "sql_ledger": {
        "title": "PostgreSQL Ledger Table (HeapTuple Column Tetris)",
        "language": "sql",
        "description": "Interleaving SMALLINT/BOOLEAN with BIGINT/TIMESTAMPTZ after the 24-byte HeapTupleHeader wastes 20 bytes per row on disk and in shared_buffers.",
        "code": """CREATE TABLE ledger_events (
    is_settled BOOLEAN NOT NULL,
    account_id BIGINT NOT NULL,
    region_id SMALLINT NOT NULL,
    settled_at TIMESTAMPTZ NOT NULL,
    fee_tier BOOLEAN NOT NULL,
    amount_cents BIGINT NOT NULL,
    currency_code SMALLINT NOT NULL,
    tx_uuid UUID NOT NULL,
    audit_seq BIGINT NOT NULL
);""",
    },
}
