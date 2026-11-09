import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
export default defineConfig({
  plugins: [react()],
  base: '/',
  build: {
    outDir: '../src/devbrain/web_static',
    emptyOutDir: true,
    sourcemap: false,
    target: 'es2022',
    rollupOptions: {
      output: {
        manualChunks: {
          terminal: ['@xterm/xterm', '@xterm/addon-fit'],
          graph: ['three', 'three/examples/jsm/controls/OrbitControls.js'],
          react: ['react', 'react-dom'],
        },
      },
    },
  },
  server: { proxy: { '/api': 'http://127.0.0.1:8766', '/auth': 'http://127.0.0.1:8766' } },
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.test.ts', 'tests/**/*.test.tsx'],
    restoreMocks: true,
  },
});
