<template>
  <div class="stack">
    <div class="page-head">
      <div>
        <h2>实例详情</h2>
        <p class="muted" style="margin: 0.2rem 0 0; word-break: break-all">{{ title }}</p>
      </div>
      <div class="page-tools">
        <button @click="refreshAll" :disabled="loading">刷新</button>
        <router-link to="/"><button type="button">返回列表</button></router-link>
      </div>
    </div>

    <div v-if="error" class="error-box">{{ error }}</div>
    <div v-if="msg" class="success-box">{{ msg }}</div>

    <div class="card stack" v-if="instance">
      <div class="grid-2">
        <div>
          <div class="muted" style="font-size: 12px">名称 / 状态</div>
          <div style="font-weight: 700">
            {{ instance.display_name }}
            <span class="badge" :class="stateClass(instance.lifecycle_state)">{{
              instance.lifecycle_state
            }}</span>
          </div>
        </div>
        <div>
          <div class="muted" style="font-size: 12px">Shape / 网络（单击 IP 可复制）</div>
          <div>
            {{ instance.shape }}
            <span v-if="instance.free_tier_tag" class="badge">{{ instance.free_tier_tag }}</span>
          </div>
          <div class="ip-lines">
            <div>
              公网
              <span
                class="copyable"
                :class="{ empty: !instance.public_ip }"
                title="单击复制"
                role="button"
                tabindex="0"
                @click="copyIp(instance.public_ip, $event)"
                @keydown.enter.prevent="copyIp(instance.public_ip)"
              >{{ instance.public_ip || '—' }}</span>
            </div>
            <div>
              私网
              <span
                class="copyable"
                :class="{ empty: !instance.private_ip }"
                title="单击复制"
                role="button"
                tabindex="0"
                @click="copyIp(instance.private_ip, $event)"
                @keydown.enter.prevent="copyIp(instance.private_ip)"
              >{{ instance.private_ip || '—' }}</span>
            </div>
            <div v-if="instance.ipv6_addresses?.length">
              IPv6
              <span
                v-for="ip6 in instance.ipv6_addresses"
                :key="ip6"
                class="copyable ipv6-chip"
                title="单击复制"
                role="button"
                tabindex="0"
                @click="copyIp(ip6, $event)"
                @keydown.enter.prevent="copyIp(ip6)"
              >{{ ip6 }}</span>
            </div>
            <div v-else class="muted" style="font-size: 12px">IPv6 —</div>
          </div>
        </div>
      </div>
      <div class="actions-row">
        <button :disabled="acting" @click="power('START')">开机</button>
        <button :disabled="acting" @click="power('SOFTSTOP')">关机</button>
        <button :disabled="acting" @click="power('SOFTRESET')">重启</button>
        <button :disabled="acting" @click="doRename">重命名</button>
        <button :disabled="acting" @click="doReplaceIp">换公网IP</button>
        <button :disabled="acting" @click="doIpv6">分配 IPv6</button>
        <button class="danger" :disabled="acting" @click="doTerminate">终止</button>
      </div>
    </div>

    <!-- Tabs: horizontal scroll on narrow screens -->
    <div class="tab-row" role="tablist">
      <button
        v-for="t in tabs"
        :key="t.id"
        type="button"
        role="tab"
        :class="{ primary: tab === t.id }"
        @click="tab = t.id"
      >
        {{ t.label }}
      </button>
    </div>

    <!-- Metrics -->
    <div v-if="tab === 'metrics'" class="card stack">
      <div class="row" style="justify-content: space-between">
        <div>
          <h3 style="margin: 0">监控</h3>
          <p class="muted" style="margin: 0.2rem 0 0; font-size: 12px">
            鼠标移到曲线上可查看该时刻数值。网络为实时速率（B/s），需实例安装并启用计算监控代理。
          </p>
        </div>
        <div class="row">
          <select v-model.number="metricHours" style="width: auto" @change="loadMetrics">
            <option :value="1">1 小时</option>
            <option :value="3">3 小时</option>
            <option :value="6">6 小时</option>
            <option :value="12">12 小时</option>
            <option :value="24">24 小时</option>
          </select>
          <button @click="loadMetrics" :disabled="loadingMetrics">刷新</button>
        </div>
      </div>
      <p class="muted" style="margin: 0; font-size: 12px">{{ metricsMsg }}</p>
      <div class="metrics-grid">
        <div
          v-for="key in metricKeys"
          :key="key"
          class="metric-card"
          @mousemove="onMetricHover($event, key)"
          @mouseleave="clearMetricHover(key)"
          @touchstart.passive="onMetricHover($event, key)"
          @touchmove.passive="onMetricHover($event, key)"
          @touchend="clearMetricHover(key)"
        >
          <div class="metric-head">
            <span class="muted" style="font-size: 12px">{{ metricLabel(key) }}</span>
            <strong class="metric-live">{{ hoverText(key) }}</strong>
          </div>
          <div class="spark-wrap" :ref="(el) => setSparkEl(key, el)">
            <svg :viewBox="`0 0 ${svgW} ${svgH}`" class="spark" preserveAspectRatio="none">
              <defs>
                <linearGradient :id="`g-${key}`" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" :stop-color="metricColor(key)" stop-opacity="0.35" />
                  <stop offset="100%" :stop-color="metricColor(key)" stop-opacity="0.02" />
                </linearGradient>
              </defs>
              <polygon
                v-if="(metricsSeries[key] || []).length > 1"
                :fill="`url(#g-${key})`"
                :points="sparkArea(metricsSeries[key] || [])"
              />
              <polyline
                fill="none"
                :stroke="metricColor(key)"
                stroke-width="2.2"
                stroke-linecap="round"
                stroke-linejoin="round"
                :points="sparkPoints(metricsSeries[key] || [])"
              />
              <circle
                v-if="metricHover[key]"
                :cx="metricHover[key]!.x"
                :cy="metricHover[key]!.y"
                r="4"
                :fill="metricColor(key)"
                stroke="#fff"
                stroke-width="1.5"
              />
              <line
                v-if="metricHover[key]"
                :x1="metricHover[key]!.x"
                :x2="metricHover[key]!.x"
                y1="4"
                :y2="svgH - 4"
                :stroke="metricColor(key)"
                stroke-opacity="0.35"
                stroke-dasharray="3 3"
              />
            </svg>
            <div
              v-if="metricHover[key]"
              class="metric-tip"
              :style="{ left: metricHover[key]!.tipLeft }"
            >
              <div class="tip-val">{{ metricHover[key]!.valueText }}</div>
              <div class="tip-time">{{ metricHover[key]!.timeText }}</div>
            </div>
          </div>
          <div class="muted metric-foot">
            {{ (metricsSeries[key] || []).length }} 个采样 · 最新
            {{ lastValue(metricsSeries[key] || [], key) }}
          </div>
        </div>
      </div>
    </div>

    <!-- Console -->
    <div v-if="tab === 'console'" class="card stack">
      <h3 style="margin: 0">串口 / VNC 控制台</h3>
      <p class="muted" style="margin: 0; font-size: 13px">
        创建控制台连接需要一条 SSH 公钥。创建后用返回的 SSH 命令连接串口或 VNC。
        串口/VNC 控制台 ≠ 系统 SSH；浏览器内终端请用「WebSSH」页签。
      </p>
      <div class="field">
        <label>SSH 公钥</label>
        <div class="row" style="margin-bottom: 0.4rem">
          <button type="button" @click="pickConsoleKey">选择 .pub…</button>
        </div>
        <textarea v-model="consoleKey" rows="3" spellcheck="false"></textarea>
      </div>
      <div class="row">
        <button class="primary" :disabled="consoleBusy" @click="createConsole">
          {{ consoleBusy ? '创建中…' : '创建控制台连接' }}
        </button>
        <button :disabled="consoleBusy" @click="loadConsole">刷新列表</button>
      </div>
      <div v-for="c in consoleList" :key="c.id" class="card" style="padding: 0.75rem">
        <div class="row" style="justify-content: space-between">
          <span class="badge">{{ c.lifecycle_state || '—' }}</span>
          <button class="danger" @click="deleteConsole(c.id)">删除</button>
        </div>
        <div class="field" style="margin-top: 0.5rem">
          <label>Serial SSH</label>
          <textarea readonly rows="2" :value="c.serial"></textarea>
          <button style="margin-top: 0.25rem" @click="copy(c.serial)">复制</button>
        </div>
        <div class="field">
          <label>VNC SSH</label>
          <textarea readonly rows="2" :value="c.vnc"></textarea>
          <button style="margin-top: 0.25rem" @click="copy(c.vnc)">复制</button>
        </div>
      </div>
    </div>

    <!-- Firewall -->
    <div v-if="tab === 'firewall'" class="card stack">
      <div class="row" style="justify-content: space-between">
        <h3 style="margin: 0">防火墙 (NSG)</h3>
        <div class="row">
          <button @click="loadFirewall">刷新</button>
          <button class="danger" :disabled="fwBusy" @click="openAllFirewall">一键全开放</button>
        </div>
      </div>
      <p class="muted" style="margin: 0; font-size: 12px">{{ fwMsg }}</p>
      <div v-for="g in fwGroups" :key="g.id" class="card" style="padding: 0.75rem">
        <div style="font-weight: 600">
          {{ g.display_name }}
          <span v-if="g.is_managed" class="badge">managed</span>
        </div>
        <div class="muted" style="font-size: 11px; word-break: break-all">{{ g.id }}</div>
        <div class="table-wrap" style="margin-top: 0.5rem">
          <table>
            <thead>
              <tr>
                <th>方向</th>
                <th>协议</th>
                <th>CIDR</th>
                <th>端口</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in g.rules || []" :key="r.id">
                <td>{{ r.direction_label || r.direction }}</td>
                <td>{{ r.protocol_label || r.protocol }}</td>
                <td>{{ r.cidr }}</td>
                <td>{{ r.port }}</td>
                <td>
                  <button class="danger" @click="deleteRule(g.id, r.id)">删</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="grid-2" style="margin-top: 0.5rem">
          <div class="field">
            <label>方向</label>
            <select v-model="ruleForm.direction">
              <option value="INGRESS">入站</option>
              <option value="EGRESS">出站</option>
            </select>
          </div>
          <div class="field">
            <label>协议</label>
            <select v-model="ruleForm.protocol">
              <option value="all">全部</option>
              <option value="6">TCP</option>
              <option value="17">UDP</option>
              <option value="1">ICMP</option>
            </select>
          </div>
          <div class="field">
            <label>CIDR</label>
            <input v-model="ruleForm.cidr" />
          </div>
          <div class="field">
            <label>端口（TCP/UDP）</label>
            <input v-model.number="ruleForm.port_min" type="number" placeholder="例如 22" />
          </div>
        </div>
        <button class="primary" style="margin-top: 0.4rem" @click="addRule(g.id)">添加规则</button>
      </div>
    </div>

    <!-- Reserved IP -->
    <div v-if="tab === 'network'" class="card stack">
      <div class="row" style="justify-content: space-between">
        <div>
          <h3 style="margin: 0">保留公网 IP</h3>
          <p class="muted" style="margin: 0.2rem 0 0; font-size: 12px">
            保留 IP 与实例解绑后不会丢失，可再次绑定；绑定时会释放实例当前的临时（EPHEMERAL）公网 IP。
          </p>
        </div>
        <div class="row">
          <button :disabled="ripBusy" @click="loadReservedIps">刷新</button>
          <button class="primary" :disabled="ripBusy" @click="createReservedIp">新建保留 IP</button>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>IP 地址</th>
              <th>名称</th>
              <th>状态</th>
              <th>绑定</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="reservedIps.length === 0">
              <td colspan="5" class="muted">该区域暂无保留 IP。「新建保留 IP」后即可绑定到实例。</td>
            </tr>
            <tr v-for="ip in reservedIps" :key="ip.id">
              <td
                class="copyable"
                title="单击复制"
                role="button"
                tabindex="0"
                @click="copy(ip.ip_address)"
                @keydown.enter.prevent="copy(ip.ip_address)"
              >{{ ip.ip_address }}</td>
              <td>{{ ip.display_name || '—' }}</td>
              <td><span class="badge">{{ ip.lifecycle_state }}</span></td>
              <td>
                <span class="badge" :class="ip.assigned ? 'running' : ''">
                  {{ ip.assigned ? '已绑定' : '未绑定' }}
                </span>
              </td>
              <td>
                <div class="row">
                  <button v-if="!ip.assigned" :disabled="ripBusy" @click="attachReservedIp(ip)">
                    绑定到本实例
                  </button>
                  <button v-if="ip.assigned" :disabled="ripBusy" @click="detachReservedIp(ip)">解绑</button>
                  <button v-if="!ip.assigned" class="danger" :disabled="ripBusy" @click="deleteReservedIp(ip)">
                    删除
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Boot / Shape -->
    <div v-if="tab === 'volume'" class="card stack">
      <div class="row" style="justify-content: space-between">
        <h3 style="margin: 0">引导卷管理</h3>
        <button @click="loadBoot" :disabled="bootBusy">{{ bootBusy ? '读取中…' : '刷新状态' }}</button>
      </div>

      <div v-if="bootInfo" class="boot-status card" style="padding: 0.75rem">
        <div class="grid-2">
          <div>
            <div class="muted" style="font-size: 12px">配置容量</div>
            <div style="font-size: 1.4rem; font-weight: 700">{{ bootInfo.size_in_gbs }} GB</div>
            <div class="muted" style="font-size: 12px">OCI 控制面配置大小（非系统内已用空间）</div>
          </div>
          <div>
            <div class="muted" style="font-size: 12px">性能</div>
            <div style="font-size: 1.1rem; font-weight: 600">
              {{ bootInfo.vpus_per_gb }} VPUs/GB
              <span class="badge">{{ bootInfo.performance_label || perfLabel(bootInfo.vpus_per_gb) }}</span>
            </div>
          </div>
          <div>
            <div class="muted" style="font-size: 12px">生命周期</div>
            <div>
              <span class="badge" :class="bootStateClass(bootInfo.lifecycle_state)">{{
                bootInfo.lifecycle_state || '—'
              }}</span>
            </div>
          </div>
          <div>
            <div class="muted" style="font-size: 12px">Hydration（镜像数据同步）</div>
            <div>
              <span
                class="badge"
                :class="
                  bootInfo.is_hydrated === true
                    ? 'running'
                    : bootInfo.is_hydrated === false
                      ? 'warn'
                      : ''
                "
              >
                {{
                  bootInfo.is_hydrated === true
                    ? '已完成'
                    : bootInfo.is_hydrated === false
                      ? '同步中'
                      : '未知'
                }}
              </span>
            </div>
          </div>
          <div>
            <div class="muted" style="font-size: 12px">名称</div>
            <div>{{ bootInfo.display_name || '—' }}</div>
          </div>
          <div>
            <div class="muted" style="font-size: 12px">可用域</div>
            <div>{{ bootInfo.availability_domain || '—' }}</div>
          </div>
          <div style="grid-column: 1 / -1">
            <div class="muted" style="font-size: 12px">引导卷 OCID（单击复制）</div>
            <div
              class="copyable"
              style="font-size: 12px; word-break: break-all"
              title="单击复制"
              role="button"
              tabindex="0"
              @click="copy(bootInfo.boot_volume_id)"
              @keydown.enter.prevent="copy(bootInfo.boot_volume_id)"
            >
              {{ bootInfo.boot_volume_id }}
            </div>
          </div>
          <div v-if="bootInfo.time_created" style="grid-column: 1 / -1">
            <div class="muted" style="font-size: 12px">创建时间</div>
            <div style="font-size: 12px">{{ formatTime(bootInfo.time_created) }}</div>
          </div>
        </div>
        <p class="muted" style="margin: 0.6rem 0 0; font-size: 12px">
          {{ bootInfo.usage_note || '无法通过 API 读取系统内磁盘已用/剩余空间；请登录系统用 df 查看。' }}
        </p>
      </div>
      <div v-else class="muted" style="font-size: 13px">
        {{ bootBusy ? '正在读取引导卷…' : '尚未加载引导卷信息，点「刷新状态」。' }}
      </div>

      <h4 style="margin: 0.25rem 0 0">调整引导卷</h4>
      <div class="grid-2">
        <div class="field">
          <label>新大小 GB（≥ 当前且 ≥50，只能扩大）</label>
          <input v-model.number="bootForm.size_in_gbs" type="number" min="50" />
        </div>
        <div class="field">
          <label>性能 VPUs/GB</label>
          <select v-model.number="bootForm.vpus_per_gb">
            <option :value="10">10 平衡</option>
            <option :value="20">20 较高</option>
            <option :value="30">30 超高</option>
            <option :value="60">60</option>
            <option :value="90">90</option>
            <option :value="120">120</option>
          </select>
        </div>
      </div>
      <div class="field" style="margin-top: 0.5rem">
        <label class="choice">
          <input v-model="autoGrowFs" type="checkbox" />
          <span>扩容后自动扩展文件系统（SSH）</span>
        </label>
        <p class="muted" style="margin: 0.25rem 0 0; font-size: 12px">
          仅在扩大容量时生效。OCI 控制面扩容后，通过 SSH 执行 growpart / resize2fs（或 xfs_growfs）。
          凭证仅用于本次请求，不会保存。
        </p>
      </div>
      <SshCredentialFields v-if="autoGrowFs" v-model="growCreds" />
      <div v-if="fsGrowResult" class="card" style="padding: 0.75rem">
        <div class="muted" style="font-size: 12px">最近一次扩容结果</div>
        <div>
          OCI：
          <span class="badge" :class="fsGrowResult.oci_ok ? 'running' : 'err'">
            {{ fsGrowResult.oci_ok ? '成功' : '失败' }}
          </span>
          · 文件系统：
          <span
            class="badge"
            :class="
              fsGrowResult.fs_ok === true
                ? 'running'
                : fsGrowResult.fs_ok === false
                  ? 'err'
                  : ''
            "
          >
            {{
              fsGrowResult.fs_ok === true
                ? '已扩展'
                : fsGrowResult.fs_ok === false
                  ? '失败'
                  : '未执行'
            }}
          </span>
        </div>
        <pre
          v-if="fsGrowResult.stdout || fsGrowResult.stderr"
          class="muted"
          style="font-size: 11px; white-space: pre-wrap; max-height: 160px; overflow: auto"
        >{{ fsGrowResult.stdout || '' }}{{ fsGrowResult.stderr ? '\n' + fsGrowResult.stderr : '' }}</pre>
        <ul v-if="(fsGrowResult.hints || []).length" style="margin: 0.3rem 0 0; padding-left: 1.2rem; font-size: 12px">
          <li v-for="(h, i) in fsGrowResult.hints" :key="i">{{ h }}</li>
        </ul>
      </div>
      <button class="primary" :disabled="bootBusy" @click="updateBoot">应用引导卷调整</button>

      <div style="border-top: 1px solid var(--border); padding-top: 0.75rem">
        <div class="row" style="justify-content: space-between">
          <h4 style="margin: 0">引导卷备份</h4>
          <div class="row">
            <button :disabled="backupBusy" @click="loadBackups">刷新</button>
            <button
              class="primary"
              :disabled="backupBusy || !bootInfo?.boot_volume_id"
              @click="createBackup"
            >
              创建备份
            </button>
          </div>
        </div>
        <p class="muted" style="margin: 0.3rem 0 0.5rem; font-size: 12px">
          Always Free 含 5 个卷备份名额。实例「制作镜像」已关闭；可用引导卷备份做数据保护。
        </p>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>名称</th>
                <th>类型</th>
                <th>状态</th>
                <th>大小</th>
                <th>创建时间</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="backups.length === 0">
                <td colspan="6" class="muted">暂无备份</td>
              </tr>
              <tr v-for="b in backups" :key="b.id">
                <td>{{ b.display_name || b.id.slice(-12) }}</td>
                <td>{{ b.type }}</td>
                <td><span class="badge">{{ b.lifecycle_state }}</span></td>
                <td>{{ b.unique_size_in_gbs ?? b.size_in_gbs ?? '—' }} GB</td>
                <td class="muted" style="font-size: 12px">{{ formatTime(b.time_created) }}</td>
                <td><button class="danger" :disabled="backupBusy" @click="deleteBackup(b)">删除</button></td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div style="border-top: 1px solid var(--border); padding-top: 0.75rem">
        <h4 style="margin: 0 0 0.5rem">修改 OCPU / 内存</h4>
        <template v-if="isFlexShape">
          <p class="muted" style="margin: 0 0 0.5rem; font-size: 12px">
            调整 A1.Flex 时，服务端会按 Always Free 剩余额度校验（免费/未知账号硬拦 4 OCPU / 24 GB 合计上限）。
          </p>
          <div class="grid-2">
            <div class="field">
              <label>OCPU</label>
              <input v-model.number="shapeForm.ocpus" type="number" min="1" step="1" />
            </div>
            <div class="field">
              <label>内存 GB</label>
              <input v-model.number="shapeForm.memory_in_gbs" type="number" min="1" step="1" />
            </div>
          </div>
          <button class="primary" :disabled="acting" @click="updateShape">应用规格</button>
        </template>
        <p v-else class="muted" style="margin: 0; font-size: 13px">
          当前 Shape <code>{{ instance?.shape || '—' }}</code> 为固定规格，
          <strong>不允许修改 OCPU / 内存</strong>。
          <template v-if="isE2Micro">
            （VM.Standard.E2.1.Micro 固定 1 OCPU / 1 GB）
          </template>
          仅 <code>*.Flex</code>（如 A1.Flex）支持调整。
        </p>
      </div>
    </div>

    <!-- WebSSH -->
    <div v-if="tab === 'webssh'" class="card stack">
      <h3 style="margin: 0">WebSSH 终端</h3>
      <p class="muted" style="margin: 0; font-size: 13px">
        通过浏览器 SSH 连接实例（默认 22 端口）。需要公网 IP 或宿主可路由的私网 IP；凭证不会保存。
      </p>
      <WebSshTerminal :tenant-id="tenantId" :instance-id="instanceId" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api, { type Instance } from '@/api/client'
