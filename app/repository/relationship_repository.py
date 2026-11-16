"""
人际关系存储库（Redis Hash）

Key:   agent:{agent_id}:relationships
Field: 人名（对方在"我"这里的称呼，也是对话时 speaker 的名字）
Value: JSON {"kind": "human"|"agent", "agent_id": 对方的 agent_id（kind=agent 时必填）,
             "relationship": "关系类型", "description": "关系描述",
             "profile": {槽位: {"value", "text", "stated_at", "mentions", "history"}}}

kind 区分对方是真人还是另一个 agent，决定"我"主动联系对方的方式：
- human：写入发件箱（outbox），由前端通过 fetch_talk 取走
- agent：用 agent_id 调用对方的 react

profile 是"我认识的这个人"：对话中对方主动提到的、稳定的个人信息（住址、职业、家人、忌讳……），
按受控槽位存放，和 relationship / description（关系本身）分开，互相不覆盖。它和关系描述共用一个 key，
不额外开 key；旧数据没有 profile 字段时读出来为空。

兼容旧格式：Value 是纯字符串 "关系类型，描述" 时，按 kind=human 解析。
"""
import json
from datetime import datetime
from typing import Dict, List, Optional

from app.client.redis_client import redis_client

KIND_HUMAN = "human"
KIND_AGENT = "agent"
KINDS = (KIND_HUMAN, KIND_AGENT)

# 个人信息的受控槽位：只有这些属性会写进 profile。对话里高频出现，靠固定槽位才能可靠地去重和更新
PROFILE_SLOTS = (
    "姓名", "称呼", "年龄", "生日", "住址", "籍贯", "职业", "单位", "职位",
    "家庭成员", "健康状况", "过敏与禁忌", "长期偏好", "忌讳", "重要日期", "习惯",
)
# 同一槽位允许有多个值（家庭成员、过敏与禁忌、长期偏好、忌讳、习惯、重要日期是列表型）
LIST_SLOTS = {"家庭成员", "过敏与禁忌", "长期偏好", "忌讳", "习惯", "重要日期"}


def _key(agent_id: str) -> str:
    return f"agent:{agent_id}:relationships"


def _parse(raw: Optional[str]) -> Optional[Dict]:
    """Redis 里的值 → 结构化记录。旧格式（纯字符串）按 human 解析"""
    if raw is None:
        return None
    if raw.lstrip().startswith("{"):
        try:
            data = json.loads(raw)
            kind = data.get("kind") if data.get("kind") in KINDS else KIND_HUMAN
            return {
                "kind": kind,
                "agent_id": data.get("agent_id") if kind == KIND_AGENT else None,
                "relationship": data.get("relationship", ""),
                "description": data.get("description", ""),
                "profile": data.get("profile") or {},
            }
        except json.JSONDecodeError:
            pass
    relationship, _, description = raw.partition("，")
    return {"kind": KIND_HUMAN, "agent_id": None, "relationship": relationship, "description": description,
            "profile": {}}


def format_profile(profile: Optional[Dict]) -> str:
    """profile → 给提示词用的几行文本，如 "- 住址：北京（2026-11 他自己说的）"；空返回空字符串"""
    if not profile:
        return ""
    lines = []
    for slot in PROFILE_SLOTS:
        fact = profile.get(slot)
        if not fact:
            continue
        values = fact if isinstance(fact, list) else [fact]
        for v in values:
            text = (v or {}).get("text") or (v or {}).get("value") or ""
            if text:
                lines.append(f"- {slot}：{text}")
    return "\n".join(lines)


def _merge_facts(existing: List[Dict], incoming: List[Dict], slot: str) -> List[Dict]:
    """
    把一个槽位的新旧事实合并。规则：
    - 值相同 → 只增加 mentions 和 last_confirmed_at（去重）
    - 列表型槽位：同一个值不重复加，最多保留 8 条
    - 单值槽位：新值替换，旧值进 history（保留旧值、时间和来源，可以回答"他以前住哪"）
    """
    now = datetime.now().isoformat()
    if slot in LIST_SLOTS:
        out = list(existing)
        for new in incoming:
            value = (new.get("value") or "").strip()
            if not value:
                continue
            same = next((e for e in out if (e.get("value") or "").strip() == value), None)
            if same:
                same["mentions"] = int(same.get("mentions", 1)) + 1
                same["last_confirmed_at"] = now
            else:
                out.append({**new, "value": value, "mentions": 1,
                            "stated_at": new.get("stated_at") or now, "last_confirmed_at": now})
        return out[-8:]
    # 单值槽位：最后一条生效
    out = list(existing)
    for new in incoming:
        value = (new.get("value") or "").strip()
        if not value:
            continue
        current = out[-1] if out else None
        if current and (current.get("value") or "").strip() == value:
            current["mentions"] = int(current.get("mentions", 1)) + 1
            current["last_confirmed_at"] = now
            continue
        if current:
            current["superseded_at"] = now
        out.append({**new, "value": value, "mentions": 1,
                    "stated_at": new.get("stated_at") or now, "last_confirmed_at": now})
    # 只保留最近 3 条历史，避免无限增长
    return out[-3:]


