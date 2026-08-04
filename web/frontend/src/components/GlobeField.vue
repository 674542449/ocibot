<template>
  <canvas ref="el" class="globe" aria-hidden="true"></canvas>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'

/**
 * Rotating region globe for the sign-in page.
 *
 * Hand-written canvas 2D with its own 3D projection rather than a WebGL library:
 * the panel's CSP is script-src 'self', so a CDN is not an option, and bundling
 * three.js would put ~600 kB in front of the very first page that loads. This is
 * a few kB and does what the brief needs.
 *
 * The points are real cloud-region coordinates, not noise. This panel manages
 * machines scattered across those regions, so the globe is showing the subject
 * rather than decorating it.
 */

const el = ref<HTMLCanvasElement | null>(null)
let raf = 0
let stop = false
let detachResize: (() => void) | null = null

/** name, latitude, longitude — the regions this panel actually talks to. */
const REGIONS: Array<[string, number, number]> = [
  ['Tokyo', 35.7, 139.7],
  ['Osaka', 34.7, 135.5],
  ['Seoul', 37.6, 127.0],
  ['Chuncheon', 37.9, 127.7],
  ['Singapore', 1.35, 103.8],
  ['Mumbai', 19.1, 72.9],
  ['Hyderabad', 17.4, 78.5],
  ['Sydney', -33.9, 151.2],
  ['Melbourne', -37.8, 145.0],
  ['Frankfurt', 50.1, 8.7],
  ['Amsterdam', 52.4, 4.9],
  ['London', 51.5, -0.1],
  ['Newport', 51.6, -3.0],
  ['Zurich', 47.4, 8.5],
  ['Marseille', 43.3, 5.4],
  ['Stockholm', 59.3, 18.1],
  ['Milan', 45.5, 9.2],
  ['Madrid', 40.4, -3.7],
  ['Paris', 48.9, 2.4],
  ['Jeddah', 21.5, 39.2],
  ['Dubai', 25.2, 55.3],
  ['Jerusalem', 31.8, 35.2],
  ['Ashburn', 39.0, -77.5],
  ['Phoenix', 33.4, -112.1],
  ['San Jose', 37.3, -121.9],
  ['Chicago', 41.9, -87.6],
  ['Toronto', 43.7, -79.4],
  ['Montreal', 45.5, -73.6],
  ['Sao Paulo', -23.6, -46.6],
  ['Vinhedo', -23.0, -46.98],
  ['Santiago', -33.5, -70.7],
  ['Bogota', 4.7, -74.1],
  ['Queretaro', 20.6, -100.4],
  ['Monterrey', 25.7, -100.3],
  ['Johannesburg', -26.2, 28.0],
]

type P3 = { x: number; y: number; z: number }

function toSphere(lat: number, lon: number): P3 {
  const a = (lat * Math.PI) / 180
  const b = (lon * Math.PI) / 180
  return { x: Math.cos(a) * Math.sin(b), y: Math.sin(a), z: Math.cos(a) * Math.cos(b) }
}

const NODES: P3[] = REGIONS.map(([, lat, lon]) => toSphere(lat, lon))

/** Latitude/longitude wireframe, sampled coarsely — it reads as a globe without
 *  becoming a mesh that competes with the region points for attention. */
const WIRE: P3[][] = (() => {
  const lines: P3[][] = []
  for (let lat = -60; lat <= 60; lat += 30) {
    const ring: P3[] = []
    for (let lon = -180; lon <= 180; lon += 9) ring.push(toSphere(lat, lon))
    lines.push(ring)
  }
  for (let lon = -180; lon < 180; lon += 30) {
    const ring: P3[] = []
    for (let lat = -90; lat <= 90; lat += 9) ring.push(toSphere(lat, lon))
    lines.push(ring)
  }
  return lines
})()

/** Great-circle-ish arcs between region pairs: the traffic this panel implies. */
const LINKS: Array<[number, number]> = [
  [0, 4], [0, 22], [0, 2], [4, 9], [9, 22], [22, 25], [25, 27], [4, 5],
  [9, 11], [7, 4], [22, 24], [0, 1], [11, 9], [20, 4], [34, 9], [30, 28],
]

function arcPoint(a: P3, b: P3, t: number, lift: number): P3 {
  const x = a.x + (b.x - a.x) * t
  const y = a.y + (b.y - a.y) * t
  const z = a.z + (b.z - a.z) * t
  const len = Math.hypot(x, y, z) || 1
  const r = 1 + lift * Math.sin(Math.PI * t)
  return { x: (x / len) * r, y: (y / len) * r, z: (z / len) * r }
}