import { pickAndReadTextFile } from '@/utils/file'
import { copyText } from '@/utils/toast'
import WebSshTerminal from '@/components/WebSshTerminal.vue'
import SshCredentialFields, { type SshCredModel } from '@/components/SshCredentialFields.vue'

const route = useRoute()
const router = useRouter()
const tenantId = computed(() => String(route.params.tenantId || ''))
const instanceId = computed(() => String(route.params.instanceId || ''))

const instance = ref<Instance | null>(null)
const loading = ref(false)
const acting = ref(false)
const error = ref('')
const msg = ref('')
const tab = ref<'metrics' | 'console' | 'webssh' | 'firewall' | 'network' | 'volume'>('metrics')
const tabs = [
  { id: 'metrics' as const, label: '监控' },
  { id: 'console' as const, label: '控制台' },
  { id: 'webssh' as const, label: 'WebSSH' },
  { id: 'firewall' as const, label: '防火墙' },
  { id: 'network' as const, label: '保留 IP' },
  { id: 'volume' as const, label: '引导卷/规格' },
]

// ---- reserved public IPs ----
type ReservedIp = {
  id: string
  ip_address: string
  display_name: string
  lifecycle_state: string
  assigned: boolean
}
const reservedIps = ref<ReservedIp[]>([])
const ripBusy = ref(false)

