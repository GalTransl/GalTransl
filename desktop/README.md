# GalTransl Desktop

This folder contains the Tauri desktop shell and the web frontend for GalTransl.

## Development

1. Install frontend dependencies:
   `npm install`
2. Start the Python backend from the repository root:
   `python run_backend.py --host 127.0.0.1 --port 12333`
3. Start the frontend dev server:
   `npm run dev`
4. With Rust installed, you can run the desktop shell:
   `npm run tauri:dev`

## Notes

- This machine currently does not have Rust/Cargo installed, so Tauri packaging cannot be verified here yet.
- The frontend is intentionally browser-compatible and talks to the Python backend over HTTP.
