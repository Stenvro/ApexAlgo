import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const certPath = path.resolve(__dirname, '../.cert/cert.pem')
const keyPath = path.resolve(__dirname, '../.cert/key.pem')

export default defineConfig({
  envDir: '../',
  plugins: [
    react(),
    tailwindcss(),
  ],
  build: {
    target: 'esnext',
    // Never inline assets as data: URLs — the nginx CSP is font-src 'self',
    // so inlined font subsets (small @fontsource files) would be blocked.
    assetsInlineLimit: 0,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('node_modules/react-dom') || id.includes('node_modules/react/')) {
            return 'react-vendor';
          }
          if (id.includes('node_modules/lightweight-charts')) {
            return 'chart';
          }
          if (id.includes('node_modules/reactflow') || id.includes('node_modules/@reactflow')) {
            return 'flow';
          }
        }
      }
    }
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    https: readHttpsConfig(),
    // Same-origin API proxy so `npm run dev` works with the default
    // empty VITE_API_BASE_URL (client calls /api/... on its own origin).
    // secure: false — the backend uses a self-signed cert.
    proxy: {
      '/api': {
        target: 'https://localhost:8000',
        changeOrigin: true,
        secure: false,
      },
      '/health': {
        target: 'https://localhost:8000',
        secure: false,
      },
    },
  },
})

// Certs live in the shared data volume and may be owned by the Docker
// user; fall back to plain http instead of crashing the config load.
function readHttpsConfig() {
  try {
    return {
      cert: fs.readFileSync(certPath),
      key: fs.readFileSync(keyPath),
    }
  } catch {
    return false
  }
}
