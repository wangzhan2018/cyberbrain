"""
Outreach Service: agent 主动触达（主动开口说话）

两种来源：
- 任务完成：替某人完成的任务（如联网查资料）做完后，告诉对方结果
- 念头：自发冒出的、想说给某人听的念头（如对主人的关心），由 System One 结合人格判断此刻要不要说

投递方式由关系表里对方的 kind 决定（不靠名字推测）：
- human：写入发件箱 outbox:{agent_id}:{人名}，前端通过 GET /brain/fetch_talk 取走（取走即删除）
- agent：用关系里记录的 agent_id 调用对方的 react，"我"作为说话人
- 不在关系表里：不主动联系（陌生人），等对方下次来问

说出口的话由 System3 按"我"的说话习惯生成；说完写入"我"的工作记忆，之后的对话知道自己已经说过。
"""
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.client.doubao_client import doubao_client
from app.client.redis_client import redis_client
from app.repository.relationship_repository import KIND_AGENT, KIND_HUMAN, relationship_repository
from app.service.agent_life_event_service import agent_life_event_service
from app.service.persona_manager import persona_manager
from app.service.prefrontal_lobe_memory import prefrontal_lobe_memory
from app.service.subjectivity_service import subjectivity_service

logger = logging.getLogger(__name__)

# 发件箱只保留最近的若干条：前端长时间不来取时，不让列表无限增长
OUTBOX_MAX = 50
# 主动开口时没有对方说的话，System3 需要一个"此刻对方说"的占位
PROACTIVE_SIGNAL = "（对方没有说话，这是我主动开口）"


def outbox_key(agent_id: str, person_name: str) -> str:
    return f"outbox:{agent_id}:{person_name}"


