# Python tests

Run the complete backend suite from this directory using an interpreter with
the dependencies in `../requirements.txt` and pytest installed:

```powershell
cd tests
../.venv/Scripts/python.exe -m pytest . -q
```

On Linux or macOS, use `../.venv/bin/python` instead. `pytest.ini` adds the
repository to the import path, so no `PYTHONPATH` configuration is needed.

For focused iteration, pass the affected test files. To locate slow tests,
append `--durations=20`; this includes fixture setup and teardown times.

Frontend tests run separately with `npm test` from `../desktop`.

Local HTTP servers use `serve_forever(poll_interval=0.01)` so shutdown does not
wait for the default half-second polling interval. Keep real HTTP requests and
join or close the server during cleanup.

The extraction resource tests build a Git/submodule template once per module
and copy it for each test. Each copy has its own files, Git objects, and upstream
remote; tests may commit, fetch, edit, or deinitialize without changing the
template or another test's repository. Keep the template immutable and reset
absolute remote URLs when copying it.

Skill download tests reuse the real interpreter discovery results within their
module: these tests change Skill files, not installed Python environments.
Interpreter discovery itself remains covered by `test_python_discovery.py`.

Tests for ordinary Agent behavior should explicitly disable the optional
billion-context plugin through their settings fixture. Developer application
settings must not change which implementation a test exercises.