async function loadReservedIps() {
  ripBusy.value = true
  try {
    const { data } = await api.get(`/tenants/${tenantId.value}/reserved-ips`)
    reservedIps.value = data.items || []
  } catch (e: any) {
    error.value = e?.message || '加载保留 IP 失败'
  } finally {
    ripBusy.value = false
  }
}

async function createReservedIp() {
  // `?? ''` mapped Cancel (null) to an empty name and provisioned the IP anyway.
  const name = prompt('保留 IP 名称（可选）', '')
  if (name == null) return
  ripBusy.value = true
  error.value = ''
  try {
    const { data } = await api.post(`/tenants/${tenantId.value}/reserved-ips`, {
      display_name: name.trim(),
    })
    if (data.ok) msg.value = data.message
    else error.value = data.message
    await loadReservedIps()
  } catch (e: any) {
    error.value = e?.message || '创建失败'
  } finally {
    ripBusy.value = false
  }
}

async function attachReservedIp(ip: ReservedIp) {
  if (
    !confirm(
      `将保留 IP ${ip.ip_address} 绑定到本实例？实例当前的临时公网 IP 会被释放（地址不可找回）。`,
    )
  )
    return
  ripBusy.value = true
  error.value = ''
  try {
    const { data } = await api.post(
      `/tenants/${tenantId.value}/instances/${instanceId.value}/reserved-ip/attach`,
      { public_ip_id: ip.id },
    )
    if (data.ok) msg.value = data.message
    else error.value = data.message
    await Promise.all([loadReservedIps(), loadInstance()])
  } catch (e: any) {
    error.value = e?.message || '绑定失败'
  } finally {
    ripBusy.value = false
  }
}

