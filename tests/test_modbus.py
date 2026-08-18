import importlib.util
import math
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "modules" / "modbus" / "module.py"
SPEC = importlib.util.spec_from_file_location("test_modbus_module", MODULE_PATH)
MODBUS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODBUS)


class ModbusCodecTests(unittest.TestCase):
    def test_uint64_and_swapped_words(self):
        mapping = {"data_type": "uint64", "word_order": "swappedWords"}
        words = MODBUS._encode(123456789, mapping)
        self.assertEqual(MODBUS._decode(words, mapping), 123456789)

    def test_sma_unavailable_is_nan(self):
        mapping = {"data_type": "uint32", "unavailable_value_policy": "sma"}
        self.assertTrue(math.isnan(MODBUS._decode([0xFFFF, 0xFFFF], mapping)))


if __name__ == "__main__":
    unittest.main()
