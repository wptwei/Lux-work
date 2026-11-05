"""奖惩条款检索：只提供证据引用，不自动执行人事处分或确定罚金。"""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
from collections import Counter
from math import log
from difflib import SequenceMatcher

RULES_PATH = Path(__file__).resolve().parent.parent / "knowledge" / "reward_penalty_rules.json"

# 条款必须带的字段；缺失的条款不参与匹配，并在健康检查中报告。
REQUIRED_RULE_FIELDS = (
    "document_code", "document_title", "source_page", "clause_no",
    "event", "original_text", "penalty", "audience", "kind", "rule_id",
)

# 事实条件探针：条款提到该条件、但报告没有相应证据时，明确列为“尚缺适用条件”。
_CONDITION_PROBES = (
    ("escort", ("未由", "陪同", "对接部门"), ("陪同", "带领", "随同", "对接部门"),
     "是否由对接部门人员陪同"),
    ("entry", ("进入", "闯入", "越过", "穿越"), ("进入", "闯入", "越过", "穿越", "滞留", "拦截", "劝阻"),
     "人员是否实际进入受限区域"),
    ("intercept", ("拦截", "劝阻", "制止"), ("拦截", "阻拦", "劝阻", "制止"),
     "是否被执勤人员拦截或劝阻"),
    ("report", ("举报", "检举", "揭发", "制止"), ("举报", "检举", "揭发", "制止"),
     "是否存在举报、检举或制止行为"),
    ("amount", ("2000", "5000", "元", "金额"), ("2000", "两千", "二千", "5000", "五千", "元"),
     "是否达到条款规定的金额阈值"),
    ("device", ("监控", "设备", "摄像头", "道闸", "封条", "门禁", "安防设施"),
     ("监控", "设备", "摄像头", "道闸", "封条", "门禁", "安防设施"),
     "是否涉及监控或安防设备"),
    ("history", ("首次", "第一次", "再次", "多次", "屡次", "历史"),
     ("首次", "第一次", "再次", "多次", "屡次", "历史"),
     "是否为首次发生或已有历史记录"),
)
_TIME_RE = re.compile(r"\d{1,2}\s*[:：点]\s*\d{0,2}|今天|昨天|上午|下午|晚上|凌晨")
_PLACE_RE = re.compile(r"(入口|大门|门岗|通道|仓库|车间|楼|层|区域|专区|现场|路口|停车场)")


