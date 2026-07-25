"""Guest filesystem grow helpers (pure; no network I/O).

Used after OCI boot-volume expand to grow the root partition + filesystem
over SSH. The script is intentionally fixed (no user-controlled commands).
"""

from __future__ import annotations

from typing import Optional


def truncate_output(text: Optional[str], max_len: int = 4000) -> str:
    s = str(text or "")
    if len(s) <= max_len:
        return s
    head = max_len // 2
    tail = max_len - head - 20
    return s[:head] + "\n...[truncated]...\n" + s[-tail:]


def build_grow_script() -> str:
    """Return a bash script that grows the root partition and filesystem.

    Idempotent best-effort: detects root device via findmnt/lsblk, runs
    growpart (or cloud-guest-utils equivalent), then resize2fs or xfs_growfs.
    Exit 0 on success; non-zero with a clear message on failure.
    """
    return r"""#!/bin/bash
set -euo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"
export DEBIAN_FRONTEND=noninteractive

log() { echo "[ocibot-grow] $*"; }
fail() { echo "[ocibot-grow] ERROR: $*" >&2; exit 1; }

# Resolve root mount source (e.g. /dev/sda1, /dev/mapper/ubuntu--vg-root)
ROOT_SRC="$(findmnt -n -o SOURCE / 2>/dev/null || true)"
if [ -z "${ROOT_SRC}" ]; then
  ROOT_SRC="$(df -P / | awk 'NR==2{print $1}')"
fi
[ -n "${ROOT_SRC}" ] || fail "无法定位根分区设备"

# Resolve to a real block device if possible
if command -v lsblk >/dev/null 2>&1; then
  PKNAME="$(lsblk -no PKNAME "${ROOT_SRC}" 2>/dev/null | head -n1 | tr -d '[:space:]' || true)"
  NAME="$(lsblk -no NAME "${ROOT_SRC}" 2>/dev/null | head -n1 | tr -d '[:space:]' || true)"
  FSTYPE="$(lsblk -no FSTYPE "${ROOT_SRC}" 2>/dev/null | head -n1 | tr -d '[:space:]' || true)"
else
  PKNAME=""
  NAME=""
  FSTYPE="$(findmnt -n -o FSTYPE / 2>/dev/null || true)"
fi

# Partition number (trailing digits of the partition name)
PART_NUM=""
DISK_DEV=""
if [ -n "${PKNAME}" ] && [ -n "${NAME}" ]; then
  DISK_DEV="/dev/${PKNAME}"
  # NAME may be sda1 / nvme0n1p2 — strip disk prefix
  SUFFIX="${NAME#${PKNAME}}"
  SUFFIX="${SUFFIX#p}"
  if echo "${SUFFIX}" | grep -Eq '^[0-9]+$'; then
    PART_NUM="${SUFFIX}"
  fi
fi

# Fallback for /dev/sda1 style without lsblk PKNAME
if [ -z "${PART_NUM}" ]; then
  case "${ROOT_SRC}" in
    /dev/nvme*p[0-9]*|/dev/mmcblk*p[0-9]*)
      DISK_DEV="$(echo "${ROOT_SRC}" | sed -E 's/p[0-9]+$//')"
      PART_NUM="$(echo "${ROOT_SRC}" | sed -E 's/^.*p([0-9]+)$/\1/')"
      ;;
    /dev/[svxy]d[a-z][0-9]*|/dev/vd[a-z][0-9]*)
      DISK_DEV="$(echo "${ROOT_SRC}" | sed -E 's/[0-9]+$//')"
      PART_NUM="$(echo "${ROOT_SRC}" | sed -E 's/^[^0-9]*([0-9]+)$/\1/')"
      ;;
  esac
fi

log "root=${ROOT_SRC} disk=${DISK_DEV:-?} part=${PART_NUM:-?} fstype=${FSTYPE:-?}"

# Grow partition when we have disk+part (skip LVM/mapper-only roots without a clear part)
if [ -n "${DISK_DEV}" ] && [ -n "${PART_NUM}" ] && [ -b "${DISK_DEV}" ]; then
  if command -v growpart >/dev/null 2>&1; then
    log "running growpart ${DISK_DEV} ${PART_NUM}"
    # growpart exits 1 when already grown — treat as success
    growpart "${DISK_DEV}" "${PART_NUM}" || {
      rc=$?
      if [ "$rc" -eq 1 ]; then
        log "growpart: no change needed (already at max)"
      else
        fail "growpart failed (exit $rc)"
      fi
    }
  elif command -v parted >/dev/null 2>&1; then
    log "growpart missing; trying parted resizepart ${PART_NUM} 100%"
    parted "${DISK_DEV}" ---pretend-input-tty resizepart "${PART_NUM}" 100% || true
  else
    log "WARN: growpart/parted not found; skipping partition grow (install cloud-guest-utils)"
  fi
else
  log "WARN: skip partition grow (no clear disk/partition for ${ROOT_SRC})"
fi

# Detect filesystem if still unknown
if [ -z "${FSTYPE}" ]; then
  FSTYPE="$(findmnt -n -o FSTYPE / 2>/dev/null || true)"
fi
FSTYPE="$(echo "${FSTYPE}" | tr '[:upper:]' '[:lower:]')"

case "${FSTYPE}" in
  ext2|ext3|ext4)
    if ! command -v resize2fs >/dev/null 2>&1; then
      fail "resize2fs 未安装，无法扩展 ${FSTYPE}"
    fi
    log "running resize2fs ${ROOT_SRC}"
    resize2fs "${ROOT_SRC}"
    ;;
  xfs)
    if ! command -v xfs_growfs >/dev/null 2>&1; then
      fail "xfs_growfs 未安装，无法扩展 xfs"
    fi
    log "running xfs_growfs /"
    xfs_growfs -d /
    ;;
  btrfs)
    if command -v btrfs >/dev/null 2>&1; then
      log "running btrfs filesystem resize max /"
      btrfs filesystem resize max /
    else
      fail "btrfs 工具未安装"
    fi
    ;;
  *)
    fail "不支持的文件系统类型: ${FSTYPE:-unknown}（仅支持 ext*/xfs/btrfs）"
    ;;
esac

log "done"
df -h / || true
exit 0
"""
