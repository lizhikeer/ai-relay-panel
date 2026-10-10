# BFF 接口契约（API Contract）

> **本文件是前后端并行开发的唯一依据。**
> 后端按此实现，前端按此调用。任何一方需要改动，必须先改本文件再动代码。

- 版本：v1
- 状态：**冻结**（2026-10-10）
- 基础前缀：`/panel-api`
- 数据格式：JSON，字段名统一 `snake_case`

---

## 1. 通用约定

### 1.1 统一响应包装

**所有**接口（含错误）都返回同一层包装：

```json
{
  "success": true,
  "message": "",
  "data": { }
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `success` | bool | 业务是否成功 |
| `message` | string | 失败原因（给用户看的中文），成功时为空串 |
| `data` | any | 业务数据；失败时为 `null` |

### 1.2 HTTP 状态码

| 状态码 | 含义 |
|---|---|
| 200 | 请求已被处理（**业务失败也返回 200**，靠 `success=false` 区分） |
| 401 | **未登录**（未携带有效 session）→ 前端应跳登录页 |
| 403 | 已登录但无权限（本面板暂不使用） |
| 422 | 请求参数格式错误（FastAPI 校验失败） |
| 502 | **上游 New API 不可达或返回异常** |
| 500 | BFF 内部错误 |

> ⚠️ 关键：业务层错误（如「渠道名重复」「测试失败」）一律用 `200 + success:false`，
> **只有认证与上游故障才用非 200**。前端拦截器按此区分「跳登录」与「弹提示」。

### 1.3 认证

- 机制：**HttpOnly Cookie**，名称 `panel_session`
- 登录成功后由服务端 `Set-Cookie` 下发，前端**不需要**也不应该手动携带 token
- 前端所有请求需带 `credentials: 'include'`
- session 有效期 7 天，滑动续期
- 前端收到 `401` → 跳转登录视图

### 1.4 分页约定

分页接口统一参数与返回：

请求 query：`?page=1&size=50`（`page` 从 1 开始）
```json
{ "items": [], "total": 0, "page": 1, "size": 50 }
```

---

## 2. 认证 `/panel-api/auth`

### 2.1 登录

`POST /panel-api/auth/login`

```json
// 请求
{ "username": "LzK", "password": "***" }
// 成功 data
{ "username": "LzK", "expires_at": 1791900000 }
```

失败：`200 + success:false, message:"用户名或密码错误"`

### 2.2 登出

`POST /panel-api/auth/logout` → 清除 cookie。`data: null`

### 2.3 会话探测

`GET /panel-api/auth/session`

> **本接口永不返回 401**。未登录时返回 `200 + authenticated:false`，
> 避免浏览器控制台出现无意义的 401 红色错误日志（其他受保护接口仍用 401）。

```json
// 已登录
{ "success": true, "data": { "authenticated": true, "username": "LzK", "expires_at": 1791900000 } }
// 未登录
{ "success": true, "data": { "authenticated": false } }
```

前端启动时调用它决定进入登录视图还是主界面。

---

## 3. 概览 `/panel-api/overview`

`GET /panel-api/overview` —— 首页看板一次性拿全，避免前端并发多个请求。

```json
{
  "success": true,
  "data": {
    "channels": {
      "total": 8,
      "enabled": 7,
      "disabled": 1,
      "abnormal": [ { "id": 3, "name": "TK-张老师-openai", "reason": "额度耗尽" } ]
    },
    "tokens": { "total": 4, "enabled": 4, "disabled": 0 },
    "today": {
      "requests": 1284,
      "tokens": 3510203,
      "quota_spent": 12.34,
      "quota_currency": "CNY"
    },
    "upstream": {
      "name": "tokendance.space",
      "reachable": true,
      "latency_ms": 95
    },
    "panel": { "version": "0.1.0", "uptime_seconds": 86400 }
  }
}
```

> 字段缺失时给安全默认值（如 `0` / `[]`），**不要**返回 `null`，避免前端判空爆炸。

---

## 4. 渠道 `/panel-api/channels`

> ℹ️ **关于渠道创建（2026-10-10 实测更正）**
> New API `v1.0.0-rc.41` 的 `POST /api/channel/` **是可用的**，但它要求**包装结构**请求体：
> ```json
> { "mode": "single", "channel": { ...扁平渠道对象... } }
> ```
> 若直接发送扁平的渠道对象，会因为顶层 `channel` 字段缺失而返回 `channel cannot be empty`——
> 这是**调用姿势问题，不是服务端缺陷**（此前版本的本文档曾误判为缺陷，此处已更正）。
>
> **契约对外形状不变**（前端仍按 §4.3 发扁平对象），包装由 BFF 在 `newapi.py` 内完成。
> BFF 仍须保留非破坏性的能力探测：万一上游版本变化导致不可用，应返回明确的错误引导而非静默失败。

### 4.1 列表

`GET /panel-api/channels?page=1&size=50&keyword=&status=`

```json
{
  "items": [
    {
      "id": 1,
      "name": "TK-LzK-openai",
      "type": 1,
      "type_name": "OpenAI",
      "base_url": "https://tokendance.space/gateway",
      "status": 1,
      "status_text": "启用",
      "priority": 0,
      "weight": 1,
      "group": "default",
      "models": ["spark-x2.5-4b"],
      "model_count": 1,
      "balance_updated_at": 1791900000,
      "last_test_at": 1791900000,
      "last_test_ok": true,
      "last_test_latency_ms": 320
    }
  ],
  "total": 8,
  "page": 1,
  "size": 50
}
```

### 4.2 详情

`GET /panel-api/channels/{id}` → 单个渠道对象（结构同上，另含 `key_masked`）

`key_masked` 规则：`sk-abcd****wxyz`（保留前 6 后 4，中间用 `****`），**绝不返回明文 key**。

### 4.3 创建

`POST /panel-api/channels`

```json
{
  "name": "TK-LzK-openai",
  "type": 1,
  "base_url": "https://tokendance.space/gateway",
  "key": "sk-***",
  "models": ["spark-x2.5-4b"],
  "group": "default",
  "priority": 0,
  "weight": 1
}
```

成功 `data: { "id": 9 }`

失败示例：`200 + success:false, message:"创建失败：上游接口返回 channel cannot be empty（New API rc.41 已知缺陷，请改用面板内【从模板批量创建】或手工在 New API 中建立）"`

### 4.4 更新 / 删除 / 启停

| 方法 | 路径 | 说明 |
|---|---|---|
| `PUT` | `/panel-api/channels/{id}` | 请求体同创建，字段可部分提供 |
| `DELETE` | `/panel-api/channels/{id}` | `data: null` |
| `POST` | `/panel-api/channels/{id}/status` | `{ "status": 1 }` 1=启用 0=禁用 |

### 4.5 连通性测试

`POST /panel-api/channels/{id}/test`

```json
// 请求
{ "model": "spark-x2.5-4b" }
// 响应
{ "success": true, "data": {
    "ok": true, "latency_ms": 320,
    "status_code": 200, "message": "测试成功"
} }
```

失败时 `data.ok=false` 并带 `status_code`（如 `403`）与 `message`。

### 4.6 元数据

| 路径 | 返回 |
|---|---|
| `GET /panel-api/channels/types` | `[{ "id": 1, "name": "OpenAI" }, { "id": 14, "name": "Anthropic" }, ...]` |
| `GET /panel-api/channels/models` | `[{ "id": "spark-x2.5-4b", "owned_by": "..." }]` |

> 已知映射（实测）：`1`=OpenAI、`14`=Anthropic。

---

## 5. 令牌 `/panel-api/tokens`

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/panel-api/tokens?page=&size=` | 列表 |
| `POST` | `/panel-api/tokens` | 创建 |
| `PUT` | `/panel-api/tokens/{id}` | 更新 |
| `DELETE` | `/panel-api/tokens/{id}` | 删除 |
| `POST` | `/panel-api/tokens/{id}/status` | `{ "status": 1 }` |

