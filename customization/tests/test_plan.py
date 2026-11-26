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

from release import plan_upstream
from release.reuse_desktop import source_hash, desktop_recipe


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

    def test_renamed_workflow_continues_formal_build_numbers(self):
        env = {"GITHUB_RUN_NUMBER": "1", "GITHUB_RUN_ATTEMPT": "2",
               "GEODOMDESK_BUILD_RUN_OFFSET": "141"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(plan_upstream.build_revision(), 14202)

    def test_same_version_cannot_replace_a_newer_revision(self):
        release = {"tag_name": "1.5.3", "assets": [
            {"name": "geodomdesk-1.5.3-r14101-windows-x64.exe"},
            {"name": "geodomdesk-1.5.3-r15001-windows-server-2008-r2-x64.exe"},
        ]}
        with mock.patch.object(plan_upstream, "get", return_value=release):
            plan_upstream.require_publishable_version("owner/GeodomDesk", "1.5.3", 14201)
            with self.assertRaisesRegex(ValueError, "revision 14101"):
                plan_upstream.require_publishable_version("owner/GeodomDesk", "1.5.3", 14101)

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
            (root / "patches").mkdir()
            for name in (
                "client/exec_bootstrap.ps1",
                "client/legacy_bootstrap.sh",
                "patches/terminal_exec.patch",
            ):
                path = root / name
                path.write_text("initial\n")
                before = plan_upstream.customization_hash(root)
                path.write_text("updated\n")
                self.assertNotEqual(before, plan_upstream.customization_hash(root))
            dart = root / "client/flutter/geodom_recent_sync.dart"
            dart.parent.mkdir(parents=True)
            dart.write_text("class Sync {}\n")
            before_dart = plan_upstream.customization_hash(root)
            dart.write_text("class Sync { int v = 1; }\n")
            self.assertNotEqual(before_dart, plan_upstream.customization_hash(root))

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            custom = repo / "customization"
            custom.mkdir()
            (custom / "README.md").write_text("docs v1\n")
            changelog = repo / "CHANGELOG.md"
            changelog.write_text("# 更新日志\n\n## 1.5.3\n\n- 初始版本\n")
            before_changelog = plan_upstream.customization_hash(custom)
            (custom / "README.md").write_text("docs v2\n")
            self.assertEqual(before_changelog, plan_upstream.customization_hash(custom))
            changelog.write_text("# 更新日志\n\n## 1.5.3\n\n- 新增修复\n")
            self.assertNotEqual(before_changelog, plan_upstream.customization_hash(custom))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in ("packaging/prepare_client.py", "release/channel.py"):
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# v1\n")
                prev = plan_upstream.customization_hash(root)
                path.write_text("# v2\n")
                self.assertNotEqual(prev, plan_upstream.customization_hash(root))
            design_doc = root / "assets/README.md"
            design_doc.parent.mkdir(parents=True, exist_ok=True)
            design_doc.write_text("notes v1\n")
            before_design = plan_upstream.customization_hash(root)
            design_doc.write_text("notes v2\n")
            self.assertEqual(before_design, plan_upstream.customization_hash(root))

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
        profile_workflow = (
            "  CARGO_PROFILE_RELEASE_LTO: thin\n"
            "  CARGO_PROFILE_RELEASE_CODEGEN_UNITS: '16'\n"
            + workflow
        )
        self.assertNotEqual(desktop_recipe(workflow), desktop_recipe(profile_workflow))
        self.assertNotEqual(
            desktop_recipe(profile_workflow),
            desktop_recipe(profile_workflow.replace("thin", "fat")),
        )
        self.assertNotEqual(
            desktop_recipe(profile_workflow),
            desktop_recipe(profile_workflow.replace("'16'", "'1'")),
        )
        with tempfile.TemporaryDirectory() as other:
            other_root = Path(other)
            helper = other_root / "packaging/repack_deb.py"
            helper.parent.mkdir(parents=True, exist_ok=True)
            helper.write_text("# v1\n")
            first = desktop_recipe(workflow, other_root)
            helper.write_text("# v2\n")
            self.assertNotEqual(first, desktop_recipe(workflow, other_root))

    def test_desktop_reuse_accepts_failed_main_or_validated_upstream_candidate(self):
        from release import reuse_desktop

        repo = "owner/GeodomDesk"
        version = "1.5.4"
        upstream = "a" * 40
        config_data = {
            "app": "GeodomDesk",
            "version": version,
            "build_number": 4201,
            "release_id": f"{version}-r4201",
            "upstream_commit": upstream,
        }

        def make_run(**overrides):
            run = {
                "head_repository": {"full_name": repo},
                "head_branch": "main",
                "event": "push",
                "name": "Build GeodomDesk",
                "status": "completed",
                "conclusion": "failure",
                "head_sha": "b" * 40,
            }
            run.update(overrides)
            return run

        workflow_text = (Path(__file__).resolve().parents[2] / ".github/workflows/build-client.yml").read_text()
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "geodomdesk-build-config.json"
            config_file.write_text(json.dumps(config_data))

            for label, overrides in [
                ("foreign_repo", {"head_repository": {"full_name": "other/GeodomDesk"}}),
                ("feature_branch", {"head_branch": "feature/x"}),
                ("successful_main", {"conclusion": "success"}),
                (
                    "failed_upstream_candidate",
                    {
                        "head_branch": "upstream/rustdesk-1.5.4-99-1",
                        "event": "workflow_dispatch",
                        "conclusion": "failure",
                    },
                ),
                (
                    "push_upstream_candidate",
                    {
                        "head_branch": "upstream/rustdesk-1.5.4-99-1",
                        "event": "push",
                        "conclusion": "success",
                    },
                ),
            ]:
                with self.subTest(rejected=label):
                    with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": repo}):
                        with self.assertRaisesRegex(ValueError, "Reuse requires"):
                            reuse_desktop.validate(
                                "123",
                                config_file,
                                version,
                                upstream,
                                lambda _path, r=make_run(**overrides): r,
                            )

            for label, overrides in [
                ("failed_main", {}),
                (
                    "validated_upstream_candidate",
                    {
                        "head_branch": "upstream/rustdesk-1.5.4-99-1",
                        "event": "workflow_dispatch",
                        "conclusion": "success",
                    },
                ),
            ]:
                with self.subTest(accepted=label):
                    customization_dir = Path(__file__).resolve().parents[1]
                    buf = io.BytesIO()
                    with reuse_desktop.tarfile.open(fileobj=buf, mode="w") as tar:
                        archive_entries = [(".github/workflows/build-client.yml", workflow_text.encode("utf-8"))]
                        for helper in [
                            "packaging/repack_deb.py",
                            "packaging/patch_flutter_sdk.py",
                            "packaging/prepare_printer.py",
                            "config/printer-assets.json",
                        ]:
                            archive_entries.append(
                                (f"customization/{helper}", (customization_dir / helper).read_bytes())
                            )
                        for arc_name, raw in archive_entries:
                            info = reuse_desktop.tarfile.TarInfo(arc_name)
                            info.size = len(raw)
                            tar.addfile(info, io.BytesIO(raw))
                    with (
                        mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": repo}),
                        mock.patch.object(reuse_desktop.subprocess, "run") as mock_run,
                        mock.patch.object(
                            reuse_desktop.subprocess, "check_output", return_value=buf.getvalue()
                        ),
                        mock.patch.object(
                            reuse_desktop,
                            "prepare_hash",
                            return_value=("hash", config_data),
                        ),
                    ):
                        result = reuse_desktop.validate(
                            "123",
                            config_file,
                            version,
                            upstream,
                            lambda _path, r=make_run(**overrides): r,
                        )
                        self.assertEqual(result, 4201)
                        mock_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
