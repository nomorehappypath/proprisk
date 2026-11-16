import react from '@vitejs/plugin-react'
import { defineConfig, type Plugin } from 'vite'
import { copyFileSync, mkdirSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const MAPLIBRE_WORKER_FILES = ['maplibre-gl-worker.mjs', 'maplibre-gl-shared.mjs'] as const

function maplibreWorkerAssets(): Plugin {
  return {
    name: 'maplibre-worker-assets',
    configureServer(server) {
      const source = resolve('node_modules/maplibre-gl/dist')
      const modules = new Map(
        MAPLIBRE_WORKER_FILES.map((file) => [`/assets/${file}`, readFileSync(resolve(source, file))]),
      )
      server.middlewares.use((request, response, next) => {
        const pathname = request.url?.split('?', 1)[0]
        const module = pathname ? modules.get(pathname) : undefined
        if (!module) {
          next()
          return
        }
        response.statusCode = 200
        response.setHeader('Content-Type', 'text/javascript; charset=utf-8')
        response.setHeader('Cache-Control', 'no-cache')
        response.end(module)
      })
    },
    closeBundle() {
      const assets = resolve('dist/assets')
      const source = resolve('node_modules/maplibre-gl/dist')
      mkdirSync(assets, { recursive: true })
      for (const file of MAPLIBRE_WORKER_FILES) {
        copyFileSync(resolve(source, file), resolve(assets, file))
      }
    },
  }
}

export default defineConfig({
  plugins: [react(), maplibreWorkerAssets()],
  // maplibre-gl ships its render worker as a module URL. The dev dependency
  // pre-bundler rewrites that URL and the worker 404s, which leaves a blank
  // map in `npm run dev` only — excluding it keeps dev identical to the build.
  optimizeDeps: { exclude: ['maplibre-gl'] },
  server: {
    // Single-user, local-only app: dev server binds the loopback address, same as the built app.
    proxy: {
      // All backend routes live under /api so they never collide with app screen URLs.
      '/api': { target: 'http://127.0.0.1:8765', changeOrigin: true },
    },
  },
})
