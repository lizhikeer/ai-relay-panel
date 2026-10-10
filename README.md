# ai-relay-panel

自建 AI API 中转站的**自定义管理面板**。

## 这是什么

NAS 上跑着一个 New API（`calciumion/new-api`）作为中转站后端引擎，把多个上游账号聚合为一个统一入口。
本项目的目标是**替换 New API 自带的 Web 面板**，换成一套自建的、更贴合实际使用场景的管理界面。

```
浏览器 → Caddy(:8443) → ai-relay-panel BFF(FastAPI) → New API(:3000) → 上游 tokendance
                        └ 静态前端(原生 HTML/CSS/JS)
```

## 架构决策

| 层 | 选择 | 理由 |
|---|---|---|
| **后端引擎** | **保留 New API** | 渠道调度、失败重试、协议转换（OpenAI ↔ Anthropic）、SMTP 告警、计费额度这些能力已验证可用，不复造 |
| **BFF 层** | FastAPI（Python）| 把 New API 的 admin token 藏在服务端；聚合多个接口给前端一次拿全；面板自身的登录态 |
| **前端** | 原生 HTML/CSS/JS | 与 NAS 上既有的 rainclass 面板技术栈一致：无构建步骤、改完即生效、依赖最少 |
| **入口** | Caddy 反代 | 复用 NAS 现有 `*.91666.icu` 通配符证书 |

> **为什么不用 New API 原生面板**：其面板功能繁杂（充值、支付、多租户、部署管理），
> 而本站只有 4 人自用，真正需要的只是「渠道活着没 / 谁用了多少 / 日志」。
> 原生面板还带了无法关闭的模块，长期维护成本高于自建。

## 目录结构

```
ai-relay-panel/
├── backend/            # FastAPI BFF
│   ├── app/
│   │   ├── main.py         # 应用入口、静态文件托管
│   │   ├── config.py       # 配置（New API 地址、admin token、面板凭证）
│   │   ├── auth.py         # 面板登录态（session cookie）
│   │   ├── newapi.py       # New API 客户端封装（唯一出口）
│   │   └── routers/        # 各业务域路由
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/           # 原生前端
│   ├── index.html
│   ├── style.css
│   └── app.js
├── docs/
│   ├── API_CONTRACT.md # ★ BFF 接口契约（前后端并行开发的依据）
│   ├── DESIGN_SPEC.md  # ★ 视觉设计规范
│   └── TASKS.md        # 任务分工与验收标准
├── docker-compose.yml
├── Caddyfile
└── deploy.sh
```

## 快速开始

见 [`docs/DEPLOY.md`](docs/DEPLOY.md)。

## 文档索引

| 文档 | 用途 |
|---|---|
| [`docs/HANDOVER.md`](docs/HANDOVER.md) | **交接文档** —— 现状、环境、凭证位置、所有实测坑。接手前先读这个 |
| [`docs/API_CONTRACT.md`](docs/API_CONTRACT.md) | 接口契约（前后端并行开发的唯一依据，含 §10 上游坑） |
| [`docs/DESIGN_SPEC.md`](docs/DESIGN_SPEC.md) | 视觉规范（CSS 变量、组件、禁止事项） |
| [`docs/DEPLOY.md`](docs/DEPLOY.md) | 部署 / 更新 / 回滚 / 排障 |
| [`docs/TASKS.md`](docs/TASKS.md) | 三个 agent 的任务书与验收标准（历史参考） |

## 接口契约

**开发前必读**：[`docs/API_CONTRACT.md`](docs/API_CONTRACT.md)、[`docs/DESIGN_SPEC.md`](docs/DESIGN_SPEC.md)。
