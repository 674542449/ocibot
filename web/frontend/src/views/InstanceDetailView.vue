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
        <!-- Only offered when there is something to remove: a button that always
             answers "该实例没有 IPv6" is noise on every instance that never had one. -->
        <button
          v-if="instance?.ipv6_addresses?.length"
          :disabled="acting"
          @click="doRemoveIpv6"
        >
          取消 IPv6
        </button>
        <!-- 以现有实例的配置预填创建表单。不产生任何新的 Oracle 调用 ——
             这些参数本页早就拿在手里了。抢 Always Free 的人重建很频繁。 -->
        <button :disabled="acting || !instance" @click="useAsTemplate">以此为模板创建</button>
        <button :disabled="acting" @click="toggleProtect">
          {{ instance?.protected ? '解除终止保护' : '开启终止保护' }}
        </button>
        <!-- 已保护时禁用终止按钮。服务端也会拒（409），这里只是别让人白点一下 ——
             真正的闸门在服务端，因为 UI 显示的对象和请求打的对象曾经可以不一致。 -->
        <button
          class="danger"
          :disabled="acting || !!instance?.protected"
          :title="instance?.protected ? '已开启终止保护，请先解除' : '终止实例'"
          @click="doTerminate"
        >
          终止
        </button>
      </div>
      <p v-if="instance?.protected" class="muted" style="margin: 0.4rem 0 0; font-size: 12px">
        🔒 已开启终止保护。标记存在 Oracle 的实例标签上（<code>ocibot_protected</code>），
        面板重装后依然有效，在 Oracle 控制台里也看得见。
      </p>
    </div>
    <!-- 换实例时 instance 会被先清空（见 resetInstanceState），上面的卡片
         整块消失。没有这句占位，页面看起来像是加载失败了。 -->
    <div v-else-if="loading" class="card muted" style="font-size: 13px">正在读取实例信息…</div>

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
      <!-- 监控插件被禁用时，下面的图表就是空的，而面板以前一个字都不解释 ——
           操作员只会当成「这功能坏了」。这个字段就在已经拿到的 GetInstance
           响应里（Instance.agent_config），不需要额外调用。
           monitoring_disabled === null 表示实例没返回 agent_config（老实例/
           老镜像），这时什么都不说，不做无根据的断言。 -->
      <div v-if="instance?.monitoring_disabled === true" class="card warn-box" style="margin: 0">
        <strong>⚠ 该实例的 Oracle Cloud Agent 监控插件已禁用</strong>
        <div class="muted" style="font-size: 12px; margin-top: 0.25rem">
          这就是下面没有数据的原因,不是面板故障。到 Oracle 控制台 → 实例 → Oracle Cloud Agent,
          启用「Compute Instance Monitoring」后,大约几分钟开始有数据。
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
    <div v-if="tab === 'bootlog'" class="card stack">
      <div class="row" style="justify-content: space-between; align-items: flex-start">
        <div>
          <h3 style="margin: 0">串口控制台输出（引导日志）</h3>
          <p class="muted" style="margin: 0.25rem 0 0; font-size: 12px">
            机器起不来、SSH 连不上时用这个。改坏 <code>/etc/fstab</code>、引导卷扩容失败，
            都会让系统停在 initramfs 提示符上 —— Oracle 那边仍然显示 RUNNING，
            只有串口输出能看到真正的原因。抓取需要十几秒。
          </p>
        </div>
        <button class="primary" :disabled="bootlogBusy" @click="loadBootLog">
          {{ bootlogBusy ? '抓取中…' : '抓取输出' }}
        </button>
      </div>
      <div v-if="bootlogMsg" class="muted" style="font-size: 12px">{{ bootlogMsg }}</div>
      <pre v-if="bootlog" class="bootlog">{{ bootlog }}</pre>
      <p v-else-if="!bootlogBusy" class="muted empty" style="margin: 0">
        点「抓取输出」拉取。和本页其他部分一样，进入页面不会自动请求 Oracle。
      </p>
    </div>

    <div v-if="tab === 'firewall'" class="card stack">
      <div class="row" style="justify-content: space-between">
        <h3 style="margin: 0">防火墙 (NSG)</h3>
        <div class="row">
          <button :disabled="fwLoading" @click="loadFirewall">刷新</button>
          <button class="danger" :disabled="fwBusy" @click="openAllFirewall">放行全部端口</button>
        </div>
      </div>
      <p class="muted" style="margin: 0; font-size: 12px">{{ fwMsg }}</p>

      <div v-if="fwLoading" class="card muted" style="padding: 0.75rem; font-size: 13px">
        正在读取防火墙规则…
      </div>
      <!-- Nothing at all: say so instead of rendering a blank panel.
           但读回来之前不能说「该实例没有 NSG」：换实例会先清空这两张表，
           那句话会替新机器下一个还没查证的结论。 -->
      <div
        v-else-if="!fwGroups.length && !fwSecurityLists.length"
        class="card"
        style="padding: 0.75rem"
      >
        <div class="muted" style="font-size: 13px">
          该实例没有关联的网络安全组（NSG），其子网也没有可读的安全列表。<br />
          放行端口可点「放行全部端口」（为该实例创建并绑定一个 NSG），或在 Oracle 控制台为子网添加安全列表规则。
        </div>
      </div>

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

      <!-- Subnet security lists: where the rules actually live for most instances.
           Read-only here — the add/delete endpoints operate on NSGs. -->
      <div
        v-for="sl in fwSecurityLists"
        :key="sl.id"
        class="card"
        style="padding: 0.75rem"
      >
        <div style="font-weight: 600">
          {{ sl.display_name }}
          <span class="badge">子网安全列表 · 只读</span>
        </div>
        <div class="muted" style="font-size: 11px; word-break: break-all">{{ sl.id }}</div>
        <div class="table-wrap" style="margin-top: 0.5rem">
          <table>
            <thead>
              <tr>
                <th>方向</th>
                <th>协议</th>
                <th>CIDR</th>
                <th>端口</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="!(sl.rules || []).length">
                <td colspan="4" class="muted empty">该安全列表没有规则</td>
              </tr>
              <tr v-for="(r, i) in sl.rules || []" :key="`${sl.id}-${i}`">
                <td>{{ r.direction_label || r.direction }}</td>
                <td>{{ r.protocol_label || r.protocol }}</td>
                <td>{{ r.cidr }}</td>
                <td>{{ r.port }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="muted" style="margin: 0.4rem 0 0; font-size: 12px">
          安全列表规则请在 Oracle 控制台修改；面板的「添加规则 / 放行全部端口」只作用于 NSG。
        </p>
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
            <!-- 读取中不说「暂无」：换租户会先清空这张表。 -->
            <tr v-if="ripBusy && !reservedIps.length">
              <td colspan="5" class="muted empty">正在读取…</td>
            </tr>
            <tr v-else-if="reservedIps.length === 0">
              <td colspan="5" class="muted empty">该区域暂无保留 IP。「新建保留 IP」后即可绑定到实例。</td>
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
              <tr v-if="backupBusy && !backups.length">
                <td colspan="6" class="muted empty">正在读取…</td>
              </tr>
              <tr v-else-if="backups.length === 0">
                <td colspan="6" class="muted empty">暂无备份</td>
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
        <!-- instance 为 null 时（换实例、清空后还没读回来）既不能显示表单，
             也不能断言「固定规格」—— 那是在替一台还没读到的机器下结论。 -->
        <p v-else-if="!instance" class="muted" style="margin: 0; font-size: 13px">
          正在读取实例规格…
        </p>
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
      <!-- key 上带实例 id：路由复用本组件时，终端组件只会收到新 props，
           A 的会话仍开着不动，而它的「断开 / 清除主机密钥」已经指向 B。
           换 key 强制重建，组件的 onBeforeUnmount 会先关掉 A 的 socket。 -->
      <WebSshTerminal
        :key="`${tenantId}:${instanceId}`"
        :tenant-id="tenantId"
        :instance-id="instanceId"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api, { type Instance } from '@/api/client'
import { pickAndReadTextFile } from '@/utils/file'
import { copyText } from '@/utils/toast'
// Loaded only when the WebSSH tab is actually opened. Imported statically it
// pulled xterm.js (~250 kB) into this page's chunk, so every visit that just
// checked an IP or hit reboot downloaded a terminal engine it never rendered.
const WebSshTerminal = defineAsyncComponent(
  () => import('@/components/WebSshTerminal.vue'),
)
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
const tab = ref<
  'metrics' | 'console' | 'bootlog' | 'webssh' | 'firewall' | 'network' | 'volume'
>('metrics')
const tabs = [
  { id: 'metrics' as const, label: '监控' },
  { id: 'console' as const, label: '控制台' },
  // 和「控制台」是两回事：那个是交互式串口（要配公钥、要网络通），这个是把
  // 开机到现在的串口输出整段拉下来。机器起不来的时候，能用的只有后者。
  { id: 'bootlog' as const, label: '引导日志' },
  { id: 'webssh' as const, label: 'WebSSH' },
  { id: 'firewall' as const, label: '防火墙' },
  { id: 'network' as const, label: '保留 IP' },
  { id: 'volume' as const, label: '引导卷/规格' },
]

// ---- 请求竞态保护 ----
//
// <router-view> 没有加 key，从实例 A 点到实例 B 时路由器**复用**本组件，
// 只有底部的 watch 重新拉数据。没有守卫的话，A 的响应（OCI 动辄好几秒）
// 落地时会无条件写进 state：页面显示 A 的名称 / 状态 / IP，而 tenantId、
// instanceId 两个 computed 早已指向 B，每个按钮拼出来的 URL 也都是 B 的。
// 看着 A 点关机，停的是 B。慢的 A 迟到覆盖掉新的 B 也是同一个洞。
//
// 计数器**按 loader 分**：refreshAll 会依次启动多个 loader，共用一个计数器
// 会让它们互相判定为过期。与 AccountView / InstancesView 同构。
const loadSeq: Record<string, number> = {}

function beginLoad(key: string): { stale: () => boolean; superseded: () => boolean } {
  const seq = (loadSeq[key] = (loadSeq[key] || 0) + 1)
  const wantedTenant = tenantId.value
  const wantedInstance = instanceId.value
  return {
    // 该不该把结果**写进** state：换了实例（或租户）就不能写。
    stale: () =>
      seq !== loadSeq[key] ||
      tenantId.value !== wantedTenant ||
      instanceId.value !== wantedInstance,
    // 该不该**关掉** spinner：只看序号。带上 id 判断的话，加载途中切实例
    // 会让这一次不关 spinner，而切换本身未必启动同名 loader 来接管所有权
    // （比如当前不在那个标签页），按钮就永久卡在 disabled。
    // InstancesView.load() 和 AccountView.loadAll() 都踩过并修过这个坑。
    superseded: () => seq !== loadSeq[key],
  }
}

/**
 * 动作入口处锁定目标实例。
 *
 * URL 一律用锁定的 id 拼，而不是发请求那一刻的 tenantId.value —— 确认框问的
 * 是哪台，打出去的请求就必须是哪台。await 回来后若路由已经换人，就不再写
 * msg / error、也不重新加载，否则 A 的操作结果会显示在 B 的页面上。
 */
function beginAction() {
  const tenant = tenantId.value
  const target = instanceId.value
  return {
    tenant,
    target,
    moved: () => tenantId.value !== tenant || instanceId.value !== target,
  }
}

/**
 * 确认框里用来指代目标机器的字样。
 *
 * 只有当已加载的 instance 确实就是当前路由指向的那台时才敢用它的名字：
 * 否则确认框写着 A 的名称、请求却打向 B，用户是照着错的东西点的「确定」。
 * 拿不准就退回 OCID 尾段 —— 不好读，但一定是即将被操作的那台。
 */
function targetLabel(id?: string): string {
  const wanted = id || instanceId.value
  const ins = instance.value
  const name = ins && ins.id === wanted ? ins.display_name : ''
  return name ? `「${name}」` : `OCID …${wanted.slice(-12)}`
}

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
  const guard = beginLoad('reservedIps')
  ripBusy.value = true
  try {
    const { data } = await api.get(`/tenants/${tenantId.value}/reserved-ips`)
    if (guard.stale()) return
    reservedIps.value = data.items || []
  } catch (e: any) {
    if (guard.stale()) return
    error.value = e?.message || '加载保留 IP 失败'
  } finally {
    if (!guard.superseded()) ripBusy.value = false
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
  // 「本实例」是哪一台要说清楚，并且说的必须是请求真正打向的那一台。
  const act = beginAction()
  if (
    !confirm(
      `将保留 IP ${ip.ip_address} 绑定到实例 ${targetLabel(act.target)}？` +
        `实例当前的临时公网 IP 会被释放（地址不可找回）。`,
    )
  )
    return
  ripBusy.value = true
  error.value = ''
  try {
    const { data } = await api.post(
      `/tenants/${act.tenant}/instances/${act.target}/reserved-ip/attach`,
      { public_ip_id: ip.id },
    )
    if (act.moved()) return
    if (data.ok) msg.value = data.message
    else error.value = data.message
    await Promise.all([loadReservedIps(), loadInstance()])
  } catch (e: any) {
    if (act.moved()) return
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
  const guard = beginLoad('backups')
  backupBusy.value = true
  try {
    const { data } = await api.get(`/tenants/${tenantId.value}/boot-volume-backups`, {
      params: { boot_volume_id: bootInfo.value.boot_volume_id },
    })
    if (guard.stale()) return
    backups.value = data.items || []
  } catch (e: any) {
    if (guard.stale()) return
    error.value = e?.message || '加载备份失败'
  } finally {
    if (!guard.superseded()) backupBusy.value = false
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
  const guard = beginLoad('instance')
  const { data } = await api.get<Instance>(
    `/tenants/${tenantId.value}/instances/${instanceId.value}`,
  )
  // 这一份是上一台机器的答复（或者迟到的旧答复）：写进去就等于页面显示 A、
  // 按钮操作 B。整个页面的正确性都压在这一行上。
  if (guard.stale()) return
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
  const guard = beginLoad('metrics')
  loadingMetrics.value = true
  try {
    const { data } = await api.get(
      `/tenants/${tenantId.value}/instances/${instanceId.value}/metrics`,
      { params: { hours: metricHours.value } },
    )
    // 曲线不带标识，画错了没人看得出来 —— 只能靠这里拦住。
    if (guard.stale()) return
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
    if (guard.stale()) return
    metricsMsg.value = e?.message || '加载监控失败'
  } finally {
    if (!guard.superseded()) loadingMetrics.value = false
  }
}

// ---- console ----
const consoleKey = ref('')
const consoleList = ref<any[]>([])
const consoleBusy = ref(false)

async function loadConsole() {
  // Surface failures instead of leaving a stale list on screen with no hint that
  // the refresh failed (the sibling loaders already do this).
  const guard = beginLoad('console')
  try {
    const { data } = await api.get(
      `/tenants/${tenantId.value}/instances/${instanceId.value}/console`,
    )
    // 列表里每条的「删除」按钮都是按当前 id 拼 URL 的，混进上一台的连接
    // 就会去删另一台机器的控制台。
    if (guard.stale()) return
    consoleList.value = data.connections || []
  } catch (e: any) {
    if (guard.stale()) return
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
// Subnet security lists — for most instances this is where the rules actually are.
const fwSecurityLists = ref<any[]>([])
const fwMsg = ref('')
const fwBusy = ref(false)
// 与 fwBusy 分开：fwBusy 是「正在改规则」，这个是「正在读规则」。读取期间
// 两张表是空的，模板要靠它区分「还没读到」和「确实没有 NSG」。
const fwLoading = ref(false)
const ruleForm = reactive({
  direction: 'INGRESS',
  protocol: '6',
  cidr: '0.0.0.0/0',
  port_min: 22 as number | null,
})

async function loadFirewall() {
  const guard = beginLoad('firewall')
  fwLoading.value = true
  try {
    const { data } = await api.get(
      `/tenants/${tenantId.value}/instances/${instanceId.value}/firewall`,
    )
    // 规则表上的「删」按钮按当前 id 发请求，显示成上一台的规则就会删错机器。
    if (guard.stale()) return
    fwMsg.value = data.message || ''
    if (data.ok === false) error.value = data.message || '加载防火墙规则失败'
    fwGroups.value = data.data?.groups || []
    fwSecurityLists.value = data.data?.security_lists || []
  } catch (e: any) {
    if (guard.stale()) return
    error.value = e?.message || '加载防火墙规则失败'
  } finally {
    if (!guard.superseded()) fwLoading.value = false
  }
}
async function openAllFirewall() {
  const act = beginAction()
  if (
    !confirm(
      `将实例 ${targetLabel(act.target)} 关联的 NSG 规则全部清空，并改为全开放，确认？\n` +
        '所有端口都会对 0.0.0.0/0 开放，原有规则不可恢复。',
    )
  )
    return
  fwBusy.value = true
  error.value = ''
  try {
    const { data } = await api.post(
      `/tenants/${act.tenant}/instances/${act.target}/firewall/open-all`,
    )
    if (act.moved()) return
    if (data.ok) msg.value = data.message
    else error.value = data.message
    await loadFirewall()
  } catch (e: any) {
    if (act.moved()) return
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
  const guard = beginLoad('boot')
  bootBusy.value = true
  try {
    const { data } = await api.get(
      `/tenants/${tenantId.value}/instances/${instanceId.value}/boot-volume`,
    )
    // 引导卷 OCID 会被「创建备份 / 调整容量」直接拿去用，串了台就是在改
    // 另一台机器的盘。
    if (guard.stale()) return
    if (data.ok) {
      bootInfo.value = data.data
      bootForm.size_in_gbs = data.data.size_in_gbs
      bootForm.vpus_per_gb = data.data.vpus_per_gb
    } else error.value = data.message
  } catch (e: any) {
    if (guard.stale()) return
    error.value = e?.message || '读取失败'
  } finally {
    if (!guard.superseded()) bootBusy.value = false
  }
}
async function updateBoot() {
  // 这一发会把 SSH 凭证连同扩容请求一起送到某台机器上，返回的还是那台机器的
  // shell 输出 —— 目标必须在入口就锁死，回来时人已经换页就别再往下写。
  const act = beginAction()
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
      `/tenants/${act.tenant}/instances/${act.target}/boot-volume`,
      payload,
    )
    if (act.moved()) return
    if (data.data) fsGrowResult.value = data.data
    if (data.ok) {
      msg.value = data.message
      if (growCreds.authMode === 'password') growCreds.password = ''
      await loadBoot()
    } else error.value = data.message
  } catch (e: any) {
    if (act.moved()) return
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
  const act = beginAction()
  acting.value = true
  error.value = ''
  try {
    const { data } = await api.post(
      `/tenants/${act.tenant}/instances/${act.target}/shape`,
      {
        ocpus: shapeForm.ocpus,
        memory_in_gbs: shapeForm.memory_in_gbs,
      },
    )
    if (act.moved()) return
    if (data.ok) {
      msg.value = data.message
      await loadInstance()
    } else error.value = data.message
  } catch (e: any) {
    if (act.moved()) return
    error.value = e?.message || '修改失败'
  } finally {
    acting.value = false
  }
}

// ---- power helpers ----
const POWER_LABEL: Record<string, string> = {
  START: '开机',
  SOFTSTOP: '关机',
  SOFTRESET: '重启',
  STOP: '强制关机',
  RESET: '强制重启',
}

async function power(action: string) {
  const act = beginAction()
  // 关机 / 重启会中断这台机器上跑的一切，点错一台就是一次线上事故，所以要
  // 确认、并且把机器名写进去。开机不会打断任何东西，不值得多一次点击。
  if (
    action !== 'START' &&
    !confirm(`确认对实例 ${targetLabel(act.target)} 执行「${POWER_LABEL[action] || action}」？`)
  ) {
    return
  }
  acting.value = true
  error.value = ''
  try {
    const { data } = await api.post(
      `/tenants/${act.tenant}/instances/${act.target}/power`,
      { action },
    )
    if (act.moved()) return
    // The API answers 200 with ok=false when OCI refuses the action (wrong
    // lifecycle state, etc.) — showing data.message as a success banner told the
    // user it worked. Branch on ok, like doReplaceIp already does.
    if (data.ok) msg.value = data.message
    else error.value = data.message || '操作失败'
    await loadInstance()
  } catch (e: any) {
    if (act.moved()) return
    error.value = e?.message || '操作失败'
  } finally {
    acting.value = false
  }
}
async function doRename() {
  const act = beginAction()
  // 预填的旧名字必须来自请求真正要改的那台机器：曾经这里直接读 instance.value，
  // 而 instance 可能还是上一台的，于是 B 被改成了 A 的名字。targetLabel /
  // currentName 都只在 instance.id 与目标 id 相符时才采信。
  const currentName =
    instance.value && instance.value.id === act.target ? instance.value.display_name : ''
  const name = prompt(`重命名实例 ${targetLabel(act.target)}\n新名称`, currentName)
  if (!name?.trim()) return
  acting.value = true
  try {
    const { data } = await api.post(
      `/tenants/${act.tenant}/instances/${act.target}/rename`,
      { display_name: name.trim() },
    )
    if (act.moved()) return
    if (data.ok) msg.value = data.message
    else error.value = data.message || '重命名失败'
    await loadInstance()
  } catch (e: any) {
    if (act.moved()) return
    error.value = e?.message || '失败'
  } finally {
    acting.value = false
  }
}
async function doReplaceIp() {
  const act = beginAction()
  if (
    !confirm(
      `更换实例 ${targetLabel(act.target)} 的临时公网 IPv4？\n` +
        '旧地址会被释放且不可找回，指向它的 DNS / 防火墙规则会失效。',
    )
  )
    return
  acting.value = true
  try {
    const { data } = await api.post(
      `/tenants/${act.tenant}/instances/${act.target}/public-ip/replace`,
    )
    if (act.moved()) return
    if (data.ok) msg.value = data.message
    else error.value = data.message
    await loadInstance()
  } catch (e: any) {
    if (act.moved()) return
    error.value = e?.message || '失败'
  } finally {
    acting.value = false
  }
}
async function doIpv6() {
  // 分配地址是增量操作，不删不断，不加确认。
  const act = beginAction()
  acting.value = true
  try {
    const { data } = await api.post(`/tenants/${act.tenant}/instances/${act.target}/ipv6`)
    if (act.moved()) return
    if (data.ok) msg.value = data.message
    else error.value = data.message
    await loadInstance()
  } catch (e: any) {
    if (act.moved()) return
    error.value = e?.message || '失败'
  } finally {
    acting.value = false
  }
}
async function doRemoveIpv6() {
  const act = beginAction()
  // 地址清单同样只在 instance 确实是目标机器时才敢列出来 —— 列出 A 的地址
  // 却删 B 的，用户在确认框里根本看不出被删的是什么。
  const list =
    instance.value && instance.value.id === act.target
      ? (instance.value.ipv6_addresses || []).join('、')
      : ''
  // Confirmed because the address is not recoverable: assigning again produces a
  // NEW one, so anything pointing at the old address (DNS, firewall rules on
  // other hosts) stops matching.
  if (
    !confirm(
      `确认取消实例 ${targetLabel(act.target)} 的 IPv6？\n${list}\n\n` +
        '重新分配会得到一个不同的地址，指向旧地址的 DNS / 防火墙规则将失效。\n' +
        '子网与 VCN 的 IPv6 前缀和路由保持不变，同子网的其他实例不受影响。',
    )
  ) {
    return
  }
  acting.value = true
  try {
    const { data } = await api.delete(`/tenants/${act.tenant}/instances/${act.target}/ipv6`)
    if (act.moved()) return
    if (data.ok) msg.value = data.message
    else error.value = data.message
    await loadInstance()
  } catch (e: any) {
    if (act.moved()) return
    error.value = e?.message || '失败'
  } finally {
    acting.value = false
  }
}

// ---- 引导日志 ----
const bootlog = ref('')
const bootlogMsg = ref('')
const bootlogBusy = ref(false)

async function loadBootLog() {
  const act = beginAction()
  bootlogBusy.value = true
  bootlogMsg.value = ''
  try {
    const { data } = await api.post(
      `/tenants/${act.tenant}/instances/${act.target}/console-output`,
    )
    // 抓取要十几秒，期间用户完全可能已经切到另一台机器上了。没有这一句，
    // A 的引导日志就会渲染在 B 的页面里 —— 而引导日志正是用来做判断的东西。
    if (act.moved()) return
    if (data.ok) {
      bootlog.value = data.content || ''
      bootlogMsg.value = data.content ? data.message || '' : '抓取成功，但输出为空。'
    } else {
      bootlogMsg.value = data.message || '抓取失败'
    }
  } catch (e: any) {
    if (act.moved()) return
    bootlogMsg.value = e?.message || '抓取失败'
  } finally {
    bootlogBusy.value = false
  }
}

async function toggleProtect() {
  const act = beginAction()
  const next = !instance.value?.protected
  if (
    next === false &&
    !confirm(
      `解除「${targetLabel(act.target)}」的终止保护？\n\n解除后即可执行终止操作。`,
    )
  ) {
    return
  }
  acting.value = true
  error.value = ''
  try {
    const { data } = await api.post(
      `/tenants/${act.tenant}/instances/${act.target}/protect`,
      { protected: next },
    )
    if (act.moved()) return
    if (data.ok) {
      msg.value = data.message || '已更新'
      // 就地改，不重载整页：这只影响一个布尔值，重载要再花一轮 Oracle 调用。
      if (instance.value) instance.value.protected = next
    } else {
      error.value = data.message || '操作失败'
    }
  } catch (e: any) {
    if (act.moved()) return
    error.value = e?.message || '操作失败'
  } finally {
    acting.value = false
  }
}

/** 带着这台机器的配置跳到创建页。不发任何请求 —— 参数本页已经有了。 */
function useAsTemplate() {
  const i = instance.value
  if (!i) return
  router.push({
    path: '/launch',
    query: {
      tenant: tenantId.value,
      from: i.id,
      shape: i.shape || '',
      ocpus: i.ocpus != null ? String(i.ocpus) : '',
      memory: i.memory_in_gbs != null ? String(i.memory_in_gbs) : '',
      ad: i.availability_domain || '',
      boot: i.boot_volume_size_in_gbs != null ? String(i.boot_volume_size_in_gbs) : '',
    },
  })
}

async function doTerminate() {
  const act = beginAction()
  if (
    !confirm(
      `确认终止实例 ${targetLabel(act.target)}？\n` +
        '实例和它的引导卷都会被删除，数据不可恢复。',
    )
  )
    return
  acting.value = true
  try {
    const { data } = await api.post(
      `/tenants/${act.tenant}/instances/${act.target}/terminate`,
      { preserve_boot_volume: false },
    )
    if (act.moved()) return
    if (!data.ok) {
      // Navigating away on a refused terminate left the user believing the
      // instance was gone.
      error.value = data.message || '终止失败'
      return
    }
    msg.value = data.message
    setTimeout(() => router.push('/'), 800)
  } catch (e: any) {
    if (act.moved()) return
    error.value = e?.message || '失败'
  } finally {
    acting.value = false
  }
}

/** 当前标签页对应的那一份数据；其余标签页按需加载，不在这里预取。 */
async function loadCurrentTab() {
  if (tab.value === 'metrics') await loadMetrics()
  else if (tab.value === 'console') await loadConsole()
  else if (tab.value === 'firewall') await loadFirewall()
  else if (tab.value === 'network') await loadReservedIps()
  else if (tab.value === 'volume') {
    await loadBoot()
    await loadBackups()
  }
}

async function refreshAll() {
  const guard = beginLoad('page')
  loading.value = true
  error.value = ''
  try {
    await loadInstance()
    await loadCurrentTab()
  } catch (e: any) {
    if (guard.stale()) return
    error.value = e?.message || '加载失败'
  } finally {
    // spinner 只归序号管：刷新途中切实例时，若这里还带上 id 判断，本次不关、
    // 而接管的是路由 watch 里同 key 的那一次 —— 它会关。但反过来，一旦把
    // id 判断写进来，任何没有后继者的场景都会让「刷新」按钮永久 disabled。
    if (!guard.superseded()) loading.value = false
  }
}

watch(tab, async () => {
  try {
    await loadCurrentTab()
  } catch (e: any) {
    error.value = e?.message || '加载失败'
  }
})

onMounted(async () => {
  const guard = beginLoad('page')
  loading.value = true
  try {
    // Only the instance summary is required to open the page.
    // Metrics / console / firewall / volume load when the user opens that tab
    // (or clicks 刷新全部), to avoid background Oracle polling.
    await loadInstance()
    await loadCurrentTab()
  } catch (e: any) {
    if (guard.stale()) return
    error.value = e?.message || '加载失败'
  } finally {
    if (!guard.superseded()) loading.value = false
  }
})

/**
 * 换实例前先清空本实例的所有数据。
 *
 * <router-view> 没有 key，A→B 时组件被复用，只有下面的 watch 重新拉数据。
 * 不清空的话，在 B 的响应回来之前（OCI 要好几秒）页面渲染的全是 A 的名称、
 * 状态、IP、规则、备份，而按钮拼 URL 用的已经是 B 的 id —— 看着 A 点关机，
 * 停的是 B。宁可空几秒，也不能显示一台、操作另一台。
 */
function resetInstanceState() {
  instance.value = null
  error.value = ''
  msg.value = ''
  metricsSeries.value = {}
  metricHover.value = {}
  metricsMsg.value = ''
  // 引导日志尤其不能留:它是用来判断「这台机器为什么起不来」的,
  // 挂在另一台实例的页面上会直接导致误判。
  bootlog.value = ''
  bootlogMsg.value = ''
  consoleList.value = []
  fwGroups.value = []
  fwSecurityLists.value = []
  fwMsg.value = ''
  reservedIps.value = []
  bootInfo.value = null
  bootForm.size_in_gbs = null
  bootForm.vpus_per_gb = 10
  backups.value = []
  fsGrowResult.value = null
  // 表单默认值：不清的话，A 的 OCPU/内存会留在框里，B 一点「应用规格」就被
  // 改成 A 的规格。loadInstance 只在字段非空时回填，兜不住这一步。
  shapeForm.ocpus = 1
  shapeForm.memory_in_gbs = 6
  // SSH 凭证是按机器给的：为 A 输入的私钥 / 密码绝不能跟着页面漂到 B，
  // 那等于把 A 的凭证发到另一台主机上去。
  autoGrowFs.value = false
  growCreds.privateKeyPem = ''
  growCreds.password = ''
}

// If the router reuses this component for a different instance, reload its data.
// Mirrors onMounted exactly: the summary, then only the tab actually on screen.
// It used to pull metrics unconditionally, so switching between instances while
// sitting on the firewall tab spent a Monitoring query nobody asked for.
watch([tenantId, instanceId], async () => {
  resetInstanceState()
  const guard = beginLoad('page')
  loading.value = true
  try {
    await loadInstance()
    await loadCurrentTab()
  } catch (e: any) {
    if (guard.stale()) return
    error.value = e?.message || '加载失败'
  } finally {
    if (!guard.superseded()) loading.value = false
  }
})
</script>

<style scoped>
/* 引导日志是等宽、可能很长的机器输出。给固定高度 + 自己滚动，否则一份几千行的
   内核日志会把整页撑到没法用；不换行，因为内核那些对齐的表格一折行就废了。 */
.bootlog {
  max-height: 60vh;
  overflow: auto;
  white-space: pre;
  font-size: 12px;
  line-height: 1.45;
  margin: 0;
  padding: 0.75rem;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--panel-2);
}

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
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 0.7rem 0.8rem 0.55rem;
  background: var(--panel-2);
  min-width: 0;
  box-shadow: none;
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
  background: rgb(20, 22, 28);
  color: #fff;
  border: 1px solid rgba(255, 255, 255, 0.16);
  box-shadow: var(--shadow-md);
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