async function detachReservedIp(ip: ReservedIp) {
  if (!confirm(`解绑保留 IP ${ip.ip_address}？地址会保留，可再次绑定。绑定它的实例会暂时没有公网 IPv4。`))
    return
  ripBusy.value = true
  error.value = ''
  try {
    const { data } = await api.post(`/tenants/${tenantId.value}/reserved-ips/${ip.id}/detach`)
    if (data.ok) msg.value = data.message
    else error.value = data.message
    await Promise.all([loadReservedIps(), loadInstance()])
  } catch (e: any) {
    error.value = e?.message || '解绑失败'
  } finally {
    ripBusy.value = false
  }
}

async function deleteReservedIp(ip: ReservedIp) {
  if (!confirm(`删除保留 IP ${ip.ip_address}？删除后该地址彻底释放。`)) return
  ripBusy.value = true
  error.value = ''
  try {
    const { data } = await api.delete(`/tenants/${tenantId.value}/reserved-ips/${ip.id}`)
    if (data.ok) msg.value = data.message
    else error.value = data.message
    await loadReservedIps()
  } catch (e: any) {
    error.value = e?.message || '删除失败'
  } finally {
    ripBusy.value = false
  }
}

// ---- boot volume backups ----
type BootBackup = {
  id: string
  display_name: string
  type: string
  lifecycle_state: string
  size_in_gbs: number | null
  unique_size_in_gbs: number | null
  time_created: string
}
const backups = ref<BootBackup[]>([])
const backupBusy = ref(false)

