import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// Im Entwicklungsmodus laeuft Vite auf 5173 und reicht /api an die Python-Engine
// auf 8765 weiter. Im gebauten Zustand liefert die Engine die Dateien aus ui/dist
// selbst aus - dann gibt es nur noch einen Prozess und einen Port.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
});
