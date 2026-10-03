"""Validate installed macOS app layouts and always detach release DMGs."""
import plistlib
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from scripts import smoke_test_release as smoke

VERSION = "8.2.0"


def make_app(directory: Path) -> Path:
    app = directory / "GalTransl Desktop.app"
    contents = app / "Contents"
    binaries = contents / "MacOS"
    binaries.mkdir(parents=True)
    for name in ("galtransl-desktop", "galtransl_backend"):
        binary = binaries / name
        binary.write_bytes(b"executable")
        binary.chmod(0o755)
    (contents / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleExecutable": "galtransl-desktop", "CFBundleShortVersionString": VERSION,
    }))
    for name in smoke.RESOURCE_DIRS:
        resource = contents / "Resources" / name
        resource.mkdir(parents=True)
        (resource / "resource.txt").write_text("resource", encoding="utf-8")
    return app


class MacOSReleaseTests(TestCase):
    def test_app_checks_both_binary_architectures_and_returns_bundle_resources(self):
        with TemporaryDirectory() as tmp:
            app = make_app(Path(tmp))
            for platform, architecture in (("macos_x86_64", "x86_64"), ("macos_arm64", "arm64")):
                with self.subTest(platform=platform), patch.object(smoke.subprocess, "run") as run:
                    backend, resources = smoke.check_macos_app(app, VERSION, platform)
                    self.assertEqual(backend, app / "Contents/MacOS/galtransl_backend")
                    self.assertEqual(resources, app / "Contents/Resources")
                    self.assertEqual([call.args[0] for call in run.call_args_list], [
                        ["lipo", "-verify_arch", architecture, str(app / "Contents/MacOS/galtransl-desktop")],
                        ["lipo", "-verify_arch", architecture, str(backend)],
                    ])

    def test_rejects_incorrect_app_version_missing_sidecar_and_empty_resources(self):
        for failure in ("version", "backend", "resources"):
            with self.subTest(failure=failure), TemporaryDirectory() as tmp:
                app = make_app(Path(tmp))
                version = "0.0.0" if failure == "version" else VERSION
                if failure == "backend":
                    (app / "Contents/MacOS/galtransl_backend").unlink()
                if failure == "resources":
                    (app / "Contents/Resources/plugins/resource.txt").unlink()
                with patch.object(smoke.subprocess, "run"), self.assertRaises(RuntimeError):
                    smoke.check_macos_app(app, version, "macos_arm64")

    def test_rejects_sidecar_built_for_a_different_architecture(self):
        with TemporaryDirectory() as tmp:
            app = make_app(Path(tmp))
            with patch.object(smoke.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0), subprocess.CalledProcessError(1, "lipo"),
            ]), self.assertRaises(subprocess.CalledProcessError):
                smoke.check_macos_app(app, VERSION, "macos_arm64")

    def test_dmg_copies_app_before_testing_and_detaches_on_success_or_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail), TemporaryDirectory() as tmp:
                directory = Path(tmp)
                mounted_apps = []

                def hdiutil(command, **kwargs):
                    if command[1] == "attach":
                        mount = Path(command[command.index("-mountpoint") + 1])
                        mounted_apps.append(make_app(mount))

                with patch.object(smoke.subprocess, "run", side_effect=hdiutil) as run:
                    try:
                        with smoke.installed_dmg_app(directory / "release.dmg", directory / "check") as app:
                            self.assertNotEqual(app, mounted_apps[0])
                            self.assertTrue((app / "Contents/MacOS/galtransl_backend").is_file())
                            if fail:
                                raise RuntimeError("backend check failed")
                    except RuntimeError:
                        if not fail:
                            raise
                    self.assertEqual(run.call_args.args[0], [
                        "hdiutil", "detach", str(directory / "check/mount"), "-quiet",
                    ])

    def test_empty_dmg_is_rejected_and_detached(self):
        with TemporaryDirectory() as tmp, patch.object(smoke.subprocess, "run") as run:
            directory = Path(tmp)
            with self.assertRaisesRegex(RuntimeError, "exactly one application"):
                with smoke.installed_dmg_app(directory / "empty.dmg", directory / "check"):
                    self.fail("must not accept an empty disk image")
            self.assertEqual(run.call_args.args[0][1], "detach")