class OutreachService:

    # ---------------- 对外入口 ----------------

    async def notify_task_done(self, agent_id: str, task: Dict, summary: str, details: str = "") -> Optional[Dict]:
        """
        替某人完成的任务做完了，告诉对方。只通知关系表里的人（陌生人不主动联系）。

        Args:
            summary: 结果概述（如"上网查了资料，整理进知识库：……"）
            details: 可以提一两句的要点（如新知识的正文）
        """
        person = task.get("related_person")
        if not person:
            return None
        plan = (f"我主动告诉对方：之前答应替对方做的事「{task.get('content', '')}」已经做完了。\n"
                f"结果：{summary}\n"
                + (f"可以简要提一两个要点（只说下面写到的，不要补充别的）：\n{details}\n" if details else "")
                + "说清做完了、结果是什么。这是我主动开口，对方刚才没有说话。")
        return await self.deliver(agent_id, person, plan, source="task", ref=task.get("task_id"))

    async def maybe_express_thought(self, agent_id: str, thought: Dict) -> Optional[Dict]:
        """
        念头里有想说给某人听的，交给 System One 结合人格、关系、刚才的对话判断此刻要不要主动说出来。
        要不要说是人格的体现（有人什么都想告诉对方，有人憋在心里），不由系统规则决定
        """
        person = (thought.get("target_person") or "").strip()
        text = (thought.get("thought") or "").strip()
        if not person or not text:
            return None
        rel = await relationship_repository.get(agent_id, person)
        if not rel:
            logger.info(f"Thought not expressed for {agent_id}: '{person}' is not in relationships")
            return None

        persona = await persona_manager.get(agent_id)
        recent = await prefrontal_lobe_memory.get_context(agent_id)
        state = (f"【我是谁】\n{persona['system1']}\n\n"
                 f"【对方是谁】{person}：{rel['relationship']}，{rel['description']}\n\n"
                 f"【最近的对话】\n{recent[-1500:] or '（无）'}\n\n"
                 f"【我此刻冒出的念头】{text}（紧迫度 {thought.get('urgency', 0.0)}）")
        try:
            resp = await doubao_client.decide(state=state, questions={"say_it": {
                "type": "choice",
                "instructions": f"按我的性格和我与{person}的关系，我会不会现在主动把这个念头的意思说给{person}听？",
                "criteria": {
                    "yes": "这是想让对方知道的事（关心、提醒、分享、请求），而且现在说合适",
                    "no": "只是自己心里想想；或者刚刚已经说过类似的话；或者现在说会打扰对方、不合时宜",
                },
            }})
            probs = resp.get("answers", {}).get("say_it", {}).get("probabilities") or {}
            p_say = float(probs.get("yes", 0.0))
        except Exception as e:
            logger.error(f"Express-thought decision failed for {agent_id}: {e}", exc_info=True)
            return None

        logger.info(f"Express thought? agent={agent_id} to={person} p_say={p_say:.2f} thought={text[:30]}")
        if p_say < 0.6:
            return None
        plan = (f"我心里想到：{text}\n"
                f"我决定主动把这个意思跟对方说出来（关心、提醒或分享），用一两句话自然地说，不要把心里话原样念出来。"
                f"这是我主动开口，对方刚才没有说话。")
        return await self.deliver(agent_id, person, plan, source="thought", ref=text)

    async def fetch_talk(self, agent_id: str, person_name: str) -> List[Dict]:
        """取走 agent 想对某人说的话（按时间先后），取走即从发件箱删除"""
        key = outbox_key(agent_id, person_name)
        async with redis_client.client.pipeline(transaction=True) as pipe:
            pipe.lrange(key, 0, -1)
            pipe.delete(key)
            raw, _ = await pipe.execute()
        return [json.loads(r) for r in raw]

    # ---------------- 投递 ----------------

    async def deliver(self, agent_id: str, person_name: str, plan: str, source: str, ref: Any = None) -> Optional[Dict]:
        """
        按关系表里对方的 kind 投递"我"主动要说的话。
        Returns: 投递记录（含说出口的话），对方不在关系表里或没有说出口时返回 None
        """
        rel = await relationship_repository.get(agent_id, person_name)
        if not rel:
            logger.info(f"Outreach skipped for {agent_id}: '{person_name}' is not in relationships (stranger)")
            return None

        text = await self._speak(agent_id, person_name, plan)
        if not text:
            return None

        message = {
            "id": f"msg_{uuid.uuid4().hex}",
            "agent_id": agent_id,
            "to": person_name,
            "content": text,
            "source": source,
            "ref": ref,
            "created_at": datetime.now().isoformat(),
        }
        if rel["kind"] == KIND_HUMAN:
            key = outbox_key(agent_id, person_name)
            async with redis_client.client.pipeline(transaction=True) as pipe:
                pipe.rpush(key, json.dumps(message, ensure_ascii=False))
                pipe.ltrim(key, -OUTBOX_MAX, -1)
                await pipe.execute()
            message["delivered_via"] = "outbox"
        elif rel["kind"] == KIND_AGENT:
            # 对方是 agent：调用对方的 react。说话人用"对方关系表里对我的称呼"，对方才认得出是谁；
            # 对方没有记录我时，退回用我的 agent_id
            from app.service.cyber_brain import cyber_brain
            my_name = await self._name_known_by(rel["agent_id"], agent_id)
            reply = await cyber_brain.react(agent_id=rel["agent_id"], signal=text, speaker=my_name)
            message["delivered_via"] = "react"
            message["reply"] = reply.get("response", "")
            # 对方的回复也是"我"经历的对话
            if message["reply"]:
                await prefrontal_lobe_memory.append(agent_id, message["reply"], role="user", speaker=person_name)

        await agent_life_event_service.record_proactive_talk(agent_id=agent_id, message=message)
        logger.info(f"Outreach {agent_id} → {person_name} via {message['delivered_via']} ({source}): {text[:60]}")
        return message

    @staticmethod
    async def _name_known_by(other_agent_id: str, my_agent_id: str) -> str:
        """对方（另一个 agent）的关系表里，记录的 kind=agent 且 agent_id 是我的那个名字"""
        for name, rel in (await relationship_repository.load_all(other_agent_id)).items():
            if rel["kind"] == KIND_AGENT and rel["agent_id"] == my_agent_id:
                return name
        return my_agent_id

    async def _speak(self, agent_id: str, person_name: str, plan: str) -> str:
        """用 System3 把要说的意思变成"我"说出口的话，并写入工作记忆"""
        from app.service.cyber_brain import NEUTRAL_EMOTION, cyber_brain, parse_lies
        speaker_info, _ = await cyber_brain._format_speaker_info(person_name, agent_id)
        _, state_only = subjectivity_service.format_for_prompts(await subjectivity_service.get_context(agent_id))
        working_memory = await prefrontal_lobe_memory.get_context(agent_id)
        raw = await cyber_brain._system3(
            agent_id, dict(NEUTRAL_EMOTION), plan, working_memory, PROACTIVE_SIGNAL, speaker_info, state_only)
        text, lies = parse_lies(raw)
        if not text:
            return ""
        # 写入工作记忆，标明是主动对谁说的：之后对方来问，"我"知道自己已经说过
        await prefrontal_lobe_memory.append(agent_id, f"（主动找{person_name}说）{text}", role="assistant",
                                            speaker="我", lies=lies)
        return text


outreach_service = OutreachService()
