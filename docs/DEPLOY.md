# 部署指南（DEPLOY）

> 项目：`ai-relay-panel`（自建 AI 中转站管理面板）
> 目标环境：NAS `192.168.0.2`（fnOS，x86_64，Docker 28.x + compose v2），登录 `ssh nas`
> 部署目录：`/home/lzk/docker/ai-relay-panel`

## 0. 架构与端口

```
浏览器 ──https──> Caddy(upsnap-caddy, host 网络)
                   ├─ :8443  → 127.0.0.1:18080  ai-relay-panel BFF（面板 + /panel-api/*）
                   ├─ :18083 → 127.0.0.1:3000   New API 原生面板（应急后路）
                   └─ :8090  → 127.0.0.1:18090  UpSnap

ai-relay-panel BFF(FastAPI, host 网络) ──> New API 容器(127.0.0.1:3000) ──> 上游
```

| 端口 | 用途 | 归属 |
|---|---|---|
| 8443 | 面板主入口（`https://api.91666.icu:8443`） | Caddy → BFF 18080 |
| 18083 | New API 原生面板应急入口 | Caddy → 3000 |
| 18080 | BFF 监听端口 | 容器 `ai-relay-panel` |
| 3000 | New API 引擎 | 容器 `new-api`（**勿动**） |
| 8090 | UpSnap | Caddy → 18090 |
| 443 | fnOS 系统 Web | **勿动** |
| 8080 | xray 伪装 nginx | **勿动** |

> ⚠️ 18080 段是本机约定：BFF=18080、应急=18083、UpSnap=18090。**不要用 8080**。

### 0.1 本次切换的影响面（重要）

`:8443` 从 New API 原生面板(3000) 切到 BFF(18080) 后：

- ✅ `https://api.91666.icu:8443/` 面板、`/panel-api/*` 接口 由 BFF 提供。
- ⚠️ **`https://api.91666.icu:8443/v1/*`（原中转 API 客户端入口）不再由 New API 处理**，
  会被 BFF 当作静态资源 → 返回 `404 {"success":false,"message":"Not Found"}`。
  因为 BFF 只实现 `/panel-api/*` 与静态前端，不转发 `/v1`。
- ✅ 中转 API 客户端如需继续调用，请改用 **`https://api.91666.icu:18083/v1/*`**（应急入口 → 3000），
  该入口同时可用于 New API 原生管理面板。
- 现状：该项目 New API 当前**渠道数=0、令牌数=0**，即无在用中转客户端，故此次切换无实际中断。
  若日后需要 `:8443/v1` 继续可用，应在 BFF 增加 `/v1` 转发（改后端，非本部署职责）或在 Caddyfile 中
  为 `:8443` 增加 `handle /v1/*` 反代到 3000 的规则（需先改契约）。

---

## 1. 首次部署

### 1.1 准备
```bash
ssh nas
mkdir -p /home/lzk/docker
cd /home/lzk/docker
# 方式A：NAS 可访问 GitHub 时
git clone https://github.com/lizhikeer/ai-relay-panel.git
# 方式B：NAS 不可访问 GitHub（本环境实测超时）→ 从开发机 scp 整个目录
#   scp -r ai-relay-panel nas:/home/lzk/docker/
cd /home/lzk/docker/ai-relay-panel
```

### 1.2 创建 `.env`（含真实凭证，**绝不入库**）
```bash
cd /home/lzk/docker/ai-relay-panel
cp .env.example .env
SECRET="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
cat > .env <<EOF
NEWAPI_BASE_URL=http://127.0.0.1:3000
NEWAPI_ADMIN_USER=LzK
NEWAPI_ADMIN_PASSWORD='<New API 管理员密码>'
PANEL_USER=LzK
PANEL_PASSWORD='<面板登录密码>'
PANEL_SESSION_SECRET=$SECRET
LISTEN_PORT=18080
EOF
chmod 600 .env
```

