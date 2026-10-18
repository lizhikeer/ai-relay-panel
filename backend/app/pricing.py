"""DeepSeek 均价定价表（契约 §3.1）。

为什么需要这个模块
------------------
New API 的计费靠三个 option：`ModelRatio` / `CompletionRatio` / `CacheRatio`。
**没有配过的模型会回落到硬编码的 37.5** —— 换算下来约 $75/M tokens，比 DeepSeek
实际价格高两个数量级（2026-10-18 实测：173k prompt 一次请求记了 6.55M 额度 ≈ ¥95，
真实成本约 ¥0.02）。所以「配价」是必需品，而不是优化项。

这里把价格定义成**唯一一份样板**：
  - 面板按它把 token 折算成人民币（历史日志同样口径，不受旧倍率污染）
  - `sync_model_ratios()` 把同一份价格换算成 New API 倍率写回，两边永不分叉

价格来源
--------
DeepSeek 官方 2026-08-17 起执行的峰谷价，本项目**不区分峰谷**，取时段加权平均
（高峰 9:00-12:00 / 14:00-18:00 共 8 小时，其余 16 小时为空闲）：

                 高峰        空闲        加权平均
  V4-Flash 输入   ¥3/M       ¥1.5/M      ¥2/M
  V4-Flash 输出   ¥9/M       ¥4.5/M      ¥6/M
  V4-Flash 缓存   ¥0.1/M     ¥0.05/M     ¥0.0667/M   （= 输入的 1/30）
  V4-Pro   各项均为 Flash 的 3 倍

倍率换算（New API 口径）
------------------------
`QuotaPerUnit = 500000` 表示 50 万额度 = $1，且倍率 1 ≈ $0.002/1K = $2/M，
故：**倍率 = 每百万美元价 / 2**，`CompletionRatio = 输出价 / 输入价 = 3`，
`CacheRatio = 缓存价 / 输入价 = 1/30`。
"""
from __future__ import annotations

# --- 价格（人民币 / 每百万 token，DeepSeek 官方均价）--- #
INPUT_CNY_PER_M = 2.0                     # 输入，缓存未命中
OUTPUT_CNY_PER_M = 6.0                    # 输出
CACHE_CNY_PER_M = INPUT_CNY_PER_M / 30.0  # 输入，缓存命中
PRO_MULTIPLIER = 3.0                      # deepseek-v4-pro 各项为 flash 的 3 倍

# --- 换算常量（与 New API 实际配置保持一致）--- #
USD_CNY = 7.3              # 后台 option USDExchangeRate
QUOTA_PER_USD = 500000     # 后台 option QuotaPerUnit

# 倍率 1 = $2/M tokens；换算成人民币价 = 2 × USD_CNY
RATIO1_CNY_PER_M = 2.0 * USD_CNY


def is_pro(model: str | None) -> bool:
    """deepseek-v4-pro 系列按 V4-Pro 计价（其余一律按 V4-Flash 均价）。"""
    return (model or "").startswith("deepseek-v4-pro")


def _input_price(model: str | None) -> float:
    return INPUT_CNY_PER_M * (PRO_MULTIPLIER if is_pro(model) else 1.0)


def cost_cny(
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cache_tokens: int = 0,
    model: str | None = None,
) -> float:
    """按 DeepSeek 均价把一次调用的 token 折算成人民币。

    `cache_tokens` 是 prompt 中被缓存命中的部分（New API 的 `cache_tokens`，
    属于 prompt 的子集），按 1/30 计价。
    """
    prompt = max(int(prompt_tokens or 0), 0)
    completion = max(int(completion_tokens or 0), 0)
    cached = min(max(int(cache_tokens or 0), 0), prompt)
    miss = prompt - cached
    mul = PRO_MULTIPLIER if is_pro(model) else 1.0
    return (
        miss * INPUT_CNY_PER_M
        + cached * CACHE_CNY_PER_M
        + completion * OUTPUT_CNY_PER_M
    ) * mul / 1_000_000.0


def model_ratio(model: str | None) -> float:
    """New API 的 ModelRatio：倍率 1 = $2/M，故 ratio = 人民币价 / (2 × 汇率)。"""
    return round(_input_price(model) / RATIO1_CNY_PER_M, 6)


def completion_ratio() -> float:
    return round(OUTPUT_CNY_PER_M / INPUT_CNY_PER_M, 6)


def cache_ratio() -> float:
    return round(CACHE_CNY_PER_M / INPUT_CNY_PER_M, 6)


def price_table() -> dict:
    """给前端/文档用的价格表（人民币 / 每百万 token）。"""
    return {
        "currency": "CNY",
        "unit": "per_1m_tokens",
        "input": INPUT_CNY_PER_M,
        "output": OUTPUT_CNY_PER_M,
        "cache_hit": round(CACHE_CNY_PER_M, 4),
        "pro_multiplier": PRO_MULTIPLIER,
        "usd_cny": USD_CNY,
        "note": "DeepSeek 官方峰谷价按时段加权后的均价，不区分峰谷",
    }


def build_ratio_options(models: list[str]) -> tuple[dict, dict, dict]:
    """给定模型列表，生成要写回 New API 的三个倍率映射。

    返回 `(model_ratio, completion_ratio, cache_ratio)`，调用方负责与已有
    配置合并（**不能整体覆盖**，上游自带 200+ 内置模型的价格要保留）。
    """
    mr: dict[str, float] = {}
    cr: dict[str, float] = {}
    car: dict[str, float] = {}
    for m in models:
        name = (m or "").strip()
        if not name:
            continue
        mr[name] = model_ratio(name)
        cr[name] = completion_ratio()
        car[name] = cache_ratio()
    return mr, cr, car
