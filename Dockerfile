# sticky-mi-sync —— 微软便笺 ⇄ 小米笔记 双向同步器
#
# 设计要点：
#   · 后端只用 **Python 标准库**（migate 是可选依赖，这里装上，
#     让小米侧能"一次登录、长期自动续期"，否则容器里没法用浏览器登录）
#   · 只挂一个卷：/app/data —— 凭据、数据库、备份、日志全在里面。
#     容器删掉重建，登录态和同步状态都还在。
#
# 构建：
#   docker build -t sticky-mi-sync .
# 运行（或直接用 docker-compose.yml）：
#   docker run -d --name sticky-mi-sync -p 8787:8787 \
#     -e SMS_HOST=0.0.0.0 -v sms-data:/app/data sticky-mi-sync

FROM python:3.13-slim

# 时区：日志和"下次同步"都按本地时间看，容器默认 UTC 会让人困惑
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# 先装依赖（单独一层，改代码不会让依赖重装）
COPY requirements.txt requirements-optional.txt ./
# migate：小米侧的 passToken 自动续期。容器里没有浏览器，
# 首次登录走网页扫码（/api/xiaomi/qr/*），之后靠它自动换新。
RUN pip install --no-cache-dir -r requirements-optional.txt

# 代码
COPY . .

# 数据目录：**必须挂出来**，否则容器一删凭据就没了。
# 里面会有：state.db（映射/凭据/日志）、config.json、backups/、
#   browser_profile/（Windows 上才有内容；容器里走扫码登录，用不到）
VOLUME ["/app/data"]

# ★ 容器里必须监听 0.0.0.0，否则端口映射不进来（代码里默认 127.0.0.1）
ENV SMS_HOST=0.0.0.0
ENV PYTHONUNBUFFERED=1
# migate 的会话文件默认写在 ~/.migatesession（主目录，**不在数据卷里**），
# 容器重建就丢。指到数据卷里，保证"所有数据持久化"成立。
ENV MIGATE_SESSION_DIR=/app/data/migatesession
EXPOSE 8787

# 健康检查：探 /api/auth/status —— 它在服务端白名单里，**未登录也返回 200**。
# ★ 别探 /api/state：那个需要登录，未登录取到 401，而 urllib 遇到 4xx/5xx 会抛
#   HTTPError → 命令 exit 1 → 容器被标记 unhealthy。表现出来就是
#   docker ps 一直显示 (unhealthy)，让人以为服务坏了，其实只是探针打错端点。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8787/api/auth/status',timeout=4)" || exit 1

CMD ["python", "-u", "server.py", "--port", "8787"]
