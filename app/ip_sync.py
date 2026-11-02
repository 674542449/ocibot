"""「多出口 IP」的系统侧同步服务：安装脚本在这里生成，经 SSH 推到实例上执行。

为什么需要它：在 Oracle 那边给网卡加辅助私网 IP、再绑上保留公网 IP 之后，
Oracle 的网关立刻就会把流量转给这个私网 IP —— 但实例系统并不会自动认它，
不在网卡上的地址收到包直接丢掉。手动 ``ip addr add`` 能用，重启就没了。

IP 列表从哪来（0.4.131 改）：
    0.4.129 照 Oracle 文档读 IMDS ``/opc/v2/vnics/`` 里的 ``secondaryPrivateIps``，
    实测这个字段根本不存在（2026-10，ap-singapore-1，A1.Flex / Ubuntu）—— 服务装好了，
    每分钟跑一次，却一个 IP 都没加，日志是「metadata has no secondary IP list」。
    现在由面板在每次绑定 / 解绑后把列表写进**实例元数据**
    （oci_client.publish_secondary_ips，键名 ``ocibot_secondary_ips``，
    值 ``{"<vnic OCID>": ["10.0.0.11", ...]}``），这里从
    ``/opc/v2/instance/metadata/ocibot_secondary_ips`` 读。不需要任何 Oracle 凭据，
    Oracle 说改动约一分钟内在 IMDS 生效。``secondaryPrivateIps`` 只作为后备
    （万一哪天 Oracle 真的开始返回它）。

做法：装一个 systemd 定时器，开机 20 秒后、此后每分钟跑一次同步脚本：

* 列表里有、网卡上没有的辅助 IP —— 按列表顺序加上（面板按公网 IP 名字排好，
  ``ip addr`` 里的先后就和面板「多出口 IP」表一致）；
* 自己以前加过、列表里已经没有的 —— 删掉；
* **从不碰**主 IP、也从不碰不是它加的地址（手工写进 netplan 的那些）。它加过
  哪些记在 ``/var/lib/ocibot-ip-sync/<网卡名>``；
* 同一时间只有一个同步在改网卡（``/var/lib/ocibot-ip-sync/.lock``）：安装时前台那次和
  定时器那次曾经同时跑，各记各的「加了哪些」，后写的把先写的覆盖掉（0.4.175 修）；
* 读不到列表（启动早期网络没好、元数据服务抖动、面板还没写过）时**什么都不删**
  —— 读失败不能被理解成「一个辅助 IP 都没有」，那会把正在用的出口全部拆掉。
"""

from __future__ import annotations

SYNC_SCRIPT_PATH = "/usr/local/sbin/ocibot-ip-sync"
SERVICE_NAME = "ocibot-ip-sync"
METADATA_KEY = "ocibot_secondary_ips"