def compact(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", str(text).lower())


def core_text(text: str) -> str:
    """去除角色和地点通用词，重点比较具体行为。"""
    value = compact(text)
    for stop in ("供应商", "承包商", "厂商", "外协", "ict人员", "员工", "内部人员", "npi区域", "mp区域", "npi", "mp", "保密区域", "保密区", "安保区", "专区", "区域内", "区域", "公司", "一名", "一位", "某人"):
        value = value.replace(stop, "")
    return value


def grams(text: str, size: int = 2) -> set[str]:
    value = core_text(text)
    return {value[i:i + size] for i in range(max(0, len(value) - size + 1))} if len(value) >= size else set()


class RewardPenaltyMatcher:
    def __init__(self, path: str | Path = RULES_PATH):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        raw_rules = list(payload.get("rules") or [])
        # 只让字段完整的条款参与匹配；缺失条款计入健康检查，避免“规则未加载”被当成“无匹配”。
        self.rules = [
            rule for rule in raw_rules
            if all(str(rule.get(field, "")).strip() for field in REQUIRED_RULE_FIELDS)
        ]
        self.rejected_rules = len(raw_rules) - len(self.rules)
        self.entries = [(r, grams(r["event"])) for r in self.rules]
        self.document_frequency = Counter(g for _, terms in self.entries for g in terms)
        # 规则版本：匹配结果留痕与重跑去重用；规则内容变化会产生新任务/新批次。
        self.ruleset_version = hashlib.sha256(
            json.dumps(self.rules, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:12]
        self.matcher_version = "reward-penalty-v1"

    def health(self) -> dict:
        """只返回规则完整性元数据，不包含条款原文。"""
        return {
            "rules_loaded": len(self.rules),
            "rules_rejected": self.rejected_rules,
            "documents": len({r["document_code"] for r in self.rules}),
            "source_pages_missing": sum(1 for r in self.rules if not str(r.get("source_page", "")).strip()),
            "ruleset_version": self.ruleset_version,
            "matcher_version": self.matcher_version,
        }

    @staticmethod
    def _missing_conditions(rule: dict, text: str, audience: str) -> list[str]:
        """列出条款需要、但报告尚未提供的事实条件；不推测事实。"""
        rule_text = compact(str(rule.get("event", "")) + str(rule.get("penalty", "")))
        query = compact(text)
        missing: list[str] = []
        for _key, rule_terms, report_terms, label in _CONDITION_PROBES:
            if any(compact(term) in rule_text for term in rule_terms) and not any(
                compact(term) in query for term in report_terms
            ):
                missing.append(label)
        if audience == "unknown":
            missing.append("人员类别（供应商/员工）未确认")
        if not _TIME_RE.search(text):
            missing.append("发生时间未提供")
        if not _PLACE_RE.search(text):
            missing.append("具体发生地点未提供")
        return list(dict.fromkeys(missing))

    def match(self, text: str, limit: int = 3) -> dict:
        if not self.rules:
            return {"status": "rules_missing", "matches": [],
                    "message": "奖惩规则库未加载或没有完整条款，现场记录已保存，请检查规则配置后重试。"}
        query = compact(text)
        if len(query) < 5:
            return {"status": "needs_information", "matches": [], "message": "请补充至少5个字的异常事件描述。"}
        supplier = bool(re.search(r"供应商|厂商|承包商|外协", text))
        employee = bool(re.search(r"ICT人员|公司员工|员工|内部人员|我司人员", text))
        if supplier and not employee: audience = "supplier"
        elif employee and not supplier: audience = "ict"
        else: audience = "unknown"
        # 现场描述常含时间、巡查经过和后续处理；按标点及人员主体分段评分，
        # 避免背景文字稀释具体行为。保留全文，兼容跨分句表达。
        sources = {core_text(query)} | {
            core_text(part) for part in re.split(
                r"[，,。；;！!？?\n\r]+|(?=供应商|厂商|承包商|外协|ICT人员|员工|内部人员|我司人员)", text
            )
            if len(core_text(part)) >= 4
        }
        def weight(token: str) -> float:
            return max(0.5, log(2.0 + len(self.entries) / (1 + self.document_frequency[token])))
        scoring = []
        for rule, tokens in self.entries:
            if audience != "unknown" and rule["audience"] != audience:
                continue
            # 稀有行为短语优先；常见的“供应商”“专区”等词不应造成误匹配。
            token_weight = sum(weight(token) for token in tokens)
            extras = [core_text(x) for x in rule.get("synonyms", [])]
            score = 0.0
            for source in sources:
                qgrams = grams(source)
                total_weight = sum(weight(token) for token in qgrams)
                evidence = sum(weight(token) for token in qgrams & tokens)
                recall = evidence / total_weight if total_weight else 0.0
                precision = evidence / token_weight if token_weight else 0.0
                base = .90 * recall + .10 * precision
                alias = max((SequenceMatcher(None, source, a).ratio() for a in extras), default=0.0)
                exact = any(len(a) >= 2 and (a in source or source in a and len(source) >= 4) for a in extras)
                score = max(score, base, .88 if exact else (.72 * alias if alias >= .66 else 0.0))
            # 明确提及被阻拦与已经进入并非同一个事实；没有证据时不给出确定分档。
            rtext = compact(rule["event"])
            if any(x in rtext for x in ("被警卫拦截", "被执勤保安拦截")) and not any(x in query for x in ("拦截", "阻拦", "劝阻", "制止")):
                score *= .70
            if rule["group"] in ("6.5.1.4", "6.5.7.5") and not any(x in query for x in ("泄露", "泄密", "丢失", "重大损失", "盗窃", "盗取")):
                score *= .45
            # 必须具备关键事实的奖励条款，不能仅因“发现异常”便给出高等级奖励。
            if rule["clause_no"] in ("6.5.6.2.1",) and not any(x in query for x in ("2000", "两千", "二千")):
                score *= .5
            if rule["clause_no"] in ("6.5.6.3.1", "6.5.6.3.2", "6.5.6.5") and not any(x in query for x in ("5000", "五千")):
                score *= .5
            if rule["clause_no"] == "6.5.6.1.5" and not any(x in query for x in ("监控", "设备", "摄像头", "道闸", "封条", "门禁", "安防设施")):
                score *= .45
            # 泛称'违规'不能覆盖案情，除非文本确实表达了举报行为。
            if rule["kind"] == "奖励" and rule["clause_no"] in ("6.4.2", "6.5.6.4") and not any(x in query for x in ("举报", "检举", "揭发", "制止")):
                score *= .40
            if score >= .24:
                scoring.append((score,rule))
        scoring.sort(key=lambda item:item[0],reverse=True)
        # 只显示接近最佳分数的少数竞争条款；避免低相关候选误导使用者。
        best = scoring[0][0] if scoring else 0
        comparable = [(score,rule) for score,rule in scoring if score >= best * .84][:max(1,min(limit,5))]
        top=[]
        for score, rule in comparable:
            item={k:rule[k] for k in ("document_code","document_title","document_version","source_page","clause_no","event","original_text","audience","kind","penalty","group","rule_id")}
            item["score"]=round(score,3)
            item["status"]="系统匹配建议·需核对"
            item["missing_conditions"]=self._missing_conditions(rule, text, audience)
            item["basis"]=(
                f"本地规则将报告行为与条款“{rule['event']}”比对，相关性 {score:.0%}；"
                "未使用大模型推断，也未认定事实或责任。"
            )
            item["source_page_label"]=(
                f"第{rule['source_page']}页" if str(rule.get("source_page", "")).strip() else "页码未提供"
            )
            top.append(item)
        if not top:
            return {"status":"no_match","matches":[],"message":"未找到足够可靠的奖惩条款；不能据此确定奖惩。"}
        return {"status":"candidate","matches":top,"message":"已找到制度候选条款，请核对人员类别、事件事实、违规次数及适用条件。"}
