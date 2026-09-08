// 拿一台真实 HTTP 服务器跑一遍 src/api/client.ts。
//
// 这个仓库没有前端测试运行器（package.json 里没有 test 脚本，也没装 vitest），
// 而这个模块在**每一个请求**的路径上 —— 0.4.111 用它替掉了 axios，那次替换要
// 逐字复刻四件调用方依赖的事（小写 headers、err.response、blob 错误正文、
// 超时消息里的 timeout）。任何一件悄悄改掉，都是散在各个视图里的分支静默失灵。
//
// 跑法：node web/frontend/test/client.check.mjs
// tests/test_api_client_contract.py 会在有 node 的机器上自动跑它。
import http from 'node:http'
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { transformSync } from 'esbuild'

// 现编译 TS —— 测的必须是**源码**，不是某个可能过期的构建产物。
// 用 esbuild 的 Node API 而不是 spawn npx：Windows 上 spawnSync 一个 .cmd 会
// EINVAL，而 client.ts 自己不 import 任何东西，一次 transform 就够，不必打包。
const here = path.dirname(fileURLToPath(import.meta.url))
const srcPath = path.join(here, '..', 'src', 'api', 'client.ts')
const js = transformSync(readFileSync(srcPath, 'utf8'), {
  loader: 'ts',
  format: 'esm',
}).code
const out = path.join(mkdtempSync(path.join(tmpdir(), 'ocibot-client-')), 'client.mjs')
writeFileSync(out, js)


// 浏览器全局的最小替身。客户端只碰这几样。
let redirected = ''
globalThis.localStorage = { removeItem() {}, getItem: () => null, setItem() {} }
globalThis.location = {
  protocol: 'http:', host: '127.0.0.1', pathname: '/instances', search: '',
  set href(v) { redirected = v }, get href() { return redirected },
}

const seen = []
const server = http.createServer((req, res) => {
  const chunks = []
  req.on('data', (c) => chunks.push(c))
  req.on('end', () => {
    const body = Buffer.concat(chunks)
    seen.push({ url: req.url, method: req.method, headers: req.headers, body })
    const p = req.url.split('?')[0]
    if (p === '/api/echo') {
      res.writeHead(200, { 'content-type': 'application/json', 'x-ocibot-reread': '1',
                           'X-Ocibot-Reread-Reason': 'boom' })
      res.end(JSON.stringify({ ok: true, url: req.url }))
    } else if (p === '/api/blob') {
      res.writeHead(200, { 'content-type': 'application/zip' })
      res.end(Buffer.from('PK\x03\x04zipzip'))
    } else if (p === '/api/blob-fail') {
      res.writeHead(500, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ detail: '导出失败了' }))
    } else if (p === '/api/boom') {
      res.writeHead(422, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ detail: [{ msg: '字段 a 不合法' }, { msg: '字段 b 不合法' }] }))
    } else if (p === '/api/401') {
      res.writeHead(401, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ detail: 'not authenticated' }))
    } else if (p === '/api/slow') {
      setTimeout(() => { res.writeHead(200); res.end('{}') }, 3000)
    } else if (p === '/api/nocontent') {
      res.writeHead(204); res.end()
    } else { res.writeHead(404); res.end('{}') }
  })
})

const port = await new Promise((r) => server.listen(0, '127.0.0.1', function () { r(this.address().port) }))
const origin = `http://127.0.0.1:${port}`
const realFetch = globalThis.fetch
globalThis.fetch = (u, o) => realFetch(origin + u, o)

const { default: api } = await import(pathToFileURL(out).href)

let pass = 0, fail = 0
function check(name, cond, extra = '') {
  if (cond) { pass++; console.log(`  ok   ${name}`) }
  else { fail++; console.log(`  FAIL ${name} ${extra}`) }
}

// 1. 查询参数：false 和 0 必须进串，undefined/null 不进。
{
  const r = await api.get('/echo', { params: { a: 1, force: false, zero: 0, no: undefined, nil: null } })
  const u = r.data.url
  check('params 保留 false/0，丢掉 undefined/null',
    u.includes('a=1') && u.includes('force=false') && u.includes('zero=0')
    && !u.includes('no=') && !u.includes('nil='), u)
}
// 2. headers 是小写普通对象，可下标取。
{
  const r = await api.get('/echo')
  check('headers 小写普通对象', r.headers['x-ocibot-reread'] === '1'
    && r.headers['x-ocibot-reread-reason'] === 'boom')
}
// 3. JSON 请求体带 content-type。
{
  seen.length = 0
  await api.post('/echo', { hello: '世界' })
  const s = seen.at(-1)
  check('JSON body + content-type',
    s.headers['content-type'] === 'application/json'
    && JSON.parse(s.body.toString()).hello === '世界')
}
// 4. FormData **不能**手写 content-type（要浏览器加 boundary）。
{
  seen.length = 0
  const fd = new FormData()
  fd.append('f', new Blob([Buffer.from('x')]), 'a.txt')
  await api.post('/echo', fd)
  const ct = seen.at(-1).headers['content-type'] || ''
  check('FormData 带 boundary', ct.startsWith('multipart/form-data') && ct.includes('boundary='), ct)
}
// 5. blob 响应。
{
  const r = await api.post('/blob', {}, { responseType: 'blob' })
  check('responseType blob -> Blob', r.data instanceof Blob && (await r.data.text()).includes('zip'))
}
// 6. blob 请求失败时，错误正文也要是 Blob（BackupView 靠 instanceof Blob 分支）。
{
  try {
    await api.post('/blob-fail', {}, { responseType: 'blob' })
    check('blob 失败抛错', false)
  } catch (e) {
    check('blob 失败时 e.response.data 是 Blob', e.response?.data instanceof Blob)
    const j = JSON.parse(await e.response.data.text())
    check('blob 错误正文可解析', j.detail === '导出失败了')
  }
}
// 7. 422 的 detail 数组要拼成一句话。
{
  try { await api.post('/boom', {}); check('422 抛错', false) }
  catch (e) {
    check('detail 数组拼成消息', e.message === '字段 a 不合法; 字段 b 不合法', e.message)
    check('错误带 status', e.response?.status === 422)
  }
}
// 8. 401 清 localStorage 并跳转，带 redirect。
{
  redirected = ''
  try { await api.get('/401') } catch {}
  check('401 跳登录并带 redirect',
    redirected.startsWith('/login?redirect=') && decodeURIComponent(redirected).includes('/instances'), redirected)
}
// 9. /auth/me 的 401 **不**跳转（交给路由守卫）。
{
  redirected = ''
  try { await api.get('/auth/me') } catch {}
  check('/auth/me 的 401 不跳转', redirected === '', redirected)
}
// 10. 超时消息里必须有 timeout（LaunchView 靠它分支）。
{
  const t0 = Date.now()
  try { await api.get('/slow', { timeout: 300 }); check('超时抛错', false) }
  catch (e) {
    check('超时消息含 timeout', /timeout/i.test(e.message), e.message)
    check('超时确实提前返回', Date.now() - t0 < 1500)
  }
}
// 11. 204 不因为空正文炸掉。
{
  const r = await api.delete('/nocontent')
  check('204 不抛', r.status === 204 && r.data === null)
}

server.close()
console.log(`\n${pass} 通过 / ${fail} 失败`)
process.exit(fail ? 1 : 0)
