import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scada_uploader.config import load_config
from scada_uploader.transport import tls_context

ROOT_PEM = Path(__file__).resolve().parents[1] / "portal_ca.pem"


class CaBundleConfigTests(unittest.TestCase):
    def _load(self, root: Path, extra):
        config_path = root / "config.json"
        values = {
            "device_id": "TEST_DEVICE",
            "shared_secret": "unit-test-secret",
            "telemetry_url": "https://example.invalid/api/v1/public/scada/telemetry",
            **extra,
        }
        config_path.write_text(json.dumps(values), encoding="utf-8")
        return load_config(config_path, environ={})

    def test_missing_or_empty_uses_default_trust(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(self._load(root, {}).ca_bundle_file)
            self.assertIsNone(self._load(root, {"ca_bundle_file": ""}).ca_bundle_file)
            self.assertIsNone(self._load(root, {"ca_bundle_file": None}).ca_bundle_file)

    def test_relative_path_is_resolved_from_config_folder_and_loaded(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "portal_ca.pem").write_text(ROOT_PEM.read_text(encoding="ascii"), encoding="ascii")
            cfg = self._load(root, {"ca_bundle_file": "portal_ca.pem"})
            self.assertEqual(cfg.ca_bundle_file, (root / "portal_ca.pem").resolve())
            subjects = [str(cert["subject"]) for cert in tls_context(cfg.ca_bundle_file).get_ca_certs()]
        self.assertTrue(any("GlobalSign Root R46" in subject for subject in subjects))

    def test_missing_file_or_bad_content_is_a_config_error(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(SystemExit) as missing:
                self._load(root, {"ca_bundle_file": "absent.pem"})
            self.assertIn("ca_bundle_file not found", str(missing.exception))
            (root / "junk.pem").write_text("not a certificate\n", encoding="ascii")
            with self.assertRaises(SystemExit) as junk:
                self._load(root, {"ca_bundle_file": "junk.pem"})
            self.assertIn("not a readable PEM certificate file", str(junk.exception))
            with self.assertRaises(SystemExit) as wrong_type:
                self._load(root, {"ca_bundle_file": 5})
            self.assertIn("ca_bundle_file must be a path string", str(wrong_type.exception))


if __name__ == "__main__":
    unittest.main()