def describe(record: Optional[Dict]) -> Optional[str]:
    """结构化记录 → 给提示词用的描述，如 "主人，史塔克是这个知识库的主人……"（与旧格式的文本一致）"""
    if not record:
        return None
    rel, desc = record.get("relationship", ""), record.get("description", "")
    return f"{rel}，{desc}" if desc else rel


class RelationshipRepository:

    async def get(self, agent_id: str, person_name: str) -> Optional[Dict]:
        """查询某人的关系记录，不存在返回 None（即陌生人）"""
        return _parse(await redis_client.client.hget(_key(agent_id), person_name))

    async def get_relationship(self, agent_id: str, person_name: str) -> Optional[str]:
        """查询某人与 agent 的关系描述（给提示词用的文本），不存在返回 None"""
        return describe(await self.get(agent_id, person_name))

    async def add_relationship(
        self,
        agent_id: str,
        person_name: str,
        relationship: str,
        description: str = "",
        kind: str = KIND_HUMAN,
        target_agent_id: Optional[str] = None,
        profile: Optional[Dict] = None,
    ) -> None:
        """
        添加/覆盖一条关系。

        Args:
            kind: human（真人）或 agent（另一个 agent）
            target_agent_id: kind=agent 时对方的 agent_id，必填；kind=human 时必须为空
            profile: 该人的个人信息；不传时保留已有的，不会被覆盖清空
        """
        if kind not in KINDS:
            raise ValueError(f"kind 必须是 {KINDS} 之一，收到 {kind!r}")
        if kind == KIND_AGENT and not target_agent_id:
            raise ValueError(f"关系「{person_name}」的 kind=agent，必须提供对方的 agent_id")
        if kind == KIND_HUMAN and target_agent_id:
            raise ValueError(f"关系「{person_name}」的 kind=human，不能提供 agent_id")
        if profile is None:
            # 更新关系时不传 profile：保留原有的人信息，不能因为改关系把积累的资料清掉
            profile = (await self.get(agent_id, person_name) or {}).get("profile") or {}
        value = json.dumps({
            "kind": kind,
            "agent_id": target_agent_id if kind == KIND_AGENT else None,
            "relationship": relationship,
            "description": description or "",
            "profile": profile,
        }, ensure_ascii=False)
        await redis_client.client.hset(_key(agent_id), person_name, value)

    async def merge_profile(self, agent_id: str, person_name: str, slot: str, facts: List[Dict]) -> bool:
        """
        把新抽出的个人信息合并进这个人的 profile，只动这个槽位，关系和描述保持不变。
        facts: [{"value": ..., "text": ..., "stated_at"?}]；读取-修改-写入在 Redis 事务里完成。
        """
        if slot not in PROFILE_SLOTS:
            raise ValueError(f"未知的个人信息槽位：{slot!r}")
        if not facts:
            return False
        key = _key(agent_id)
        async with redis_client.client.pipeline(transaction=True) as pipe:
            await pipe.watch(key)
            raw = await pipe.hget(key, person_name)
            record = _parse(raw)
            if not record:
                await pipe.unwatch()
                return False  # 不在关系表里（陌生人）：不记录个人信息
            profile = dict(record.get("profile") or {})
            current = profile.get(slot)
            existing = current if isinstance(current, list) else ([current] if current else [])
            merged = _merge_facts(existing, facts, slot)
            if slot in LIST_SLOTS:
                profile[slot] = merged
            else:
                # 单值槽位只存当前值，但把旧值挂在 history 上（最多 2 条）：
                # "他以前住北京，现在搬去上海了"要答得出来
                current = dict(merged[-1])
                history = []
                for old_fact in merged[:-1]:
                    history.append({k: old_fact.get(k) for k in ("value", "text", "stated_at", "superseded_at")})
                current["history"] = (current.get("history") or []) + history
                profile[slot] = {**current, "history": current["history"][-2:]}
            record["profile"] = profile
            pipe.multi()
            pipe.hset(key, person_name, json.dumps(record, ensure_ascii=False))
            await pipe.execute()
        return True

    async def load_all(self, agent_id: str) -> Dict[str, Dict]:
        """全部关系：{人名: 结构化记录}"""
        raw = await redis_client.client.hgetall(_key(agent_id))
        return {name: _parse(value) for name, value in raw.items()}

    async def delete_relationship(self, agent_id: str, person_name: str) -> bool:
        return await redis_client.client.hdel(_key(agent_id), person_name) > 0

    async def clear_all_relationships(self, agent_id: str) -> None:
        await redis_client.delete(_key(agent_id))


relationship_repository = RelationshipRepository()
