"""Upstream planning must rebuild changes and skip unchanged scheduled runs."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import plan_upstream
from reuse_desktop import source_hash, desktop_recipe


class PlanTests(unittest.TestCase):
    def run_plan(self, event, current=None):
        official = "a" * 40
        custom = "b" * 40

        def get(path):
            if path == "/repos/rustdesk/rustdesk/releases/latest":
                return {"tag_name": "1.5.1", "draft": False, "prerelease": False}
            if path == "/repos/rustdesk/rustdesk/commits/1.5.1":
                return {"sha": official}
            if path == "/repos/owner/GeodomDesk/releases/latest":
                return {
                    "assets": [
                        {
                            "name": "geodomdesk-build.json",
                            "browser_download_url": "https://github.com/owner/GeodomDesk/descriptor",
                        }
                    ]
                }
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            env = {
                "GITHUB_EVENT_NAME": event,
                "GITHUB_REPOSITORY": "owner/GeodomDesk",
                "GITHUB_SHA": custom,
                "GITHUB_RUN_NUMBER": "12",
                "GITHUB_RUN_ATTEMPT": "2",
                "GITHUB_OUTPUT": str(output),
            }
            with mock.patch.object(
                plan_upstream, "customization_hash", return_value="b" * 64
            ), mock.patch.dict(os.environ, env), mock.patch.object(
                plan_upstream, "get", side_effect=get
            ), mock.patch.object(
                plan_upstream.urllib.request,
                "urlopen",
                return_value=io.BytesIO(json.dumps(current or {}).encode()),
            ), contextlib.redirect_stdout(
                io.StringIO()
            ):
                plan_upstream.plan()
            return dict(line.split("=", 1) for line in output.read_text().splitlines())

    def test_manual_rebuild_keeps_official_version(self):
        result = self.run_plan("workflow_dispatch")
        self.assertEqual(result["version"], "1.5.1")
        self.assertEqual(result["build_number"], "1202")
        self.assertEqual(result["build"], "true")

    def test_scheduled_unchanged_skips_build(self):
        result = self.run_plan(
            "schedule", {"upstream_commit": "a" * 40, "customization_hash": "b" * 64}
        )
        self.assertEqual(result["build"], "false")

    def test_scheduled_either_source_change_rebuilds(self):
        for upstream, custom in [("c" * 40, "b" * 64), ("a" * 40, "c" * 64)]:
            result = self.run_plan(
                "schedule", {"upstream_commit": upstream, "customization_hash": custom}
            )
            self.assertEqual(result["build"], "true")

    def test_documentation_changes_do_not_trigger_a_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "client").mkdir()
            (root / "client/module.rs").write_text("// initial implementation\n")
            (root / "README.md").write_text("initial documentation")
            before = plan_upstream.customization_hash(root)
            (root / "README.md").write_text("updated documentation")
            self.assertEqual(before, plan_upstream.customization_hash(root))
            (root / "client/module.rs").write_text("// changed implementation\n")
            self.assertNotEqual(before, plan_upstream.customization_hash(root))
            (root / "customizations").mkdir()
            for name in (
                "client/exec_bootstrap.ps1",
                "client/legacy_bootstrap.sh",
                "customizations/terminal_exec.patch",
            ):
                path = root / name
                path.write_text("initial\n")
                before = plan_upstream.customization_hash(root)
                path.write_text("updated\n")
                self.assertNotEqual(before, plan_upstream.customization_hash(root))

    def test_desktop_reuse_ignores_only_android_sources_and_checks_build_recipe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src/main.rs").write_text("fn main() {}")
            (root / "flutter/android").mkdir(parents=True)
            resource = root / "flutter/android/colors.xml"
            resource.write_text("<resources/>")
            before = source_hash(root)
            resource.write_text("<resources><!-- icon fix --></resources>")
            self.assertEqual(source_hash(root), before)
            (root / "src/main.rs").write_text("fn main() { panic!() }")
            self.assertNotEqual(source_hash(root), before)
        workflow = "  RUST_VERSION: '1.85.1'\n  FLUTTER_VERSION: '3.24.5'\n  VCPKG_COMMIT: abc\n  windows:\n    run: cargo build\n  ubuntu:\n    run: flutter build\n  android:\n    run: build apk\n"
        skipped = workflow.replace(
            "  windows:\n", "  windows:\n    if: needs.plan.outputs.reuse_run == ''\n"
        )
        self.assertEqual(desktop_recipe(workflow), desktop_recipe(skipped))
        self.assertNotEqual(
            desktop_recipe(workflow),
            desktop_recipe(workflow.replace("cargo build", "cargo build --features changed")),
        )
        self.assertNotEqual(
            desktop_recipe(workflow), desktop_recipe(workflow.replace("'1.85.1'", "'1.86.0'"))
        )


if __name__ == "__main__":
    unittest.main()
