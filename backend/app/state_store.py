"""BFF 本地状态持久化（当前只存 New API 的 access_token）。

为什么需要：access_token 有效期只有 900 秒，BFF 是无状态单进程，重启/重建容器
都会丢 token → 立刻触发一次登录。更糟的是 409 会话上限死局里，「重启丢 token」
意味着没法用旧 token 去清理会话自救。落盘后：

- 重启直接复用未过期的 token，跳过登录（少一次登录 = 少一条会话）
- 409 自救有牌可打（旧 token 通常仍有效，够调会话清理接口）

只存 token/uid/过期时间，**绝不落盘密码**。文件权限 600。
写入失败只记日志、绝不抛异常 —— 持久化是优化项，不能变成新的故障点。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger("bff.state")


def _candidate_paths() -> list[Path]:
    """按优先级列出候选路径，第一个「可写目录」胜出。

    - PANEL_STATE_FILE 环境变量显式指定时只用它
    - POSIX（NAS 容器）优先 /data/panel-state.json（compose 挂载的持久卷）
    - Windows 开发机退回系统临时目录（避免在盘根创建 C:\\data 之类的垃圾）
    """
    explicit = os.environ.get("PANEL_STATE_FILE")
    if explicit:
        return [Path(explicit)]
    out: list[Path] = []
    if os.name == "posix":
        out.append(Path("/data/panel-state.json"))
    out.append(Path(tempfile.gettempdir()) / "ai-relay-panel-state.json")
    return out


class StateStore:
    """极简 JSON 状态文件。任何失败都降级为「无持久化」，不影响主流程。"""

    def __init__(self) -> None:
        self.path: Path | None = None
        for p in _candidate_paths():
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                probe = p.parent / ".state-write-probe"
                probe.touch()
                probe.unlink()
                self.path = p
                break
            except OSError:
                continue
        if self.path is None:
            logger.warning("找不到可写的状态目录，token 持久化已禁用（每次重启都会重新登录）")

    def load(self) -> dict:
        """读取状态。损坏/不存在/不可读一律返回 {}。"""
        if self.path is None:
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning("状态文件读取失败（忽略）：%s", exc)
            return {}

    def save(self, data: dict) -> bool:
        """原子写入（同目录临时文件 + os.replace）。成功返回 True。"""
        if self.path is None:
            return False
        tmp = self.path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            return True
        except OSError as exc:
            logger.warning("状态文件写入失败（忽略）：%s", exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