### 1.3 执行部署
```bash
sudo chmod +x deploy.sh   # 或 bash deploy.sh
./deploy.sh deploy
```
脚本依次完成：构建镜像 → `up -d` → 等待 `127.0.0.1:18080` 健康 → **caddy validate** →
**备份现网 Caddyfile** → **原地写入并 `caddy reload --force`** → 端到端校验（8443/18083/8090/3000）。

### 1.4 验证
```bash
./deploy.sh status
curl -ks https://api.91666.icu:8443/panel-api/system/health   # {"success":true,"data":{"bff":"ok",...}}
```
浏览器打开 `https://api.91666.icu:8443/`，用面板账号登录。

---

## 2. 更新（发布新版本）

```bash
ssh nas
cd /home/lzk/docker/ai-relay-panel
git pull            # 或从开发机 scp 覆盖代码
./deploy.sh deploy  # 重新构建并热加载，零停机
```

- **只改前端**（`frontend/*`）：挂载即生效，`docker restart ai-relay-panel` 即可，无需重建镜像。
- **改后端**（`backend/**`）：必须 `./deploy.sh deploy` 重新构建镜像。
- **改 Caddy**（`Caddyfile`）：`./deploy.sh deploy`（会 validate→备份→reload），或手动走 §4 的流程。

---

## 3. 回滚

### 3.1 回滚 Caddy（最快恢复管理入口）
```bash
cd /home/lzk/docker/ai-relay-panel
./deploy.sh rollback                 # 恢复最新备份（一般是切换前的新面板/原生面板配置）
./deploy.sh rollback /vol4/docker/upsnap/Caddyfile.bak-20261010-120000   # 指定某份备份
```
脚本会：校验备份 → 原地恢复 → `caddy reload --force` → 验证 `:8443` 与 `:8090` 可访问。
回滚后 `:8443` 会回到备份时的目标（通常是 New API 原生面板 3000）。再次上线执行 `./deploy.sh deploy`。

> 回滚前脚本会把「当前配置」另存为 `Caddyfile.bak-pre-rollback-<时间戳>`，可再次前进。

### 3.2 回滚后端容器
```bash
cd /home/lzk/docker/ai-relay-panel
docker compose down            # 停掉本面板（不影响 new-api / upsnap-caddy）
./deploy.sh rollback           # 把 Caddy 切回原生面板
```
备份文件位于 `/vol4/docker/upsnap/Caddyfile.bak-*`。

---

## 4. 手动运维参考（deploy.sh 已封装，排障时用）

### 4.1 改 Caddyfile 的正确姿势（顺序不可乱）
```bash
# ① 校验候选（拷进容器临时目录，不影响线上）
docker cp ./Caddyfile upsnap-caddy:/tmp/Caddyfile.candidate
docker exec upsnap-caddy caddy validate --adapter caddyfile --config /tmp/Caddyfile.candidate
# ② 备份现网
cp -p /vol4/docker/upsnap/Caddyfile /vol4/docker/upsnap/Caddyfile.bak-$(date +%Y%m%d-%H%M%S)
# ③ 原地写入（必须原地！见下方说明）
cat ./Caddyfile > /vol4/docker/upsnap/Caddyfile
# ④ 热加载（不是 restart）
docker exec upsnap-caddy caddy reload --config /etc/caddy/Caddyfile --force
```
> ⚠️ **必须原地写入**（`cat > file`）。`/vol4/docker/upsnap/Caddyfile` 是被单文件 bind mount
> 到容器 `/etc/caddy/Caddyfile` 的。若用 `mv`/`rm` 替换文件，会换掉 inode，容器内看到的仍是旧文件
> （或空的），reload 无效。**禁止 `docker restart upsnap-caddy`**：会中断 UpSnap，且可能因监听套接字竞态启动失败。

### 4.2 查看日志
```bash
docker logs --tail 100 -f ai-relay-panel        # BFF
docker exec upsnap-caddy tail -n 50 /var/log/caddy/*.log 2>/dev/null || docker logs --tail 50 upsnap-caddy
```

---

## 5. 排障