async function loadBackups() {
  if (!bootInfo.value?.boot_volume_id) return
  backupBusy.value = true
  try {
    const { data } = await api.get(`/tenants/${tenantId.value}/boot-volume-backups`, {
      params: { boot_volume_id: bootInfo.value.boot_volume_id },
    })
    backups.value = data.items || []
  } catch (e: any) {
    error.value = e?.message || '加载备份失败'
  } finally {
    backupBusy.value = false
  }
}

async function createBackup() {
  if (!bootInfo.value?.boot_volume_id) return
  const name = prompt('备份名称', `backup-${new Date().toISOString().slice(0, 10)}`)
  if (name == null) return
  backupBusy.value = true
  error.value = ''
  try {
    const { data } = await api.post(`/tenants/${tenantId.value}/boot-volume-backups`, {
      boot_volume_id: bootInfo.value.boot_volume_id,
      display_name: name,
      backup_type: 'INCREMENTAL',
    })
    if (data.ok) msg.value = data.message
    else error.value = data.message
    await loadBackups()
  } catch (e: any) {
    error.value = e?.message || '创建备份失败'
  } finally {
    backupBusy.value = false
  }
}

async function deleteBackup(b: BootBackup) {
  if (!confirm(`删除备份「${b.display_name || b.id.slice(-12)}」？不可恢复。`)) return
  backupBusy.value = true
  error.value = ''
  try {
    const { data } = await api.delete(`/tenants/${tenantId.value}/boot-volume-backups/${b.id}`)
    if (data.ok) msg.value = data.message
    else error.value = data.message
    await loadBackups()
  } catch (e: any) {
    error.value = e?.message || '删除失败'
  } finally {
    backupBusy.value = false
  }
}

// custom image feature disabled

const title = computed(
  () => instance.value?.display_name || instanceId.value.slice(0, 18) + '…',
)

const isFlexShape = computed(() => {
  const shape = String(instance.value?.shape || '').toLowerCase()
  if (!shape) return false
  if (shape.includes('e2.1.micro') || shape.endsWith('.micro')) return false
  return shape.endsWith('.flex') || shape.includes('.flex.')
})

const isE2Micro = computed(() => /e2\.1\.micro/i.test(String(instance.value?.shape || '')))

function stateClass(state: string) {
  const s = (state || '').toUpperCase()
  if (s === 'RUNNING') return 'running'
  if (s === 'STOPPED') return 'stopped'
  if (s.includes('STOP') || s.includes('START') || s === 'PROVISIONING') return 'warn'
  if (s.includes('TERMINAT')) return 'err'
  return ''
}

async function copy(text: string) {
  await copyText(text)
}

function copyIp(text?: string | null, ev?: Event) {
  if (ev) {
    ev.preventDefault()
    ev.stopPropagation()
  }
  if (!text) return
  void copyText(text, '已复制 IP')
}

function perfLabel(vpu: number) {
  const n = Number(vpu || 10)
  if (n <= 10) return '平衡'
  if (n <= 20) return '较高性能'
  return '超高性能'
}

function bootStateClass(state: string) {
  const s = (state || '').toUpperCase()
  if (s === 'AVAILABLE') return 'running'
  if (s === 'PROVISIONING' || s === 'RESTORING') return 'warn'
  if (s.includes('FAULT') || s.includes('TERMINAT')) return 'err'
  return ''
}

function formatTime(v: string) {
  if (!v) return '—'
  try {
    return new Date(v).toLocaleString()
  } catch {
    return v
  }
}

async function loadInstance() {
  const { data } = await api.get<Instance>(
    `/tenants/${tenantId.value}/instances/${instanceId.value}`,
  )
  instance.value = data
  if (data.ocpus != null) shapeForm.ocpus = data.ocpus
  if (data.memory_in_gbs != null) shapeForm.memory_in_gbs = data.memory_in_gbs
  // Do not prefetch boot volume here — volume tab loads on demand.
}

// ---- metrics ----
const metricHours = ref(3)
const loadingMetrics = ref(false)
const metricsMsg = ref('')
const metricsSeries = ref<Record<string, [string | null, number][]>>({})
const metricKeys = ['cpu', 'memory', 'net_in', 'net_out']
const svgW = 320
const svgH = 96

type MetricHover = {
  x: number
  y: number
  tipLeft: string
  valueText: string
  timeText: string
  index: number
}
const metricHover = ref<Record<string, MetricHover | null>>({})
const sparkEls: Record<string, HTMLElement | null> = {}

function setSparkEl(key: string, el: unknown) {
  sparkEls[key] = (el as HTMLElement) || null
}

