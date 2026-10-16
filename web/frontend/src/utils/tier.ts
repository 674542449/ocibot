/**
 * 账号等级的简短标签（租户页「等级」列、账号用量页的租户下拉）。
 *
 * 升级状态来自 Oracle 的升级记录（OSP Gateway 订阅的 upgrade_state），在「等级查询」/
 * 「刷新用量」时缓存到租户行上。account_tier 是最终结论：已经是付费号就显示「升级」，
 * 不管缓存里的升级状态是什么 —— 那条可能是升级完成之前读到的旧值。
 */
export interface TierFields {
  account_tier?: string
  upgrade_state?: string
  upgrade_state_details?: string
}

const UPGRADE_ERROR_LABELS: Record<string, string> = {
  TAX_ERROR: '税务信息有误',
  UPGRADE_ERROR: '升级处理出错',
  P2C_ERROR: '付费转换出错',
}

export function tierLabel(t: TierFields): string {
  if (t.account_tier === 'paid' || t.upgrade_state === 'UPGRADED') return '升级'
  if (t.upgrade_state === 'SUBMITTED') return '升级中'
  if (t.upgrade_state === 'ERROR') return '升级失败'
  return t.account_tier === 'free' ? '免费' : '未知'
}

/** 悬停说明：升级中 / 升级失败时解释含义，其余为空。 */
export function tierHint(t: TierFields): string {
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
