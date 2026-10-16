/**
 * 账号等级的简短标签（租户页「等级」列、账号用量页的租户下拉）。
 *
 * 「准备中」最优先：刚注册、Oracle 还在开通的账号（setup_state=PROVISIONING）什么都
 * 做不了，这比它是免费还是付费更要紧。
 *
 * 升级状态来自 Oracle 的升级记录（OSP Gateway 订阅的 upgrade_state），在「等级查询」/
 * 「刷新用量」时缓存到租户行上。account_tier 是最终结论：已经是付费号就显示「升级」，
 * 不管缓存里的升级状态是什么 —— 那条可能是升级完成之前读到的旧值。
 */
export interface TierFields {
  account_tier?: string
  upgrade_state?: string
  upgrade_state_details?: string
  setup_state?: string
}

const UPGRADE_ERROR_LABELS: Record<string, string> = {
  TAX_ERROR: '税务信息有误',
  UPGRADE_ERROR: '升级处理出错',
  P2C_ERROR: '付费转换出错',
}

export function tierLabel(t: TierFields): string {
  if (t.setup_state === 'PROVISIONING') return '准备中'
  if (t.account_tier === 'paid' || t.upgrade_state === 'UPGRADED') return '升级'
  if (t.upgrade_state === 'SUBMITTED') return '升级中'
  if (t.upgrade_state === 'ERROR') return '升级失败'
  return t.account_tier === 'free' ? '免费' : '未知'
}

/** 悬停说明：准备中 / 升级中 / 升级失败时解释含义，其余为空。 */
export function tierHint(t: TierFields): string {
  if (t.setup_state === 'PROVISIONING') {
    return '账号刚注册，Oracle 还在开通，创建实例等操作多半会失败。开通完成后点「等级查询」更新。'
  }
  if (t.account_tier === 'paid' || t.upgrade_state === 'UPGRADED') return ''
  if (t.upgrade_state === 'SUBMITTED') {
    return '已向 Oracle 提交升级申请，尚未完成；完成前仍按免费账号处理。完成后点「等级查询」更新。'
  }
  if (t.upgrade_state === 'ERROR') {
    const why = UPGRADE_ERROR_LABELS[t.upgrade_state_details || ''] || t.upgrade_state_details || ''
    return `Oracle 的升级记录显示升级出错${why ? `（${why}）` : ''}，账号仍是免费账号；请到 Oracle 控制台「升级和管理付款」页面处理。`
  }
  return ''
}

/** 等级标签的徽标样式：准备中灰、升级中黄、升级失败红，其余不加徽标。 */
export function tierBadgeClass(t: TierFields): string {
  if (t.setup_state === 'PROVISIONING') return 'badge stopped'
  if (t.account_tier === 'paid' || t.upgrade_state === 'UPGRADED') return ''
  if (t.upgrade_state === 'SUBMITTED') return 'badge warn'
  if (t.upgrade_state === 'ERROR') return 'badge err'
  return ''
}

/** 账号用量页「升级状态」一行的文字。 */
export function upgradeStateLabel(state: string, details = ''): string {
  switch (state) {
    case 'PROMO':
      return '未升级（试用 / 免费）'
    case 'SUBMITTED':
      return '升级中（已提交，等待 Oracle 完成）'
    case 'ERROR': {
      const why = UPGRADE_ERROR_LABELS[details] || details
      return why ? `升级失败（${why}）` : '升级失败'
    }
    case 'UPGRADED':
      return '已升级'
    default:
      return state || '—'
  }
}

const DAY_MS = 24 * 60 * 60 * 1000

function ymd(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/**
 * 租户列表里「注册 N 天 · 积分剩 M 天」那一行，及其悬停说明（具体日期）。
 * 没记过注册时间（还没做过等级查询）时返回 null，不显示。积分到期后只显示注册天数。
 */
export function accountAge(
  t: { registered_at?: string | null; trial_expires_at?: string | null },
  now: number = Date.now(),
): { text: string; hint: string } | null {
  if (!t.registered_at) return null
  const reg = new Date(t.registered_at)
  if (Number.isNaN(reg.getTime())) return null
  const days = Math.max(0, Math.floor((now - reg.getTime()) / DAY_MS))
  let text = `注册 ${days} 天`
  let hint = `注册于 ${ymd(reg)}`
  const exp = t.trial_expires_at ? new Date(t.trial_expires_at) : null
  if (exp && !Number.isNaN(exp.getTime())) {
    const left = Math.ceil((exp.getTime() - now) / DAY_MS)
    if (left > 0) text += ` · 积分剩 ${left} 天`
    hint += `，试用积分 ${ymd(exp)} 到期（注册日期 + 30 天）`
    if (left <= 0) hint += '，已到期'
  }
  return { text, hint }
}
