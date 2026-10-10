# 任务分工与验收标准

> 三个 agent 并行/串行协作。**每个 agent 动手前必须先读**：
> - `docs/API_CONTRACT.md`（接口契约，冻结版）
> - `docs/DESIGN_SPEC.md`（视觉规范）
>
> 契约是唯一依据。**任何一方认为契约有问题，应先改契约再改代码，并在 PR 说明中标注。**

---

## 背景（三个 agent 都需知道）

NAS（`192.168.0.2`，fnOS，x86）上已部署好：

| 组件 | 状态 |
|---|---|
| New API 后端引擎 | `~/docker/new-api/`，容器 `new-api`，`network_mode: host`，监听 `127.0.0.1:3000` |
| Caddy 入口 | 容器 `upsnap-caddy`（host 网络），配置 `/vol4/docker/upsnap/Caddyfile`，`:8443` 反代到 `127.0.0.1:3000` |
| 域名 | `api.91666.icu:8443`，通配符证书 `*.91666.icu` 位于 `/vol4/docker/xray/certs/` |
| 上游 | `https://tokendance.space/gateway`（NAS 可直连，无需代理） |
| New API 管理凭证 | 账号 `LzK`，密码见部署方提供 |

**本项目要做的**：把 Caddy 的 `:8443` 从「直接反代 New API 原生面板」改为「反代新的面板 BFF」。

---

## Agent A — 后端 BFF（FastAPI）

### 职责

实现 `docs/API_CONTRACT.md` 中的**全部** `/panel-api/*` 接口，作为浏览器与 New API 之间的唯一桥梁。

### 技术约束

- Python 3.11+，FastAPI + uvicorn
- 依赖仅限：`fastapi`、`uvicorn[standard]`、`httpx`、`pydantic`（不引入 ORM、不引入额外中间件框架）
- **不持久化**：面板不建数据库，会话存内存即可（4 人自用，重启丢登录可接受）
- 所有对 New API 的调用集中在 `app/newapi.py`，其他模块不得直接 `httpx` 发请求

### 目录要求

```
backend/
├── app/
│   ├── main.py          # FastAPI 实例、CORS、静态文件托管(../frontend)、异常处理器
│   ├── config.py        # 环境变量读取(New API 地址/凭证、面板账号密码、监听端口)
│   ├── auth.py          # 登录、session 生成/校验、依赖注入 require_login
│   ├── newapi.py        # ★ New API 客户端封装（唯一出口，含 token 自动登录)
│   └── routers/
│       ├── auth.py  overview.py  channels.py  tokens.py  users.py  logs.py  system.py
├── requirements.txt
├── Dockerfile           # python:3.11-slim, 非 root 运行
└── tests/
    └── test_contract.py # 契约冒烟测试(见验收标准)
```

### 关键实现要求

1. **New API admin token 管理**：BFF 启动时用配置的账号密码调 `/api/user/login` 拿 `access_token`，
   缓存并在过期时自动重登。该 token **只存在于服务端内存**，绝不下发。
2. **统一响应包装**：按契约 §1.1。业务失败 → `200 + success:false`；未登录 → `401`；上游故障 → `502`。
3. **key 脱敏**：任何返回渠道/令牌的地方，key 一律 `sk-abcd****wxyz`，**不得出现明文**。
4. **渠道接口缺陷降级**：New API `rc.41` 的 `POST /api/channel/` 有缺陷（任何请求体都返回
   `channel cannot be empty`）。实现时**先探测可用性**：
   - 若不可用 → 返回 `200 + success:false`，`message` 明确指出该缺陷并引导替代方案（契约 §4.3）
   - 不得因此让整个服务启动失败或静默吞错
   - 需在日志中打印一次性告警
5. **超时与重试**：对 New API 的请求超时 10s；上游不可达返回 `502` 而非 500。
6. **配置项**（环境变量）：
   ```
   NEWAPI_BASE_URL=http://127.0.0.1:3000
   NEWAPI_ADMIN_USER=LzK
   NEWAPI_ADMIN_PASSWORD=<部署时注入>
   PANEL_USER=LzK
   PANEL_PASSWORD=<部署时注入>
   PANEL_SESSION_SECRET=<随机>
   LISTEN_PORT=8080
   ```