function metricLabel(k: string) {
  return (
    {
      cpu: 'CPU 使用率',
      memory: '内存使用率',
      net_in: '网络入（速率）',
      net_out: '网络出（速率）',
    } as Record<string, string>
  )[k]
}
function metricColor(k: string) {
  return (
    {
      cpu: '#3370ff',
      memory: '#6b4eff',
      net_in: '#00b42a',
      net_out: '#ff7d00',
    } as Record<string, string>
  )[k]
}

function formatMetricValue(v: number, key: string) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  if (key.startsWith('net')) {
    // Backend returns bytes/sec (MQL .rate() on NetworksBytes*).
    const abs = Math.abs(n)
    if (abs >= 1e9) return (n / 1e9).toFixed(2) + ' GB/s'
    if (abs >= 1e6) return (n / 1e6).toFixed(2) + ' MB/s'
    if (abs >= 1e3) return (n / 1e3).toFixed(1) + ' KB/s'
    return n.toFixed(0) + ' B/s'
  }
  // CPU / memory utilization percent
  const pct = Math.max(0, Math.min(100, n))
  return pct.toFixed(1) + '%'
}

function formatMetricTime(ts: string | null | undefined) {
  if (!ts) return '—'
  try {
    const d = new Date(ts)
    if (Number.isNaN(d.getTime())) return String(ts).slice(0, 19)
    return d.toLocaleString()
  } catch {
    return String(ts).slice(0, 19)
  }
}

function sparkLayout(points: any[]) {
  const vals = points
    .map((p) => Number(p?.[1] ?? 0))
    .map((v) => (Number.isFinite(v) ? v : 0))
  if (!vals.length) {
    return { vals: [] as number[], max: 1, min: 0, span: 1 }
  }
  const max = Math.max(...vals, 1)
  const min = Math.min(...vals, 0)
  const span = Math.max(max - min, 1e-9)
  return { vals, max, min, span }
}

function sparkXY(points: any[], index: number) {
  const { vals, min, span } = sparkLayout(points)
  if (!vals.length) return { x: 0, y: svgH / 2 }
  const i = Math.max(0, Math.min(index, vals.length - 1))
  const x = (i / Math.max(vals.length - 1, 1)) * (svgW - 8) + 4
  const y = svgH - 8 - ((vals[i] - min) / span) * (svgH - 16)
  return { x, y }
}

function sparkPoints(points: any[]) {
  const { vals, min, span } = sparkLayout(points)
  if (!vals.length) return ''
  return vals
    .map((v, i) => {
      const x = (i / Math.max(vals.length - 1, 1)) * (svgW - 8) + 4
      const y = svgH - 8 - ((v - min) / span) * (svgH - 16)
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
}

function sparkArea(points: any[]) {
  const line = sparkPoints(points)
  if (!line) return ''
  // Close the area path against the bottom of the sparkline viewport.
  return `4,${(svgH - 4).toFixed(1)} ${line} ${(svgW - 4).toFixed(1)},${(svgH - 4).toFixed(1)}`
}

function lastValue(points: any[], key: string) {
  if (!points.length) return '—'
  const v = Number(points[points.length - 1]?.[1] ?? 0)
  return formatMetricValue(v, key)
}

function hoverText(key: string) {
  const h = metricHover.value[key]
  if (h) return h.valueText
  return lastValue(metricsSeries.value[key] || [], key)
}

function clearMetricHover(key: string) {
  metricHover.value = { ...metricHover.value, [key]: null }
}

function onMetricHover(ev: MouseEvent | TouchEvent, key: string) {
  const el = sparkEls[key]
  const points = metricsSeries.value[key] || []
  if (!el || points.length === 0) return
  const rect = el.getBoundingClientRect()
  let clientX = 0
  if ('touches' in ev && ev.touches.length) clientX = ev.touches[0].clientX
  else if ('clientX' in ev) clientX = (ev as MouseEvent).clientX
  const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / Math.max(rect.width, 1)))
  const index = Math.round(ratio * Math.max(points.length - 1, 0))
  const { x, y } = sparkXY(points, index)
  const raw = points[index] || []
  const value = Number(raw?.[1] ?? 0)
  const tipPct = Math.min(86, Math.max(8, (x / svgW) * 100))
  metricHover.value = {
    ...metricHover.value,
    [key]: {
      x,
      y,
      tipLeft: `${tipPct}%`,
      valueText: formatMetricValue(value, key),
      timeText: formatMetricTime(raw?.[0] ?? null),
      index,
    },
  }
}

async function loadMetrics() {
  loadingMetrics.value = true
  try {
    const { data } = await api.get(
      `/tenants/${tenantId.value}/instances/${instanceId.value}/metrics`,
      { params: { hours: metricHours.value } },
    )
    metricsMsg.value = data.message || ''
    const series = data.data?.series || {}
    // normalize to array of [ts, value]
    const out: Record<string, any[]> = {}
    for (const k of metricKeys) {
      out[k] = (series[k] || []).map((p: any) =>
        Array.isArray(p) ? p : [p?.timestamp ?? null, Number(p?.value ?? 0)],
      )
    }
    metricsSeries.value = out
    metricHover.value = {}
  } catch (e: any) {
    metricsMsg.value = e?.message || '加载监控失败'
  } finally {
    loadingMetrics.value = false
  }
}

// ---- console ----
const consoleKey = ref('')
const consoleList = ref<any[]>([])
const consoleBusy = ref(false)

async function loadConsole() {
  // Surface failures instead of leaving a stale list on screen with no hint that
  // the refresh failed (the sibling loaders already do this).
  try {
    const { data } = await api.get(
      `/tenants/${tenantId.value}/instances/${instanceId.value}/console`,
    )
    consoleList.value = data.connections || []
  } catch (e: any) {
    error.value = e?.message || '加载控制台连接失败'
  }
}
async function pickConsoleKey() {
  const text = await pickAndReadTextFile('.pub,.txt,text/plain')
  if (text == null) return
  if (/PRIVATE KEY/i.test(text)) {
    error.value = '请选择公钥，不要选私钥'
    return
  }
  consoleKey.value = text.trim().split(/\r?\n/).filter(Boolean)[0] || ''
}
async function createConsole() {
  error.value = ''
  msg.value = ''
  consoleBusy.value = true
  try {
    const { data } = await api.post(
      `/tenants/${tenantId.value}/instances/${instanceId.value}/console`,
      { ssh_public_key: consoleKey.value },
    )
    if (data.ok) {
      msg.value = data.message || '控制台已就绪'
      await loadConsole()
    } else error.value = data.message || '创建失败'
  } catch (e: any) {
    error.value = e?.message || '创建失败'
  } finally {
    consoleBusy.value = false
  }
}
async function deleteConsole(id: string) {
  if (!confirm('删除此控制台连接？')) return
  error.value = ''
  try {
    await api.delete(
      `/tenants/${tenantId.value}/instances/${instanceId.value}/console/${id}`,
    )
    msg.value = '已删除'
    await loadConsole()
  } catch (e: any) {
    error.value = e?.message || '删除失败'
  }
}

