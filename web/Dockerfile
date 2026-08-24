# Multi-stage: build the Vue frontend, then ship API + built SPA in one image.
# The FastAPI app serves web/frontend/dist automatically when it exists.

# ---------------------------------------------------------------- 基础镜像钉版
#
# 这三个都是可变标签：node:22-alpine 今天和下个月指向的不是同一份内容，而
# `scripts/install.sh update` 每次都跑 `compose build --pull`，也就是每次更新都
# 重新拉一遍「现在的」那一份。上游被投毒或被改动一次，当天更新的每一台安装都会
# 装上它，而这个进程持有解密所有租户 OCI 私钥的主密钥。
#
# 正确做法是钉 digest。但不要直接改这个文件 —— `install.sh update` 会
# `git reset --hard`，任何手改都会在下次更新时被悄悄还原。改成从 web/.env 传进来
# （docker-compose.yml 已把这三个作为 build args 透传），那样才能活过更新：
#
#     # web/.env
#     OCIBOT_NODE_IMAGE=node:22-alpine@sha256:<...>
#     OCIBOT_PYTHON_IMAGE=python:3.12-slim@sha256:<...>
#     OCIBOT_DOCKER_CLI_IMAGE=docker:27-cli@sha256:<...>
#
# digest 用 `docker buildx imagetools inspect <镜像标签>` 取。
ARG NODE_IMAGE=node:22-alpine
ARG PYTHON_IMAGE=python:3.12-slim
ARG DOCKER_CLI_IMAGE=docker:27-cli

FROM ${NODE_IMAGE} AS frontend
WORKDIR /src
# package-lock.json 不再是可选的（原来写 package-lock.json*）：没有它就没有完整性
# 校验，构建也就不可复现。
COPY web/frontend/package.json web/frontend/package-lock.json ./
# 只有 npm ci，没有 `|| npm install` 兜底。兜底那一版的问题不是它会失败，而是它
# **不会**失败：lockfile 和 package.json 一旦对不上（现在 package-lock.json 的根
# version 是 0.1.0，package.json 是 0.4.13），构建会静默改走一次全新的 ^ 范围解析，
# 没有任何完整性哈希，日志里也没有一行看起来像出错。宁可在这里红着停下来，让人跑
# 一次 `npm install` 把 lockfile 提交上去。
# --ignore-scripts：依赖的 install 钩子不在构建期执行任意代码。
RUN npm ci --ignore-scripts
COPY web/frontend ./
# Bust layer cache when only source files change under some builders.
ARG FRONTEND_CACHE_BUST=1
RUN echo "frontend_cache_bust=${FRONTEND_CACHE_BUST}" && npm run build

# docker CLI 从官方镜像里取，而不是 curl 一个 tgz 下来。
#
# 原来那段 `curl https://download.docker.com/.../docker-27.5.1.tgz | tar -xz` 没有
# 任何校验和或签名 —— 而这个二进制驱动的是挂进来的宿主机 docker.sock，一次被替换
# 等于宿主机 root。官方 docker:*-cli 镜像里的 /usr/local/bin/docker 就是同一个静态
# 二进制（该镜像自己也是从 download.docker.com 解出来的），区别在于走的是镜像分发：
# 内容寻址、逐层校验，而且可以用 @sha256 钉死（见上面的 DOCKER_CLI_IMAGE）。
# 顺带去掉了原来手写的 TARGETARCH → x86_64/aarch64 映射：多架构镜像自己会选对。
FROM ${DOCKER_CLI_IMAGE} AS dockercli

FROM ${PYTHON_IMAGE}

WORKDIR /app

# curl（健康检查）+ git（自更新时在宿主机仓库上执行 fetch/reset）。
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 静态二进制，直接落到 glibc 镜像里也能跑。只有开启面板内一键更新（挂载
# docker.sock，见 docker-compose.update.yml）时才会被用到；没挂 socket 时它什么
# 也做不了。
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker

COPY requirements.txt /app/requirements.txt
COPY web/backend/requirements.txt /app/web/backend/requirements.txt
COPY web/backend/requirements.lock /app/web/backend/requirements.lock
COPY scripts/trim_oci_sdk.py /tmp/trim_oci_sdk.py

