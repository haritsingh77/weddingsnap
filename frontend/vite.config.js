import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  server: {
    hmr: false,
    host: true,
  },
  plugins: [
    tailwindcss(),
    react(),
  ],
})