// ---- firewall ----
const fwGroups = ref<any[]>([])
const fwMsg = ref('')
const fwBusy = ref(false)
const ruleForm = reactive({
  direction: 'INGRESS',
  protocol: '6',
  cidr: '0.0.0.0/0',
  port_min: 22 as number | null,
})

async function loadFirewall() {
  try {
    const { data } = await api.get(
      `/tenants/${tenantId.value}/instances/${instanceId.value}/firewall`,
    )
    fwMsg.value = data.message || ''
    fwGroups.value = data.data?.groups || []
  } catch (e: any) {
    error.value = e?.message || '加载防火墙规则失败'
  }
}
async function openAllFirewall() {
  if (!confirm('将清空关联 NSG 规则并全开放，确认？')) return
  fwBusy.value = true
  error.value = ''
  try {
    const { data } = await api.post(
      `/tenants/${tenantId.value}/instances/${instanceId.value}/firewall/open-all`,
    )
    if (data.ok) msg.value = data.message
    else error.value = data.message
    await loadFirewall()
  } catch (e: any) {
    error.value = e?.message || '操作失败'
  } finally {
    fwBusy.value = false
  }
}
async function addRule(nsgId: string) {
  error.value = ''
  try {
    const { data } = await api.post(
      `/tenants/${tenantId.value}/instances/${instanceId.value}/firewall/rules`,
      {
        nsg_id: nsgId,
        direction: ruleForm.direction,
        protocol: ruleForm.protocol,
        cidr: ruleForm.cidr,
        // An empty input yields '' which fails int validation with a raw 422;
        // send null so the backend treats it as "all ports" as intended.
        port_min: portOrNull(),
        port_max: portOrNull(),
      },
    )
    if (data.ok) msg.value = data.message
    else error.value = data.message
    await loadFirewall()
  } catch (e: any) {
    error.value = e?.message || '添加失败'
  }
}
/** Port for the rule payload, or null when not applicable / left blank. */
function portOrNull(): number | null {
  if (ruleForm.protocol === 'all' || ruleForm.protocol === '1') return null
  const n = Number(ruleForm.port_min)
  return Number.isFinite(n) && n > 0 ? n : null
}

async function deleteRule(nsgId: string, ruleId: string) {
  if (!confirm('删除该规则？')) return
  try {
    const { data } = await api.post(
      `/tenants/${tenantId.value}/instances/${instanceId.value}/firewall/delete-rules`,
      { nsg_id: nsgId, rule_ids: [ruleId] },
    )
    if (data.ok) msg.value = data.message
    else error.value = data.message
    await loadFirewall()
  } catch (e: any) {
    error.value = e?.message || '删除失败'
  }
}

// ---- boot / shape ----
const bootInfo = ref<any>(null)
const bootBusy = ref(false)
const bootForm = reactive({ size_in_gbs: null as number | null, vpus_per_gb: 10 })
const shapeForm = reactive({ ocpus: 1, memory_in_gbs: 6 })
const autoGrowFs = ref(false)
const growCreds = reactive<SshCredModel>({
  username: 'ubuntu',
  port: 22,
  authMode: 'key',
  privateKeyPem: '',
  password: '',
})
const fsGrowResult = ref<any>(null)

async function loadBoot() {
  bootBusy.value = true
  try {
    const { data } = await api.get(
      `/tenants/${tenantId.value}/instances/${instanceId.value}/boot-volume`,
    )
    if (data.ok) {
      bootInfo.value = data.data
      bootForm.size_in_gbs = data.data.size_in_gbs
      bootForm.vpus_per_gb = data.data.vpus_per_gb
    } else error.value = data.message
  } catch (e: any) {
    error.value = e?.message || '读取失败'
  } finally {
    bootBusy.value = false
  }
}
async function updateBoot() {
  bootBusy.value = true
  error.value = ''
  fsGrowResult.value = null
  try {
    const payload: Record<string, any> = {
      size_in_gbs: bootForm.size_in_gbs,
      vpus_per_gb: bootForm.vpus_per_gb,
      auto_grow_fs: !!autoGrowFs.value,
    }
    if (autoGrowFs.value) {
      payload.ssh_username = growCreds.username || 'ubuntu'
      payload.ssh_port = growCreds.port || 22
      if (growCreds.authMode === 'key') {
        payload.ssh_private_key_pem = growCreds.privateKeyPem
        payload.ssh_password = null
      } else {
        payload.ssh_password = growCreds.password
        payload.ssh_private_key_pem = null
      }
    }
    const { data } = await api.post(
      `/tenants/${tenantId.value}/instances/${instanceId.value}/boot-volume`,
      payload,
    )
    if (data.data) fsGrowResult.value = data.data
    if (data.ok) {
      msg.value = data.message
      if (growCreds.authMode === 'password') growCreds.password = ''
      await loadBoot()
    } else error.value = data.message
  } catch (e: any) {
    error.value = e?.message || '调整失败'
  } finally {
    bootBusy.value = false
  }
}
async function updateShape() {
  if (!isFlexShape.value) {
    error.value = '当前 Shape 为固定规格，不允许修改 OCPU / 内存'
    return
  }
  acting.value = true
  error.value = ''
  try {
    const { data } = await api.post(
      `/tenants/${tenantId.value}/instances/${instanceId.value}/shape`,
      {
        ocpus: shapeForm.ocpus,
        memory_in_gbs: shapeForm.memory_in_gbs,
      },
    )
    if (data.ok) {
      msg.value = data.message
      await loadInstance()
    } else error.value = data.message
  } catch (e: any) {
    error.value = e?.message || '修改失败'
  } finally {
    acting.value = false
  }
}

