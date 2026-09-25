/**
 * IPv6 地址段（IPv6 CIDR）可选的前缀长度。
 *
 * 取值来自 OCI 文档「IPv6 Addresses → Assignment of IPv6 Addresses to a VNIC」：
 * 用 CreateIpv6 的 cidrPrefixLength 给 VNIC 分配一段连续地址，前缀必须在 80–128
 * 之间且能被 4 整除。服务端 app/oci_client.py::normalize_ipv6_prefix_length 用的是
 * 同一套规则 —— 两边要一起改。
 *
 * 128 排在最前：它就是普通的单个地址，也是原来唯一的行为。
 */
export const IPV6_PREFIX_OPTIONS: number[] = [128, 124, 120, 116, 112, 108, 104, 100, 96, 92, 88, 84, 80]

/** 下拉里显示的文字，例如 "/120 · 256 个地址"。 */
export function ipv6PrefixLabel(prefix: number): string {
  if (prefix >= 128) return '/128 · 单个地址'
  const bits = 128 - prefix
  // 2^24 以内写精确数字，再大就写成 2^n —— 一长串数字没人数得清位数。
  const count = bits <= 24 ? (2 ** bits).toLocaleString('en-US') : `2^${bits}`
  return `/${prefix} · ${count} 个地址`
}
