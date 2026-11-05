"""事件处置 Agent：补全要素、风险研判并生成待人工确认的响应方案。"""
from __future__ import annotations

from agents.base import BaseAgent
from agents.risk_agent import RiskAgent
from agents.urgency_agent import UrgencyAgent
from core.incident_response_config import get_response_config
from core.policy_engine import PolicyMatchEngine


class IncidentAgent(BaseAgent):
    name = "incident"
    keywords = (
        "异常", "闯入", "事件", "报警", "事故", "冲突", "斗殴", "破坏",
        "门禁", "监控", "盗窃", "可疑", "消防", "故障",
    )
    system_prompt = (
        "你是安保事件辅助研判助手。只能依据已提供事实输出简洁建议；必须区分已知事实、"
        "待核信息和建议动作，不得编造人员、时间、地点或处置结果，不得宣称事件已经结案。"
    )

    # 时限 / 责任人 / 首轮动作来自 config/incident_response.json（带批准状态），不再写死在代码里。
    RESPONSE_PLANS = get_response_config().plans

    EVENT_PROFILES = {
        "人员闯入": {
            "keywords": ("闯入", "翻越", "尾随", "非法进入"),
            "checks": ("确认人数、位置及移动方向", "确认是否持械、攻击或拒不配合", "确认进入路径及当前控制状态"),
            "evidence": ("相关时段监控片段", "对应门禁点位记录", "现场处置时间线"),
            "escalation": ("发现持械、攻击或人员受伤", "进入核心或受控区域", "人员失控或风险持续扩大"),
            "actions": ("限制继续深入并保持安全距离", "同步核查门禁点位与相邻区域"),
        },
        "门禁异常": {
            "keywords": ("门禁", "未锁", "强开", "刷卡", "门磁", "尾随"),
            "checks": ("确认门体、门磁和锁具当前状态", "确认异常发生时间与开门方式", "确认是否存在尾随或未授权进入"),
            "evidence": ("门禁事件记录", "对应时段监控片段", "设备告警与维修记录"),
            "escalation": ("门体无法恢复闭锁", "出现未授权进入", "核心区域门禁连续异常"),
            "actions": ("先安排现场看守或临时管控", "核对门体状态并通知设备责任人"),
        },
        "监控异常": {
            "keywords": ("监控", "摄像头", "离线", "黑屏", "遮挡", "录像"),
            "checks": ("确认故障点位、开始时间和影响范围", "确认实时画面与录像是否同时中断", "确认重点区域是否存在监控盲区"),
            "evidence": ("设备离线与恢复时间", "平台告警记录", "维修处置记录"),
            "escalation": ("核心区域形成监控盲区", "多点位同时离线", "录像缺失且同期发生其他异常"),
            "actions": ("安排临时巡查覆盖受影响区域", "通知监控或设备责任人排查恢复"),
        },
        "冲突纠纷": {
            "keywords": ("冲突", "纠纷", "斗殴", "打斗", "争执"),
            "checks": ("确认参与人数及是否有人受伤", "确认是否存在器械或持续威胁", "确认双方是否已分离和现场秩序"),
            "evidence": ("现场视频或执勤记录", "参与人员基本情况", "目击人员陈述"),
            "escalation": ("出现持械、伤亡或群体聚集", "冲突无法控制", "涉及重要区域或外部人员"),
            "actions": ("优先隔离双方并保护无关人员", "按现场情况请求增援或主管到场"),
        },
        "盗窃破坏": {
            "keywords": ("盗窃", "偷盗", "破坏", "损坏"),
            "checks": ("确认物品或设施损失范围", "确认人员特征、去向和最后出现位置", "确认现场是否已隔离并停止触碰"),
            "evidence": ("现场照片与视频", "物品或设施清单", "出入口与门禁记录"),
            "escalation": ("嫌疑人仍在现场", "涉及重要资产或受控区域", "存在暴力、工具或进一步破坏风险"),
            "actions": ("保护现场并暂停无关人员进入", "同步核查出入口和相关时段记录"),
        },
        "消防报警": {
            "keywords": ("消防", "烟雾", "明火", "火灾", "报警"),
            "checks": ("确认是否存在烟雾、明火或异味", "确认报警点位与人员疏散情况", "确认消防设施状态及现场危险源"),
            "evidence": ("报警主机点位记录", "现场照片或视频", "人员疏散和处置时间线"),
            "escalation": ("发现明火、浓烟或人员被困", "火势无法立即控制", "涉及危险品或关键设施"),
            "actions": ("按预案核实报警点位并优先组织人员安全", "严禁在未确认安全前贸然进入危险区域"),
        },
        "设备故障": {
            "keywords": ("故障", "失灵", "离线", "损坏", "断电"),
            "checks": ("确认故障设备、开始时间和影响范围", "确认是否影响门禁、监控或报警能力", "确认临时替代措施是否到位"),
            "evidence": ("设备告警记录", "现场故障照片", "维修工单和恢复时间"),
            "escalation": ("故障造成安防能力缺失", "核心设备或多设备同时故障", "无法在本班次恢复且无替代措施"),
            "actions": ("设置临时巡查或人工值守", "通知设备责任人并记录恢复进度"),
        },
        "可疑人员": {
            "keywords": ("可疑", "徘徊", "滞留", "尾随", "踩点"),
            "checks": ("确认人数、位置、行为和移动方向", "确认是否进入受控区域或接触重要设施", "确认现场人员是否已保持观察和安全距离"),
            "evidence": ("相关时段监控片段", "现场观察时间线", "涉及点位记录"),
            "escalation": ("拒不配合或试图闯入", "出现工具、器械或破坏行为", "多人协同行动或长时间踩点"),
            "actions": ("保持观察并通知就近岗位协同", "避免单人近距离接触未知风险人员"),
        },
        "其他": {
            "keywords": (),
            "checks": ("确认具体异常现象及影响对象", "确认事件是否仍在持续", "确认现场已采取的临时措施"),
            "evidence": ("现场时间线", "相关照片或记录", "处置人员反馈"),
            "escalation": ("出现人身危险", "影响范围扩大", "无法判断风险或无法控制"),
            "actions": ("先保护人员安全并控制影响范围", "交由值班主管复核事件类别和响应等级"),
        },
    }

    @staticmethod
    def _fields(task: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        for line in str(task or "").splitlines():
            if "：" not in line:
                continue
            key, value = line.split("：", 1)
            fields[key.strip()] = value.strip()
        return fields

    @classmethod
    def _information_gaps(cls, task: str) -> list[str]:
        fields = cls._fields(task)
        required = ("事件类别", "发生区域", "现场状态", "发生时间", "事件描述")
        gaps = []
        for name in required:
            value = fields.get(name, "")
            if not value or value in {"其他", "待补充", "待确认", "地点待确认", "状态待确认"}:
                gaps.append(name)
        if fields.get("事件描述") and len(fields["事件描述"]) < 12 and "事件描述" not in gaps:
            gaps.append("事件描述（需补充关键经过）")
        return gaps

    @classmethod
    def _profile(cls, task: str) -> tuple[str, dict]:
        fields = cls._fields(task)
        declared = fields.get("事件类别", "")
        if declared in cls.EVENT_PROFILES:
            return declared, cls.EVENT_PROFILES[declared]
        text = str(task or "")
        scored = []
        for name, profile in cls.EVENT_PROFILES.items():
            score = sum(1 for keyword in profile["keywords"] if keyword and keyword in text)
            scored.append((score, name, profile))
        score, name, profile = max(scored, key=lambda item: item[0])
        return (name, profile) if score else ("其他", cls.EVENT_PROFILES["其他"])

    def __init__(self, guard=None, llm_fn=None, external: bool = False, policy_engine=None):
        super().__init__(guard=guard, llm_fn=llm_fn, external=external)
        self.risk_agent = RiskAgent()
        self.urgency_agent = UrgencyAgent()
        self.policy_engine = policy_engine or PolicyMatchEngine()

    def run(self, task: str) -> dict:
        risk = self.risk_agent.score(task)
        urgency = self.urgency_agent.score(task)
        policy = self.policy_engine.match(task)
        plan = dict(self.RESPONSE_PLANS.get(risk["level"], self.RESPONSE_PLANS["unknown"]))
        gaps = list(self._information_gaps(task))
        gaps.extend(
            self.policy_engine.FACT_LABELS.get(name, name)
            for name in policy.get("missing_facts") or []
        )
        gaps = list(dict.fromkeys(gaps))
        event_type, profile = self._profile(task)
        sourced_actions = tuple(policy.get("sourced_actions") or [])
        general_actions = tuple(dict.fromkeys((*profile["actions"], *plan["actions"])))[:5]
        plan["actions"] = sourced_actions or general_actions
        plan["actions_source"] = "policy_clause" if sourced_actions else "general_safety_guidance"
        plan["actions_require_human_confirmation"] = not bool(sourced_actions)
        citation_text = "\n".join(
            f"- {item['document_code']} {item['clause_no']}《{item['document_title']}》：{item['clause_text']}"
            for item in policy.get("citations") or []
        )
        if not citation_text:
            citation_text = "未检索到可引用的制度条款；不得把通用建议表述为制度要求。"
        suggestion = self._llm(
            f"事件接报：\n{task}\n\n规则研判等级：{risk['level']}。"
            f"\n现场紧急程度：{urgency['level']}。"
            f"\n信息缺口：{'、'.join(gaps) if gaps else '无'}。"
            f"\n专项核查：{'；'.join(profile['checks'])}。"
            f"\n升级条件：{'；'.join(profile['escalation'])}。"
            f"\n可引用制度：\n{citation_text}"
            "\n请输出：1.已知事实 2.有条款依据的动作 3.需人工确认的建议 4.必须补充的信息。"
            "不得虚构条款、条款号、审批要求或已经完成的动作。",
            task_desc="事件处置",
        )
        manual_reasons = list(policy.get("manual_review_reasons") or [])
        if plan["actions_require_human_confirmation"]:
            manual_reasons.append("首轮动作未获得制度条款授权，仅作为通用安全建议")
        return {
            "agent": self.name,
            "status": "ok",
            "risk": risk,
            "urgency": urgency,
            "suggestion": suggestion or "请按响应方案先确认现场状态，补齐信息，并由值班主管确认后执行和留痕。",
            "information_gaps": gaps,
            "response_plan": plan,
            "extracted_facts": policy.get("facts") or {},
            "policy_match": policy,
            "policy_citations": policy.get("citations") or [],
            "policy_confidence": policy.get("confidence", 0.0),
            "follow_up_questions": policy.get("follow_up_questions") or [],
            "manual_review_reasons": list(dict.fromkeys(manual_reasons)),
            "event_profile": event_type,
            "verification_questions": list(profile["checks"]),
            "evidence_to_preserve": list(profile["evidence"]),
            "escalation_triggers": list(profile["escalation"]),
            "requires_human_confirmation": True,
            "decision_status": "待值班主管确认",
            "task": task,
        }
