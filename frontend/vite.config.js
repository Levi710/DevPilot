import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

// The app talks to the local API through this development proxy.  When LAN
// mode is explicitly enabled, the proxy keeps the backend loopback-only and
// attaches its token server-side instead of exposing it to browser code.
export default defineConfig(({ mode }) => {
  const frontendRoot = dirname(fileURLToPath(import.meta.url))
  const projectRoot = resolve(frontendRoot, '..')
  const env = {
    ...loadEnv(mode, projectRoot, ''),
    ...loadEnv(mode, frontendRoot, ''),
  }
  const lanEnabled = env.DEVPILOT_LAN_ENABLED === 'true'
  const apiToken = env.DEVPILOT_API_TOKEN || ''

  return {
    plugins: [react()],
    server: {
      host: lanEnabled ? '0.0.0.0' : '127.0.0.1',
      proxy: {
        '/api': {
          target: env.DEVPILOT_API_PROXY_TARGET || 'http://127.0.0.1:8000',
          changeOrigin: true,
          configure: (proxy) => {
            proxy.on('proxyReq', (proxyRequest) => {
              if (apiToken) {
                proxyRequest.setHeader('X-DevPilot-Token', apiToken)
              }
            })
          },
        },
      },
    },
  }
})
