import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 1420,
    watch: {
      // Tauri watches Rust sources; Vite must not watch locked build artifacts.
      ignored: ['**/src-tauri/**'],
    },
  },
  clearScreen: false,
});
