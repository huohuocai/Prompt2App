"""agents_v2 公共基础设施：常量、日志、.env、共享异常。"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("prompt2app.agents_v2")

CACHE_DIR = Path(os.getenv("AGENT_CACHE_DIR", ".cache"))

DEMO_TAG = "【演示模式】"

# 并发与超时
MAX_CONCURRENT_LLM = int(os.getenv("MAX_CONCURRENT_LLM", "5"))
_MODEL_CACHE_TTL = 300.0        # 模型发现结果缓存 300s
_MODELS_TIMEOUT = 30.0          # GET /v1/models 超时
_LLM_STREAM_OPEN_TIMEOUT = 30.0
_MAX_RETRIES = 2                # 除首次外最多退避重试 2 次
_BACKOFF_BASE = 1.5
_HEARTBEAT_CHARS = 1500         # 流式心跳：每产出 1500 字符回调一次
_CODE_MIN_LEN = 200             # 合法 HTML 的最小长度
_PATCH_CODE_CAP = 20000         # PATCH 场景喂给模型的现有代码上限（字符）
_MODELS_STALE_GRACE = 600.0     # 发现接口失败时过期缓存最多沿用时长
_LLM_TIMEOUT = 120.0            # 单次流式调用总超时
_NODE_CHECK_TIMEOUT = 15.0      # node --check 语法校验超时


def _load_dotenv() -> None:
    """项目根目录 .env 仅补充未定义的环境变量，绝不覆盖已有值。"""
    try:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = os.path.join(root, ".env")
        if not os.path.isfile(path):
            return
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and value and key not in os.environ:
                    os.environ[key] = value
    except Exception:  # noqa: BLE001
        pass


_load_dotenv()


class LLMUnavailableError(Exception):
    """第 2/3 层全部失败时抛出，由流水线捕获后落入第 4 层兜底。"""


class CheckpointAbort(Exception):
    """在 on_checkpoint 回调中抛出：于当前阶段优雅取消流水线（保留已有代码，不报错）。"""
