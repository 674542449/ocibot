"""「多出口 IP」的系统侧同步服务：安装脚本在这里生成，经 SSH 推到实例上执行。

为什么需要它：在 Oracle 那边给网卡加辅助私网 IP、再绑上保留公网 IP 之后，
Oracle 的网关立刻就会把流量转给这个私网 IP —— 但实例系统并不会自动认它，
不在网卡上的地址收到包直接丢掉。手动 ``ip addr add`` 能用，重启就没了。

做法：装一个 systemd 定时器，开机 20 秒后、此后每分钟跑一次同步脚本。脚本只读
实例元数据（IMDS v2 的 ``/opc/v2/vnics/``，里面有 ``secondaryPrivateIps``），
不需要任何 Oracle 凭据：

* 元数据里有、网卡上没有的辅助 IP —— 加上；
* 自己以前加过、元数据里已经没有的 —— 删掉；
* **从不碰**主 IP、也从不碰不是它加的地址（手工写进 netplan 的那些）。它加过
  哪些记在 ``/var/lib/ocibot-ip-sync/<网卡名>``；
* 元数据读不到（启动早期网络没好、元数据服务抖动）或者响应里根本没有
  ``secondaryPrivateIps`` 这个键时，**什么都不删** —— 读失败不能被理解成
  「一个辅助 IP 都没有」，那会把正在用的出口全部拆掉。

所以面板上增减 IP 之后不用再登录服务器，最多一分钟系统就跟上；重启后自动恢复。
"""

from __future__ import annotations

SYNC_SCRIPT_PATH = "/usr/local/sbin/ocibot-ip-sync"
SERVICE_NAME = "ocibot-ip-sync"

# 跑在**实例**上的同步脚本。保持 Python 3.6 兼容：Oracle Linux 8 的系统解释器
# 是 3.6（/usr/libexec/platform-python），最小安装时甚至没有 python3 命令。
_SYNC_PY = r'''
import ipaddress
import json
import os
import subprocess
import sys
import urllib.request

STATE_DIR = "/var/lib/ocibot-ip-sync"
IMDS = "http://169.254.169.254/opc/v2/vnics/"


def log(msg):
    sys.stdout.write("ocibot-ip-sync: %s\n" % msg)


def read_vnics():
    req = urllib.request.Request(IMDS, headers={"Authorization": "Bearer Oracle"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError("unexpected IMDS response")
    return data


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
    with open(path + ".tmp", "w") as fh:
        fh.write("\n".join(sorted(addrs)) + "\n")
    os.replace(path + ".tmp", path)


def ip_cmd(*args):
    return subprocess.run(["ip"] + list(args)).returncode == 0


def main():
    try:
        vnics = read_vnics()
    except Exception as exc:  # noqa: BLE001 - never act on a failed read
        log("metadata unavailable, nothing changed (%s)" % exc)
        return 0
    macs = interfaces_by_mac()
    for vnic in vnics:
        dev = macs.get(str(vnic.get("macAddr") or "").lower())
        if not dev:
            continue  # a secondary VNIC the OS has not brought up
        key = "secondaryPrivateIps" if "secondaryPrivateIps" in vnic else "secondaryPrivateIPs"
        if key not in vnic:
            log("%s: metadata has no secondary IP list, skipped" % dev)
            continue
        try:
            prefix = ipaddress.ip_network(str(vnic.get("subnetCidrBlock") or ""), strict=False).prefixlen
        except ValueError:
            continue
        primary = str(vnic.get("privateIp") or "")
        want = set(ip for ip in (vnic.get(key) or []) if ip and ":" not in ip and ip != primary)
        have = current_ipv4(dev)
        managed = load_state(dev)
        for ip in sorted(want - have):
            if ip_cmd("addr", "add", "%s/%d" % (ip, prefix), "dev", dev):
                managed.add(ip)
                log("%s: added %s" % (dev, ip))
        for ip in sorted(managed - want):
            if ip != primary and ip in have and ip_cmd("addr", "del", "%s/%d" % (ip, prefix), "dev", dev):
                log("%s: removed %s" % (dev, ip))
            managed.discard(ip)
        # 只记自己加的。已经在网卡上的（手工写进 netplan 的）不收编，
        # 否则它从 Oracle 那边解绑后会被这里删掉 —— 那不是本服务该动的东西。
        save_state(dev, managed)
    return 0


sys.exit(main())
'''

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
    Prints the resulting IPv4 addresses so the panel can show what the OS now has.
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
systemctl enable --now {SERVICE_NAME}.timer >/dev/null
systemctl start {SERVICE_NAME}.service
echo "同步服务已安装并启用（开机自动运行，之后每分钟同步一次）。"
echo "当前网卡上的 IPv4 地址："
ip -4 -o addr show | awk '{{print "  " $2 "  " $4}}'
"""
