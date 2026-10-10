# 交接文档（Handover）

> 面向：接手本项目的下一位开发者 / AI agent
> 更新：2026-10-10
> 阅读顺序建议：本文 → `docs/API_CONTRACT.md`（含 §10 上游坑）→ `docs/TASKS.md` → `README.md`

---

## 一、这个项目是什么

一套**自建的 AI API 中转站管理面板**，跑在用户家里的 NAS 上。

**业务背景**：4 个人共用一个上游 AI 服务商（`tokendance.space`）的账号额度，
用一个统一入口把这 4 个账号聚合成一个 API 网关，各自拿 token 用。
某人的额度耗尽时，其他人的请求要能无感继续。

**技术上的分工**：

```
浏览器/面板 → Caddy(:8443  "/" 与 "/panel-api/*") → 自建面板 BFF(FastAPI, :18080) → New API(:3000) → tokendance.space
客户端 API → Caddy(:8443  "/v1*"、"/v1beta*")     → New API(:3000, 上游引擎)      → tokendance.space
```

两路共用同一个 HTTPS 域名与证书（`api.91666.icu:8443`）：
- **面板 UI** 走 BFF（`/panel-api/*` + 静态前端）
- **客户端 API（出口）** 由 Caddy 按路径**直接**反代到 New API 引擎，BFF 不参与这部分流量
  （2026-10-10 之前 8443 整站只反代 BFF，客户端打 `/v1` 会 404 —— 即"出口其实没接上"）

- **New API**（`calciumion/new-api`）是**上游引擎**，没重写：渠道调度、失败重试、协议转换（OpenAI ↔ Anthropic）、额度计费都是它干的
- **本项目（ai-relay-panel）** = 一套自建前端 UI + 一层 FastAPI BFF，**替代 New API 自带的 Web 面板**

**为什么重写面板**：New API 原生面板功能繁杂（充值、支付、多租户、部署管理…），
而这个场景只有 4 人自用，真正需要的只是「渠道活着没 / 谁用了多少 / 调用日志」。
原生面板还带了关不掉的模块，维护成本高于自建。

> ⚠️ 关键边界：**不要试图重写 New API**。它的渠道调度/重试/协议转换已跑通且难做，
> 项目的价值在于"贴合场景的界面"，不在于重造引擎。

---

## 二、当前状态（部署实况）

### 已上线

| 项 | 状态 |
|---|---|
| 面板 | **https://api.91666.icu:8443/** —— 已可访问、可登录 |
| 面板账号 | 见 NAS 上 `/home/lzk/docker/ai-relay-panel/.env` 的 `PANEL_USER` / `PANEL_PASSWORD` |
| 后端 BFF | 容器 `ai-relay-panel`，`network_mode: host`，**仅监听 127.0.0.1:18080** |
| 前端 | 原生 HTML/CSS/JS，由 BFF 托管，挂载自 `./frontend`（改完重启容器即生效，无需重建镜像） |
| **客户端 API（出口）** | **https://api.91666.icu:8443/v1** —— 已打通；客户端 `base_url` 就填这里 |
| 应急后路 | `https://api.91666.icu:18083/` → New API 原生面板（**别删**，新面板挂了靠它） |

### 功能完成度

| 模块 | 状态 |
|---|---|
| 面板登录 / 会话 | ✅ |
| 运行看板（渠道/令牌/今日用量/上游连通性） | ✅ |
| **渠道异常告警横幅**（顶部置顶，列出渠道名+ID+原因） | ✅ |
| 渠道管理（列表/新建/编辑/启停/删除/连通性测试） | ✅ |
| 令牌管理 | ✅ |
| 调用日志（分页/筛选/统计） | ✅ |
| 系统设置（脱敏展示） | ✅ |

### ✅ 初始化已完成（2026-10-10 接手后）

**1. 上游渠道：3 条已建并测试通过**

| id | 名称 | 类型 | Base URL | 模型数 | 连通性 |
|---|---|---|---|---|---|
| 13 | 柯柯 | `1` (OpenAI) | `https://tokendance.space/gateway` | 108 | ok / 200 |
| 14 | 老陈 | `1` (OpenAI) | 同上 | 108 | ok / 200 |
| 15 | 姜姜 | `1` (OpenAI) | 同上 | 108 | ok / 200 |

