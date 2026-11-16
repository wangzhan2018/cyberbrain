"""
CyberBrain Controller: 大脑主接口

提供 react 接口，直接透传给 cyber_brain.react() 方法
用于测试和集成调用
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Body, Query
from pydantic import BaseModel, Field

from app.service.cyber_brain import cyber_brain
from app.service.outreach_service import outreach_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/brain", tags=["CyberBrain"])


class ReactRequest(BaseModel):
    """大脑反应请求"""
    agent_id: str = Field(..., description="Agent ID")
    signal: str = Field(..., description="外部输入信号")
    speaker: Optional[str] = Field(None, description="说话人姓名，用于关系查询、情绪反应和工作记忆标记")


class ReactResponse(BaseModel):
    """大脑反应响应"""
    response: str = Field(..., description="说出口的话")
    lies: list[str] = Field(default_factory=list, description="被标记为谎言的句子")
    silent: bool = Field(False, description="是否选择不说话（此时 response 为空）")
    route: str = Field(..., description="认知路径：fast / memory / deliberate")
    route_reason: str = Field("", description="选择该路径的原因：needs_reasoning / think_prob / hostile_to_known / low_action_confidence / knowledge / needs_memory / intuitive")
    emotion: dict = Field(..., description="System1 本能反应与认知门控")
    decision: Optional[dict] = Field(None, description="System2 社交决策（仅 deliberate 路径）")
    brief: list = Field(default_factory=list, description="召回的 Brief 记忆")
    episodic: list = Field(default_factory=list, description="召回的情景记忆")
    knowledge: list = Field(default_factory=list, description="召回的专业知识")
    requires_knowledge: bool = Field(..., description="是否需要专业知识")
    subjectivity: dict = Field(default_factory=dict, description="主体性观测：situation 进了哪些系统、是否有 overall_state")
    timings_ms: dict = Field(..., description="各阶段耗时（毫秒）")


@router.post("/react", response_model=ReactResponse, summary="大脑反应")
async def react(request: ReactRequest = Body(...)):
    """
    大脑主方法：接收外部信号，返回响应

    这是 CyberBrain 的核心接口，完整流程：
    1. 召回 Brief（信条/事实）
    2. System1：tev1 快速情绪量化打分
    3. 召回 Episodic（情景记忆）
    4. 如果需要知识，召回 Knowledge（知识记忆）
    5. 信号进入 PFC 短时记忆队列
    6. System2：结合所有信息，生成最终响应

    Args:
        request: 包含 agent_id, signal, speaker

    Returns:
        完整的响应结果，包含 response、emotion、记忆召回、耗时统计等
    """
    logger.info(f"CyberBrain.react: agent_id={request.agent_id}, speaker={request.speaker}, "
                f"signal={request.signal[:50]}...")

    result = await cyber_brain.react(
        agent_id=request.agent_id,
        signal=request.signal,
        speaker=request.speaker
    )

    return result



class TalkMessage(BaseModel):
    """agent 主动想对某人说的一句话"""
    id: str
    agent_id: str
    to: str = Field(..., description="说给谁（关系表里的人名）")
    content: str = Field(..., description="说出口的话")
    source: str = Field(..., description="来源：task（任务完成的通知）/ thought（想说出口的念头）")
    ref: Optional[str] = Field(None, description="关联的任务 id 或念头原文")
    created_at: str


@router.get("/fetch_talk", response_model=List[TalkMessage], summary="取走 agent 想对某人说的话")
async def fetch_talk(
    agent_id: str = Query(..., description="Agent ID"),
    human: str = Query(..., description="人类的角色名（与关系表里的人名一致，如'史塔克'）"),
):
    """
    agent 主动想对这个人说的话（任务完成后的通知、想说出口的念头），按时间先后返回。
    **取走即从发件箱删除**，同一条消息只会返回一次；没有消息时返回空列表。

    只有关系表里 kind=human 的人才会有消息：对 agent 的主动联系直接调用对方的 react，不经过发件箱。
    """
    return await outreach_service.fetch_talk(agent_id, human)
