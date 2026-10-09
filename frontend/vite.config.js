import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // Same-origin /api calls from the SPA land on Django in dev.
    proxy: { '/api': 'http://127.0.0.1:8000' },
  },
})