// ---- power helpers ----
async function power(action: string) {
  acting.value = true
  error.value = ''
  try {
    const { data } = await api.post(
      `/tenants/${tenantId.value}/instances/${instanceId.value}/power`,
      { action },
    )
    // The API answers 200 with ok=false when OCI refuses the action (wrong
    // lifecycle state, etc.) — showing data.message as a success banner told the
    // user it worked. Branch on ok, like doReplaceIp already does.
    if (data.ok) msg.value = data.message
    else error.value = data.message || '操作失败'
    await loadInstance()
  } catch (e: any) {
    error.value = e?.message || '操作失败'
  } finally {
    acting.value = false
  }
}
async function doRename() {
  const name = prompt('新名称', instance.value?.display_name || '')
  if (!name?.trim()) return
  acting.value = true
  try {
    const { data } = await api.post(
      `/tenants/${tenantId.value}/instances/${instanceId.value}/rename`,
      { display_name: name.trim() },
    )
    if (data.ok) msg.value = data.message
    else error.value = data.message || '重命名失败'
    await loadInstance()
  } catch (e: any) {
    error.value = e?.message || '失败'
  } finally {
    acting.value = false
  }
}
async function doReplaceIp() {
  if (!confirm('更换临时公网 IPv4？')) return
  acting.value = true
  try {
    const { data } = await api.post(
      `/tenants/${tenantId.value}/instances/${instanceId.value}/public-ip/replace`,
    )
    if (data.ok) msg.value = data.message
    else error.value = data.message
    await loadInstance()
  } catch (e: any) {
    error.value = e?.message || '失败'
  } finally {
    acting.value = false
  }
}
async function doIpv6() {
  acting.value = true
  try {
    const { data } = await api.post(
      `/tenants/${tenantId.value}/instances/${instanceId.value}/ipv6`,
    )
    if (data.ok) msg.value = data.message
    else error.value = data.message
    await loadInstance()
  } catch (e: any) {
    error.value = e?.message || '失败'
  } finally {
    acting.value = false
  }
}
async function doTerminate() {
  if (!confirm('确认终止该实例？')) return
  acting.value = true
  try {
    const { data } = await api.post(
      `/tenants/${tenantId.value}/instances/${instanceId.value}/terminate`,
      { preserve_boot_volume: false },
    )
    if (!data.ok) {
      // Navigating away on a refused terminate left the user believing the
      // instance was gone.
      error.value = data.message || '终止失败'
      return
    }
    msg.value = data.message
    setTimeout(() => router.push('/'), 800)
  } catch (e: any) {
    error.value = e?.message || '失败'
  } finally {
    acting.value = false
  }
}

async function refreshAll() {
  loading.value = true
  error.value = ''
  try {
    await loadInstance()
    if (tab.value === 'metrics') await loadMetrics()
    if (tab.value === 'console') await loadConsole()
    if (tab.value === 'firewall') await loadFirewall()
    if (tab.value === 'network') await loadReservedIps()
    if (tab.value === 'volume') {
      await loadBoot()
      await loadBackups()
    }
  } catch (e: any) {
    error.value = e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

watch(tab, async (t) => {
  try {
    if (t === 'metrics') await loadMetrics()
    if (t === 'console') await loadConsole()
    if (t === 'firewall') await loadFirewall()
    if (t === 'network') await loadReservedIps()
    if (t === 'volume') {
      await loadBoot()
      await loadBackups()
    }
  } catch (e: any) {
    error.value = e?.message || '加载失败'
  }
})

onMounted(async () => {
  try {
    // Only the instance summary is required to open the page.
    // Metrics / console / firewall / volume load when the user opens that tab
    // (or clicks 刷新全部), to avoid background Oracle polling.
    await loadInstance()
    if (tab.value === 'metrics') await loadMetrics()
    else if (tab.value === 'console') await loadConsole()
    else if (tab.value === 'firewall') await loadFirewall()
    else if (tab.value === 'network') await loadReservedIps()
    else if (tab.value === 'volume') {
      await loadBoot()
      await loadBackups()
    }
  } catch (e: any) {
    error.value = e?.message || '加载失败'
  }
})

// If the router reuses this component for a different instance, reload its data.
watch([tenantId, instanceId], async () => {
  try {
    await loadInstance()
    await loadMetrics()
  } catch (e: any) {
    error.value = e?.message || '加载失败'
  }
})
</script>

<style scoped>
.tab-row {
  gap: 0.4rem;
}
.metrics-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.85rem;
}
@media (max-width: 700px) {
  .metrics-grid {
    grid-template-columns: 1fr;
  }
}
.metric-card {
  border: 1px solid var(--glass-border);
  border-radius: 14px;
  padding: 0.7rem 0.8rem 0.55rem;
  background: color-mix(in srgb, var(--panel) 70%, transparent);
  min-width: 0;
  box-shadow: var(--glass-highlight);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  position: relative;
  color: var(--text);
}
.metric-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 0.5rem;
  margin-bottom: 0.35rem;
}
.metric-live {
  font-size: 15px;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.02em;
}
.spark-wrap {
  position: relative;
  width: 100%;
  cursor: crosshair;
}
.spark {
  width: 100%;
  height: 96px;
  display: block;
}
.metric-tip {
  position: absolute;
  top: 0.25rem;
  transform: translateX(-50%);
  pointer-events: none;
  z-index: 2;
  padding: 0.3rem 0.5rem;
  border-radius: 10px;
  background: rgba(20, 22, 28, 0.92);
  color: #fff;
  border: 1px solid rgba(255, 255, 255, 0.1);
  box-shadow: 0 8px 20px rgba(0, 0, 0, 0.28);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  white-space: nowrap;
  text-align: center;
  min-width: 4.5rem;
}
.tip-val {
  font-size: 13px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}
.tip-time {
  font-size: 10px;
  opacity: 0.78;
  margin-top: 0.1rem;
}
.metric-foot {
  font-size: 11px;
  margin-top: 0.25rem;
}
.ip-lines {
  font-size: 13px;
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  margin-top: 0.25rem;
}
.ip-lines > div {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.35rem 0.5rem;
}
.ipv6-chip {
  margin-left: 0;
  margin-top: 0;
  font-size: 12px;
}
.boot-status {
  background: var(--input-bg);
}
@media (max-width: 800px) {
  .metrics-grid {
    grid-template-columns: 1fr;
  }
}
</style>
