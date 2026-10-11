import locale
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_project import run


class ProcessEncodingTests(unittest.TestCase):
    def test_cargo_style_utf8_output_decodes_on_a_windows_code_page(self):
        # Ł contains byte 0x81 in UTF-8, which is undefined in CP1252.
        # Cargo metadata contains Unicode author names and descriptions.
        with mock.patch.object(locale, "getencoding", return_value="cp1252"):
            output = run([sys.executable, "-c",
                          'import os; os.write(1, "Łódź — OpenPGP".encode("utf-8"))'], capture=True)
        self.assertEqual(output, "Łódź — OpenPGP")


if __name__ == "__main__":
    unittest.main()
