from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_rpm_spec_uses_pyproject_build_macros(self):
        spec = (ROOT / "packaging" / "scout.spec").read_text(encoding="utf-8")

        self.assertIn("BuildRequires:  pyproject-rpm-macros", spec)
        self.assertIn("%generate_buildrequires\n%pyproject_buildrequires -w", spec)
        self.assertIn("%pyproject_wheel", spec)
        self.assertIn("%pyproject_install", spec)
        self.assertIn("%pyproject_save_files scout", spec)
        self.assertIn("%files -f %{pyproject_files}", spec)
        self.assertNotIn("%py3_build", spec)
        self.assertNotIn("%py3_install", spec)

    def test_legacy_setup_py_is_not_present(self):
        self.assertFalse((ROOT / "setup.py").exists())

    def test_build_metadata_is_compatible_with_el9_setuptools(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        setup_cfg = (ROOT / "setup.cfg").read_text(encoding="utf-8")

        self.assertIn('requires = ["setuptools>=53", "wheel"]', pyproject)
        self.assertIn("[metadata]", setup_cfg)
        self.assertIn("name = scout", setup_cfg)
        self.assertIn("scout = scout.cli:main", setup_cfg)