| 现象 | 可能原因 | 处理 |
|---|---|---|
| **401** 打开面板立刻跳登录 / 接口全 401 | 未登录属正常；若登录后仍 401：浏览器未带 cookie（跨站/`credentials` 未 include）、或 `PANEL_SESSION_SECRET` 变了导致服务端重启后 session 失效 | 重新登录；检查前端 `fetch` 带 `credentials:'include'`；确认 `.env` 的 secret 未变 |
| 登录报**用户名或密码错误** | `.env` 的 `PANEL_USER/PANEL_PASSWORD` 与输入不符；或 `.env` 未生效 | 改 `.env` 后 `docker compose up -d` 重建容器；`docker exec ai-relay-panel env \| grep PANEL_` 核对 |
| **502** `:8443` 打开报错/接口 502 | Caddy 反代目标不可达：BFF 未起、端口写错、或 New API(3000) 挂了（health 里 `upstream:"down"`） | `curl -s 127.0.0.1:18080/panel-api/system/health`；`docker ps` 看容器；`curl -s 127.0.0.1:3000/api/status` |
| 面板能开但数据全空/概览为 0 | BFF 登不上 New API（密码错/被限流 429） | `docker logs ai-relay-panel \| grep -i 'login\|429\|限流'`；核对 `NEWAPI_ADMIN_*`；429 需等 20 分钟 |
| **证书**错误（`NET::ERR_CERT_*`） | 证书路径 `/certs/91666.icu.pem(.key)` 不在容器内，或证书过期 | `docker exec upsnap-caddy ls -l /certs`；`openssl x509 -enddate -noout -in /vol4/docker/xray/certs/91666.icu.pem` |
| **端口占用**（`bind: address already in use`） | 18080/18083 被占（如误用 8080 与 xray 冲突） | `ss -tlnp \| grep -E ':(18080\|18083\|8080)'`；换端口须同步改 `.env`/compose/Caddyfile |
| Caddy reload 报 `config is unchanged` | 正常（内容没变）；报 `connection refused` 说明候选配置有误 | 先 validate；`caddy reload ... --force` 可强制重载 |
| UpSnap 打不开 | 误用了 `docker restart upsnap-caddy` 且监听竞态失败 | `docker logs upsnap-caddy`；用 `caddy reload` 而非 restart；必要时 `docker start upsnap-caddy` |
| 静态前端 404（`/` 返回 JSON 而非页面） | `frontend` 未挂载到容器内 `/frontend` | `docker exec ai-relay-panel ls /frontend`；确认 compose 的 `./frontend:/frontend:ro` |
| `:8443/v1/*` 返回 404 | 预期行为：BFF 不转发 `/v1`（见 §0.1） | 中转客户端改用 `https://api.91666.icu:18083/v1/*` |

### 快速自检清单
```bash
curl -s  127.0.0.1:18080/panel-api/system/health   # BFF + 上游
curl -ks https://api.91666.icu:8443/panel-api/system/health   # 经 Caddy 的 BFF
curl -s  127.0.0.1:3000/api/status                 # New API 引擎
curl -ks https://api.91666.icu:18083/api/status    # 应急原生面板
curl -ks https://127.0.0.1:8090/                   # UpSnap
```

---

## 6. 硬性禁区（造成生产事故的根因）

1. ❌ 不要 `docker stop/rm/restart`、重建或修改 **`new-api`** 容器 —— 它必须持续在 3000 提供服务。
2. ❌ 不要占用/修改 **fnOS 的 443**（系统 Web 管理）。
3. ❌ 不要用 `docker restart upsnap-caddy` —— 一律用 `caddy reload --force`。
4. ❌ 不要 `mv`/`rm` `/vol4/docker/upsnap/Caddyfile` —— 必须原地写入。
5. ✅ 永远保留 **`:18083`** 原生面板后路，避免新面板故障时彻底失去管理入口。
6. ❌ `.env` 含明文凭证，**禁止提交到 git**（已在 `.gitignore`）。
