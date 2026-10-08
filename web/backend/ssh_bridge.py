"""SSH helpers for guest OS access (WebSSH + boot FS grow).

Credentials are session-only: never log or persist private keys / passwords.
Target hosts are always resolved server-side from the instance's own IPs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from app.fs_grow import truncate_output

# Conservative username allowlist (no shell metacharacters).
_USERNAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")


@dataclass
class SshTarget:
    host: str
    public_ip: str = ""
    private_ip: str = ""
    allowed_ips: set[str] = field(default_factory=set)
    instance_id: str = ""
    display_name: str = ""
    lifecycle_state: str = ""


@dataclass
class SshExecResult:
    ok: bool
    exit_status: int = -1
    stdout: str = ""
    stderr: str = ""
    message: str = ""
    host: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "exit_status": self.exit_status,
            "stdout": truncate_output(self.stdout),
            "stderr": truncate_output(self.stderr),
            "message": self.message,
            "host": self.host,
        }


def validate_ssh_username(username: str) -> str:
    u = (username or "").strip()
    if not u or not _USERNAME_RE.match(u):
        raise ValueError("SSH 用户名无效（仅字母/数字/下划线/连字符，且以字母或下划线开头）")
    return u


def validate_ssh_auth(
    *,
    username: str,
    private_key_pem: Optional[str] = None,
    password: Optional[str] = None,
    port: int = 22,
) -> dict[str, Any]:
    user = validate_ssh_username(username)
    key = (private_key_pem or "").strip() or None
    pwd = password if password not in (None, "") else None
    if bool(key) == bool(pwd):
        # XOR: exactly one auth method
        if key and pwd:
            raise ValueError("请只提供一种认证方式：私钥 或 密码")
        raise ValueError("请提供 SSH 私钥或密码")
    p = int(port or 22)
    if not 1 <= p <= 65535:
        raise ValueError("SSH 端口无效")
    return {
        "username": user,
        "private_key_pem": key,
        "password": pwd,
        "port": p,
        "auth_mode": "key" if key else "password",
    }


def resolve_instance_ssh_target(session: Any, instance_id: str) -> SshTarget:
    """Resolve SSH host from instance public/private IP (never client-supplied)."""
    info = session.get_instance(instance_id, resolve_ips=True)
    public_ip = str(getattr(info, "public_ip", "") or "").strip()
    private_ip = str(getattr(info, "private_ip", "") or "").strip()
    allowed: set[str] = set()
    if public_ip:
        allowed.add(public_ip)
    if private_ip:
        allowed.add(private_ip)
    # IPv6 optional
    for ip6 in list(getattr(info, "ipv6_addresses", None) or []):
        s = str(ip6 or "").strip()
        if s:
            allowed.add(s)
    host = public_ip or private_ip
    if not host:
        raise ValueError("实例没有可用的 IP（需要公网 IP，或宿主能访问的私网 IP）")
    state = str(getattr(info, "lifecycle_state", "") or "")
    return SshTarget(
        host=host,
        public_ip=public_ip,
        private_ip=private_ip,
        allowed_ips=allowed,
        instance_id=str(getattr(info, "id", "") or instance_id),
        display_name=str(getattr(info, "display_name", "") or ""),
        lifecycle_state=state,
    )


def assert_host_allowed(host: str, target: SshTarget) -> None:
    h = (host or "").strip()
    if h not in target.allowed_ips:
        raise ValueError("拒绝连接到非本实例 IP")


def _import_private_key(private_key_pem: str) -> Any:
    import asyncssh

    try:
        return asyncssh.import_private_key(private_key_pem)
    except (asyncssh.KeyImportError, ValueError, TypeError) as exc:
        raise ValueError(f"SSH 私钥无法解析：{exc}") from exc


# ssh_exec() / ssh_exec_sync() lived here with `known_hosts=None` and a comment
# reading "None disables host key check (OCI IPs rotate often)". They had no
# callers left — every live SSH path (webssh.py, instance_ops.py) passes the key
# verified by the KEX-only probe — but the comment stood as an endorsement of the
# very default the host-key work was created to eliminate, and the next person
# needing a one-off remote command would have reached for it. Deleted rather than
# left as a loaded gun; ssh_hostkey.check_instance_host_key + known_hosts_for()
# is the pattern to copy if a command runner is ever needed again.


def _exec_script(
    host: str,
    script: str,
    *,
    command: str,
    port: int,
    username: str,
    private_key_pem: Optional[str],
    password: Optional[str],
    known_hosts: Any,
    timeout: float,
    ok_message: str,
    fail_message: str,
) -> SshExecResult:
    """One SSH connection that runs ``command`` with ``script`` on stdin.

    Feeding the script on stdin means nothing is written to the remote disk that
    would need cleanup. ``known_hosts`` must be the key check_instance_host_key()
    verified — both public callers refuse to get here without one.
    """
    import asyncio as _asyncio

    async def _once() -> SshExecResult:
        import asyncssh

        user = validate_ssh_username(username)
        connect_kwargs: dict[str, Any] = {
            "host": host,
            "port": int(port or 22),
            "username": user,
            # Always the key check_instance_host_key() just verified; the public
            # wrappers refuse to call at all without one. Do not "simplify" this
            # back to a default of None.
            "known_hosts": known_hosts,
            "login_timeout": 30.0,
        }
        if private_key_pem:
            connect_kwargs["client_keys"] = [_import_private_key(private_key_pem)]
        elif password is not None:
            connect_kwargs["password"] = str(password)
        else:
            return SshExecResult(ok=False, message="缺少 SSH 凭据", host=host)
        try:
            async with asyncssh.connect(**connect_kwargs) as conn:
                result = await _asyncio.wait_for(
                    conn.run(command, input=script, check=False),
                    timeout=float(timeout),
                )
                stdout = result.stdout if isinstance(result.stdout, str) else (
                    result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
                )
                stderr = result.stderr if isinstance(result.stderr, str) else (
                    result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
                )
                status = int(result.exit_status if result.exit_status is not None else -1)
                ok = status == 0
                return SshExecResult(
                    ok=ok,
                    exit_status=status,
                    stdout=stdout,
                    stderr=stderr,
                    message=ok_message if ok else f"{fail_message}（退出码 {status}）",
                    host=host,
                )
        except _asyncio.TimeoutError:
            return SshExecResult(ok=False, message=f"SSH 执行超时（{timeout}s）", host=host)
        except Exception as exc:  # noqa: BLE001
            return SshExecResult(ok=False, message=f"SSH 连接/执行失败：{exc}", host=host)

    try:
        loop = _asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: _asyncio.run(_once())).result(timeout=float(timeout) + 30)
    return _asyncio.run(_once())


def install_ip_sync_over_ssh(
    host: str,
    *,
    port: int = 22,
    username: str,
    private_key_pem: Optional[str] = None,
    password: Optional[str] = None,
    timeout: float = 120.0,
    known_hosts: Any = None,
) -> SshExecResult:
    """Install / update the 多出口 IP sync service (app.ip_sync) on the instance.

    One attempt, no retries: the script is idempotent and the operator can press
    the button again. Non-root logins go through ``sudo -n`` — OCI's stock
    ubuntu / opc users have passwordless sudo; anything else fails with a clear
    "a password is required" instead of hanging on a prompt.
    """
    from app.ip_sync import build_install_script

    if known_hosts is None:
        # Same fail-closed gate as grow_filesystem_over_ssh.
        return SshExecResult(ok=False, message="缺少已验证的 SSH 主机密钥，已拒绝连接", host=host)
    user = validate_ssh_username(username)
    result = _exec_script(
        host,
        build_install_script(),
        command="bash -s" if user == "root" else "sudo -n bash -s",
        port=port,
        username=user,
        private_key_pem=private_key_pem,
        password=password,
        known_hosts=known_hosts,
        timeout=timeout,
        ok_message="同步服务已安装",
        fail_message="同步服务安装失败",
    )
    if not result.ok and "password is required" in (result.stderr or ""):
        result.message += "：该用户执行 sudo 需要密码，请改用 root 或有免密 sudo 的用户"
    return result if result.ok else _enrich_hints(result)


def grow_filesystem_over_ssh(
    host: str,
    *,
    port: int = 22,
    username: str,
    private_key_pem: Optional[str] = None,
    password: Optional[str] = None,
    retries: int = 3,
    retry_delay_sec: float = 8.0,
    timeout: float = 120.0,
    known_hosts: Any = None,
) -> SshExecResult:
    """Upload-free: pipe grow script via bash -s over SSH, with short retries."""
    from app.fs_grow import build_grow_script

    if known_hosts is None:
        # Fail closed. asyncssh reads known_hosts=None as "trust anything", so a
        # caller that simply forgot the argument would silently hand the user's
        # private key to whatever answered on the address — the exact hole the
        # TOFU work closed. The parameter keeps its None default only because
        # making it required would break the signature; this check is the gate.
        return SshExecResult(ok=False, message="缺少已验证的 SSH 主机密钥，已拒绝连接", host=host)

    script = build_grow_script()
    last: Optional[SshExecResult] = None

    attempts = max(1, int(retries))
    for i in range(attempts):
        last = _exec_script(
            host,
            script,
            command="bash -s",
            port=port,
            username=username,
            private_key_pem=private_key_pem,
            password=password,
            known_hosts=known_hosts,
            timeout=timeout,
            ok_message="文件系统已扩展",
            fail_message="文件系统扩展失败",
        )
        if last.ok:
            return last
        if i + 1 < attempts:
            import time

            time.sleep(float(retry_delay_sec))
    assert last is not None
    return _enrich_hints(last)


def _enrich_hints(last):
    """Append a diagnosis to a failed SSH result.

    抽成独立函数是为了能单独测：判断「连接失败」还是「命令失败」的规则曾经是反的，
    而它内联在重试循环里时没有任何办法直接验证。
    """
    #
    # 连接层的诊断只能看**连接层**的信息（last.message），不能看 last.stderr ——
    # stderr 是**远端命令**的输出，能拿到它本身就证明 SSH 已经连上并认证成功了。
    # 以前两者被拼在一起做子串匹配，于是最常见的一种真实失败被彻底诊断反了：
    # fs_grow 的脚本执行 growpart / resize2fs / xfs_growfs 时**不带 sudo**，
    # 而登录名默认是 ubuntu，非 root 跑这些工具会在 stderr 打出
    # "Permission denied" —— 然后面板告诉你「请检查用户名与私钥是否匹配」。
    # 同理，客体输出里只要出现 "timeout" / "refused" 字样（日志、报错都可能），
    # 就会为一条明明连通了的连接给出网络诊断。
    hints = []
    low = last.message.lower()
    guest = (last.stderr or "").lower()
    if "timed out" in low or "timeout" in low or "超时" in last.message:
        hints.append("连接超时：请确认实例已 RUNNING、22 端口 NSG/安全列表放行，且本机可访问该 IP")
    if "auth" in low or "permission denied" in low:
        hints.append("认证失败：请检查用户名与私钥/密码是否匹配（Ubuntu 常用 ubuntu，Oracle Linux 常用 opc）")
    if "refused" in low:
        hints.append("连接被拒绝：sshd 可能未启动，或防火墙未放行 22")
    # 客体侧的权限失败是另一回事：连接是好的，是命令没有 root。
    if not hints and ("permission denied" in guest or "operation not permitted" in guest or "not permitted" in guest):
        hints.append(
            "SSH 已连接成功，是命令本身没有权限：扩容需要 root，而当前登录用户不是。"
            "请用 root 登录，或在实例内手动执行 sudo growpart / sudo resize2fs"
        )
    if hints:
        last.message = last.message + "；" + "；".join(hints)
    return last
