import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

const apiTarget = process.env.VITE_API_PROXY || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  // Vue 会按这两个开关摇树。@vitejs/plugin-vue 把 __VUE_OPTIONS_API__ 默认成
  // true，于是 Options API 那条运行时路径被打进包里 —— 而这个前端从头到尾是
  // <script setup>，一个 Options 组件都没有。关掉省 1,758 B（gzip）。
  // __VUE_PROD_DEVTOOLS__ 不用管，plugin-vue 本来就默认 false。
  define: {
    __VUE_OPTIONS_API__: 'false',
  },
  build: {
    // Latency, not bandwidth, is what this panel waits on: a self-hosted box is
    // often a long way from its operator, and the network tab shows a 60kB bundle
    // arriving in 169ms while a 1.3kB chunk takes 380ms. What costs time is the
    // number of SEQUENTIAL round trips, so the build is tuned to shorten the
    // waterfall rather than to shave bytes.

    // One stylesheet instead of one per view. Each split file was a separate
    // request for 1-3kB, discovered only after its view chunk parsed.
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        // Fold tiny shared chunks (a 1.3kB logo module, a 1.7kB store) back into
        // whoever imports them. Below this size a separate file costs far more in
        // round trip than it saves in duplication — and the router prefetch warms
        // every view anyway, so splitting them buys nothing.
        experimentalMinChunkSize: 20_000,
      },
    },
  },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
        ws: true,
      },
    },
  },
})