令牌对象：

```json
{
  "id": 1,
  "name": "张老师",
  "key_masked": "sk-abcd****wxyz",
  "status": 1,
  "status_text": "启用",
  "quota": 100,
  "quota_used": 12.34,
  "unlimited_quota": false,
  "expired_time": -1,
  "created_at": 1791900000,
  "accessed_at": 1791900000
}
```

创建请求：

```json
{ "name": "张老师", "quota": 100, "unlimited_quota": false, "expired_days": -1,
  "models": [], "group": "default" }
```

---

## 6. 用户 `/panel-api/users`

`GET /panel-api/users` → `{ "items": [ { "id":1, "username":"LzK", "role":100, "role_text":"管理员", "quota":100000000, "quota_used":0, "status":1 } ], "total": 1 }`

---

## 7. 日志 `/panel-api/logs`

### 7.1 列表

`GET /panel-api/logs?page=1&size=50&type=&keyword=&start_ts=&end_ts=`

```json
{
  "items": [
    {
      "id": 12345,
      "created_at": 1791900000,
      "type": 2,
      "type_text": "消费",
      "username": "LzK",
      "token_name": "LzK",
      "model_name": "deepseek-v4-flash",
      "channel_name": "TK-LzK-openai",
      "prompt_tokens": 23,
      "completion_tokens": 10,
      "quota": 0.012,
      "latency_ms": 680,
      "is_stream": false,
      "content": ""
    }
  ],
  "total": 1284, "page": 1, "size": 50
}
```

