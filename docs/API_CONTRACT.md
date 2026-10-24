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
      "input_tokens": 2100000,
      "output_tokens": 1410203,
      "avg_tokens_per_request": 2733,
      "cost_cny": 4.2183,
      "cost_currency": "CNY"
    },
    "pricing": {
      "currency": "CNY",
      "unit": "per_1m_tokens",
      "input": 2.0,
      "output": 6.0,
      "cache_hit": 0.0667,
      "pro_multiplier": 3.0,
      "usd_cny": 7.3,
      "note": "DeepSeek 官方峰谷价按时段加权后的均价，不区分峰谷"
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

### 3.1 计价口径：DeepSeek 均价（2026-10-18 起）

**金额一律由 BFF 从 token 现算**（`backend/app/pricing.py`），**不读**日志里的 `quota` 字段 ——
历史日志是按旧倍率记的，直接加总会虚高近三个数量级。

价格 = DeepSeek 官方 2026-08-17 起的峰谷价，**按时段加权平均**（高峰 9-12 / 14-18 共 8 小时，
其余 16 小时空闲），不区分峰谷：

| 项目 | 高峰 | 空闲 | 加权均价（采用） |
|---|---|---|---|
| 输入（缓存未命中） | ¥3/M | ¥1.5/M | **¥2/M** |
| 输出 | ¥9/M | ¥4.5/M | **¥6/M** |
| 输入（缓存命中） | ¥0.1/M | ¥0.05/M | **¥0.0667/M**（= 输入的 1/30） |

`deepseek-v4-pro*` 各项 ×3；其余模型**统一按 V4-Flash 均价**（简单口径）。

计算式：`cost = (未命中输入×2 + 命中输入×0.0667 + 输出×6) / 1e6` ×（pro ? 3 : 1）

**同步到 New API**：BFF 启动时会调 `sync_model_ratios()`，把同一份价格换算成三个 option 写回：

| New API option | 值 | 说明 |
|---|---|---|
| `ModelRatio[<model>]` | `2.0 / (2×7.3) ≈ 0.136986` | 倍率 1 = $2/M；pro 为 0.410959 |
| `CompletionRatio[<model>]` | `3` | 输出价 / 输入价 |
| `CacheRatio[<model>]` | `1/30 ≈ 0.033333` | 缓存价 / 输入价 |

> ⚠️ **不配倍率的模型会被 New API 按硬编码 `37.5` 计费**（≈$75/M，高出两个数量级）。
> 所以新增模型后要重启一次 BFF（或触发启动流程）让倍率补齐。合并写入，只覆盖我们渠道里
> 出现的模型名，上游内置的 200+ 条价格原样保留。

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
      "auto_ban": true,
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
| `GET /panel-api/channels/models?type=1` | 同上结构，但只含**该类型且启用中**渠道实际挂载的模型（去重，保持渠道内顺序） |

> 已知映射（实测）：`1`=OpenAI、`14`=Anthropic。
> 带 `type` 的用法供「快速开始」按协议给出真正可用的候选；不带 `type` 时保持原行为（上游全量模型）。

### 4.7 优先级排序（按协议批量写回）

`POST /panel-api/channels/reorder`

```json
{
  "groups": [
    { "type": 1,  "ids": [15, 13, 14] },
    { "type": 14, "ids": [18, 16, 17] }
  ]
}
```

成功 `data: { "updated": [ { "id": 15, "priority": 3 }, { "id": 13, "priority": 2 }, ... ] }`

语义与约束：

- **组内第 1 个 → 优先级最高**。数值规则（2026-10-18 起）：
  - OpenAI（type 1）：正数 `n .. 1`
  - 其它协议（Anthropic type 14 等）：**负数 `-1 .. -n`**
  - 目的：New API 选渠道只看「分组 + 模型」，**不看入站协议**；OpenAI 与 Anthropic
    类型渠道若同优先级就会随机互抢（实测 OpenAI 请求被 Anthropic 渠道接走，日志里出现
    `·Anthropic`）。负数保证任何 OpenAI 渠道都排在 Anthropic 渠道之前，后者降为备用层。
- ⚠️ **写完必须重建 `abilities` 表**：`PUT /api/channel/` 只改 `channels.priority`，
  而路由真正读的是 `abilities`，不同步就会出现「面板显示优先级 3、实际按旧值随机」。
  BFF 会自动调 `POST /api/channel/fix`（`NewAPIClient.fix_abilities()`），返回里带
  `abilities: {rebuilt: true, ...}`；重建失败时会额外给 `warning`。
- 上游 New API 语义：`priority` **越大越优先**；**同一模型内，高优先级渠道未失败/未被禁用前不会使用低优先级渠道**
- 渠道因额度耗尽被上游拒绝时，`auto_ban=1` 会让 New API 把它置为 `status=3`（**自动禁用**），
  流量随即落到下一个优先级 —— 这就是「额度用完自动禁用 + 高优先级优先」的落地机制
- 两组仍按 type 分开调序（各自组内相对顺序独立），但**数值区间不重叠**：OpenAI 恒为正、其它恒为负
- 校验（防止误排）：`id` 必须存在、类型必须与所在组 `type` 一致、不得重复出现；
  违反时返回 `200 + success:false`
- 未出现在请求中的渠道**不受影响**（不会被动改优先级）

---

## 5. 令牌 `/panel-api/tokens`

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/panel-api/tokens?page=&size=` | 列表 |
| `GET` | `/panel-api/tokens/{id}/key` | **按需取回明文 key**（见 §5.1） |
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
  "quota": 0,
  "quota_used": 0,
  "unlimited_quota": true,
  "expired_time": -1,
  "created_at": 1791900000,
  "accessed_at": 1791900000,
  "today_requests": 42,
  "today_input_tokens": 18000,
  "today_output_tokens": 6200,
  "today_tokens": 24200,
  "today_cost_cny": 0.0728
}
```

> 🆕 **`today_*`（2026-10-18 新增）**：由 BFF 拉取**今日消费日志**按 `token_name` 聚合而来。
> 取不到日志时全部为 `0`（不影响列表）。
> `today_cost_cny` = 按 DeepSeek 均价从 token 现算的金额（§3.1），**不是**日志里的 `quota`。
> ⚠️ 上游单页上限 500 条，日志量大时该统计会**低估**（本项目规模足够）。

### 5.2 面板不计量：新建令牌一律不限额度（2026-10-18）

本面板定位是「自用中转站的用量看板」，**不做额度控制**：

- `POST /panel-api/tokens` 会**忽略**请求体里的 `quota` / `unlimited_quota`，
  强制以 `unlimited_quota=true, quota=0` 创建（前端新建表单也不再提供额度输入）
- 前端不再展示金额：令牌列表改为「今日请求 / 今日 Tokens」，
  看板「今日消费」改为「今日 Tokens（输入/输出拆分）」，日志去掉「消费」列
- `quota` / `quota_used` 字段仍**保留返回**（兼容旧前端），但界面不展示

> ⚠️ **重要**：令牌的 `unlimited_quota` 只解除**令牌这一层**的限制，
> New API 仍会从**用户账户**的 `quota` 扣费；账户余额不足时任何令牌都会 403
> 「余额不足」（`PreConsumedQuota=500`，余额低于 500 连小请求也过不去）。
> 因此本面板把账户额度一次性设成天文数字（1e12），见 `HANDOVER.md` §3.1。

创建请求：

```json
{ "name": "张老师", "quota": 100, "unlimited_quota": false, "expired_days": -1,
  "models": [], "group": "default" }
```

创建响应（**唯一一次明文回显**）：

```json
{ "success": true, "message": "", "data": { "id": 12, "key": "sk-4d5e6f...(48位)" } }
```

> ⚠️ **创建时的明文 key 回显（2026-10-10 新增 / 2026-10-12 修订）**
> 上游 New API 的 `POST /api/token/` **既不返回令牌 id 也不返回明文 key**，且
> 列表/详情一律只回掩码（`MaskTokenKey`，形如 `lX5l**********GrzF`）。
> BFF 在创建后按 `name` 定位新令牌，再调 `POST /api/token/{id}/key`
> （上游返回 `token.GetFullKey()`）取回明文，随创建响应返回。
>
> - `data.id`：新建令牌 id（可能为 `null`，表示未能定位）
> - `data.key`：`sk-` 前缀 + 48 位完整密钥（可能为 `null`，取回失败时）
> - `GET /tokens` 的 `key_masked` **恒为掩码**，列表不泄露明文
> - 前端仍应在创建成功后弹窗提示用户复制保存

### 5.1 按需取回明文 key

`GET /panel-api/tokens/{id}/key`

```json
{ "success": true, "message": "", "data": { "id": 7, "key": "sk-pMks...(48位)" } }
```

> **2026-10-12 实测更正**：此前认为上游明文**仅创建那一次**可取 —— 实际上
> `POST /api/token/{id}/key` 对**任意已有令牌**都会返回明文（`token.GetFullKey()`），
> 只是列表/详情接口恒为掩码。因此本接口可随时取回。
>
> - 仅供「快速开始」在弹窗内自动填入真实密钥；失败时返回 `200 + success:false`，
>   前端降级为 `YOUR_API_KEY` 占位符
> - ⚠️ 这是**管理面接口**（需登录），会通过网络传输明文 key —— 面板始终在 HTTPS 下访问。
>   若日后要收紧，可在 BFF 侧改为仅创建后 N 分钟内可取。

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
      "cache_tokens": 0,
      "quota": 6551588,
      "cost_cny": 0.000057,
      "latency_ms": 680,
      "is_stream": false,
      "content": ""
    }
  ],
  "total": 1284, "page": 1, "size": 50
}
```

> `quota` 是 **New API 的历史记账**，2026-10-18 之前按错误倍率（37.5）记的，会虚高近三个数量级；
> `cost_cny` 才是面板口径的金额（按 DeepSeek 均价从 token 现算，见 §3.1）。前端展示一律用 `cost_cny`。

### 7.2 统计

`GET /panel-api/logs/stats?days=7`

```json
{ "success": true, "data": {
    "daily": [ { "date": "2026-10-10", "requests": 1284, "tokens": 3510203, "cost_cny": 12.34 } ],
    "by_model": [ { "model_name": "deepseek-v4-flash", "requests": 800, "tokens": 2000000, "cost_cny": 8.0 } ],
    "by_channel": [ { "channel_name": "TK-LzK-openai", "requests": 640, "cost_cny": 6.0 } ],
    "total_cost_cny": 12.34,
    "currency": "CNY"
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
{ "success": true, "data": { "bff": "ok", "upstream": "ok", "upstream_latency_ms": 12, "auth": "ok", "token_expires_in": 640 } }
```

`auth` 字段（2026-10-24 新增）：
- `ok` —— 本地 token 未过期，且上游对它做了一次真实鉴权请求予以认可
- `invalid` —— 本地未过期但服务端拒绝（401/403，如会话被手动清理）；下一笔业务请求会自动重登
- `expired` —— 本地 token 已过 900 秒有效期（尚未触发重登；健康检查绝不代登录）
- `none` —— 进程内没有 token（启动后尚未登录成功）
- `unknown` —— 探测请求网络失败，无法判定
- 该字段**只用于监控观察**，健康检查本身恒返回 200（绝不因 auth 异常让容器被
  docker 反复重启 —— 重启=重新登录=加重会话问题）。
- `token_expires_in`：本地 token 剩余秒数（无 token 时为 0）。

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

### 10.1 鉴权与会话治理

- 登录：`POST /api/user/login`，响应取 `data.access_token` 与 `data.user.id`
- 后续管理请求需**同时**带：`Authorization: Bearer <token>` 与 `New-Api-User: <user_id>`
- token 有过期时间（`access_expires_at`，**实测硬编码 900 秒，无配置项**），BFF 需缓存 + 过期自动重登
- ⚠️ **登录接口有 IP 级强限流**：每 IP 每 20 分钟仅 20 次，超出返回 `429`（空 body）。
  BFF 靠 token 缓存基本不会触发；但**联调/压测时极易被限流**，遇到 429 应快速失败并给出明确提示，不要无效重试。

**会话治理（2026-10-24 实测，P0 级教训）**：

- 每次登录都会在 `user_sessions` 表新建一条记录，**30 天才过期、无自动清理**；
  每用户上限 **50 条**，打满后登录返回 `409 AUTH_SESSION_LIMIT` —— 而登录失败
  就拿不到 token，没 token 也调不了清理接口，形成自锁死局（面板全站 502）。
- BFF 的对策（`backend/app/newapi.py`）：
  1. **token 落盘持久化**（`state_store.py`，`/data/panel-state.json`）：重启复用，
     少登录就少积累会话；
  2. **登录成功后立即回收自己的历史脚本会话**：`GET /api/user/sessions` 列出，
     仅回收 `user_agent` 以 `python-httpx` / `python-requests` / `python-urllib` /
     `curl/` / `go-http-client` / `wget/` 开头且 `current=false` 的条目，逐条
     `DELETE /api/user/sessions/{sid}`；**浏览器会话（Mozilla 系）绝不触碰**；
  3. **409 自救**：旧 access_token 在会话上限打满后**依然有效**（上限只挡新建），
     直接用它执行第 2 步腾出名额再重登一次。
- ⚠️ 匹配脚本会话**只能看 user_agent，绝不能用 ip=127.0.0.1** —— 经 Caddy 反代
  进来的浏览器请求在 New API 看来同样来自 127.0.0.1，按 IP 清理会误杀浏览器会话。

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

> 补充（2026-10-12 实测）：渠道的 `PUT /api/channel/` 用**最小体 `{"id","priority"}`**
> 只改优先级，其它字段（name/models/group/weight/auto_ban/status/key）**均保持原值不变**
> —— 已在临时渠道上建→改→读回→删 全流程验证。可安全用于批量排序（§4.7）。
> 仍然禁止在 body 里带 `status`（见 §10.3）。

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

---

## 11. 出口模型清单（增强版 `/v1/models`）

> ⚠️ 本接口**不在 `/panel-api` 前缀下**，它属于客户端出口 `/v1`，
> 由 **Caddy 单独分流**到 BFF（其余 `/v1*` 仍直连 New API）。
> 见 `Caddyfile` 里的 `handle /v1/models`。

### 11.1 背景（2026-10-12 实测）

New API 自己生成的 `/v1/models` 只返回：

```json
{ "id": "glm-5", "object": "model", "created": 1626777600, "owned_by": "openai" }
```

而**上游业务网关**（`tokendance.space/gateway/v1/models`，实测**免鉴权**）其实提供了完整能力元数据：

```json
{ "id": "glm-5", "name": "Z.ai: GLM 5", "context_length": 200000,
  "description": "……", "supported_protocols": ["openai:chat-completions", "anthropic:messages"] }
```

New API 把这些字段**全丢了** → 客户端（Cherry Studio / LibreChat / 各类 agent）
因此无法自动识别**上下文长度**。本接口就是为了补回这些元数据。

### 11.2 行为

```
GET /v1/models          （必须带 API key，与原来一致）
Authorization: Bearer sk-xxx   或   x-api-key: sk-xxx
```

处理顺序：

1. BFF 用**请求原样的** `Authorization` / `x-api-key` 调 New API `/v1/models` ——
   **鉴权、计费、限流语义完全不变**；非 200 时**状态码与响应体原样透传**（无效 key 依旧 401/403）
2. 200 时，把上游元数据合并进每个模型对象（**仅补空缺，不覆盖**已有字段）：
   `context_length`、`supported_protocols`、`description`、`name`
3. 上游元数据拉取失败 → **原样返回** New API 的响应（客户端无感）

返回结构仍是标准 OpenAI 形状，只是每个模型多了上述字段：

```json
{ "object": "list", "data": [
  { "id": "glm-5", "object": "model", "created": 1626777600, "owned_by": "openai",
    "supported_endpoint_types": ["openai"],
    "context_length": 200000,
    "supported_protocols": ["openai:chat-completions", "anthropic:messages"],
    "description": "……", "name": "Z.ai: GLM 5" }
] }
```

### 11.3 配置与降级

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `UPSTREAM_MODELS_URL` | `https://tokendance.space/gateway/v1/models` | 上游模型清单地址 |
| `MODELS_META_TTL` | `600` | 元数据进程内缓存秒数 |

- 元数据**进程内缓存**，到期后台刷新；失败沿用旧缓存
- Caddy 的两个 upstream 顺序为 `127.0.0.1:18080`（BFF）→ `127.0.0.1:3000`（New API），
  `lb_policy first` + `fail_duration 30s`。
- **2026-10-12 实测的降级行为**（停掉 BFF 容器实测）：

  | 状态 | 结果 |
  |---|---|
  | BFF 正常 | `200`，108 模型，**76 个带 `context_length`** |
  | BFF 刚挂 | **第 1 个请求 502**（Caddy 靠它才发现故障） |
  | BFF 挂（其后） | `200`，回落到 New API 原生列表（**无** `context_length`） |
  | BFF 恢复后 ≤30s | 仍走回落（`fail_duration` 未过期）；之后自动回到增强版 |

  > 即：**最坏情况只会损失「第一个探测到故障的请求」**，模型清单本身不会整体不可用。
  > 之所以不用主动健康检查（`health_uri`）：两个 upstream 是异构服务，
  > BFF 的 `/panel-api/system/health` 在 New API 上不存在，统一探活会把 New API 误判为不健康。
- `/v1/models` 不在推理关键路径上（对话/补全走 `/v1/chat/completions`、`/v1/messages`，
  始终直连 New API），因此上述降级窗口可以接受。

### 11.4 关于「思考档位」与「自定义上下文长度」

- **思考档位是请求参数，不是模型属性**，因此不出现在模型清单里。上游支持：
  `reasoning_effort`（`none` 关闭、`low`/`medium`/`high` 开启）、
  `thinking: {"type":"disabled"}` / `{"type":"enabled","budget_tokens":N}`。
  实测经本出口**可正常透传**（`enable_thinking` 不透传，勿用）。
  档位梯度仅在 `glm-5` 上明显，`deepseek-v4-pro` / `qwen3.7-max` 基本只认开/关。
- **上下文长度不可自定义** —— 它是模型固有属性，硬改会 400；
  本接口只是把真实值**如实告知**客户端。真正可自定义的是**输出上限** `max_tokens`。
