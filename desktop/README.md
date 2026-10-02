# GalTransl Desktop

This folder contains the Tauri desktop shell and the React frontend for GalTransl.

## Development

1. Install frontend dependencies:
   `npm ci`
2. Start the Python backend from the repository root:
   `python run_backend.py --host 127.0.0.1 --port 12333`
3. Start the frontend dev server:
   `npm run dev`
4. With Rust installed, run the desktop shell:
   `npm run tauri:dev`

On Linux x86_64, `../run_desktop_dev.sh` performs steps 2-4 and stops the
backend when the desktop process exits. The frontend remains browser-compatible
and talks to the Python backend over HTTP.

- The frontend is intentionally browser-compatible and talks to the Python backend over HTTP.
- Each packaged desktop process starts its own backend with `--port 0`. The operating system selects the port while binding; the backend publishes its actual address through a private `--ready-file`, and Tauri passes that address to the frontend before API requests begin.
- Closing a packaged window stops only the backend process tree it started. Concurrent windows have separate backends. Startup failures and timeouts also clean up their own child processes.
- Development continues to use the external backend at `127.0.0.1:12333`; closing the dev window leaves that backend running. Explicit `VITE_BACKEND_URL` overrides remain available.
- Rebuild both the desktop executable and the Python backend when packaging these changes, since the startup handshake changed. Assigned ports are kept in memory and are not saved to frontend preferences.

## Linux build

Install the Tauri Linux prerequisites for your distribution, then run:

```bash
python ../build_linux_x64.py
```

The script builds the PyInstaller backend, embeds it as a Tauri sidecar, bundles
`plugins`, `Dict`, `translation_guidelines`, and `res`, and generates `.deb`,
`.rpm`, `.AppImage`, and portable `.tar.gz` artifacts. Only x86_64 is supported;
ARM targets are intentionally out of scope.

## Connection tests

- Frontend address selection, HTTP/SSE routing, and reconnects: `npm run test:backend-connection`.
- Native process lifecycle (from `src-tauri`): `cargo test --offline backend::tests -- --include-ignored`. These tests launch real Python backends using the repository's `.venv`; set `GALTRANSL_TEST_PYTHON` to use another Python environment with the backend dependencies installed. On Windows, the test environment must allow stopping its own child process trees.
