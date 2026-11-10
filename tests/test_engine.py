"""Unit test suite for padzero cross-language layout and optimization engine."""

import unittest

from padzero.engine import PRESETS, analyze_source, detect_language


class TestPadZeroEngine(unittest.TestCase):

    def test_c_orderbook_preset_packing(self) -> None:
        preset = PRESETS["c_orderbook"]
        # Fix typo in preset if any and verify deterministic layout
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


if __name__ == "__main__":
    unittest.main()
