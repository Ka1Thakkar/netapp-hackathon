import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');

  const devPort = Number(env.VITE_DEV_PORT || 5173);
  const previewPort = Number(env.VITE_PREVIEW_PORT || 5173);
  const openBrowser = (env.VITE_OPEN_BROWSER ?? 'true').toLowerCase() !== 'false';

  // Optional backend bases for proxying during local dev
  const apiPy = env.VITE_API_PYTHON || 'http://localhost:8080';
  const apiGo = env.VITE_API_GO || 'http://localhost:8090';

  return {
    plugins: [react()],

    server: {
      port: devPort,
      open: openBrowser,
      strictPort: false,
      // Dev proxy helpers (use /py/... or /go/... to avoid CORS in dev)
      proxy: {
        '/py': {
          target: apiPy,
          changeOrigin: true,
          secure: false,
          rewrite: (p) => p.replace(/^\/py/, ''),
        },
        '/go': {
          target: apiGo,
          changeOrigin: true,
          secure: false,
          rewrite: (p) => p.replace(/^\/go/, ''),
        },
      },
    },

    preview: {
      port: previewPort,
      open: false,
    },

    build: {
      sourcemap: true,
      target: 'es2020',
      outDir: 'dist',
      assetsDir: 'assets',
      emptyOutDir: true,
    },

    define: {
      __APP_BUILD_TIME__: JSON.stringify(new Date().toISOString()),
    },

    // Optional: customize base if you host under a subpath
    // base: env.VITE_BASE_PATH || '/',
  };
});
