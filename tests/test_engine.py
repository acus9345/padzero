"""Unit test suite for padzero cross-language layout and optimization engine."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from padzero.cli import main as cli_main
from padzero.engine import (
    PRESETS,
    FieldSpec,
    analyze_source,
    compute_layout,
    detect_language,
)


class TestPadZeroEngine(unittest.TestCase):

    def test_c_orderbook_preset_packing(self) -> None:
        preset = PRESETS["c_orderbook"]
        comp = analyze_source(preset["code"], language="c", instances=10_000_000)
        self.assertEqual(comp.language, "c")
        self.assertEqual(comp.struct_name, "OrderTickNode")
        self.assertGreater(comp.original.total_size, comp.optimized.total_size)
        self.assertGreaterEqual(comp.bytes_saved_per_item, 16)
        self.assertEqual(comp.original.payload_bytes, comp.optimized.payload_bytes)
        self.assertGreaterEqual(len(comp.original.false_sharing_hazards), 1)

    def test_go_trailing_zero_sized_struct_trap(self) -> None:
        go_src = """type BadTrap struct {
            ID uint64
            Guard struct{}
        }"""
        comp = analyze_source(go_src, language="go")
        # In Go, a trailing struct{} at the end of a non-empty struct adds 1 guard byte + 7 tail pad = 16B!
        self.assertEqual(comp.original.total_size, 16)
        # Placing struct{} first eliminates the trailing guard byte so total size is 8B!
        self.assertEqual(comp.optimized.total_size, 8)
        self.assertEqual(comp.bytes_saved_per_item, 8)

    def test_rust_repr_c_u128_alignment(self) -> None:
        comp = analyze_source(PRESETS["rust_telemetry"]["code"], language="rust")
        self.assertEqual(comp.struct_name, "TelemetryFrame")
        self.assertEqual(comp.original.struct_align, 16)
        self.assertLess(comp.optimized.total_size, comp.original.total_size)
        self.assertEqual(comp.original.payload_bytes, comp.optimized.payload_bytes)

    def test_postgres_heap_tuple_column_tetris(self) -> None:
        comp = analyze_source(PRESETS["sql_ledger"]["code"], language="sql")
        self.assertEqual(comp.original.header_bytes, 24)
        self.assertLess(comp.optimized.total_size, comp.original.total_size)
        self.assertGreaterEqual(comp.bytes_saved_per_item, 16)

    def test_language_autodetect(self) -> None:
        self.assertEqual(detect_language(PRESETS["c_orderbook"]["code"]), "c")
        self.assertEqual(detect_language(PRESETS["go_session"]["code"]), "go")
        self.assertEqual(detect_language(PRESETS["rust_telemetry"]["code"]), "rust")
        self.assertEqual(detect_language(PRESETS["sql_ledger"]["code"]), "sql")

    def test_rust_autodetect_pub_crate_and_raw_pointers(self) -> None:
        rust_src = """#[repr(C)]
        pub(crate) struct NativeHandle {
            ptr: *const u8,
            marker: (),
            name: &'static str,
            slice: &[u8],
            zero_arr: [u64; 0],
        }"""
        self.assertEqual(detect_language(rust_src), "rust")
        comp = analyze_source(rust_src)
        self.assertEqual(comp.language, "rust")
        self.assertEqual(comp.struct_name, "NativeHandle")
        by_name = {f.name: f for f in comp.original.placed_fields}
        self.assertEqual(by_name["ptr"].size, 8)
        self.assertEqual(by_name["marker"].size, 0)
        self.assertEqual(by_name["name"].size, 16)
        self.assertEqual(by_name["slice"].size, 16)
        self.assertEqual(by_name["zero_arr"].size, 0)

    def test_c_block_comments_atomic_paren_multidim_and_trailing_code(self) -> None:
        c_src = """struct Packet {
            /* multi-line header
               int ignored_field; */
            /* @hot */ _Atomic(uint64_t) seq;
            uint8_t matrix[2][4];
            unsigned long int count; /* @hot */
        };
        int main(void) {
            return 0;
        }"""
        comp = analyze_source(c_src, language="c")
        self.assertEqual(comp.struct_name, "Packet")
        names = [f.name for f in comp.original.placed_fields]
        self.assertEqual(names, ["seq", "matrix", "count"])
        by_name = {f.name: f for f in comp.original.placed_fields}
        self.assertEqual(by_name["seq"].size, 8)
        self.assertTrue(by_name["seq"].is_atomic_or_hot)
        self.assertEqual(by_name["matrix"].size, 8)
        self.assertEqual(by_name["count"].size, 8)
        self.assertTrue(by_name["count"].is_atomic_or_hot)
        self.assertIn("uint8_t matrix[2][4];", comp.refactored_code)

    def test_go_struct_tags_with_urls_zero_array_and_trailing_func(self) -> None:
        go_src = """package main

        type Webhook struct {
            URL   string `json:"url" example:"https://example.com/hook"` // @hot
            Pad   [0]int64
            State atomic.Uint32
        }

        func main() {
            println("ok")
        }"""
        comp = analyze_source(go_src)
        self.assertEqual(comp.language, "go")
        self.assertEqual(comp.struct_name, "Webhook")
        names = [f.name for f in comp.original.placed_fields]
        self.assertEqual(names, ["URL", "Pad", "State"])
        by_name = {f.name: f for f in comp.original.placed_fields}
        self.assertEqual(by_name["URL"].size, 16)
        self.assertTrue(by_name["URL"].is_atomic_or_hot)
        self.assertEqual(by_name["Pad"].size, 0)
        self.assertEqual(by_name["State"].size, 4)
        self.assertEqual(by_name["State"].align, 4)

    def test_sql_quoted_table_precision_and_trailing_index(self) -> None:
        sql_src = """CREATE TABLE IF NOT EXISTS public."audit_log" (
            flag BOOLEAN NOT NULL,
            created_at TIMESTAMP(6) WITH TIME ZONE NOT NULL,
            tz_time TIMETZ NOT NULL
        );
        CREATE INDEX idx_audit ON public."audit_log" (created_at);"""
        comp = analyze_source(sql_src)
        self.assertEqual(comp.language, "sql")
        self.assertEqual(comp.struct_name, "public.audit_log")
        names = [f.name for f in comp.original.placed_fields]
        self.assertEqual(names, ["flag", "created_at", "tz_time"])
        by_name = {f.name: f for f in comp.original.placed_fields}
        self.assertEqual(by_name["created_at"].align, 8)
        self.assertEqual(by_name["tz_time"].size, 12)
        self.assertEqual(by_name["tz_time"].align, 8)

    def test_false_sharing_reports_exact_shared_cache_line(self) -> None:
        fields = [
            FieldSpec(name="pad0", raw_type="uint8_t[56]", size=1, align=1, count=56),
            FieldSpec(name="hot_straddle", raw_type="uint8_t[16]", size=1, align=1, count=16, is_atomic_or_hot=True),
            FieldSpec(name="hot_second", raw_type="uint64_t", size=8, align=8, count=1, is_atomic_or_hot=True),
        ]
        report = compute_layout("StraddleTest", "c", fields)
        self.assertEqual(len(report.false_sharing_hazards), 1)
        haz = report.false_sharing_hazards[0]
        # hot_straddle spans bytes 56..71 (lines 0..1); hot_second is at 72..79 (line 1).
        self.assertEqual(haz.cache_line_index, 1)

    def test_cli_check_json_and_error_handling(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            rc = cli_main(["c_orderbook", "--json", "--check"])
        self.assertEqual(rc, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["struct_name"], "OrderTickNode")

        with tempfile.TemporaryDirectory() as tmpdir:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                rc_dir = cli_main([tmpdir])
            self.assertEqual(rc_dir, 2)

            packed_file = os.path.join(tmpdir, "packed.c")
            with open(packed_file, "w", encoding="utf-8") as f:
                f.write("struct Packed { uint64_t a; uint32_t b; uint8_t c; };")
            stdout_ok = io.StringIO()
            with redirect_stdout(stdout_ok):
                rc_ok = cli_main([packed_file, "--check"])
            self.assertEqual(rc_ok, 0)

        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                cli_main(["c_orderbook", "--instances", "-1"])
            self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
