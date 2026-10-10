# 视觉设计规范（Design Spec）

> 目标：与 NAS 上既有的 **rainclass 面板**保持一致的观感——深色、克制、信息密度高。
> 前端 agent 必须**直接复用**下述 CSS 变量，不要自创配色。

- 参照实现：`/home/lzk/rainclass-assistant/src/web/static/style.css`（同款变量来源）
- 适配场景：本面板以「表格 + 表单 + 状态徽章」为主

---

## 1. CSS 变量（照抄，勿改值）

```css
:root {
  /* 背景层级 */
  --bg-primary:   #0f1419;   /* 页面底 */
  --bg-secondary: #192734;   /* 卡片 */
  --bg-tertiary:  #22303c;   /* 卡片内嵌块 / 表头 */
  --border-color: #2f3336;
  --border-light: #38444d;

  /* 主色 */
  --accent-blue:  #1d9bf0;
  --accent-hover: #1a8cd8;

  /* 文字 */
  --text-primary:   #f7f9f9;
  --text-secondary: #8899a6;
  --text-muted:     #657786;

  /* 状态色 */
  --status-green:  #17bf63;
  --status-red:    #e0245e;
  --status-yellow: #ffad1f;
  --status-purple: #794bc4;

  /* 圆角 */
  --radius-sm: 6px;
  --radius-md: 10px;
  --radius-lg: 14px;

  --font-mono: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
}
```

**状态色语义**（必须遵守）：

| 变量 | 用于 |
|---|---|
| `--status-green` | 启用 / 成功 / 健康 |
| `--status-red` | 禁用 / 失败 / 额度耗尽 |
| `--status-yellow` | 警告 / 待生效 / 重试中 |
| `--status-purple` | 中性信息 / 处理中 |

> ⚠️ 注意：这里的红绿是**状态语义**，与本项目金融场景的涨跌色无关。

---

## 2. 页面骨架

```
┌────────────────────────────────────────────────┐
│ .navbar（sticky, backdrop-filter: blur(10px)）  │
│  .nav-container                                 │
│    .brand        ← 图标方块 + 标题 + 副标题      │
│    .nav-status   ← 一排 .badge 状态徽章          │
│  .nav-tabs       ← 标签按钮（.tab-btn）          │
├────────────────────────────────────────────────┤
│ .main-container（max-width: 1200px, margin auto）│
│  #toast-container ← 提示挂载点（固定右上）       │
│  section.tab-pane.active ← 当前标签内容          │
│    .card → .card-header(.card-title) + body      │
└────────────────────────────────────────────────┘
```

- 字体：`-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif`
- 基准字号 `14px`，行高 `1.5`
- 数字/密钥/token 一律用 `--font-mono`
- 主容器 `max-width: 1200px`，两侧 `padding: 20px`

---

## 3. 组件规范

### 3.1 卡片 `.card`

```css
.card {
  background: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  padding: 16px;
  margin-bottom: 16px;
}
.card-header {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 12px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--border-color);
}
.card-title { font-size: 15px; font-weight: 600; }
```

### 3.2 按钮 `.btn`

基础：`padding: 8px 14px; border-radius: var(--radius-sm); border: 1px solid transparent; cursor: pointer; font-size: 13px;`
禁用态：`opacity: .5; cursor: not-allowed;`

| 类 | 背景 | 用途 |
|---|---|---|
| `.btn-primary` | `--accent-blue`，hover `--accent-hover` | 主操作（保存、创建、启动） |
| `.btn-danger` | `--status-red` | 危险操作（删除、停止） |
| `.btn-warning` | `--status-yellow`，文字用深色 | 次级操作 |
| `.btn-outline` | 透明 + `--border-light` 描边 | 取消、次要动作 |

`.btn-group { display: flex; gap: 8px; flex-wrap: wrap; }`

按钮文案可带 emoji 前缀（与 rainclass 一致），如 `⚡ 启动`、`⏹️ 停止`、`🔄 刷新`。

### 3.3 状态徽章 `.badge`

