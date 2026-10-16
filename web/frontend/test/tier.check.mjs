// 跑一遍 src/utils/tier.ts：租户页「等级」列的标签、徽标、注册天数那一行。
//
// 这几个函数决定一个账号在列表里显示成「准备中 / 升级中 / 升级失败 / 升级 / 免费」，
// 优先级写错了就是静默显示错 —— 没有前端测试运行器，所以照 client.check.mjs 的办法：
// 现编译源码，直接调用。
//
// 跑法：node web/frontend/test/tier.check.mjs
// tests/test_frontend_tier_labels.py 会在有 node 的机器上自动跑它。
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { transformSync } from 'esbuild'

const here = path.dirname(fileURLToPath(import.meta.url))
const srcPath = path.join(here, '..', 'src', 'utils', 'tier.ts')
const js = transformSync(readFileSync(srcPath, 'utf8'), { loader: 'ts', format: 'esm' }).code
const out = path.join(mkdtempSync(path.join(tmpdir(), 'ocibot-tier-')), 'tier.mjs')
writeFileSync(out, js)
const { tierLabel, tierBadgeClass, accountAge, upgradeStateLabel } = await import(pathToFileURL(out).href)

let pass = 0
let fail = 0
function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected)
  if (ok) pass++
  else fail++
  console.log(`${ok ? '  ok  ' : '  FAIL'} ${name}${ok ? '' : `\n         got      ${JSON.stringify(actual)}\n         expected ${JSON.stringify(expected)}`}`)
}

// ---- 等级标签的优先级
check('准备中排最前（连付费号也是）', tierLabel({ account_tier: 'paid', setup_state: 'PROVISIONING' }), '准备中')
check('付费号不受旧的升级中缓存影响', tierLabel({ account_tier: 'paid', upgrade_state: 'SUBMITTED' }), '升级')
check('升级中', tierLabel({ account_tier: 'free', upgrade_state: 'SUBMITTED' }), '升级中')
check('升级失败', tierLabel({ account_tier: 'free', upgrade_state: 'ERROR' }), '升级失败')
check('免费', tierLabel({ account_tier: 'free', upgrade_state: 'PROMO', setup_state: 'READY' }), '免费')
check('没查过是未知', tierLabel({ account_tier: '' }), '未知')
check('准备中灰色徽标', tierBadgeClass({ setup_state: 'PROVISIONING' }), 'badge stopped')
check('升级中黄色徽标', tierBadgeClass({ account_tier: 'free', upgrade_state: 'SUBMITTED' }), 'badge warn')
check('付费号不加徽标', tierBadgeClass({ account_tier: 'paid', upgrade_state: 'ERROR' }), '')
check('升级失败带原因', upgradeStateLabel('ERROR', 'TAX_ERROR'), '升级失败（税务信息有误）')

// ---- 注册天数 / 积分剩余
const DAY = 24 * 60 * 60 * 1000
const reg = Date.UTC(2026, 9, 1, 0, 0, 0)
const exp = new Date(reg + 30 * DAY).toISOString()
const at = (days) => reg + days * DAY
const t = { registered_at: new Date(reg).toISOString(), trial_expires_at: exp }
check('没记过注册时间不显示', accountAge({ registered_at: null, trial_expires_at: null }), null)
check('不满一天不说「注册 0 天」', accountAge(t, at(0.4))?.text, '注册不到 1 天 · 积分剩 30 天')
check('一天半', accountAge(t, at(1.5))?.text, '注册 1 天 · 积分剩 29 天')
check('最后几个小时还算 1 天', accountAge(t, at(29.9))?.text, '注册 29 天 · 积分剩 1 天')
check('到期后只显示注册天数', accountAge(t, at(40))?.text, '注册 40 天')
check('到期后悬停说明已到期', accountAge(t, at(40))?.hint.endsWith('已到期'), true)

console.log(`\n${pass} 通过 / ${fail} 失败`)
process.exit(fail ? 1 : 0)
