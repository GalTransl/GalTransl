"""A job that dies of a bare CancelledError must leave a traceback in the project's error.log.

User stops arrive as JobCancelledError, so a bare asyncio.CancelledError reaching run_job is never a
stop request. It used to be skipped by the error log like a normal stop: the 8.6.1 report showed only
"Read was cancelled" in the workbench, with nothing in error.log to tell which code path raised it.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from GalTransl.Service import JobCancelledError, JobSpec, run_job_async


def _fake_cfg(cache_dir: str):
    return SimpleNamespace(
        projectConfig={"backendSpecific": {}},
        keyValues={},
        non_interactive=False,
        runtime_project_dir="",
        print_translation_log_in_terminal=True,
        getCommonConfigSection=lambda: {"loggingLevel": "info"},
        getKey=lambda key, default=None: 1 if key == "workersPerProject" else default,
        getCachePath=lambda: cache_dir,
    )


class RunJobUnexpectedCancelTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, project_dir: str, error: BaseException):
        spec = JobSpec(job_id="job1", project_dir=project_dir, config_file_name="config.yaml", translator="ForGal-json")
        cache_dir = os.path.join(project_dir, "transl_cache")
        os.makedirs(cache_dir)
        with patch("GalTransl.server.reset_runtime_project"), patch(
            "GalTransl.server.update_runtime_status"
        ), patch("GalTransl.server.record_runtime_error"), patch(
            "GalTransl.Service.CProjectConfig", return_value=_fake_cfg(cache_dir)
        ), patch("GalTransl.Service.load_app_settings", return_value={}), patch(
            "GalTransl.Service.run_galtransl", new=AsyncMock(side_effect=error)
        ):
            return await run_job_async(spec)

    async def test_bare_cancelled_error_fails_the_job_and_is_logged_with_traceback(self):
        with tempfile.TemporaryDirectory() as project_dir:
            state = await self._run(project_dir, asyncio.CancelledError("Read was cancelled"))

            self.assertEqual(state.status, "failed")
            with open(os.path.join(project_dir, "error.log"), encoding="utf-8") as f:
                log = f.read()
            self.assertIn("exception_type=asyncio.exceptions.CancelledError", log)
            self.assertIn("exception_message=Read was cancelled", log)
            self.assertIn("Traceback", log)

    async def test_user_stop_is_still_not_logged(self):
        with tempfile.TemporaryDirectory() as project_dir:
            state = await self._run(project_dir, JobCancelledError())

            self.assertEqual(state.status, "cancelled")
            self.assertFalse(os.path.exists(os.path.join(project_dir, "error.log")))


if __name__ == "__main__":
    unittest.main()
