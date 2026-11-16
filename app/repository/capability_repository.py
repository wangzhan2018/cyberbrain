"""
Agent 能力存储库（Redis Set）

Key:    agent:{agent_id}:capabilities
Member: 能力名，目前只有 web_search

能力是这个 agent 客观上有没有某个工具，不是性格，所以不写进 persona、不交给提示词判断。
没有配置时视为没有任何能力：拟人角色（真实的人类）默认不会联网，只有显式开启的 agent（如个人知识库助手）才会。
"""
from typing import List

from app.client.redis_client import redis_client

CAP_WEB_SEARCH = "web_search"  # 联网搜索：对话 / 念头里生成的待办会判断要不要联网，联网任务可以立即执行
CAPABILITIES = (CAP_WEB_SEARCH,)


def capabilities_key(agent_id: str) -> str:
    return f"agent:{agent_id}:capabilities"


class CapabilityRepository:

    async def get(self, agent_id: str) -> List[str]:
        """该 agent 的全部能力（排序后返回），未配置时为空列表"""
        return sorted(await redis_client.client.smembers(capabilities_key(agent_id)))

    async def has(self, agent_id: str, capability: str) -> bool:
        return bool(await redis_client.client.sismember(capabilities_key(agent_id), capability))

    async def set(self, agent_id: str, capabilities: List[str]) -> None:
        """覆盖式设置；传空列表即清空全部能力"""
        unknown = set(capabilities) - set(CAPABILITIES)
        if unknown:
            raise ValueError(f"未知的能力：{sorted(unknown)}，可选：{CAPABILITIES}")
        key = capabilities_key(agent_id)
        async with redis_client.client.pipeline(transaction=True) as pipe:
            pipe.delete(key)
            if capabilities:
                pipe.sadd(key, *capabilities)
            await pipe.execute()


capability_repository = CapabilityRepository()