### 7.2 统计

`GET /panel-api/logs/stats?days=7`

```json
{ "success": true, "data": {
    "daily": [ { "date": "2026-10-10", "requests": 1284, "tokens": 3510203, "quota": 12.34 } ],
    "by_model": [ { "model_name": "deepseek-v4-flash", "requests": 800, "tokens": 2000000, "quota": 8.0 } ],
    "by_channel": [ { "channel_name": "TK-LzK-openai", "requests": 640, "quota": 6.0 } ]
} }
```

---

## 8. 系统 `/panel-api/system`

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/panel-api/system/health` | BFF 与上游连通性（**免登录**，供容器健康检查） |
| `GET` | `/panel-api/system/info` | 版本、上游地址、启动时间 |
| `GET` | `/panel-api/system/options` | 关键设置（**脱敏**，`*_secret`/`*_key` 一律返回 `"***"`） |
| `PUT` | `/panel-api/system/options` | 更新设置，请求体 `{ "key": "value" }` |

`GET /panel-api/system/health` 响应（**无需登录**）：
```json
{ "success": true, "data": { "bff": "ok", "upstream": "ok", "upstream_latency_ms": 12 } }
```

---

## 9. 前端实现要点（给前端 agent）

1. **统一 fetch 封装**：拦截 401 → 切登录视图；`success:false` → toast 提示 `message`。
2. **不要在前端拼接 New API 原始字段**：只用本契约定义的字段，后端负责转换。
3. **轮询而非长连接**（v1）：概览每 15s、日志每 5s 拉一次即可，简单可靠。
4. **渠道 key 永不回显明文**：编辑渠道时 key 输入框留空表示「不修改」。

---

## 10. 上游 New API 实现细节与已知约束

> 以下均为 2026-10-10 在 `new-api v1.0.0-rc.41` 上**实测**得出的结论，
> BFF 实现必须据此处理。写在这里避免后来者重复踩坑。

### 10.1 鉴权

- 登录：`POST /api/user/login`，响应取 `data.access_token` 与 `data.user.id`
- 后续管理请求需**同时**带：`Authorization: Bearer <token>` 与 `New-Api-User: <user_id>`
- token 有过期时间（`access_expires_at`），BFF 需缓存 + 过期自动重登
- ⚠️ **登录接口有 IP 级强限流**：每 IP 每 20 分钟仅 20 次，超出返回 `429`（空 body）。
  BFF 靠 token 缓存基本不会触发；但**联调/压测时极易被限流**，遇到 429 应快速失败并给出明确提示，不要无效重试。

### 10.2 路由尾斜杠敏感

`/api/channel/`（带斜杠）与 `/api/channel` 行为不同——后者会被 Go `ServeMux` 用 `307` 跳回带斜杠版本。
BFF 调用时**必须带尾斜杠**，否则可能拿到 307 空响应体。

### 10.3 渠道状态有三态，需映射

| New API 值 | 含义 |
|---|---|
| `1` | 启用 |
| `2` | 手动禁用 |
| `3` | 自动禁用（额度耗尽/连续失败触发）|

**没有 `0`**。而契约对外是 `1`(启用) / `0`(禁用)，BFF 须做双向映射（面板 `0` → 上游 `2`）。

另外：
- `PUT /api/channel/` **改不动 status**，必须走 `POST /api/channel/{id}/status`，
  body 为 `{"channel_id": id, "status": 1|2}`（传 `{"status":0}` 会报 `Invalid parameters`）。
- 渠道创建请求体需包装为 `{"mode":"single","channel":{...}}`（见 §4 提示）。

### 10.4 更新是「全量覆盖」语义（危险）

`PUT /api/token/` 若请求体缺字段，**会把缺的字段清零**（实测会清空 name/quota/expired_time）。
BFF 更新令牌时必须**先取回原值再合并**。
渠道更新在 `key` 留空时不下发该字段（契合 §9.4）。
令牌状态更新走 `PUT /api/token/?status_only=true` + `{"id","status"}`。

### 10.5 渠道明文 key 无法回取

上游渠道列表/详情中 key 已被置空，`POST /api/channel/{id}/key` 返回 `403 SECURITY_PROOF_REQUIRED`。
因此 BFF 用**进程内缓存 + 掩码**：
- 本进程创建/更新过的渠道 → 可显示 `sk-abc****abcd`
- BFF 启动前就存在的渠道 → 只能回落为 `****`

因此 §4.2 的「key_masked 保留前 6 后 4」对**历史渠道**无法满足，前端不得依赖该字段判断 key 是否正确。

### 10.6 httpx 必须关闭环境代理

httpx 默认 `trust_env=True`，会读取 `HTTP_PROXY`/`HTTPS_PROXY`。
本项目目标是局域网地址（如 `192.168.0.2`），走代理会得到错误结果。
**`app/newapi.py` 的 httpx client 必须显式 `trust_env=False`。**

### 10.7 字段语义说明

| 字段 | 说明 |
|---|---|
| 令牌 `quota` / `quota_used` | 上游是**内部整数单位**（`remain_quota`/`used_quota`，`QuotaPerUnit=500000`），不是货币。契约示例中的 `100 / 12.34` 仅为形状示意；货币换算由前端决定 |
| `overview.upstream.name` | 取「首个渠道 base_url 的 host」，无渠道时回落 New API host |
| `overview.today` / `logs.stats` | 由 BFF 拉取消费日志后在服务端聚合（`page_size` 上限 500，超量会低估）。4 人自用足够 |
| `system.options` | 返回 `{key: value}` 字典；脱敏按「key 后缀为 secret/key/password/token/cert」判定 |

### 10.8 未定义的细节（前端按宽松方式处理即可）

- `/channels/{id}/test` 的 `model` 省略/为空 → BFF 取渠道首个模型
- 令牌 `expired_days`（相对）与 `expired_time`（绝对）并存时，前端以 `expired_time` 为准
- 分页 `size` 上限建议 100，前端不应请求更大的值
