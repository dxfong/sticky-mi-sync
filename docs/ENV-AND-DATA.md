# 环境变量 · 数据目录 · 备份

面向需要"调参 / 迁移 / 排障"的场景。日常使用不需要看这份。

---

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `SMS_HOST` | `0.0.0.0` | 监听地址。想**只允许本机**访问就设成 `127.0.0.1` |
| `SMS_PORT` | `8787` | 监听端口（也可以在页面的「设置」里改） |
| `SMS_DATA_DIR` | `./data` | 数据目录（容器里是 `/app/data`） |
| `SMS_PASSWORD` | 空 | 设定/重置访问密码。**只在和现有密码不一致时才写**，所以不会每次重启都踢掉会话 |
| `PIP_INDEX` | 官方 PyPI | 构建时的 pip 源。国内建议换成镜像（见 `docker-compose.yml` 内注释） |
| `MIGATE_SESSION_DIR` | `~/.migatesession` | migate 的会话文件位置（Dockerfile 里指向数据卷） |

---

## 数据目录里有什么

整个数据目录是**唯一需要持久化的东西**：

| 文件 / 目录 | 内容 | 丢了会怎样 |
|---|---|---|
| `state.db` | **全部核心状态**：映射表 / 内容基线 / 凭据 / 日志 / 回收站 | ⚠️ 映射丢失 → 两端记录会被当成"互不相关"，可能触发重复新建 |
| `config.json` | 端口 / 同步间隔 / 各种策略开关 | 回到默认值，重新设一遍即可 |
| `migatesession/` | migate 的登录态文件 | 影响小米侧续期，重新登录即可 |
| `browser_profile/` | 浏览器登录态（**只有本机跑过"浏览器登录"才有**） | 无影响（容器里本来就没有） |

> **最关键的是 `state.db`。** 它保住了，映射关系就在；丢了虽然不会丢笔记内容
> （内容在云端），但两端会退化成"各有一份"，需要重新用「按内容配对」认亲。

---

## 备份

页面右上角有「**备份**」按钮 —— 会导出一份全量 JSON（两端全部记录 + 配对状态 + 配置）。

**⚠️ 不含任何凭据**（这是有意的：备份文件可能被随意存放）。所以恢复后要重新登录。

命令行备份整个数据目录也行：

```bash
docker run --rm -v sticky-mi-sync_sms-data:/d -v "$PWD":/b alpine \
  tar czf /b/sms-backup.tgz -C /d .
```

---

## 清空重来

页面上的「清空后台数据」按钮**已按需求移除**。需要时直接调接口：

```bash
# 只清映射（保留凭据 / 配置 / 文件夹选择）——「清空一端后重新配对」用这个
curl -X POST http://127.0.0.1:8787/api/reset?what=links

# 其余取值：creds（清凭据）/ xiaomi（清小米）/ all（全清）
```

> ⚠️ **`/api/reset` 的 `what` 是白名单校验的**，传别的值会直接 400。
> 这是故意的 —— 以前没有校验时，手滑传错参数会落到兜底分支把凭据清掉。

**清空小米笔记后要重新同步的话，必须先清映射**（`what=links`）。
否则本地上百条映射会指向已不存在的小米笔记，被引擎解读成"小米侧删除了"，
**反过来把便笺也删掉**（会同步到微软云端，真丢数据）。

---

## 忘记访问密码

**这是唯一一层"忘了就进不去"的密码**（微软 / 小米账号不受影响，它们存在 `state.db` 里）。

```bash
# 本机
python server.py --clear-password

# 容器
docker exec sticky-mi-sync python server.py --clear-password
```

清掉后重新打开页面会要求你**重新设一个**。

---

## 网络受限环境（容器能上国内网、上不了国外网）

**症状**：容器里 `i.mi.com` 通，但所有 `*.microsoft.com` / github / cloudflare
全部 TCP 超时 —— 而**宿主机直连是通的**。这是 Docker bridge 网络的问题。

**解决**：让容器用宿主网络。建一个 `docker-compose.override.yml`
（**不必改仓库文件**，Compose 会自动合并）：

```yaml
services:
  sticky-mi-sync:
    network_mode: host
    ports: !reset []
```

> `ports: !reset []` 是 Compose 2.24+ 的语法，用来清掉继承来的端口映射
> （host 模式下端口已由宿主机提供，再映射会冲突）。

**构建时卡在 `Connection to pypi.org timed out`**：换 pip 镜像源
```bash
PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple docker compose up -d --build
```

**构建时卡在拉 `python:3.13-slim`**：给 Docker 本身配镜像加速
（iStoreOS 一般在「Docker → 镜像加速」里配）。