- 三个人各自的 key，聚合到同一个出口
- ⚠️ 当前**都是 OpenAI 协议**；如需给 Anthropic 客户端（Claude Code 等）直连，
  要另建 type `14` 的渠道（上游按模型支持 `anthropic:messages`，见第六节）

**2. 出口入口已打通**

`https://api.91666.icu:8443/v1` 现由 Caddy 按路径直接反代到 New API 引擎。
实测：`/v1/models` 200（108 个模型）、`/v1/chat/completions` 200、未带 key 时 401。

**3. 出口令牌已建**

- `测试令牌·内部联调`（id 7，不限额度）—— **名称即备注**（用户选择用令牌名承载备注）
- 面板创建令牌后会**一次性弹出完整 key**（新增能力，见第五节坑 #11）

**4. 供应商「额度耗尽」的自动禁用**（此前已配好并验证）
- `AutomaticDisableChannelEnabled = true`
- `AutomaticDisableKeywords` 已追加 tokendance 特征串（`api_key_quota_exceeded`、`额度上限`）
- `AutomaticDisableStatusCodes` 保持默认 `401`（**没有**加 403，关键词匹配更精准、避免误伤）

### ⚠️ 仍未完成 / 需注意

**1. 邮件通知已放弃**
原计划复用 `hdu-grabber` 的 SMTP 凭证发告警邮件，SMTP 链路本身**已调试打通**，但用户决定
**不依赖邮件，改为定期查看面板**，因此做了顶部的告警横幅。
SMTP 配置仍留在 New API 里（不影响功能），如将来要用可参考第五节的坑。

**2. 令牌明文 key 只有「创建那一刻」能拿到**
列表/详情接口恒为掩码；BFF 已用 `POST /api/token/{id}/key` 在创建响应里一次性回显。
**关掉弹窗就再也取不到**（除非直接读库）—— 交接令牌时务必当场复制。

**3. New API 登录会话有上限，会拖垮整个面板**
见第五节坑 #12。联调时曾把 admin 会话堆到 50 个，导致登录 409、BFF 502。

**4. 历史测试令牌未清理**
`probe-token-2`(id1) / 空名(id2) / `__st2`(id3) / `ZZ-TEST-TOKEN-E2E`(id4) 是前人联调遗留，
尚未删除（未经用户确认）。

---

## 三、仓库与开发流程

### 仓库

- **https://github.com/lizhikeer/ai-relay-panel**（**public** —— 所以**绝不要提交任何凭证**）
- 默认分支 `main`

### 目录结构

```
ai-relay-panel/
├── backend/                # FastAPI BFF
│   ├── app/
│   │   ├── main.py         # 入口、异常处理、静态托管、启动能力探测
│   │   ├── config.py       # 环境变量（手写 .env 解析，未引 dotenv）
│   │   ├── response.py     # 统一响应包装 + 业务异常
│   │   ├── auth.py         # 面板登录态（内存 session + HttpOnly Cookie）
│   │   ├── newapi.py       # ★ New API 客户端封装（唯一 httpx 出口）
│   │   └── routers/        # auth / overview / channels / tokens / users / logs / system
│   ├── tests/test_contract.py   # 契约冒烟测试（9 项）
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/               # 原生前端（无构建步骤）
│   ├── index.html          # 登录视图 + navbar + 5 个 tab-pane
│   ├── style.css           # 深色主题，变量照抄自用户既有 rainclass 面板
│   ├── app.js              # IIFE，统一 fetch 封装 + 各页渲染
│   └── mock-server.py      # 仅标准库的 mock 后端，可无真实后端演示全部页面
├── docs/
│   ├── API_CONTRACT.md     # ★ 接口契约（含 §10 上游实测坑，改接口前必读）
│   ├── DESIGN_SPEC.md      # ★ 视觉规范（CSS 变量、组件、禁止事项）
│   ├── TASKS.md            # 三个 agent 的任务书与验收标准（历史参考）
│   ├── DEPLOY.md           # 部署文档（首次部署/更新/回滚/排障）
│   └── HANDOVER.md         # 本文
├── docker-compose.yml
├── Caddyfile               # 部署用 Caddy 配置的版本化副本
├── deploy.sh               # deploy / rollback / status
└── .env.example            # 配置模板（真实 .env 在 NAS 上，不入库）
```

