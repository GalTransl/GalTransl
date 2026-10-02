"""Offline checks for the cross-platform release smoke test and workflow."""
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import yaml

import build_release
from scripts import smoke_test_release as smoke


VERSION = "8.2.0"
ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "build-release.yml"


class ReleaseArchiveTests(unittest.TestCase):
    def make_archive(self, directory, platform="win", omit=None, empty=None):
        name = f"GalTransl_{VERSION}_{platform}"
        files = {
            "GalTransl Desktop.exe" if platform == "win" else "galtransl-desktop": b"frontend",
            "backend/galtransl_backend.exe" if platform == "win" else "backend/galtransl_backend": b"backend",
            **{f"{folder}/resource.txt": b"resource" for folder in smoke.RESOURCE_DIRS},
        }
        files = {path: b"" if path == empty else data for path, data in files.items() if path != omit}
        if platform == "win":
            archive = directory / f"{name}.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                for path, data in files.items():
                    bundle.writestr(f"{name}/{path}", data)
        else:
            archive = directory / f"{name}.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                for path, data in files.items():
                    info = tarfile.TarInfo(f"{name}/{path}")
                    info.size = len(data)
                    info.mode = 0o755
                    bundle.addfile(info, io.BytesIO(data))
        return archive

    def test_expected_artifacts_requires_every_nonempty_file(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for platform, formats in smoke.FORMATS.items():
                for ext in formats:
                    (directory / f"GalTransl_{VERSION}_{platform}.{ext}").write_bytes(b"package")
                self.assertEqual(len(smoke.expected_artifacts(directory, VERSION, platform)), len(formats))
            missing = directory / f"GalTransl_{VERSION}_linux_x86_64.rpm"
            missing.unlink()
            with self.assertRaisesRegex(RuntimeError, "Missing or empty"):
                smoke.expected_artifacts(directory, VERSION, "linux_x86_64")
            missing.touch()
            with self.assertRaisesRegex(RuntimeError, "Missing or empty"):
                smoke.expected_artifacts(directory, VERSION, "linux_x86_64")

    def test_extracts_windows_archive_with_resources(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            archive = self.make_archive(directory)
            backend = smoke.unpack_release(archive, directory / "out", VERSION, "win")
            self.assertEqual(backend.read_bytes(), b"backend")

    def test_rejects_missing_executables_and_resources(self):
        for missing in ("GalTransl Desktop.exe", "backend/galtransl_backend.exe", "plugins/resource.txt"):
            with self.subTest(missing=missing), TemporaryDirectory() as tmp:
                directory = Path(tmp)
                archive = self.make_archive(directory, omit=missing)
                with self.assertRaisesRegex(RuntimeError, "Missing or empty"):
                    smoke.unpack_release(archive, directory / "out", VERSION, "win")

    def test_rejects_empty_executable(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            archive = self.make_archive(directory, empty="GalTransl Desktop.exe")
            with self.assertRaisesRegex(RuntimeError, "Missing or empty"):
                smoke.unpack_release(archive, directory / "out", VERSION, "win")

    def test_rejects_wrong_archive_version_and_corrupt_archive(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            archive = self.make_archive(directory)
            with self.assertRaises(RuntimeError):
                smoke.unpack_release(archive, directory / "out", "0.0.0", "win")
            archive.write_bytes(b"not a ZIP")
            with self.assertRaises(zipfile.BadZipFile):
                smoke.unpack_release(archive, directory / "out", VERSION, "win")

    def test_linux_archive_requires_executable_permissions(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            archive = self.make_archive(directory, platform="linux_x86_64")
            with patch.object(smoke.os, "access", return_value=True):
                backend = smoke.unpack_release(archive, directory / "out", VERSION, "linux_x86_64")
                self.assertEqual(backend.read_bytes(), b"backend")
            with patch.object(smoke.os, "access", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "execute permission"):
                    smoke.unpack_release(archive, directory / "out", VERSION, "linux_x86_64")

    def test_linux_archive_cannot_extract_outside_destination(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            archive = directory / "unsafe.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                info = tarfile.TarInfo("../outside")
                info.size = 1
                bundle.addfile(info, io.BytesIO(b"x"))
            with self.assertRaises(tarfile.FilterError):
                smoke.unpack_release(archive, directory / "out", VERSION, "linux_x86_64")
            self.assertFalse((directory / "outside").exists())


class BackendSmokeTests(unittest.TestCase):
    def test_environment_does_not_reuse_checkout_or_user_settings(self):
        overrides = {name: "source-checkout" for name in (
            "PYTHONPATH", "PYTHONHOME", "GALTRANSL_RESOURCE_DIR", "GALTRANSL_DATA_DIR", "GALTRANSL_APP_SETTINGS_PATH",
        )}
        with patch.dict(os.environ, overrides):
            env = smoke.isolated_environment(Path("temporary"))
            for name in overrides:
                self.assertNotIn(name, env)
            self.assertEqual(env["XDG_CONFIG_HOME"], str(Path("temporary/config")))
            self.assertEqual(os.environ["PYTHONPATH"], "source-checkout")

    def test_ready_file_uses_dynamic_loopback_port(self):
        with TemporaryDirectory() as tmp:
            ready = Path(tmp) / "ready.json"
            ready.write_text(json.dumps({"host": "127.0.0.1", "port": 34567}), encoding="utf-8")
            process = Mock()
            process.poll.return_value = None
            self.assertEqual(smoke.wait_for_backend(process, ready, 1), "http://127.0.0.1:34567")
            ready.write_text(json.dumps({"host": "example.com", "port": 34567}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Invalid backend ready"):
                smoke.wait_for_backend(process, ready, 1)

    def test_detects_crash_and_readiness_timeout(self):
        process = Mock(returncode=7)
        process.poll.return_value = 7
        with TemporaryDirectory() as tmp:
            ready = Path(tmp) / "missing.json"
            with self.assertRaisesRegex(RuntimeError, "exited with code 7"):
                smoke.wait_for_backend(process, ready, 1)
            process.poll.return_value = None
            with patch.object(smoke.time, "monotonic", side_effect=[0, 0, 2]), patch.object(smoke.time, "sleep"):
                with self.assertRaises(TimeoutError):
                    smoke.wait_for_backend(process, ready, 1)

    def test_api_checks_version_and_nonempty_plugins(self):
        for version, plugins, success in ((VERSION, [{}], True), ("0.0.0", [{}], False), (VERSION, [], False), (VERSION, {}, False)):
            with self.subTest(version=version, plugins=plugins):
                opener = Mock()
                opener.open.side_effect = [
                    io.BytesIO(json.dumps({"version": version}).encode()),
                    io.BytesIO(json.dumps({"plugins": plugins}).encode()),
                ]
                with patch.object(smoke, "build_opener", return_value=opener):
                    if success:
                        smoke.check_api("http://127.0.0.1:34567", VERSION)
                    else:
                        with self.assertRaises(RuntimeError):
                            smoke.check_api("http://127.0.0.1:34567", VERSION)

    def test_always_stops_backend_on_success_api_error_or_timeout(self):
        for failure in (None, RuntimeError("wrong version"), TimeoutError("not ready")):
            with self.subTest(failure=failure), TemporaryDirectory() as tmp:
                process = Mock()
                with patch.object(smoke.subprocess, "Popen", return_value=process) as start, \
                     patch.object(smoke, "wait_for_backend", return_value="http://127.0.0.1:34567") as ready, \
                     patch.object(smoke, "check_api") as check, \
                     patch.object(smoke, "stop_backend") as stop:
                    if isinstance(failure, TimeoutError):
                        ready.side_effect = failure
                    else:
                        check.side_effect = failure
                    if failure:
                        with self.assertRaises(type(failure)):
                            smoke.smoke_backend(Path("backend.exe"), Path(tmp), VERSION, 1)
                    else:
                        smoke.smoke_backend(Path("backend.exe"), Path(tmp), VERSION, 1)
                    stop.assert_called_once_with(process)
                    self.assertEqual(start.call_args.kwargs["cwd"], Path(tmp))
                    self.assertNotIn("PYTHONPATH", start.call_args.kwargs["env"])

    def test_windows_cleanup_targets_only_its_process_tree(self):
        process = Mock(pid=123)
        process.poll.return_value = None
        with patch.object(sys, "platform", "win32"), patch.object(smoke.subprocess, "run") as run:
            smoke.stop_backend(process)
        self.assertEqual(run.call_args.args[0], ["taskkill", "/PID", "123", "/T", "/F"])
        process.wait.assert_called_once_with(timeout=10)

    def test_linux_cleanup_escalates_after_timeout(self):
        process = Mock(pid=123)
        process.wait.side_effect = [subprocess.TimeoutExpired("backend", 10), 0]
        with patch.object(sys, "platform", "linux"), patch.object(smoke.os, "killpg", create=True) as killpg:
            # SIGKILL is not defined by Python on Windows.
            with patch.object(smoke.signal, "SIGKILL", 9, create=True):
                smoke.stop_backend(process)
        self.assertEqual(killpg.call_count, 2)
        self.assertEqual(killpg.call_args.args, (123, 9))


class ReleaseWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    def test_platforms_and_release_permissions(self):
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        jobs = self.workflow["jobs"]
        matrix = jobs["build"]["strategy"]["matrix"]["include"]
        self.assertEqual({entry["os"] for entry in matrix}, {"windows-2022", "ubuntu-22.04"})
        self.assertEqual(jobs["build"]["strategy"]["fail-fast"], "false")
        release = jobs["draft_release"]
        self.assertEqual(release["permissions"], {"contents": "write"})
        self.assertEqual(set(release["needs"]), {"metadata", "build"})
        self.assertEqual(release["if"], "github.event_name == 'push' && github.ref_type == 'tag'")
        self.assertIn("workflow_dispatch", self.workflow["on"])

    def test_checksum_manifest_only_includes_expected_assets(self):
        steps = self.workflow["jobs"]["draft_release"]["steps"]
        script = next(step["run"] for step in steps if step.get("shell") == "python")
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            assets = directory / "release-assets"
            assets.mkdir()
            names = [f"GalTransl_{VERSION}_{platform}.{ext}" for platform, formats in smoke.FORMATS.items() for ext in formats]
            for name in names:
                (assets / name).write_bytes(b"package")
            (assets / "unrelated.txt").write_bytes(b"not a release asset")
            env = {**os.environ, "PYTHONPATH": str(ROOT), "RELEASE_VERSION": VERSION, "RUNNER_TEMP": tmp}
            result = subprocess.run([sys.executable, "-c", script], cwd=directory, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            checksum_lines = (assets / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
            self.assertEqual({line.split("  ")[1] for line in checksum_lines}, set(names))
            manifest = (directory / "release-assets.txt").read_text(encoding="utf-8").splitlines()
            self.assertEqual({Path(path).name for path in manifest}, {*names, "SHA256SUMS.txt"})
            self.assertEqual(len(checksum_lines), 6)

    @unittest.skipUnless(
        sys.platform != "win32" and shutil.which("bash") and shutil.which("jq"),
        "POSIX bash and jq are needed for offline release workflow tests",
    )
    def test_release_states_without_network(self):
        script = self.workflow["jobs"]["draft_release"]["steps"][-1]["run"]
        # gh is a shell function: these tests must never call GitHub or need a token.
        stub = r'''
        gh() {
          printf '%s\n' "$*" >> "$CALLS"
          case "$1 $2" in
            "api --paginate")
              case "$STATE" in
                new) ;;
                api-error) return 1 ;;
                published) printf '{"id":1,"tag_name":"8.2.0","draft":false}\n' ;;
                *) printf '{"id":1,"tag_name":"8.2.0","draft":true}\n' ;;
              esac ;;
            "release view")
              if [[ "$STATE" == "published-during-upload" ]]; then
                printf 'false\n'
              else
                printf 'true\n'
              fi ;;
            "release create") ;;
            "release upload")
              if [[ "$STATE" == "upload-error" ]]; then return 1; fi ;;
            *) return 99 ;;
          esac
        }
        '''
        for state in ("new", "draft", "published", "api-error", "published-during-upload", "upload-error"):
            with self.subTest(state=state), TemporaryDirectory() as tmp:
                directory = Path(tmp)
                calls = directory / "calls.txt"
                calls.touch()
                (directory / "release-assets.txt").write_text("first.zip\nsecond.tar.gz\n", encoding="utf-8")
                env = {
                    **os.environ, "STATE": state, "CALLS": calls.name,
                    "RUNNER_TEMP": ".", "RELEASE_TAG": VERSION,
                    "RELEASE_VERSION": VERSION, "GH_REPO": "example/repo",
                }
                result = subprocess.run(["bash", "-c", stub + "\n" + script], cwd=directory, env=env,
                                        capture_output=True, text=True, timeout=30)
                commands = calls.read_text(encoding="utf-8").splitlines()
                writes = [line for line in commands if line.startswith(("release create", "release upload", "release edit"))]
                if state in ("new", "draft"):
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(sum(line.startswith("release upload") for line in writes), 2)
                    self.assertEqual(sum(line.startswith("release create") for line in writes), int(state == "new"))
                    if state == "new":
                        self.assertIn("--draft --verify-tag", writes[0])
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(len(writes), 1 if state == "upload-error" else 0)
                self.assertFalse(any(line.startswith("release edit") for line in commands))

    def test_metadata_checks_only_tag_pushes(self):
        script = next(step["run"] for step in self.workflow["jobs"]["metadata"]["steps"] if step.get("id") == "version")
        cases = [
            ("push", "tag", VERSION, True),
            ("push", "tag", f"v{VERSION}", True),
            ("push", "tag", "8.1.0", False),
            ("push", "tag", f"{VERSION}-rc1", False),
            ("push", "branch", "main", True),
            ("pull_request", "branch", "42/merge", True),
            ("workflow_dispatch", "tag", "8.1.0", True),
        ]
        for event, ref_type, ref_name, success in cases:
            with self.subTest(event=event, ref=ref_name), TemporaryDirectory() as tmp:
                output = Path(tmp) / "output.txt"
                env = {"EVENT_NAME": event, "REF_TYPE": ref_type, "REF_NAME": ref_name, "GITHUB_OUTPUT": str(output)}
                with patch.dict(os.environ, env), patch.object(build_release, "get_version", return_value=VERSION):
                    if success:
                        exec(compile(script, "workflow-version", "exec"), {})
                        self.assertEqual(output.read_text(encoding="utf-8"), f"version={VERSION}\n")
                    else:
                        with self.assertRaises(SystemExit):
                            exec(compile(script, "workflow-version", "exec"), {})
                        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