# The OCI SDK installs ~404 MB (208 MB of .py plus the 195 MB of .pyc pip
# byte-compiles alongside it), of which this panel reaches ~58 MB. The other
# ~346 MB is Autonomous Database, Data Safe, GoldenGate and the like — code that
# cannot execute here. It matters because the image is rebuilt on the operator's
# own server on every update, often an Always-Free instance where the boot
# volume is the scarce resource.
#
# ONE RUN, deliberately. Layers are additive: with `pip install` and the trim as
# two instructions, the install layer keeps all 404 MB and the trim layer only
# records the deletions, so the image does not shrink by a single byte. That is
# not hypothetical — it shipped that way in 0.4.64/0.4.65 and the operator's
# image came back at 1.15 GB. tests/test_oci_sdk_trim.py now pins them together.
#
# The script imports every client afterwards and exits non-zero if a delete was
# wrong, so a mistake fails the BUILD rather than surfacing as a 502 weeks later.
#
# 装的是 requirements.lock，不是 requirements.txt。
#
# .lock 里是**全量闭包**（含 starlette / pydantic-core / cffi / uvloop 这些间接
# 依赖），每一个包都带 sha256。`--require-hashes` 会让 pip 拒绝任何哈希对不上、
# 或者根本没在锁里出现的包 —— 也就是说 `compose build --pull` 不再是「向 PyPI
# 重新解析一遍」，而是「装回同一批字节」。这个进程持有解密所有租户 OCI 私钥的
# 主密钥，上游任何一次投毒发布都不该能顺着每日更新进来。
#
# requirements.txt 仍然保留，作为人类维护的直接依赖清单（改版本改那里）；
# 改完必须重新生成锁，两者不同步时构建会失败而不是静默降级：
#     uv pip compile web/backend/requirements.txt \
#         --python-platform linux --python-version 3.12 \
#         --generate-hashes -o web/backend/requirements.lock
# 必须交叉解析到 linux/py3.12（镜像是 python:3.12-slim）；在 Windows 上直接跑
# 会漏掉 uvloop 这类平台相关依赖。
RUN pip install --no-cache-dir --require-hashes -r /app/web/backend/requirements.lock \
    && python /tmp/trim_oci_sdk.py \
    && rm -f /tmp/trim_oci_sdk.py

COPY app /app/app
COPY web/backend /app/web/backend

# 容器以只读根文件系统运行（docker-compose.yml 的 read_only: true），运行时写不了
# __pycache__。在这里先编译好，否则每次启动都要重新解析全部源码，而且每个 import
# 都会白白尝试写一次盘。
RUN python -m compileall -q /app/app /app/web/backend

COPY --from=frontend /src/dist /app/web/frontend/dist

# Build-time commit (compose / CI can override with --build-arg)
ARG OCIBOT_GIT_SHA=unknown
ENV OCIBOT_GIT_SHA=${OCIBOT_GIT_SHA}

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV OCIBOT_API_WORKERS=2
# 只读根文件系统下写不进去，写失败是静默的，只是白费一次 syscall。上面的
# compileall 已经把 .pyc 烤进镜像了。
ENV PYTHONDONTWRITEBYTECODE=1

# 非 root 运行。这个进程解密所有租户的 OCI 私钥，没有任何一步需要 root：监听的是
# 8000（非特权端口），数据在 PostgreSQL 里，上传只往 /tmp（compose 挂 tmpfs）写。
#
# 固定 UID/GID 而不是让系统分配：docker-compose.yml 里也写了同一个 user:，两处必须
# 一致，否则镜像换了 UID 而 compose 没跟上，容器会以一个不存在的用户启动。
# HOME 指到一个真实存在的目录，避免个别库调用 expanduser("~") 时炸掉。
RUN groupadd --system --gid 10001 ocibot \
    && useradd --system --uid 10001 --gid 10001 \
       --home-dir /home/ocibot --create-home --shell /usr/sbin/nologin ocibot
ENV HOME=/home/ocibot
USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=25s --retries=5 \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["python", "-m", "web.backend.run"]