```css
.badge {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 3px 9px; border-radius: 999px;
  font-size: 12px; font-weight: 500;
}
.badge-green  { background: rgba(23,191,99,.15);  color: var(--status-green); }
.badge-red    { background: rgba(224,36,94,.15);  color: var(--status-red); }
.badge-yellow { background: rgba(255,173,31,.15); color: var(--status-yellow); }
.badge-purple { background: rgba(121,75,196,.15); color: var(--status-purple); }
.badge-gray   { background: rgba(136,153,166,.15);color: var(--text-secondary); }
```

映射规则：
- 渠道/令牌 `status=1` → `.badge-green`「启用」
- `status=0` → `.badge-red`「禁用」
- 上游不可达 / 测试失败 → `.badge-red`
- 加载中 / 未检测 → `.badge-gray` + 文案「检测中...」

### 3.4 表格

```css
table { width: 100%; border-collapse: collapse; font-size: 13px; }
thead th {
  background: var(--bg-tertiary);
  color: var(--text-secondary);
  font-weight: 500; text-align: left;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border-color);
}
tbody td { padding: 10px 12px; border-bottom: 1px solid var(--border-color); }
tbody tr:hover { background: var(--bg-tertiary); }
```

- 表格外层 `.table-wrap { overflow-x: auto; }` 保证窄屏可横向滚动
- 数字列右对齐并用 `--font-mono`
- 空数据行：显示居中灰字「暂无数据」，不要留空白表格

### 3.5 表单

```css
input, select, textarea {
  background: var(--bg-primary);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  padding: 8px 10px;
  font-size: 13px;
  width: 100%;
}
input:focus, select:focus, textarea:focus {
  outline: none;
  border-color: var(--accent-blue);
}
label { display: block; margin-bottom: 5px; font-size: 12px; color: var(--text-secondary); }
```

- 表单项间距 `margin-bottom: 12px`
- 校验失败：`border-color: var(--status-red)` + 下方红字说明

### 3.6 Toast 提示

```css
.toast-container { position: fixed; top: 70px; right: 20px; z-index: 999; display: flex; flex-direction: column; gap: 8px; }
.toast {
  min-width: 240px; padding: 10px 14px;
  border-radius: var(--radius-sm);
  background: var(--bg-tertiary);
  border-left: 3px solid var(--accent-blue);
  box-shadow: 0 4px 12px rgba(0,0,0,.4);
  animation: slideIn .2s ease;
}
.toast-success { border-left-color: var(--status-green); }
.toast-error   { border-left-color: var(--status-red); }
.toast-warning { border-left-color: var(--status-yellow); }
```

自动消失：成功 2.5s、错误 5s（错误停留久一点，内容可能较长）。

---

## 4. 导航标签（本面板的页面划分）

| 标签 | 内容 |
|---|---|
| **运行看板** | 概览卡片组：渠道/令牌统计、今日用量、上游连通性、异常渠道列表 |
| **渠道管理** | 8 条渠道表格 + 新建/编辑/测试/启停/删除 |
| **令牌管理** | 4 人令牌表格 + 新建/编辑/额度 |
| **调用日志** | 日志表格 + 分页 + 筛选（类型/关键词）+ 简单统计 |
| **系统设置** | 面板账号、上游信息、SMTP 等（脱敏展示） |

顶部 `.nav-status` 常驻徽章建议：**上游连通性**、**渠道 启用/总**、**今日请求数**。

---

## 5. 响应式

- 断点 `@media (max-width: 768px)`：
  - `.nav-container` 改为纵向堆叠
  - `.nav-tabs` 允许横向滚动（`overflow-x: auto`）
  - 表格依赖 `.table-wrap` 横滚，不强制折行
- 不做移动端专用布局，保证「能用」即可

---

## 6. 禁止事项

- ❌ 引入任何 CSS 框架（Bootstrap/Tailwind）或外部 CDN 字体
- ❌ 使用浅色背景（本面板固定深色）
- ❌ 自定义新的颜色值——只能用第 1 节的变量
- ❌ 把 key/token 明文渲染进 DOM 或 `console.log`