# 跑在**实例**上的同步脚本。保持 Python 3.6 兼容：Oracle Linux 8 的系统解释器
# 是 3.6（/usr/libexec/platform-python），最小安装时甚至没有 python3 命令。
_SYNC_PY = r'''
import ipaddress
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

STATE_DIR = "/var/lib/ocibot-ip-sync"
IMDS_VNICS = "http://169.254.169.254/opc/v2/vnics/"
IMDS_LIST = "http://169.254.169.254/opc/v2/instance/metadata/__METADATA_KEY__"


def log(msg):
    sys.stdout.write("ocibot-ip-sync: %s\n" % msg)
    sys.stdout.flush()


def imds_get(url):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer Oracle"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.read().decode("utf-8")


def read_vnics():
    data = json.loads(imds_get(IMDS_VNICS))
    if not isinstance(data, list):
        raise ValueError("unexpected IMDS response")
    return data


def read_panel_list():
    """{vnic OCID: [ip, ...]} written by the panel, or None when absent / unreadable."""
    try:
        data = json.loads(imds_get(IMDS_LIST))
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            log("panel IP list unreadable (HTTP %s)" % exc.code)
        return None
    except Exception as exc:  # noqa: BLE001
        log("panel IP list unreadable (%s)" % exc)
        return None
    return data if isinstance(data, dict) else None


def interfaces_by_mac():
    out = {}
    for name in os.listdir("/sys/class/net"):
        try:
            with open("/sys/class/net/%s/address" % name) as fh:
                out[fh.read().strip().lower()] = name
        except OSError:
            continue
    return out


def current_ipv4(dev):
    res = subprocess.run(
        ["ip", "-4", "-o", "addr", "show", "dev", dev],
        stdout=subprocess.PIPE, universal_newlines=True, check=True,
    )
    addrs = set()
    for line in res.stdout.splitlines():
        parts = line.split()
        if "inet" in parts:
            addrs.add(parts[parts.index("inet") + 1].split("/")[0])
    return addrs


def load_state(dev):
    try:
        with open(os.path.join(STATE_DIR, dev)) as fh:
            return set(fh.read().split())
    except OSError:
        return set()


def save_state(dev, addrs):
    if not os.path.isdir(STATE_DIR):
        os.makedirs(STATE_DIR)
    path = os.path.join(STATE_DIR, dev)
    # 临时文件带上进程号：两个同步同时写时，以前共用 <网卡>.tmp，一个把它挪走了，
    # 另一个 os.replace 就 FileNotFoundError（0.4.174 安装时实际出现过）。
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as fh:
        fh.write("\n".join(sorted(addrs)) + "\n")
    os.replace(tmp, path)


def acquire_lock():
    """同一时间只让一个同步改网卡。返回的文件对象要一直拿着，进程退出时自动释放。

    安装脚本在前台跑一次同步，同时定时器也可能正好触发一次：两边各自读到「网卡上还没有」，
    同一批地址一起加，慢的那个报 Address already assigned；更糟的是两边各记各的「我加了哪些」，
    后写的覆盖先写的，大部分地址就不在记录里了 —— 以后在面板解绑，它们也不会从网卡上删掉。
    排队之后，后来的那个会重新读网卡、发现都加好了，什么也不做。
    """
    import fcntl

    if not os.path.isdir(STATE_DIR):
        os.makedirs(STATE_DIR)
    fh = open(os.path.join(STATE_DIR, ".lock"), "w")
    fcntl.flock(fh, fcntl.LOCK_EX)
    return fh


def ip_cmd(*args):
    res = subprocess.run(["ip"] + list(args), stderr=subprocess.PIPE, universal_newlines=True)
    if res.returncode != 0:
        log("ip %s failed: %s" % (" ".join(args), (res.stderr or "").strip()))
    return res.returncode == 0


def wanted_for(vnic, panel):
    """IPs this VNIC should carry, or None when nobody has told us."""
    vnic_id = str(vnic.get("vnicId") or "")
    if panel is not None and vnic_id in panel:
        return panel[vnic_id] or []
    for key in ("secondaryPrivateIps", "secondaryPrivateIPs"):
        if key in vnic:
            return vnic.get(key) or []
    return None


def main(wait=0):
    deadline = time.time() + wait
    while True:
        try:
            vnics = read_vnics()
        except Exception as exc:  # noqa: BLE001 - never act on a failed read
            log("metadata unavailable, nothing changed (%s)" % exc)
            return 0
        panel = read_panel_list()
        if panel is not None or time.time() >= deadline:
            break
        # 安装时用：面板刚写的元数据要一会儿才在 IMDS 出现（Oracle 说最多约一分钟）。
        time.sleep(5)
    # 等列表的时候不占锁（那可能要一分多钟）；从这里开始要读网卡、改网卡、写记录，必须排队。
    lock = acquire_lock()  # noqa: F841 - 拿着就行，进程退出时释放
    macs = interfaces_by_mac()
    for vnic in vnics:
        dev = macs.get(str(vnic.get("macAddr") or "").lower())
        if not dev:
            continue  # a secondary VNIC the OS has not brought up
        listed = wanted_for(vnic, panel)
        if listed is None:
            log("%s: no IP list from the panel yet, nothing changed" % dev)
            continue
        try:
            prefix = ipaddress.ip_network(str(vnic.get("subnetCidrBlock") or ""), strict=False).prefixlen
        except ValueError:
            continue
        primary = str(vnic.get("privateIp") or "")
        # 按面板给的顺序加（面板按公网 IP 名字排：proxy-01、proxy-02 …），ip addr 里的先后
        # 就和面板列表一致。以前是按字符串排序加的，10.0.0.100 会排在 10.0.0.11 前面。
        # 已经在网卡上的不重新排 —— 那要先删再加，会断流；重启后全部按这个顺序重新加。
        order = []
        for ip in listed:
            if ip and ":" not in ip and ip != primary and ip not in order:
                order.append(ip)
        want = set(order)
        have = current_ipv4(dev)
        managed = load_state(dev)
        for ip in order:
            if ip in have:
                continue
            if ip_cmd("addr", "add", "%s/%d" % (ip, prefix), "dev", dev):
                managed.add(ip)
                log("%s: added %s" % (dev, ip))
        for ip in sorted(managed - want):
            if ip == primary or ip not in have:
                managed.discard(ip)  # 已经不在网卡上（或是主 IP），不用再管
                continue
            # 删成功了才从记录里去掉。以前不管成败都去掉，一次失败之后这个地址就再也
            # 不会被重试，永远挂在网卡上。
            if ip_cmd("addr", "del", "%s/%d" % (ip, prefix), "dev", dev):
                log("%s: removed %s" % (dev, ip))
                managed.discard(ip)
        # 只记自己加的。已经在网卡上的（手工写进 netplan 的）不收编，
        # 否则它从 Oracle 那边解绑后会被这里删掉 —— 那不是本服务该动的东西。
        save_state(dev, managed)
        log("%s: %d secondary IP(s) expected, %d on the interface" % (dev, len(want), len(want & current_ipv4(dev))))
    return 0


sys.exit(main(int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[1] == "--wait" else 0))
'''.replace("__METADATA_KEY__", METADATA_KEY)