### 验收标准（必须逐条自测并在交付说明中给出证据）

| # | 检查项 | 通过标准 |
|---|---|---|
| A1 | 服务可启动 | `uvicorn app.main:app` 无异常，`GET /panel-api/system/health` 返回 `{"bff":"ok","upstream":"ok"}` |
| A2 | 契约覆盖 | `docs/API_CONTRACT.md` 中每个接口都已实现（列出对照表） |
| A3 | 未登录拦截 | 未带 cookie 请求 `/panel-api/overview` → **401** |
| A4 | 登录流程 | `POST /panel-api/auth/login` 正确凭证 → `200 success:true` 且 `Set-Cookie: panel_session`；错误凭证 → `200 success:false` |
| A5 | 会话探测 | 带 cookie `GET /panel-api/auth/session` → `authenticated:true` |
| A6 | key 脱敏 | 渠道列表/详情响应中**不存在**任何以 `sk-` 开头的完整明文（用 `grep -E 'sk-[a-z0-9]{30,}'` 校验为 0 命中） |
| A7 | 上游故障处理 | 把 `NEWAPI_BASE_URL` 指向死地址 → 接口返回 **502** 且 `success:false`，服务不崩溃 |
| A8 | 渠道测试接口 | `POST /panel-api/channels/{id}/test` 能返回 `latency_ms` 与真实 `status_code` |
| A9 | Docker 构建 | `docker build ./backend` 成功，镜像可 `docker run` |
| A10 | 契约测试 | `tests/test_contract.py` 可运行（pytest），覆盖 A3–A8 |

### 明确不做

- ❌ 不做用户注册/多租户/支付
- ❌ 不做前端页面
- ❌ 不改 New API 源码
- ❌ 不引入数据库

---

## Agent B — 前端面板（原生 HTML/CSS/JS）

### 职责

按 `docs/DESIGN_SPEC.md` 实现面板 UI，**只调用** `docs/API_CONTRACT.md` 定义的 `/panel-api/*`。

### 技术约束

- **原生 HTML/CSS/JS**，无构建步骤、无 CDN 依赖、无框架
- 文件仅三个：`frontend/index.html`、`frontend/style.css`、`frontend/app.js`
- 不使用 ES module `import`（保持 `file://` 也能打开调试）；用 IIFE 或普通函数组织
- 所有网络请求走统一 `api()` 封装（含 401 跳登录、`success:false` 弹 toast）

### 页面要求

按 DESIGN_SPEC §4 的 5 个标签页：

| 标签 | 核心功能 |
|---|---|
| 运行看板 | 概览卡片组 + 异常渠道列表；15s 自动刷新 |
| 渠道管理 | 表格（名称/类型/状态/模型数/延迟/操作）；新建/编辑弹窗；测试按钮显示耗时；启停；删除（二次确认）|
| 令牌管理 | 表格（名称/状态/已用额度）+ 新建/编辑/删除 |
| 调用日志 | 表格 + 分页 + 类型/关键词筛选；5s 自动刷新（可暂停）|
| 系统设置 | 只读展示上游信息与关键设置；面板账号修改入口（若契约未提供则灰置并注明）|

### 关键实现要求

1. **登录视图**：未登录时只显示登录卡片（用户名/密码），登录后进入主界面。
2. **顶部常驻徽章**：上游连通性、渠道启用数/总数、今日请求数。
3. **表格空态**：「暂无数据」；加载态骨架或「加载中...」；错误态显示 message。
4. **编辑渠道时 key 输入框留空 = 不修改**（契约 §9.4）。
5. **所有渲染值需转义**，防止上游返回内容里带 HTML 导致 XSS。
6. **不打印敏感信息**：`console` 中不得出现 key/token 明文。

### 验收标准

