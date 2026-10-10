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

```json
// 已登录
{ "success": true, "data": { "authenticated": true, "username": "LzK", "expires_at": 1791900000 } }
// 未登录 → 返回 401（前端据此跳登录页）
```

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

> ⚠️ **实现提示**：New API 的渠道创建接口（`POST /api/channel/`）在 `v1.0.0-rc.41` 版本存在缺陷，
> 对任何请求体都返回 `channel cannot be empty`。后端须**先探测该接口是否可用**，
> 不可用时在响应中给出明确引导（见 §4.4 的降级说明），不得静默失败。

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
