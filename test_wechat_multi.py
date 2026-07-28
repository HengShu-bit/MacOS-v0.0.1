import argparse
import io
import json
import plistlib
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import wechat_multi as wm


def make_fake_app(
    root: Path,
    name: str = "微信.app",
    bundle_id: str = "com.tencent.xinWeChat",
    marker: bool = False,
    source_build: str = "269136",
) -> Path:
    app = root / name
    executable = app / "Contents" / "MacOS" / "WeChat"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    plist = {
        "CFBundlePackageType": "APPL",
        "CFBundleExecutable": "WeChat",
        "CFBundleIdentifier": bundle_id,
        "CFBundleShortVersionString": "4.1.11",
        "CFBundleVersion": "269136",
        "CFBundleName": "WeChat",
        "CFBundleURLTypes": [{"CFBundleURLSchemes": ["weixin"]}],
    }
    if marker:
        plist[wm.CLONE_MARKER] = True
        plist[wm.CLONE_INDEX_KEY] = 2
        plist[wm.CLONE_SOURCE_ID_KEY] = "com.tencent.xinWeChat"
        plist[wm.CLONE_SOURCE_BUILD_KEY] = source_build
    with (app / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(plist, handle, fmt=plistlib.FMT_BINARY)
    return app


class AppInfoTests(unittest.TestCase):
    def test_reads_official_binary_plist_and_unicode_path(self):
        with tempfile.TemporaryDirectory() as temp:
            app = make_fake_app(Path(temp))
            info = wm.read_app_info(app, require_official=True)
            self.assertEqual(info.bundle_id, "com.tencent.xinWeChat")
            self.assertEqual(info.version, "4.1.11")
            self.assertFalse(info.is_clone)

    def test_rejects_unknown_source_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            app = make_fake_app(Path(temp), bundle_id="example.invalid")
            with self.assertRaises(wm.WeChatMultiError):
                wm.read_app_info(app, require_official=True)


class ClonePlanningTests(unittest.TestCase):
    def test_extra_means_additional_instances(self):
        specs = wm.make_clone_specs(Path("/tmp/output"), 2)
        self.assertEqual([item.index for item in specs], [2, 3])
        self.assertEqual(specs[0].destination.name, "微信 2.app")
        self.assertNotEqual(specs[0].bundle_id, specs[1].bundle_id)

    def test_limits_extra_instances(self):
        with self.assertRaises(wm.WeChatMultiError):
            wm.make_clone_specs(Path("/tmp/output"), 0)
        with self.assertRaises(wm.WeChatMultiError):
            wm.make_clone_specs(Path("/tmp/output"), wm.MAX_EXTRA_INSTANCES + 1)


class PlistEditingTests(unittest.TestCase):
    def test_clone_gets_identity_marker_and_no_url_scheme(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_path = make_fake_app(root, name="source.app")
            clone_path = make_fake_app(root, name="clone.app")
            source = wm.read_app_info(source_path, require_official=True)
            spec = wm.CloneSpec(
                index=2,
                display_name="微信 2",
                bundle_id=wm.CLONE_BUNDLE_PREFIX + "2",
                destination=clone_path,
            )

            wm._edit_clone_plist(
                clone_path,
                source=source,
                spec=spec,
                keep_url_schemes=False,
            )

            info = wm.read_app_info(clone_path)
            self.assertTrue(info.is_clone)
            self.assertEqual(info.clone_index, 2)
            self.assertEqual(info.source_build, "269136")
            plist, _ = wm._read_plist(clone_path / "Contents" / "Info.plist")
            self.assertNotIn("CFBundleURLTypes", plist)
            self.assertIs(plist["LSMultipleInstancesProhibited"], False)


class ExistingCloneTests(unittest.TestCase):
    def test_start_reuses_a_valid_existing_clone(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_path = make_fake_app(root, name="source.app")
            source = wm.read_app_info(source_path, require_official=True)
            output = root / "out"
            spec = wm.make_clone_specs(output, 1)[0]
            fake_clone = make_fake_app(
                output,
                name=spec.destination.name,
                bundle_id=spec.bundle_id,
                marker=True,
            )
            self.assertEqual(fake_clone.resolve(), spec.destination.resolve())

            with mock.patch.object(wm, "_verify_clone_signature"):
                state, backup = wm.create_clone(
                    source,
                    spec,
                    runner=wm.CommandRunner(),
                    replace=False,
                    reuse_existing=True,
                    keep_url_schemes=False,
                    dry_run=False,
                )
            self.assertEqual(state, "reused")
            self.assertIsNone(backup)

    def test_dry_run_creates_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = wm.read_app_info(
                make_fake_app(root, name="source.app"), require_official=True
            )
            spec = wm.make_clone_specs(root / "out", 1)[0]
            state, _ = wm.create_clone(
                source,
                spec,
                runner=wm.CommandRunner(),
                replace=False,
                reuse_existing=False,
                keep_url_schemes=False,
                dry_run=True,
            )
            self.assertEqual(state, "planned")
            self.assertFalse(spec.destination.exists())

    def test_start_refuses_to_reuse_clone_from_an_old_build(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = wm.read_app_info(
                make_fake_app(root, name="source.app"), require_official=True
            )
            spec = wm.make_clone_specs(root / "out", 1)[0]
            make_fake_app(
                spec.destination.parent,
                name=spec.destination.name,
                bundle_id=spec.bundle_id,
                marker=True,
                source_build="older-build",
            )

            with self.assertRaisesRegex(wm.WeChatMultiError, "--replace"):
                wm.create_clone(
                    source,
                    spec,
                    runner=wm.CommandRunner(),
                    replace=False,
                    reuse_existing=True,
                    keep_url_schemes=False,
                    dry_run=False,
                )

    def test_replace_refuses_an_unrecognized_app_at_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = wm.read_app_info(
                make_fake_app(root, name="source.app"), require_official=True
            )
            spec = wm.make_clone_specs(root / "out", 1)[0]
            make_fake_app(
                spec.destination.parent,
                name=spec.destination.name,
                bundle_id="example.some-other-app",
                marker=False,
            )

            with self.assertRaisesRegex(wm.WeChatMultiError, "不会替换"):
                wm.create_clone(
                    source,
                    spec,
                    runner=wm.CommandRunner(),
                    replace=True,
                    reuse_existing=False,
                    keep_url_schemes=False,
                    dry_run=False,
                )

    def test_replace_refuses_a_running_clone(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = wm.read_app_info(
                make_fake_app(root, name="source.app"), require_official=True
            )
            spec = wm.make_clone_specs(root / "out", 1)[0]
            make_fake_app(
                spec.destination.parent,
                name=spec.destination.name,
                bundle_id=spec.bundle_id,
                marker=True,
            )

            with (
                mock.patch.object(wm, "_app_processes", return_value=[4242]),
                self.assertRaisesRegex(wm.WeChatMultiError, "仍在运行"),
            ):
                wm.create_clone(
                    source,
                    spec,
                    runner=wm.CommandRunner(),
                    replace=True,
                    reuse_existing=True,
                    keep_url_schemes=False,
                    dry_run=False,
                )

    def test_replace_rechecks_processes_immediately_before_rename(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = wm.read_app_info(
                make_fake_app(root, name="source.app"), require_official=True
            )
            spec = wm.make_clone_specs(root / "out", 1)[0]
            make_fake_app(
                spec.destination.parent,
                name=spec.destination.name,
                bundle_id=spec.bundle_id,
                marker=True,
            )

            with (
                mock.patch.object(
                    wm,
                    "_app_processes",
                    side_effect=[[], [4242]],
                ),
                mock.patch.object(wm, "_verify_official_source"),
                mock.patch.object(wm, "_resign_clone"),
                self.assertRaisesRegex(wm.WeChatMultiError, "仍在运行"),
            ):
                wm.create_clone(
                    source,
                    spec,
                    runner=wm.CommandRunner(),
                    replace=True,
                    reuse_existing=True,
                    keep_url_schemes=False,
                    dry_run=False,
                )

            self.assertTrue(spec.destination.exists())
            self.assertEqual(
                list(spec.destination.parent.glob("*.backup-*")),
                [],
            )

    def test_list_ignores_backups_and_staging_apps(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_fake_app(
                root,
                name="微信 2.app",
                bundle_id=wm.CLONE_BUNDLE_PREFIX + "2",
                marker=True,
            )
            make_fake_app(
                root,
                name="微信 2.backup-20260729-120000.app",
                bundle_id=wm.CLONE_BUNDLE_PREFIX + "2",
                marker=True,
            )
            make_fake_app(
                root,
                name=".微信 3.codex-staging.app",
                bundle_id=wm.CLONE_BUNDLE_PREFIX + "3",
                marker=True,
            )

            clones = wm.list_clones(root)

            self.assertEqual([item.root.name for item in clones], ["微信 2.app"])


class LaunchTests(unittest.TestCase):
    def test_launch_uses_open_without_shell(self):
        app = wm.AppInfo(
            root=Path("/Applications/微信 2.app"),
            executable=Path("/Applications/微信 2.app/Contents/MacOS/WeChat"),
            bundle_id=wm.CLONE_BUNDLE_PREFIX + "2",
            version="4.1.11",
            build="269136",
            display_name="微信 2",
            is_clone=True,
            clone_index=2,
            source_build="269136",
        )
        runner = mock.Mock()
        wm.launch_app(app, runner, background=True)
        runner.run.assert_called_once_with(
            ["/usr/bin/open", "-g", "/Applications/微信 2.app"], timeout=15
        )

    def test_create_json_does_not_skip_requested_launch(self):
        source = wm.AppInfo(
            root=Path("/Applications/微信.app"),
            executable=Path("/Applications/微信.app/Contents/MacOS/WeChat"),
            bundle_id="com.tencent.xinWeChat",
            version="4.1.11",
            build="269136",
            display_name="微信",
            is_clone=False,
            clone_index=None,
            source_build=None,
        )
        clone = wm.AppInfo(
            root=Path("/Applications/微信 2.app"),
            executable=Path("/Applications/微信 2.app/Contents/MacOS/WeChat"),
            bundle_id=wm.CLONE_BUNDLE_PREFIX + "2",
            version="4.1.11",
            build="269136",
            display_name="微信 2",
            is_clone=True,
            clone_index=2,
            source_build="269136",
        )
        args = argparse.Namespace(
            json=True,
            dry_run=False,
            launch=True,
            background=True,
        )
        report = {
            "state": "created",
            "spec": wm.CloneSpec(
                2, "微信 2", clone.bundle_id, clone.root
            ).to_json(),
            "backup": None,
        }
        output = io.StringIO()

        with (
            mock.patch.object(
                wm,
                "_create_requested",
                return_value=(source, [clone], [report]),
            ),
            mock.patch.object(wm, "launch_app") as launch,
            mock.patch.object(
                wm, "wait_for_independent_container", return_value=True
            ),
            redirect_stdout(output),
        ):
            result = wm.command_create(args, mock.Mock())

        self.assertEqual(result, 0)
        launch.assert_called_once()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["launched"][0]["bundle_id"], clone.bundle_id)
        self.assertTrue(payload["independent_containers"][clone.bundle_id])

    def test_launch_validates_clone_before_writing_risk_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            make_fake_app(
                output,
                name="微信 2.app",
                bundle_id="com.tencent.xinWeChat",
                marker=False,
            )
            args = argparse.Namespace(
                app=None,
                output_dir=str(output),
                extra=1,
                include_original=False,
                background=False,
                accept_risk=True,
            )

            with self.assertRaisesRegex(wm.WeChatMultiError, "身份校验失败"):
                wm.command_launch(args, wm.CommandRunner())

            self.assertFalse((output / wm.RISK_ACK_FILE).exists())


class SafetyChecksTests(unittest.TestCase):
    @staticmethod
    def official_app_info() -> wm.AppInfo:
        return wm.AppInfo(
            root=Path("/Applications/微信.app"),
            executable=Path("/Applications/微信.app/Contents/MacOS/WeChat"),
            bundle_id="com.tencent.xinWeChat",
            version="4.1.11",
            build="269136",
            display_name="微信",
            is_clone=False,
            clone_index=None,
            source_build=None,
        )

    def test_accept_risk_persists_versioned_acknowledgement(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "clones"

            wm.ensure_risk_acknowledged(
                output,
                accept_risk=True,
                dry_run=False,
            )

            marker = output / wm.RISK_ACK_FILE
            self.assertEqual(
                marker.read_text(encoding="utf-8").strip(),
                wm.RISK_ACK_VERSION,
            )

    def test_dry_run_does_not_write_risk_acknowledgement(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "clones"

            wm.ensure_risk_acknowledged(
                output,
                accept_risk=False,
                dry_run=True,
            )

            self.assertFalse(output.exists())

    def test_interactive_risk_prompt_does_not_pollute_stdout(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "clones"
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                mock.patch.object(wm.sys.stdin, "isatty", return_value=True),
                mock.patch("builtins.input", return_value="yes"),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                wm.ensure_risk_acknowledged(
                    output,
                    accept_risk=False,
                    dry_run=False,
                )

            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("风险提示", stderr.getvalue())

    def test_clone_container_uses_its_bundle_id(self):
        app = wm.AppInfo(
            root=Path("/Applications/微信 2.app"),
            executable=Path("/Applications/微信 2.app/Contents/MacOS/WeChat"),
            bundle_id=wm.CLONE_BUNDLE_PREFIX + "2",
            version="4.1.11",
            build="269136",
            display_name="微信 2",
            is_clone=True,
            clone_index=2,
            source_build="269136",
        )

        self.assertEqual(
            wm.expected_container(app),
            Path.home()
            / "Library"
            / "Containers"
            / (wm.CLONE_BUNDLE_PREFIX + "2"),
        )

    def test_stale_container_without_clone_process_is_not_ready(self):
        app = wm.AppInfo(
            root=Path("/Applications/微信 2.app"),
            executable=Path("/Applications/微信 2.app/Contents/MacOS/WeChat"),
            bundle_id=wm.CLONE_BUNDLE_PREFIX + "2",
            version="4.1.11",
            build="269136",
            display_name="微信 2",
            is_clone=True,
            clone_index=2,
            source_build="269136",
        )
        with tempfile.TemporaryDirectory() as temp:
            stale_container = Path(temp) / "stale-container"
            stale_container.mkdir()
            with (
                mock.patch.object(
                    wm,
                    "expected_container",
                    return_value=stale_container,
                ),
                mock.patch.object(wm, "_main_processes", return_value=[]),
            ):
                ready = wm.wait_for_independent_container(
                    app,
                    wm.CommandRunner(),
                    timeout=0,
                )

        self.assertFalse(ready)

    def test_official_signature_accepts_expected_team_identity(self):
        runner = mock.Mock()
        runner.run.side_effect = [
            wm.CommandResult(
                ("/usr/bin/codesign",),
                0,
                "",
                "Identifier=com.tencent.xinWeChat\n"
                "TeamIdentifier=5A4RE8SF68\n",
            ),
            wm.CommandResult(
                ("/usr/bin/codesign",),
                1,
                "",
                "/Applications/微信.app: CSSMERR_TP_NOT_TRUSTED\n",
            ),
        ]

        wm._verify_official_source(self.official_app_info(), runner)

        self.assertEqual(runner.run.call_count, 2)

    def test_official_signature_rejects_wrong_team_identity(self):
        runner = mock.Mock()
        runner.run.return_value = wm.CommandResult(
            ("/usr/bin/codesign",),
            0,
            "",
            "Identifier=com.tencent.xinWeChat\n"
            "TeamIdentifier=EXAMPLE123\n",
        )

        with self.assertRaisesRegex(wm.WeChatMultiError, "腾讯签名身份"):
            wm._verify_official_source(self.official_app_info(), runner)


if __name__ == "__main__":
    unittest.main()
