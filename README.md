# sticky-mi-sync

**微软便笺 ⇄ 小米笔记 双向同步器。**

电脑上用 Windows 便笺、手机上用系统自带的小米笔记，这个服务在中间把两边对齐。
**两端都不需要装任何第三方 App** —— 它直接对接各自官方服务。

纯 Python 标准库实现（唯一的可选依赖 `migate` 用于小米凭据自动续期），
单容器部署，一条数据卷搞定持久化。

---

## 它做什么

| 能力 | 说明 |
|---|---|
| **双向同步** | 便笺改了推小米，小米改了推便笺。按**内容哈希**判断差异，不依赖时间戳 |
| **删除传播** | 一端删除，另一端跟着删；但若另一端改过，则**把被删的重建回来**（保数据） |
| **冲突处理** | 两侧都改过时，以微软便笺为准，把小米侧版本另存为 `[冲突副本]`，谁也不丢 |
| **回收站** | 页面删除**和**云端删除都能恢复 |
| **批量操作** | 列表可复选，支持批量删除、批量同步成两侧一致 |
| **状态灯** | 每条记录左右两盏灯，直观显示两端是否一致 |
| **定时同步** | 后台轮询，间隔可调；也可手动触发、可随时中止 |
| **凭据持久化** | 登录态存数据卷，容器重建不丢 |

---

## 快速开始（Docker）

```bash
git clone https://github.com/dxfong/sticky-mi-sync.git
cd sticky-mi-sync
docker compose up -d --build
```

然后打开 `http://<这台机器的IP>:8787`。

> **验证容器健康**（不只是 Up，要看 healthy）：
> ```bash
> docker compose ps
> ```
> 探针探的是 `/api/auth/status`（免鉴权端点）。若显示 `unhealthy`，
> 用 `docker compose logs` 看日志。

---

## 首次使用

**部署后是全新的初始化状态** —— 镜像里不含任何凭据，需要你现场登录一次。

按顺序做五件事：

### 1. 设置访问密码

打开页面 → 会看到「第一次使用，先给这个服务设一个访问密码」→ 设一个 ≥8 位的密码。

这一步是防止同网段的人随便打开你的页面读笔记正文。

### 2. 登录小米笔记（扫码）

在页面顶部点 **「账号与登录」** → 展开面板 → 点小米卡片里的 **「扫码登录」**，
用小米手机 App 扫码确认。

> ⚠️ **容器 / 服务器环境只能用扫码登录**。页面里的「浏览器登录」走的是
> Playwright + 本机 Chrome/Edge，容器里没有桌面环境，点了必然失败。

**扫码后请务必装 `migate`**（Docker 镜像里已经装了）。它让服务用 `passToken`
**自动续期**，也就是说这一次扫码管长期；不装的话 Cookie 过期就得重新扫码。

### 3. 选择要同步的文件夹

小米卡片里点开文件夹下拉框，选中目标文件夹（例如「便笺」）。点开后会自动拉取最新列表。

### 4. 登录微软便笺（粘贴凭据）

**这台机器上如果已经登录过便笺**（比如你日常用的电脑），最省事：

```bash
python -m notesync.export_creds
```

它会打印一段文本（含微软凭据 + 小米 Cookie + 文件夹 ID），**整段复制**，
粘到页面微软卡片的「导入并验证」框里 → 点按钮 → 看到「导入成功，已读到 N 条便笺」即可。

**为什么是粘贴而不是扫码登录**：NotesFabric 用的 `MSAuth1.0 usertoken` 由
**便笺网页版在页面里生成**，OAuth / 设备码都换不出来（设备码换到的是 Graph 令牌，
那条通道读不到 2026 年的便笺）。所以在没有浏览器 profile 的新环境里，
从已经登录好的机器把凭据搬过来是**唯一可行**的方式。

