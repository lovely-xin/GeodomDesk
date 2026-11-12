"""Upstream planning must rebuild changes and skip unchanged scheduled runs."""

import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import plan_upstream
from reuse_desktop import source_hash, desktop_recipe


class PlanTests(unittest.TestCase):
    def run_plan(self, event, current=None, pin=None):
        official = "a" * 40
        custom = "b" * 40

        def get(path):
            if path == "/repos/rustdesk/rustdesk/releases/tags/1.5.1":
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
            root = Path(tmp)
            (root / "reference").mkdir()
            (root / "reference/Cargo.toml").write_text(
                '[package]\nname = "rustdesk"\nversion = "1.5.1"\n\n[dependencies]\n'
            )
            (root / "config").mkdir()
            (root / "config/client-channel.json").write_text(
                json.dumps({"upstream_commit": pin or official})
            )
            output = root / "output"
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
                plan_upstream.plan(root)
            return dict(line.split("=", 1) for line in output.read_text().splitlines())

    def test_manual_rebuild_uses_reviewed_version_instead_of_latest(self):
        result = self.run_plan("workflow_dispatch")
        self.assertEqual(result["version"], "1.5.1")
        self.assertEqual(result["build_number"], "1202")
        self.assertEqual(result["build"], "true")

    def test_reviewed_pin_must_match_the_official_tag(self):
        with self.assertRaisesRegex(ValueError, "reviewed upstream commit"):
            self.run_plan("pull_request", pin="c" * 40)

    def test_repair_resolves_multiple_stable_versions(self):
        def get(path):
            responses = {
                "/releases/latest": {"tag_name": "2.1.0", "draft": False, "prerelease": False},
                "/releases/tags/1.9.4": {"tag_name": "1.9.4", "draft": False, "prerelease": False},
                "/releases/tags/v2.1.0": {"tag_name": "v2.1.0", "draft": False, "prerelease": False},
                "/commits/1.9.4": {"sha": "a" * 40},
                "/commits/2.1.0": {"sha": "b" * 40},
                "/commits/v2.1.0": {"sha": "b" * 40},
            }
            route = path.removeprefix("/repos/rustdesk/rustdesk")
            if route == "/releases/tags/2.1.0":
                raise urllib.error.HTTPError(path, 404, "missing tag", {}, None)
            return responses[route]

        with mock.patch.object(plan_upstream, "get", side_effect=get):
            self.assertEqual(plan_upstream.official_release("  v1.9.4  "), ("1.9.4", "1.9.4", "a" * 40))
            self.assertEqual(plan_upstream.official_release("2.1.0"), ("2.1.0", "v2.1.0", "b" * 40))
            self.assertEqual(plan_upstream.official_release(), ("2.1.0", "2.1.0", "b" * 40))

    def test_invalid_draft_prerelease_and_mismatched_release_are_rejected(self):
        with mock.patch.object(plan_upstream, "get") as get:
            for invalid in ("v", "nightly", "1.2.3-rc1", "1.2.3/../../main"):
                with self.subTest(version=invalid), self.assertRaises(ValueError):
                    plan_upstream.official_release(invalid)
            get.assert_not_called()
            stable = {"tag_name": "2.1.0", "draft": False, "prerelease": False}
            for changes in ({"draft": True}, {"prerelease": True}, {"tag_name": "2.0.0"}):
                get.return_value = {**stable, **changes}
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    plan_upstream.official_release("2.1.0")

    def test_publication_keeps_the_release_channel_forward_only(self):
        with mock.patch.object(plan_upstream, "get", return_value={"tag_name": "2.1.0"}):
            plan_upstream.require_publishable_version("owner/GeodomDesk", "2.1.0")
            plan_upstream.require_publishable_version("owner/GeodomDesk", "2.2.0")
            with self.assertRaisesRegex(ValueError, "already serves 2.1.0"):
                plan_upstream.require_publishable_version("owner/GeodomDesk", "1.9.4")

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