onMounted(() => {
  const canvas = el.value
  if (!canvas) return
  const ctx = canvas.getContext('2d')
  if (!ctx) return

  const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
  let w = 0
  let h = 0
  let dpr = 1

  const resize = () => {
    const r = canvas.getBoundingClientRect()
    // Cap DPR: past 2 the extra pixels cost far more than they show.
    dpr = Math.min(window.devicePixelRatio || 1, 2)
    w = Math.max(1, Math.round(r.width))
    h = Math.max(1, Math.round(r.height))
    canvas.width = Math.round(w * dpr)
    canvas.height = Math.round(h * dpr)
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  }
  resize()

  // Ambient depth behind the globe. Fixed seed-free random is fine: they never
  // need to be reproducible, only to sit still.
  const STARS = Array.from({ length: 90 }, () => ({
    x: Math.random(),
    y: Math.random(),
    r: Math.random() * 1.1 + 0.25,
    a: Math.random() * 0.35 + 0.08,
  }))

  const rotate = (p: P3, ry: number, rx: number): P3 => {
    const cy = Math.cos(ry)
    const sy = Math.sin(ry)
    const x1 = p.x * cy - p.z * sy
    const z1 = p.x * sy + p.z * cy
    const cx = Math.cos(rx)
    const sx = Math.sin(rx)
    return { x: x1, y: p.y * cx - z1 * sx, z: p.y * sx + z1 * cx }
  }

  const draw = (t: number) => {
    const cx = w * 0.5
    const cy = h * 0.5
    const R = Math.min(w, h) * 0.36
    const ry = t * 0.00035
    const rx = -0.38
    // Perspective: points nearer the camera spread out and brighten.
    const proj = (p: P3) => {
      const q = rotate(p, ry, rx)
      const k = 2.6 / (2.6 - q.z)
      return { x: cx + q.x * R * k, y: cy - q.y * R * k, z: q.z, k }
    }

    ctx.clearRect(0, 0, w, h)

    for (const s of STARS) {
      ctx.globalAlpha = s.a
      ctx.fillStyle = '#c9c7ff'
      ctx.beginPath()
      ctx.arc(s.x * w, s.y * h, s.r, 0, Math.PI * 2)
      ctx.fill()
    }
    ctx.globalAlpha = 1

    // Wireframe — back hemisphere first so the front reads as in front.
    ctx.lineWidth = 1
    for (const ring of WIRE) {
      for (const front of [false, true]) {
        ctx.beginPath()
        let started = false
        for (const p of ring) {
          const q = proj(p)
          if (q.z > 0 !== front) {
            started = false
            continue
          }
          if (!started) {
            ctx.moveTo(q.x, q.y)
            started = true
          } else ctx.lineTo(q.x, q.y)
        }
        ctx.strokeStyle = front ? 'rgba(140,132,255,0.22)' : 'rgba(140,132,255,0.07)'
        ctx.stroke()
      }
    }

    // Link arcs with a travelling pulse — the panel reaching across regions.
    for (let i = 0; i < LINKS.length; i++) {
      const [ai, bi] = LINKS[i]
      const a = NODES[ai]
      const b = NODES[bi]
      if (!a || !b) continue
      ctx.beginPath()
      let started = false
      for (let s = 0; s <= 1.0001; s += 1 / 24) {
        const q = proj(arcPoint(a, b, s, 0.22))
        if (q.z < -0.15) {
          started = false
          continue
        }
        if (!started) {
          ctx.moveTo(q.x, q.y)
          started = true
        } else ctx.lineTo(q.x, q.y)
      }
      ctx.strokeStyle = 'rgba(150,140,255,0.16)'
      ctx.lineWidth = 1
      ctx.stroke()

      const phase = ((t * 0.0004 + i * 0.137) % 1 + 1) % 1
      const q = proj(arcPoint(a, b, phase, 0.22))
      if (q.z >= -0.15) {
        ctx.globalAlpha = 0.9
        ctx.fillStyle = '#d7d2ff'
        ctx.beginPath()
        ctx.arc(q.x, q.y, 1.7 * q.k, 0, Math.PI * 2)
        ctx.fill()
        ctx.globalAlpha = 1
      }
    }

    // Region nodes: depth drives size and brightness, so the sphere reads solid.
    for (let i = 0; i < NODES.length; i++) {
      const q = proj(NODES[i])
      const front = q.z > 0
      const glow = 0.5 + 0.5 * Math.sin(t * 0.0022 + i * 0.9)
      const r = (front ? 2.1 : 1.2) * q.k
      ctx.globalAlpha = front ? 0.55 + 0.45 * glow : 0.16
      ctx.fillStyle = front ? '#a99fff' : '#6f68b8'
      ctx.beginPath()
      ctx.arc(q.x, q.y, r, 0, Math.PI * 2)
      ctx.fill()
      if (front && glow > 0.75) {
        ctx.globalAlpha = (glow - 0.75) * 0.9
        ctx.beginPath()
        ctx.arc(q.x, q.y, r * 3.2, 0, Math.PI * 2)
        ctx.fillStyle = '#8a80ff'
        ctx.fill()
      }
    }
    ctx.globalAlpha = 1
  }

  // Drawn once synchronously so the globe is present even before the first
  // animation frame — and in any environment where one never arrives.
  draw(0)

  // Reduced motion runs the whole scene at a fraction of the speed rather than
  // freezing it: the setting asks for calm, not for a still image, and a globe
  // that never turns reads as broken. Freezing it outright is what made the
  // first version look like it had failed to start.
  const speed = reduced ? 0.4 : 1
  const loop = (t: number) => {
    if (stop) return
    if (!document.hidden) draw(t * speed)
    raf = requestAnimationFrame(loop)
  }
  raf = requestAnimationFrame(loop)

  const onResize = () => {
    resize()
    draw(performance.now())
  }
  window.addEventListener('resize', onResize)
  detachResize = () => window.removeEventListener('resize', onResize)
})

onBeforeUnmount(() => {
  stop = true
  if (raf) cancelAnimationFrame(raf)
  detachResize?.()
  detachResize = null
})
</script>

<style scoped>
.globe {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  display: block;
}
</style>
