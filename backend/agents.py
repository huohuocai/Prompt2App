"""agents.py — 兼容薄壳。

智能体流水线的完整实现位于同级 ``agents_v2`` 包（多文件结构）：
- ``backend/agents_v2/gateway.py``     四层容灾网关（Kimi 主通道 + SiliconFlow 备用通道）
- ``backend/agents_v2/checkpoints.py`` 检查点落盘、断点续跑、会话级并发隔离
- ``backend/agents_v2/compressor.py``  递归增量上下文压缩
- ``backend/agents_v2/sanitizer.py``   三层模糊补丁引擎 + JS 静态/语法确定性校验
- ``backend/agents_v2/prompts.py``     全部提示词
- ``backend/agents_v2/actors.py``      PM / DesignSpec / Coder / Reviewer / Tester / ChatHost
- ``backend/agents_v2/pipeline.py``    工作流编排与 L4 离线兜底
- ``backend/agents_v2/selftest.py``    离线自检与在线核验

本文件仅做再导出，保持 ``from backend.agents import run_agent_pipeline`` 等既有调用不变。
"""

from .agents_v2 import *  # noqa: F401,F403
from .agents_v2 import __all__  # noqa: F401

if __name__ == "__main__":
    from .agents_v2.selftest import main

    main()