_SERVICE_UNIT = f"""[Unit]
Description=OCIBot: keep OCI secondary private IPs configured on this instance
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=__PYTHON__ {SYNC_SCRIPT_PATH}
"""

_TIMER_UNIT = f"""[Unit]
Description=OCIBot: run {SERVICE_NAME} at boot and every minute

[Timer]
OnBootSec=20s
OnUnitActiveSec=60s
AccuracySec=5s

[Install]
WantedBy=timers.target
"""


def build_install_script() -> str:
    """Bash script that installs (or updates) the sync service. Idempotent.

    Runs as root — the caller pipes it to ``sudo -n bash -s`` for non-root users.
    Runs one sync in the foreground (waiting up to 75 s for the panel's list to show
    up in IMDS) so the panel can show what was actually added, then prints the
    interface addresses.
    """
    return f"""set -eu
PY="$(command -v python3 || true)"
if [ -z "$PY" ] && [ -x /usr/libexec/platform-python ]; then PY=/usr/libexec/platform-python; fi
if [ -z "$PY" ]; then echo "找不到 python3，无法安装同步服务" >&2; exit 3; fi
if ! command -v systemctl >/dev/null 2>&1; then echo "系统没有 systemd，无法安装同步服务" >&2; exit 3; fi

install -d -m 0755 /usr/local/sbin /var/lib/ocibot-ip-sync
cat > {SYNC_SCRIPT_PATH} <<'OCIBOT_SYNC_PY'
{_SYNC_PY.strip()}
OCIBOT_SYNC_PY
chmod 0755 {SYNC_SCRIPT_PATH}

cat > /etc/systemd/system/{SERVICE_NAME}.service <<'OCIBOT_UNIT'
{_SERVICE_UNIT.strip()}
OCIBOT_UNIT
sed -i "s#__PYTHON__#$PY#" /etc/systemd/system/{SERVICE_NAME}.service

cat > /etc/systemd/system/{SERVICE_NAME}.timer <<'OCIBOT_TIMER'
{_TIMER_UNIT.strip()}
OCIBOT_TIMER

systemctl daemon-reload
# 先只设开机启动，等下面前台那次同步做完再启动定时器：开机超过 20 秒的机器上，定时器一启动
# 就会立刻触发一次，和前台那次同时改网卡。（同步脚本自己也有锁，这里是让输出里能看到加了哪些。）
# 成功时不打印 systemctl 的 "Created symlink ..."，失败时把原因原样显示出来。
if ! out="$(systemctl enable {SERVICE_NAME}.timer 2>&1)"; then echo "$out" >&2; exit 3; fi
echo "同步服务已安装并启用（开机自动运行，之后每分钟同步一次）。"
echo "立即同步一次（面板刚写入的 IP 列表最多约一分钟后才能读到）："
"$PY" {SYNC_SCRIPT_PATH} --wait 75 || true
# restart 而不是 start：更新时定时器本来就在跑，要换成刚写入的新配置。
systemctl restart {SERVICE_NAME}.timer
echo "当前网卡上的 IPv4 地址："
ip -4 -o addr show | awk '{{print "  " $2 "  " $4}}'
"""