手动获取方式：在已登录的浏览器打开
[便笺网页版](https://outlook.live.com/mail/notes) → F12 → Network →
点开任意便笺 → 找一条发往 `substrate.office.com` 的请求 →
抄下 `Authorization` 和 `x-anchormailbox` 两个请求头，一起粘进去。

> **想用设备码登录？** 那要把通道切成 `real`（设置里改 `graph.mode`）。
> 但它**读不到 2026 年的便笺** —— 数据不全，所以默认不用它。

### 5. 开启自动同步

顶部最右侧的 **「自动同步」** 按钮点一下即可。

> **建议先把轮询间隔改大**：设置 → 间隔(秒)。默认 5 秒适合本机短时使用，
> 但服务器是 7×24 长时间跑，5 秒 ≈ **每天 1.7 万次请求**，小米侧风控风险高。
> **建议改成 30~60 秒。**

顶部的 **「只读拉取两端列表」** 可以随时手动刷新一次两端列表（不写任何数据）。

---

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `SMS_HOST` | `127.0.0.1` | 监听地址。**容器里必须设 `0.0.0.0`**，否则端口映射不进来 |
| `SMS_PASSWORD` | 无 | 启动时设置/重置访问密码。**只在需要时用，用完就删** —— 环境变量对 `docker inspect` 是明文可见的 |
| `MIGATE_SESSION_DIR` | `~/.migatesession` | migate 的会话文件位置。容器里指到数据卷，否则重建就丢 |
| `TZ` | 系统 | 时区。影响日志时间与「下次同步」显示 |

`docker-compose.yml` 里已经配好前三个。

---

## 数据与持久化

**全部状态都在一个卷里**：`sms-data` → 容器内 `/app/data`

| 文件 | 内容 |
|---|---|
| `state.db` | 映射表、凭据、访问密码哈希、日志、回收站 |
| `config.json` | 同步间隔、冲突策略等 |
| `migatesession/` | 小米 passToken 会话 |

```bash
# 备份
docker run --rm -v sms-data:/d -v "$PWD:/b" alpine tar czf /b/sms-backup.tgz -C /d .

# 清空重来（★ 会删掉全部凭据与映射）
docker compose down -v
```

容器重建、`docker compose down && up -d`（**不带 `-v`**）都不会丢数据。

---

## 忘记访问密码

在**运行这个服务的机器**上执行：

```bash
# 本机
python server.py --set-password

# 容器
docker compose exec -it sticky-mi-sync python server.py --set-password
```

会提示输入新密码（**内容不进 shell 历史**）。修改后**所有已登录的浏览器会被踢下线**。

> 刻意不提供 `--password` 参数 —— 命令行参数会留在 shell 历史和 `ps` 里。

---

## 本地运行（不用 Docker）

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements-optional.txt   # Windows
# source .venv/bin/activate && pip install -r requirements-optional.txt   # Linux/macOS

python server.py --port 8787
```

Windows 上也可以直接双击 `start-server.bat`。

> **用 `http://127.0.0.1:8787` 打开，不要用 `localhost`** ——
> Windows 上 `localhost` 解析到 IPv6 `::1`，而服务默认只监听 IPv4，
> 浏览器可能连不上。也别直接双击 `web/index.html`（那是 `file://`，
> 请求会被 CORS 拦掉）。

---

## 安全说明

- 服务默认**只监听本机**（`127.0.0.1`）。要在局域网/容器访问，才设 `SMS_HOST=0.0.0.0`。
- 所有 `/api/*` 都需要登录，只放行 `/api/auth/status|login|setup` 三个端点。
- 密码用 **PBKDF2-HMAC-SHA256**（26 万次迭代）哈希后存储，不存明文。
- 会话令牌只存 **sha256 摘要**，cookie 是 `HttpOnly` + `SameSite=Lax`。
- 登录失败节流：同 IP 连续 5 次失败锁 5 分钟。
- **CORS 只对 loopback 来源放开**。所以必须在 `127.0.0.1` / `localhost` 上打开页面 ——
  这是有意的：别的来源读不到你的数据。
- 跨网访问**请套 HTTPS 反代**（密码在纯 HTTP 下是明文传输的），
  并在反代层给 cookie 补 `Secure`。

---

## 常见问题

**页面打不开 / 点登录报 `Failed to fetch`**
后端没连上，或页面不是从 `http://127.0.0.1:8787` 打开的。
页面顶部会出现红色横幅，写明「页面来源」和「已尝试的地址」，照着排查。

**容器一直是 `unhealthy`**
看 `docker compose logs`。若日志里 `/api/auth/status` 返回 200 却仍不健康，
检查是不是把端口映射写错了。

**小米这边看不到文件夹**
首次拉取需要翻页才能拿到文件夹列表（接口只在最后一页返回 `folders`）。
如果一直为空，点一次「只读拉取两端列表」。

**小米凭据经常过期**
装 `migate`（镜像里已装）。没有它就只能手工粘贴 Cookie，且过期要重贴。

**微软卡片只有「粘贴凭据」，没有设备码登录**
这是**有意**的。NotesFabric 的 `MSAuth1.0 usertoken` 由便笺网页版生成，OAuth 拿不到它，
设备码换到的是 Graph 令牌（读不到 2026 年的便笺）。所以在没有浏览器 profile 的
容器 / 服务器里，从已登录的机器把凭据搬过来是唯一可行方式 —— 见「首次使用」第 4 步。

**点「导入并验证」说"微软拒绝了这次请求"**
按提示的四条排查：令牌过期（重新导一次）、账号不对、复制不完整（usertoken 很长）、
或者**源机器上的便笺网页版本身也已经掉登录了**（回那台机器刷新一下网页版再导）。

**小米扫码一直停在「获取二维码…」**
说明这台机器连不上 `i.mi.com`（20 秒会超时并给出提示）。改用「粘贴 Cookie」登录，
或检查容器网络。

**构建时卡在 `Connection to pypi.org timed out`**
国内直连 PyPI 会超时。换成本地镜像源即可 —— 不用改文件，命令行传一下：

```bash
PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple docker compose up -d --build
```

也可以直接改 `docker-compose.yml` 里的 `PIP_INDEX`（常用源见文件内注释：
清华 / 阿里 / 腾讯 / 中科大）。

**构建时卡在拉取 `python:3.13-slim`**
同理，给 Docker 本身配镜像加速（iStoreOS 一般在「Docker → 镜像加速」里配）。

**同步会自动暂停**
连续失败 3 轮会自动暂停。看日志里的失败原因。

---

## 开发

```
server.py            HTTP 服务 + JSON API + 后台同步线程
web/index.html       单文件前端（vanilla JS）
notesync/
  engine.py          三方比较 + 四方决策（同步核心）
  store.py           SQLite：映射表 / 基线 / 凭据 / 日志 / 回收站
  graph.py           微软便笺：MockGraph / RealGraph / NotesFabricGraph
  notesfabric.py     NotesFabric 协议（云端全量可读写）
  xiaomi.py          小米笔记：MockXiaomi / RealXiaomi
  auth.py            访问密码 + 会话
  textutil.py        文本规范化（幂等映射的基石）
devtools/
  check_js.py        检查前端内联 JS 语法 / 漏写连接符
```

改完前端跑一下：

```bash
python devtools/check_js.py
```

### 设计上的几个要害

- **判变化只能比内容哈希，不能比时间戳。** 时间戳会被同步动作本身刷新。
- **便笺不是标题的容器。** 便笺 → 小米设 `subject` = 首行；小米 → 便笺**只用 content**，
  否则每轮都会往正文里插一次标题。
- **删除传播必须有安全阀。** 一侧清空时，上百条映射会被解读成「用户删了」→
  反过来删另一端（会同步到云端，真丢数据）。所以单轮删除量超阈值就拦下整轮。
- **写入限速。** 请求到达速率过快会触发小米风控。有限速、单轮写入上限、配额冷却三重保护。

---

## License

MIT