### 开发与部署流程

**本地开发**（Windows，用户机器）：
```bash
# Python
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe
# 依赖
cd backend && <上面的python> -m pip install -r requirements.txt
# 跑 BFF（读 .env，本地 .env 指向 NAS 的 New API）
<上面的python> -m uvicorn app.main:app --host 127.0.0.1 --port 8080
# 契约测试
<上面的python> -m pytest tests/test_contract.py -v
```
前端可以**完全独立**开发：`python3 frontend/mock-server.py` 一条命令起 mock 后端，
浏览器打开就能看到全部页面（含登录流程）。

**部署到 NAS**：
```bash
# 1) 本地提交推送
git add -A && git commit -m "..." && git push origin main

# 2) NAS 拉取（注意：NAS → GitHub 网络不稳，见第五节坑）
ssh nas 'cd /home/lzk/docker/ai-relay-panel && git pull origin main'

# 3) 生效
ssh nas 'cd /home/lzk/docker/ai-relay-panel && docker compose up -d --build'
#   仅改前端时更简单：文件已挂载，`docker restart ai-relay-panel` 即可（甚至无需重启）
```

**回滚**：`ssh nas 'cd /home/lzk/docker/ai-relay-panel && ./deploy.sh rollback'`

---

## 四、环境与凭证

### NAS

| 项 | 值 |
|---|---|
| 登录 | **`ssh nas`**（已配 SSH config 免密；等价 `ssh lzk@192.168.0.2`） |
| 主机 | `kekeshenla`，fnOS（飞牛OS），x86，内网 `192.168.0.2` |
| 部署目录 | `/home/lzk/docker/ai-relay-panel` |
| 配置 | `.env`（在 NAS 上，**不入库**） |
| 备份 | `/home/lzk/backup-ai-relay-panel/` |

> ⚠️ **`ssh lzk@91666.icu` 不可用**：该域名只解析出公网 IPv6，从内网访问会绕出去
> （NAT Loopback 问题）。运维一律用 `ssh nas`。

### 凭证位置（不写明文，自己去取）

| 凭证 | 在哪 |
|---|---|
| New API 管理员账号密码 | NAS `/home/lzk/docker/ai-relay-panel/.env` 的 `NEWAPI_ADMIN_*` |
| 面板登录账号密码 | 同上，`PANEL_*` |
| New API root 密码 | 同上（初始化时设置） |
| SMTP 授权码 | NAS `/home/lzk/hdu-grabber/data/global.json` 的 `smtpEmail.password` |

### NAS 上的端口占用（极易混淆，务必对照）

| 端口 | 服务 | 备注 |
|---|---|---|
| **443** | **fnOS 系统 Web 管理**（自签证书 `CN=fnOS`） | ⚠️ **绝对不能动**，动了会砸掉 NAS 管理后台 |
| 3000 | New API 引擎 | host 网络，监听 `*` |
| 8090 | UpSnap（`upsnap-caddy` 容器） | |
| 8080 | `xray-camouflage-nginx` | ⚠️ 所以 BFF **不能**用 8080 |
| **8443** | **本面板主入口**（Caddy 反代 → 18080） | |
| 18080 | BFF（仅回环） | |
| 18083 | New API 原生面板后路 | |
| 40000/40001 | hdu-grabber 面板 | |

### Caddy 网关

- 容器 `upsnap-caddy`（host 网络），配置文件 `/vol4/docker/upsnap/Caddyfile`
- 证书：`*.91666.icu` 通配符（ZeroSSL），位于 `/vol4/docker/xray/certs/`
- **改配置的正确姿势**：
  ```bash
  # 1) 先校验
  docker run --rm --network host -v <新配置>:/etc/caddy/Caddyfile:ro \
    -v /vol4/docker/xray/certs:/certs:ro caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile
  # 2) 备份现行配置
  # 3) 写入后热加载（必须 --force，见第五节）
  docker exec upsnap-caddy caddy reload --config /etc/caddy/Caddyfile --force
  ```
