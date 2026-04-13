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
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from packages_config_to_csproj import (
    parse_packages_config,
    resolve_target_framework,
    build_csproj,
    convert,
    scan_directory,
    strip_incompatible_packages,
    parse_direct_deps_from_assets,
    resolve_direct_packages,
    _NU1202_RE,
    SYNTHETIC_SUBDIR,
    SYNTHETIC_FILENAME,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_temp_config(content: str, suffix=".config") -> Path:
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
        pkg_lines = "\n".join(
            f'  <package id="Pkg{i}" version="1.0.{i}" targetFramework="net48" />'
            for i in range(100)
        )
        cfg = write_temp_config(f"<packages>\n{pkg_lines}\n</packages>")
        packages = parse_packages_config(cfg)
        assert len(packages) == 100
        assert packages[42]["id"] == "Pkg42"
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
        """TFM resolution picks highest — incompatible packages are stripped at restore time."""
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

    def test_asset_target_fallback_present(self):
        result = build_csproj(self._packages(), "net48")
        assert "AssetTargetFallback" in result
        assert "net35" in result
        assert "net40" in result

    def test_asset_target_fallback_is_valid_xml(self):
        result = build_csproj(self._packages(), "net48")
        root = parse_csproj_string(result)
        fallback = root.findtext(".//AssetTargetFallback")
        assert fallback is not None
        assert "net35" in fallback


# ---------------------------------------------------------------------------
# Test: NU1202 regex
# ---------------------------------------------------------------------------

class TestNU1202Regex:

    def test_matches_standard_error_format(self):
        line = (
            "error NU1202: Package 'MarkdownDeep.NET 1.5.0' is not compatible with "
            "net48 (.NETFramework,Version=v4.8)."
        )
        match = _NU1202_RE.search(line)
        assert match is not None
        assert match.group(1) == "MarkdownDeep.NET"
        assert match.group(2) == "1.5.0"

    def test_matches_without_quotes(self):
        line = "NU1202: Package SomeLib 2.0.0 is not compatible with net48"
        match = _NU1202_RE.search(line)
        assert match is not None
        assert match.group(1) == "SomeLib"
        assert match.group(2) == "2.0.0"

    def test_no_match_on_unrelated_error(self):
        line = "error NU1101: Unable to find package Foo"
        assert _NU1202_RE.search(line) is None

    def test_extracts_multiple_offenders(self):
        output = (
            "NU1202: Package 'PkgA 1.0.0' is not compatible with net48\n"
            "NU1202: Package 'PkgB 2.3.4' is not compatible with net48\n"
        )
        matches = list(_NU1202_RE.finditer(output))
        assert len(matches) == 2
        ids = [m.group(1) for m in matches]
        assert "PkgA" in ids
        assert "PkgB" in ids


# ---------------------------------------------------------------------------
# Test: strip_incompatible_packages
# ---------------------------------------------------------------------------

class TestStripIncompatiblePackages:
    """
    Tests use mocked _try_restore so no real dotnet SDK is needed.
    """

    def _packages(self):
        return [
            {"id": "Newtonsoft.Json", "version": "13.0.1", "developmentDependency": False,
             "targetFramework": "net48"},
            {"id": "MarkdownDeep.NET", "version": "1.5.0", "developmentDependency": False,
             "targetFramework": "net35"},
            {"id": "log4net", "version": "2.0.15", "developmentDependency": False,
             "targetFramework": "net48"},
        ]

    def test_strips_nu1202_offender_and_retries(self):
        """
        Core scenario: MarkdownDeep.NET causes NU1202 on first restore attempt.
        It should be stripped and the remaining two packages should be written.
        """
        calls = []

        def mock_try_restore(path):
            calls.append(len(calls))
            if len(calls) == 1:
                # First attempt fails with MarkdownDeep.NET as offender
                return False, [("MarkdownDeep.NET", "1.5.0")]
            # Second attempt succeeds
            return True, []

        with tempfile.TemporaryDirectory() as tmpdir:
            csproj = Path(tmpdir) / "_semgrep_sc" / "project.csproj"
            with patch("packages_config_to_csproj._try_restore", side_effect=mock_try_restore):
                remaining = strip_incompatible_packages(
                    self._packages(), csproj, "net48"
                )

        assert len(remaining) == 2
        ids = [p["id"] for p in remaining]
        assert "MarkdownDeep.NET" not in ids
        assert "Newtonsoft.Json" in ids
        assert "log4net" in ids
        assert len(calls) == 2  # tried twice: once failed, once succeeded

    def test_strips_multiple_offenders_across_retries(self):
        """Multiple incompatible packages should all be stripped across successive retries."""
        calls = []

        def mock_try_restore(path):
            calls.append(len(calls))
            if len(calls) == 1:
                return False, [("MarkdownDeep.NET", "1.5.0")]
            if len(calls) == 2:
                return False, [("log4net", "2.0.15")]
            return True, []

        with tempfile.TemporaryDirectory() as tmpdir:
            csproj = Path(tmpdir) / "_semgrep_sc" / "project.csproj"
            with patch("packages_config_to_csproj._try_restore", side_effect=mock_try_restore):
                remaining = strip_incompatible_packages(
                    self._packages(), csproj, "net48"
                )

        assert len(remaining) == 1
        assert remaining[0]["id"] == "Newtonsoft.Json"
        assert len(calls) == 3

    def test_non_nu1202_failure_leaves_file_in_place(self):
        """If restore fails for a non-NU1202 reason, the file is left as-is."""
        def mock_try_restore(path):
            return False, []  # failure with no NU1202 offenders

        with tempfile.TemporaryDirectory() as tmpdir:
            csproj = Path(tmpdir) / "_semgrep_sc" / "project.csproj"
            with patch("packages_config_to_csproj._try_restore", side_effect=mock_try_restore):
                remaining = strip_incompatible_packages(
                    self._packages(), csproj, "net48"
                )

        # All packages returned unchanged — file left for Semgrep to handle
        assert len(remaining) == 3

    def test_all_stripped_removes_file(self):
        """If every package is incompatible the synthetic file is removed entirely."""
        def mock_try_restore(path):
            return False, [("Newtonsoft.Json", "13.0.1"), ("MarkdownDeep.NET", "1.5.0"),
                           ("log4net", "2.0.15")]

        with tempfile.TemporaryDirectory() as tmpdir:
            csproj = Path(tmpdir) / "_semgrep_sc" / "project.csproj"
            with patch("packages_config_to_csproj._try_restore", side_effect=mock_try_restore):
                remaining = strip_incompatible_packages(
                    self._packages(), csproj, "net48"
                )

        assert remaining == []
        assert not csproj.exists()

    def test_immediate_success_returns_all_packages(self):
        """If restore succeeds on first try, all packages are returned unchanged."""
        def mock_try_restore(path):
            return True, []

        with tempfile.TemporaryDirectory() as tmpdir:
            csproj = Path(tmpdir) / "_semgrep_sc" / "project.csproj"
            with patch("packages_config_to_csproj._try_restore", side_effect=mock_try_restore):
                remaining = strip_incompatible_packages(
                    self._packages(), csproj, "net48"
                )

        assert len(remaining) == 3


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

    def test_validate_restore_strips_incompatible(self):
        """validate_restore=True should strip MarkdownDeep.NET and still write the file."""
        cfg = write_temp_config("""
            <packages>
              <package id="Newtonsoft.Json" version="13.0.1" targetFramework="net48" />
              <package id="MarkdownDeep.NET" version="1.5.0" targetFramework="net35" />
            </packages>
        """)
        calls = []
        def mock_try_restore(path):
            calls.append(1)
            if len(calls) == 1:
                return False, [("MarkdownDeep.NET", "1.5.0")]
            return True, []

        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "_semgrep_sc" / "project.csproj"
            with patch("packages_config_to_csproj._try_restore", side_effect=mock_try_restore):
                result = convert(cfg, output_path=out, validate_restore=True)

            assert result is not None
            assert out.exists()
            content = out.read_text()
            assert "Newtonsoft.Json" in content
            assert "MarkdownDeep.NET" not in content
        cfg.unlink()


# ---------------------------------------------------------------------------
# Test: scan_directory
# ---------------------------------------------------------------------------

class TestScanDirectory:

    def _make_repo(self, base: Path):
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
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            self._make_repo(base)
            scan_directory(base)
            assert (base / "ProjectA" / SYNTHETIC_SUBDIR / SYNTHETIC_FILENAME).exists()
            assert (base / "ProjectB" / SYNTHETIC_SUBDIR / SYNTHETIC_FILENAME).exists()
            assert (base / "ProjectC" / "nested" / SYNTHETIC_SUBDIR / SYNTHETIC_FILENAME).exists()

    def test_no_collision_with_existing_csproj(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            self._make_repo(base)
            scan_directory(base)
            assert not (base / "ProjectA" / SYNTHETIC_FILENAME).exists()
            assert not (base / "ProjectB" / SYNTHETIC_FILENAME).exists()
            assert (base / "ProjectA" / "ProjectA.csproj").exists()
            assert (base / "ProjectB" / "ProjectB.csproj").exists()

    def test_each_synthetic_subdir_has_only_one_csproj(self):
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
# Test: parse_direct_deps_from_assets + resolve_direct_packages
# ---------------------------------------------------------------------------

class TestDirectTransitiveSplit:
    """
    Tests for the project.assets.json parsing that identifies true direct
    vs transitive dependencies so the synthetic .csproj only lists directs.
    """

    def _write_assets(self, tmpdir: Path, direct_ids: list[str]) -> Path:
        """Write a minimal project.assets.json with the given direct dep IDs."""
        obj_dir = tmpdir / "obj"
        obj_dir.mkdir(parents=True, exist_ok=True)
        assets = {
            "version": 3,
            "targets": {},
            "libraries": {},
            "projectFileDependencyGroups": {
                "net48": [f"{pkg} >= 1.0.0" for pkg in direct_ids]
            },
            "packageFolders": {},
            "project": {}
        }
        path = obj_dir / "project.assets.json"
        import json
        path.write_text(json.dumps(assets), encoding="utf-8")
        return path

    def test_parses_direct_ids_from_assets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assets_path = self._write_assets(
                Path(tmpdir),
                ["Newtonsoft.Json", "log4net", "Autofac"]
            )
            direct_ids = parse_direct_deps_from_assets(assets_path)
            assert "newtonsoft.json" in direct_ids
            assert "log4net" in direct_ids
            assert "autofac" in direct_ids

    def test_returns_empty_set_for_missing_file(self):
        direct_ids = parse_direct_deps_from_assets(Path("/nonexistent/project.assets.json"))
        assert direct_ids == set()

    def test_returns_empty_set_for_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bad = Path(tmpdir) / "obj" / "project.assets.json"
            bad.parent.mkdir()
            bad.write_text("not json")
            direct_ids = parse_direct_deps_from_assets(bad)
            assert direct_ids == set()

    def test_case_insensitive_matching(self):
        """Package IDs are case-insensitive in NuGet."""
        with tempfile.TemporaryDirectory() as tmpdir:
            assets_path = self._write_assets(Path(tmpdir), ["Newtonsoft.Json"])
            direct_ids = parse_direct_deps_from_assets(assets_path)
            assert "newtonsoft.json" in direct_ids  # stored lowercase

    def test_resolve_splits_direct_from_transitive(self):
        """
        Core scenario: packages.config has 5 packages, project.assets.json
        says only 2 are truly direct. resolve_direct_packages should split them.
        """
        packages = [
            {"id": "Newtonsoft.Json", "version": "13.0.1", "targetFramework": "net48", "developmentDependency": False},
            {"id": "log4net", "version": "2.0.15", "targetFramework": "net48", "developmentDependency": False},
            {"id": "Castle.Core", "version": "5.1.1", "targetFramework": "net48", "developmentDependency": False},
            {"id": "System.Runtime", "version": "4.3.1", "targetFramework": "net48", "developmentDependency": False},
            {"id": "Microsoft.Bcl", "version": "1.1.10", "targetFramework": "net48", "developmentDependency": False},
        ]
        # Only Newtonsoft.Json and log4net are truly direct
        with tempfile.TemporaryDirectory() as tmpdir:
            assets_path = self._write_assets(
                Path(tmpdir), ["Newtonsoft.Json", "log4net"]
            )
            csproj_path = assets_path.parent.parent / "project.csproj"
            direct, transitive = resolve_direct_packages(packages, csproj_path)

        assert len(direct) == 2
        assert len(transitive) == 3
        direct_ids = [p["id"] for p in direct]
        assert "Newtonsoft.Json" in direct_ids
        assert "log4net" in direct_ids
        transitive_ids = [p["id"] for p in transitive]
        assert "Castle.Core" in transitive_ids

    def test_resolve_falls_back_to_all_direct_if_no_assets(self):
        """If project.assets.json is missing, return all packages as direct."""
        packages = [
            {"id": "Newtonsoft.Json", "version": "13.0.1", "targetFramework": "net48", "developmentDependency": False},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            # No obj/ directory created - assets file won't exist
            csproj_path = Path(tmpdir) / "project.csproj"
            direct, transitive = resolve_direct_packages(packages, csproj_path)

        assert len(direct) == 1
        assert len(transitive) == 0

    def test_rewritten_csproj_contains_only_direct_deps(self):
        """
        After resolve_direct_packages, building the .csproj with only direct
        deps should NOT include the transitive ones.
        """
        direct = [
            {"id": "Newtonsoft.Json", "version": "13.0.1", "developmentDependency": False},
        ]
        result = build_csproj(direct, "net48")
        assert "Newtonsoft.Json" in result
        assert "Castle.Core" not in result
        assert "System.Runtime" not in result


# ---------------------------------------------------------------------------
# Simple self-runner (no pytest required)
# ---------------------------------------------------------------------------

def run_tests_without_pytest():
    test_classes = [
        TestParsePackagesConfig,
        TestResolveTargetFramework,
        TestBuildCsproj,
        TestNU1202Regex,
        TestStripIncompatiblePackages,
        TestDirectTransitiveSplit,
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