| # | 检查项 | 通过标准 |
|---|---|---|
| B1 | 三文件交付 | 仅 `index.html` / `style.css` / `app.js`，无其他依赖文件 |
| B2 | 视觉一致 | 使用 DESIGN_SPEC §1 的 CSS 变量；**页面中不出现** `#fff`/`white` 等浅色背景 |
| B3 | 登录流程 | 未登录只能看到登录视图；登录成功后进入主界面；登出回到登录视图 |
| B4 | 401 处理 | 模拟 401 → 自动回到登录视图并提示 |
| B5 | 5 个标签页 | 全部可切换，无 JS 报错 |
| B6 | Mock 可独立验收 | 提供一个 `frontend/mock-server.py`（仅用标准库 `http.server`）返回契约样例数据，使前端可在**无真实后端**时演示全部页面 |
| B7 | 响应式 | 768px 宽度下导航堆叠、表格可横滚，不出现横向溢出整个页面 |
| B8 | 无外部请求 | 打开 DevTools Network，除 `/panel-api/*` 与本机资源外**无任何第三方请求** |
| B9 | 无控制台错误 | 全流程操作中 Console 无 error |

### 明确不做

- ❌ 不碰后端代码
- ❌ 不引入 npm/vite/任何构建链
- ❌ 不直接调用 New API 的 `/api/*`（必须走 BFF）

---

## Agent C — 集成与部署

### 职责

把 A 与 B 组装成可部署产物，并真正部署到 NAS，完成端到端验收。

> 依赖：A、B 完成后开始（可先做 C1/C2 的配置编写，最后统一验证）。

### 交付物

1. `docker-compose.yml` —— 定义 `ai-relay-panel` 服务（构建 `./backend`，挂载 `./frontend` 为静态资源，`network_mode: host`，监听 8080）
2. `Caddyfile` —— 更新 `/vol4/docker/upsnap/Caddyfile`：`:8443` 反代目标从 `127.0.0.1:3000` 改为 `127.0.0.1:8080`，保留 `flush_interval -1` 与 `X-Forwarded-Proto`
3. `deploy.sh` —— 一键部署脚本（构建 → 起容器 → 热加载 Caddy → 健康检查），含**回滚**分支
4. `docs/DEPLOY.md` —— 部署文档（含回滚步骤、常见故障）
5. `.env.example` —— 配置模板（不含真实凭证）

### 硬性约束

- ⚠️ **不要动 `new-api` 容器**：它必须继续在 3000 端口提供引擎能力
- ⚠️ **不要动 fnOS 的 443**（系统 Web 管理占用）
- ⚠️ Caddy 改动必须：**先 validate → 再备份 → 最后 `caddy reload --force`**（不是 restart）
- ⚠️ 部署前备份 `/vol4/docker/upsnap/Caddyfile`
- ⚠️ 保留 New API 原生面板的应急访问路径（例如用另一个端口直接指向 3000），不要完全切断

### 验收标准

| # | 检查项 | 通过标准 |
|---|---|---|
| C1 | 容器起来 | `docker ps` 显示 `ai-relay-panel` 为 Up，`curl -s 127.0.0.1:8080/panel-api/system/health` 返回 `bff:ok` |
| C2 | Caddy 生效 | `curl -k https://api.91666.icu:8443/panel-api/system/health` 返回 200 且为 BFF 响应 |
| C3 | UpSnap 未受影响 | `curl -k https://127.0.0.1:8090/` 仍返回 200 |
| C4 | New API 未受影响 | `curl -s 127.0.0.1:3000/api/status` 仍返回 200 |
| C5 | 端到端登录 | 通过 `https://api.91666.icu:8443/` 能用面板账号登录并看到概览数据 |
| C6 | 重启自愈 | `docker restart ai-relay-panel` 后 30s 内健康检查恢复 |
| C7 | 回滚可用 | 执行 `deploy.sh rollback` 能恢复到改动前的 Caddy 配置且站点可访问 |
| C8 | 文档完整 | `docs/DEPLOY.md` 含：首次部署、更新、回滚、排障（401/502/证书） |

---

## 协作与交付流程

1. **分支**：每个 agent 在自己分支开发（`feat/backend`、`feat/frontend`、`feat/deploy`）
2. **提交**：提交信息用中文或英文均可，但须说明「改了什么 + 为什么」
3. **交付说明**：每个 agent 结束时输出一份简短报告，包含：
   - 完成的验收项逐条打勾 + 证据（命令与输出片段）
   - 未完成项及原因
   - 发现的契约问题或阻塞
4. **不越界**：严格遵守各自「明确不做」清单