- ⚠️ **不要给 SPA 加 Caddy `basic_auth`** —— 实测会导致面板"登录不进去"，
  见第五节。

---

## 五、必须先知道的坑（全部实测踩过）

### 5.1 New API 相关

| # | 坑 | 说明 |
|---|---|---|
| 1 | **渠道创建需要包装体** | `POST /api/channel/` 的 body 必须是 `{"mode":"single","channel":{...扁平对象...}}`；直接发扁平对象会返回 `channel cannot be empty`（曾被误判为服务端 bug，实际是调用姿势） |
| 2 | **路由尾斜杠敏感** | `/api/channel/`（带斜杠）与 `/api/channel` 行为不同，后者会被 307 跳转 |
| 3 | **渠道状态是三态** | `1`启用 / `2`手动禁用 / `3`自动禁用（**没有 0**）；改状态必须走 `POST /api/channel/{id}/status`，body `{"channel_id","status"}`，`PUT` 改不动 |
| 4 | **更新是全量覆盖** | `PUT /api/token/` 缺字段会把字段**清零**，必须先取回原值再合并 |
| 5 | **令牌 key 列表返回的是缩写** | 列表接口给的 key 只有 18 位，**真实 key 是 48 位**（建令牌时仅返回一次或从数据库取）。使用时需加 `sk-` 前缀 |
| 6 | **登录接口有 IP 级强限流** | 每 IP 每 20 分钟 20 次，超出 429。**联调/压测极易触发** |
| 7 | **`SMTPPassword` 才是密码字段** | `SMTPToken` / `SMTPSecret` 会被 API 静默接受但**不写入**（无法通过回读发现，只有实际发信才暴露） |
| 8 | **模型价格未配置会拦请求** | 真实调用会报 `模型 X 的价格未配置`。需开启「自用模式」（`SelfUseModeEnabled=true`）或在分组定价里配价格 |
| 9 | **SMTP 有未解之谜** | 587+STARTTLS 与 465+SSL 都曾报 `need EHLO and AUTH first`；后来错误变成 `notification limit exceeded`（说明发信已通、被防刷屏限流）。**最终可用组合未确认**——若将来要启用邮件，需重新验证 |
| 10 | **httpx 必须 `trust_env=False`** | 否则会把局域网请求交给环境代理（开发机有代理时必踩） |
| 11 | **令牌明文 key 只能「创建时」取** | 列表/详情恒为掩码（`MaskTokenKey`，形如 `lX5l**********GrzF`）。**唯一**能取明文的口子是 `POST /api/token/{id}/key`（返回 `GetFullKey()`，需 owner 身份）。BFF 已用它：创建后按 `name` 定位新令牌 → 取明文 → 随创建响应**一次性**回显（契约 §5）。上游存的是不带 `sk-` 的 48 位，对外统一补前缀 |
| 12 | **登录会话有每用户上限（约 50），超限会拖垮整个面板** | 超限时 `POST /api/user/login` 直接返回 **409 `AUTH_SESSION_LIMIT`**；BFF 登不进 New API → 面板所有接口 502。会话 **30 天**过期且**不自动清理**，而 BFF 每次重启都会新建一个会话（长时间开发会累积）。清理：拿任一仍有效的 admin token 调 `POST /api/user/sessions/revoke-others`（⚠️ 会**同时踢掉浏览器/原生面板**的会话）；或改 `user_sessions` 表（文件 root 属主，`sudo` 需密码，走不通） |

### 5.2 Caddy 相关

| # | 坑 | 说明 |
|---|---|---|
| 1 | **reload 需要 `--force`** | 新增监听端口时，普通 `reload` 会报 `bind: address already in use`（listener 竞态），加 `--force` 解决。它是**原子操作**，失败时保持旧配置、不会破坏现有服务 |
| 2 | **别给 SPA 加 `basic_auth`** | 只保护 `/` 会导致 SPA 加载失败（表现为"登录不进去"）；保护全部路径则每次前端跳转都要重新认证。**面板的访问控制交给应用自身 session** |
| 3 | **`header_up` 要在 `reverse_proxy` 块内** | 写在站点块层级会报 `unrecognized directive` |

