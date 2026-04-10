#!/usr/bin/env python3
"""
test_packages_config_to_csproj.py

Test suite for packages_config_to_csproj.py

Run with:
    python -m pytest test_packages_config_to_csproj.py -v

Or without pytest:
    python test_packages_config_to_csproj.py
"""

import sys
import os
import tempfile
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

# Make sure the converter module is importable from the same directory
sys.path.insert(0, os.path.dirname(__file__))
from packages_config_to_csproj import (
    parse_packages_config,
    resolve_target_framework,
    build_csproj,
    convert,
    scan_directory,
    SYNTHETIC_SUBDIR,
    SYNTHETIC_FILENAME,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_temp_config(content: str, suffix=".config") -> Path:
    """Write XML string to a temp file and return its Path."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, delete=False, encoding="utf-8"
    )
    tmp.write(textwrap.dedent(content))
    tmp.close()
    return Path(tmp.name)


def parse_csproj_string(xml_str: str) -> ET.Element:
    return ET.fromstring(xml_str)


# ---------------------------------------------------------------------------
# Test: parse_packages_config
# ---------------------------------------------------------------------------

class TestParsePackagesConfig:

    def test_basic_parse(self):
        cfg = write_temp_config(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            "<packages>\n"
            '  <package id="Newtonsoft.Json" version="13.0.1" targetFramework="net48" />\n'
            '  <package id="log4net" version="2.0.15" targetFramework="net48" />\n'
            "</packages>\n"
        )
        packages = parse_packages_config(cfg)
        assert len(packages) == 2
        assert packages[0]["id"] == "Newtonsoft.Json"
        assert packages[0]["version"] == "13.0.1"
        assert packages[0]["targetFramework"] == "net48"
        assert packages[1]["id"] == "log4net"
        cfg.unlink()

    def test_dev_dependency_flagged(self):
        cfg = write_temp_config("""
            <packages>
              <package id="Moq" version="4.18.4" targetFramework="net48" developmentDependency="true" />
            </packages>
        """)
        packages = parse_packages_config(cfg)
        assert packages[0]["developmentDependency"] is True
        cfg.unlink()

    def test_missing_target_framework(self):
        """Packages without targetFramework should still parse — TFM defaults to empty string."""
        cfg = write_temp_config("""
            <packages>
              <package id="SomeLib" version="1.0.0" />
            </packages>
        """)
        packages = parse_packages_config(cfg)
        assert len(packages) == 1
        assert packages[0]["targetFramework"] == ""
        cfg.unlink()

    def test_skips_package_with_no_version(self):
        cfg = write_temp_config("""
            <packages>
              <package id="BadPackage" />
              <package id="GoodPackage" version="2.0.0" />
            </packages>
        """)
        packages = parse_packages_config(cfg)
        assert len(packages) == 1
        assert packages[0]["id"] == "GoodPackage"
        cfg.unlink()

    def test_skips_package_with_no_id(self):
        cfg = write_temp_config("""
            <packages>
              <package version="1.0.0" />
              <package id="RealPackage" version="1.0.0" />
            </packages>
        """)
        packages = parse_packages_config(cfg)
        assert len(packages) == 1
        assert packages[0]["id"] == "RealPackage"
        cfg.unlink()

    def test_invalid_xml_raises(self):
        cfg = write_temp_config("<this is not valid xml")
        try:
            parse_packages_config(cfg)
            assert False, "Should have raised ValueError"
        except ValueError:
            pass
        finally:
            cfg.unlink()

    def test_wrong_root_element_raises(self):
        cfg = write_temp_config("<dependencies><dep id='x' version='1'/></dependencies>")
        try:
            parse_packages_config(cfg)
            assert False, "Should have raised ValueError"
        except ValueError:
            pass
        finally:
            cfg.unlink()

    def test_empty_packages(self):
        cfg = write_temp_config("<packages></packages>")
        packages = parse_packages_config(cfg)
        assert packages == []
        cfg.unlink()

    def test_large_package_list(self):
        """Stress test: 100 packages."""
        pkg_lines = "\n".join(
            f'  <package id="Pkg{i}" version="1.0.{i}" targetFramework="net48" />'
            for i in range(100)
        )
        cfg = write_temp_config(f"<packages>\n{pkg_lines}\n</packages>")
        packages = parse_packages_config(cfg)
        assert len(packages) == 100
        assert packages[42]["id"] == "Pkg42"
        assert packages[42]["version"] == "1.0.42"
        cfg.unlink()


# ---------------------------------------------------------------------------
# Test: resolve_target_framework
# ---------------------------------------------------------------------------

class TestResolveTargetFramework:

    def test_single_tfm(self):
        packages = [{"targetFramework": "net48"}]
        assert resolve_target_framework(packages) == "net48"

    def test_no_tfm_uses_fallback(self):
        packages = [{"targetFramework": ""}]
        assert resolve_target_framework(packages, fallback="net472") == "net472"

    def test_multiple_tfms_picks_highest(self):
        packages = [
            {"targetFramework": "net45"},
            {"targetFramework": "net48"},
            {"targetFramework": "net46"},
        ]
        result = resolve_target_framework(packages)
        assert result == "net48"

    def test_mixed_empty_and_real_tfm(self):
        packages = [
            {"targetFramework": ""},
            {"targetFramework": "net48"},
        ]
        assert resolve_target_framework(packages) == "net48"

    def test_all_empty_uses_fallback(self):
        packages = [{"targetFramework": ""}, {"targetFramework": ""}]
        assert resolve_target_framework(packages, fallback="net48") == "net48"


# ---------------------------------------------------------------------------
# Test: build_csproj
# ---------------------------------------------------------------------------

class TestBuildCsproj:

    def _packages(self):
        return [
            {"id": "Newtonsoft.Json", "version": "13.0.1", "developmentDependency": False},
            {"id": "log4net", "version": "2.0.15", "developmentDependency": False},
        ]

    def test_output_is_valid_xml(self):
        result = build_csproj(self._packages(), "net48")
        root = parse_csproj_string(result)
        assert root is not None

    def test_contains_package_references(self):
        result = build_csproj(self._packages(), "net48")
        root = parse_csproj_string(result)
        refs = root.findall(".//PackageReference")
        ids = [r.get("Include") for r in refs]
        assert "Newtonsoft.Json" in ids
        assert "log4net" in ids

    def test_versions_correct(self):
        result = build_csproj(self._packages(), "net48")
        root = parse_csproj_string(result)
        refs = {r.get("Include"): r.get("Version") for r in root.findall(".//PackageReference")}
        assert refs["Newtonsoft.Json"] == "13.0.1"
        assert refs["log4net"] == "2.0.15"

    def test_target_framework_set(self):
        result = build_csproj(self._packages(), "net472")
        root = parse_csproj_string(result)
        tfm = root.findtext(".//TargetFramework")
        assert tfm == "net472"

    def test_auto_generated_comment_present(self):
        result = build_csproj(self._packages(), "net48")
        assert "AUTO-GENERATED" in result

    def test_is_packable_false(self):
        result = build_csproj(self._packages(), "net48")
        root = parse_csproj_string(result)
        assert root.findtext(".//IsPackable") == "false"

    def test_dev_dependency_comment(self):
        packages = [{"id": "Moq", "version": "4.18.4", "developmentDependency": True}]
        result = build_csproj(packages, "net48")
        assert "dev dependency" in result


# ---------------------------------------------------------------------------
# Test: convert (integration)
# ---------------------------------------------------------------------------

class TestConvert:

    def _basic_config(self):
        return write_temp_config("""
            <packages>
              <package id="Newtonsoft.Json" version="13.0.1" targetFramework="net48" />
            </packages>
        """)

    def test_stdout_mode_returns_string(self):
        cfg = self._basic_config()
        result = convert(cfg, output_path=None)
        assert result is not None
        assert "Newtonsoft.Json" in result
        cfg.unlink()

    def test_write_to_file(self):
        cfg = self._basic_config()
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "project.csproj"
            convert(cfg, output_path=out)
            assert out.exists()
            content = out.read_text()
            assert "Newtonsoft.Json" in content
        cfg.unlink()

    def test_dry_run_does_not_write(self):
        cfg = self._basic_config()
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "project.csproj"
            convert(cfg, output_path=out, dry_run=True)
            assert not out.exists()
        cfg.unlink()

    def test_empty_packages_returns_none(self):
        cfg = write_temp_config("<packages></packages>")
        result = convert(cfg, output_path=None)
        assert result is None
        cfg.unlink()

    def test_creates_parent_dirs(self):
        cfg = self._basic_config()
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "_semgrep_sc" / "project.csproj"
            convert(cfg, output_path=out)
            assert out.exists()
        cfg.unlink()


# ---------------------------------------------------------------------------
# Test: scan_directory — KEY TESTS for the MSB1050 fix
# ---------------------------------------------------------------------------

class TestScanDirectory:

    def _make_repo(self, base: Path):
        """
        Create a fake repo tree that mirrors the real-world collision scenario:
        each project folder has BOTH a packages.config AND an existing .csproj.
        This is the exact setup that triggered MSB1050.
        """
        projects = {
            "ProjectA/packages.config": """
                <packages>
                  <package id="Newtonsoft.Json" version="13.0.1" targetFramework="net48" />
                </packages>
            """,
            "ProjectB/packages.config": """
                <packages>
                  <package id="log4net" version="2.0.15" targetFramework="net472" />
                  <package id="Moq" version="4.18.4" targetFramework="net472" developmentDependency="true" />
                </packages>
            """,
            "ProjectC/nested/packages.config": """
                <packages>
                  <package id="AutoMapper" version="12.0.1" targetFramework="net48" />
                </packages>
            """,
        }
        # Also create pre-existing .csproj files in the same folders
        # to simulate the real collision scenario
        existing_csprojs = {
            "ProjectA/ProjectA.csproj": "<Project></Project>",
            "ProjectB/ProjectB.csproj": "<Project></Project>",
            "ProjectC/nested/ProjectC.csproj": "<Project></Project>",
        }
        for rel_path, content in {**projects, **existing_csprojs}.items():
            full = base / rel_path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(textwrap.dedent(content))
        return projects

    def test_finds_and_converts_all(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            self._make_repo(base)
            count = scan_directory(base)
            assert count == 3

    def test_synthetic_in_own_subdirectory(self):
        """
        Core MSB1050 fix: synthetic .csproj must be in _semgrep_sc/ subdir,
        NOT sitting next to the existing .csproj in the same folder.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            self._make_repo(base)
            scan_directory(base)

            # Synthetic files should be in _semgrep_sc/ subdirs
            assert (base / "ProjectA" / SYNTHETIC_SUBDIR / SYNTHETIC_FILENAME).exists()
            assert (base / "ProjectB" / SYNTHETIC_SUBDIR / SYNTHETIC_FILENAME).exists()
            assert (base / "ProjectC" / "nested" / SYNTHETIC_SUBDIR / SYNTHETIC_FILENAME).exists()

    def test_no_collision_with_existing_csproj(self):
        """
        The synthetic file must NOT land in the same folder as the existing .csproj.
        If it did, dotnet restore would hit MSB1050.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            self._make_repo(base)
            scan_directory(base)

            # Confirm the original .csproj folders do NOT contain the synthetic file
            assert not (base / "ProjectA" / SYNTHETIC_FILENAME).exists()
            assert not (base / "ProjectB" / SYNTHETIC_FILENAME).exists()

            # Confirm original .csproj files are untouched
            assert (base / "ProjectA" / "ProjectA.csproj").exists()
            assert (base / "ProjectB" / "ProjectB.csproj").exists()

    def test_each_synthetic_subdir_has_only_one_csproj(self):
        """
        dotnet restore needs exactly one .csproj per folder.
        Verify _semgrep_sc/ contains only project.csproj and nothing else.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            self._make_repo(base)
            scan_directory(base)

            sc_dir = base / "ProjectA" / SYNTHETIC_SUBDIR
            csproj_files = list(sc_dir.glob("*.csproj"))
            assert len(csproj_files) == 1
            assert csproj_files[0].name == SYNTHETIC_FILENAME

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            self._make_repo(base)
            scan_directory(base, dry_run=True)
            assert not (base / "ProjectA" / SYNTHETIC_SUBDIR).exists()
            assert not (base / "ProjectB" / SYNTHETIC_SUBDIR).exists()

    def test_synthetic_csproj_is_valid_xml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            self._make_repo(base)
            scan_directory(base)
            csproj = base / "ProjectA" / SYNTHETIC_SUBDIR / SYNTHETIC_FILENAME
            root = ET.parse(csproj).getroot()
            refs = root.findall(".//PackageReference")
            assert len(refs) == 1
            assert refs[0].get("Include") == "Newtonsoft.Json"

    def test_empty_directory_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            count = scan_directory(Path(tmpdir))
            assert count == 0


# ---------------------------------------------------------------------------
# Simple self-runner (no pytest required)
# ---------------------------------------------------------------------------

def run_tests_without_pytest():
    test_classes = [
        TestParsePackagesConfig,
        TestResolveTargetFramework,
        TestBuildCsproj,
        TestConvert,
        TestScanDirectory,
    ]

    passed = 0
    failed = 0

    for cls in test_classes:
        instance = cls()
        methods = [m for m in dir(cls) if m.startswith("test_")]
        print(f"\n{cls.__name__} ({len(methods)} tests)")
        print("-" * 50)
        for method_name in methods:
            try:
                getattr(instance, method_name)()
                print(f"  ✓ {method_name}")
                passed += 1
            except Exception as e:
                print(f"  ✗ {method_name}")
                print(f"      {type(e).__name__}: {e}")
                failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed")
    print('='*50)
    return failed == 0


if __name__ == "__main__":
    success = run_tests_without_pytest()
    sys.exit(0 if success else 1)