### 5.3 NAS / 网络相关

| # | 坑 | 说明 |
|---|---|---|
| 1 | **NAS → GitHub 连接不稳** | `git pull` 常见 `GnuTLS recv error` / `HTTP2 framing layer` / 连接超时。可重试，或临时用 `scp` 直传文件（前端是挂载卷，传完即生效） |
| 2 | **数据库文件属主是 root** | `/home/lzk/docker/new-api/data/one-api.db` 是 `root:root`，普通用户改不了，需要 `sudo`。改完要 `docker restart new-api` 让缓存刷新 |
| 3 | **别改 `new-api` 容器** | 它是引擎，重启会短暂中断服务；改它的配置前先备份 |
| 4 | **改 Caddy 前必须备份** | `cp -a /vol4/docker/upsnap/Caddyfile /vol4/docker/upsnap/Caddyfile.bak-$(date +%Y%m%d-%H%M%S)` |

### 5.4 开发机（Windows）相关

| # | 坑 | 说明 |
|---|---|---|
| 1 | **本机挂着代理（TUN 模式）** | DNS 被劫持返回 fake-ip（`198.18.x.x` / `2001:2::x` / `fdfe:dcba:9876::2`）→ **从本机 curl 内网域名的结果不可信**，验证一律在 NAS 上做 |
| 2 | **pip 需要走代理** | `pip install --proxy http://127.0.0.1:<port>` |
| 3 | **Bash 工具 PATH 可能丢失** | 命令找不到时先 `export PATH=...PortableGit...:/c/Windows/System32:/usr/bin:/bin` |
| 4 | **PowerShell 工具不回显 stdout** | 别用它跑 ssh/curl，用 Bash 工具 |

---

## 六、下一步待办（按优先级）

1. **把出口分发给使用者**：在面板「令牌管理」逐个新建（**名称即备注**），
   创建后**立即复制完整 key** 交给对方，客户端 `base_url = https://api.91666.icu:8443/v1`
2. （可选）**给每人加一条 Anthropic 协议渠道**（type `14`），兼容 Claude Code 等客户端。
   上游按模型支持 `anthropic:messages`：glm-5 / deepseek-v4-pro / kimi-k2.5 / qwen3.7-max /
   mimo-v2.6-pro 等**支持**；seed-2.1-pro、glm-4.7 **不支持**（报「模型 X 不支持协议」）
3. （可选）**清理历史测试令牌**：`probe-token-2` / 空名 / `__st2` / `ZZ-TEST-TOKEN-E2E`
4. （可选）**验证额度耗尽的端到端行为**：等某条渠道真的耗尽时，确认自动禁用 + 横幅提示
5. （可选）**路由器加 `api.91666.icu → 192.168.0.2` 静态映射**
   —— 现在内网访问该子域会走公网 IPv6 绕出（NAT Loopback），体验不佳
6. （可选）**重新评估邮件通知**：若用户改主意，需先解决 5.1#9 的 SMTP 谜题

---

## 七、给下一位 agent 的建议

1. **先读 `docs/API_CONTRACT.md`**，特别是 §10「上游 New API 实现细节与已知约束」——
   那里沉淀了所有实测坑，能省你几小时。
2. **改接口前先改契约文档**，因为前端按契约写、BFF 按契约实现，两边都依赖它。
3. **验证一律在 NAS 上做**（`ssh nas`），开发机的代理环境会让网络测试结果失真。
4. **不要动的东西**：fnOS 的 443、`new-api` 容器的既有配置、`hdu-grabber` 的配置
   （那是用户在用的另一个服务）、Caddy 里 `:18083` 的后路。
5. **前端改动很轻**：无构建、文件挂载，`scp` 上去就生效，不用重建镜像。
6. **用户的沟通偏好**：务实、要证据（命令输出），不喜欢含糊的"应该可以"；
   他会自己提需求，所以**把现状和约束讲清楚比替他想需求更重要**。
