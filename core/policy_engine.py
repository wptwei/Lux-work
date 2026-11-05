"""可扩展的管理制度知识库、事件事实提取与条款匹配。

生产条款全部从 ``knowledge/policies/*.json`` 加载。本模块不内置任何制度动作，
因此在源文件缺失或没有满足适用条件的条款时，只会要求补充信息或转人工确认。
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge" / "policies"


def _normalise_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _ngrams(value: str, size: int = 2) -> Counter[str]:
    text = _normalise_text(value)
    if not text:
        return Counter()
    if len(text) < size:
        return Counter({text: 1})
    return Counter(text[index:index + size] for index in range(len(text) - size + 1))


def _cosine(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(value * right.get(key, 0) for key, value in left.items())
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


@dataclass(frozen=True)
class PolicyDocument:
    code: str
    title: str
    source_status: str
    source_file: str
    version: str
    clauses: tuple[dict, ...]


class PolicyKnowledgeBase:
    """从磁盘读取并校验制度数据；新增制度不需要修改 Agent 代码。"""

    def __init__(self, root: str | Path = DEFAULT_KNOWLEDGE_DIR):
        self.root = Path(root)
        self.documents, self.errors = self._load()

    @staticmethod
    def _validate_clause(document: dict, clause: dict, source_name: str) -> dict:
        required = ("id", "clause_no", "title", "text")
        missing = [name for name in required if not str(clause.get(name, "")).strip()]
        if missing:
            raise ValueError(f"{source_name} 条款缺少字段：{', '.join(missing)}")
        actions = clause.get("actions") or []
        if not isinstance(actions, list) or any(not isinstance(item, str) for item in actions):
            raise ValueError(f"{source_name} 条款 {clause['id']} 的 actions 必须是字符串数组")
        conditions = clause.get("conditions") or []
        if not isinstance(conditions, list) or any(not isinstance(item, dict) for item in conditions):
            raise ValueError(f"{source_name} 条款 {clause['id']} 的 conditions 必须是对象数组")
        normalised = dict(clause)
        normalised["document_code"] = str(document["code"]).strip()
        normalised["document_title"] = str(document["title"]).strip()
        normalised["source_file"] = str(document.get("source_file", "")).strip()
        normalised["keywords"] = [str(item).strip() for item in clause.get("keywords") or [] if str(item).strip()]
        normalised["semantic_terms"] = [
            str(item).strip() for item in clause.get("semantic_terms") or [] if str(item).strip()
        ]
        normalised["required_facts"] = [
            str(item).strip() for item in clause.get("required_facts") or [] if str(item).strip()
        ]
        normalised["questions"] = {
            str(key): str(value) for key, value in (clause.get("questions") or {}).items()
        }
        return normalised

    def _load(self) -> tuple[list[PolicyDocument], list[str]]:
        documents: list[PolicyDocument] = []
        errors: list[str] = []
        if not self.root.exists():
            return documents, [f"制度库目录不存在：{self.root}"]
        for path in sorted(self.root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                document = payload.get("document") or {}
                if not str(document.get("code", "")).strip() or not str(document.get("title", "")).strip():
                    raise ValueError("document.code 和 document.title 不能为空")
                raw_clauses = payload.get("clauses") or []
                if not isinstance(raw_clauses, list):
                    raise ValueError("clauses 必须是数组")
                clauses = tuple(self._validate_clause(document, item, path.name) for item in raw_clauses)
                documents.append(PolicyDocument(
                    code=str(document["code"]).strip(),
                    title=str(document["title"]).strip(),
                    source_status=str(document.get("source_status", "ready")).strip() or "ready",
                    source_file=str(document.get("source_file", "")).strip(),
                    version=str(document.get("version", "")).strip(),
                    clauses=clauses,
                ))
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                errors.append(f"{path.name}: {exc}")
        return documents, errors

    @property
    def clauses(self) -> list[dict]:
        return [clause for document in self.documents for clause in document.clauses]

    def status(self) -> list[dict]:
        return [
            {
                "code": item.code,
                "title": item.title,
                "source_status": item.source_status,
                "source_file": item.source_file,
                "version": item.version,
                "clause_count": len(item.clauses),
            }
            for item in self.documents
        ]


class IncidentFactExtractor:
    """可解释的本地事实抽取；只记录原文明确表达的事实。"""

    SUBJECT_TERMS = {
        "supplier": ("供应商", "承包商", "厂商", "外协", "施工单位"),
        "ict": ("ict", "it人员", "信息技术人员", "运维人员", "网络工程师"),
        "visitor": ("访客", "来访人员"),
        "employee": ("员工", "内部人员"),
    }
    ACTIVITY_TERMS = {
        "entry": ("入场", "进入", "进场", "门禁", "入园"),
        "exit": ("离场", "出场", "离开"),
        "work": ("作业", "施工", "维修", "运维", "检修"),
        "device_access": ("接入", "联网", "电脑", "笔记本", "设备"),
    }
    BOOLEAN_MARKERS = {
        "approved": (("已审批", "审批通过", "已批准", "已授权"), ("未审批", "未经审批", "未授权")),
        "registered": (("已登记", "完成登记"), ("未登记", "没有登记")),
        "escorted": (("有人陪同", "已陪同", "全程陪同"), ("无人陪同", "未陪同")),
        "credential_valid": (("证件有效", "证件正常"), ("证件过期", "无证", "未带证件")),
    }

    @staticmethod
    def _structured_fields(text: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        for line in str(text or "").splitlines():
            if "：" in line:
                key, value = line.split("：", 1)
                fields[key.strip()] = value.strip()
        return fields

    @staticmethod
    def _boolean_fact(text: str, positive: tuple[str, ...], negative: tuple[str, ...]) -> bool | None:
        negative_hits = [(text.find(marker), False) for marker in negative if marker in text]
        positive_hits = [(text.find(marker), True) for marker in positive if marker in text]
        hits = negative_hits + positive_hits
        if not hits:
            return None
        return sorted(hits, key=lambda item: item[0])[-1][1]

    def extract(self, text: str) -> dict:
        raw = str(text or "")
        lower = raw.lower()
        fields = self._structured_fields(raw)
        subject_types = [
            name for name, terms in self.SUBJECT_TERMS.items()
            if any(term.lower() in lower for term in terms)
        ]
        activities = [
            name for name, terms in self.ACTIVITY_TERMS.items()
            if any(term.lower() in lower for term in terms)
        ]
        boolean_facts = {
            name: self._boolean_fact(lower, tuple(x.lower() for x in positive), tuple(x.lower() for x in negative))
            for name, (positive, negative) in self.BOOLEAN_MARKERS.items()
        }
        known_boolean_facts = {name: value for name, value in boolean_facts.items() if value is not None}
        return {
            "event_type": fields.get("事件类别", ""),
            "region": fields.get("发生区域", ""),
            "reported_status": fields.get("现场状态", ""),
            "occurred_at": fields.get("发生时间", ""),
            "description": fields.get("事件描述", raw if not fields else ""),
            "subject_types": subject_types,
            "activities": activities,
            "facts": known_boolean_facts,
        }


class PolicyMatchEngine:
    """关键词召回 + 字符语义相似度 + 条款适用条件校验。"""

    FACT_LABELS = {
        "event_type": "事件类别",
        "region": "发生区域",
        "reported_status": "现场状态",
        "occurred_at": "发生时间",
        "description": "事件描述",
        "subject_types": "人员身份/所属单位",
        "activities": "现场活动类型",
        "approved": "审批/授权状态",
        "registered": "登记状态",
        "escorted": "陪同状态",
        "credential_valid": "证件有效状态",
    }

    def __init__(
        self,
        knowledge_dir: str | Path = DEFAULT_KNOWLEDGE_DIR,
        *,
        low_confidence_threshold: float = 0.55,
        extractor: IncidentFactExtractor | None = None,
    ):
        self.knowledge = PolicyKnowledgeBase(knowledge_dir)
        self.low_confidence_threshold = float(low_confidence_threshold)
        self.extractor = extractor or IncidentFactExtractor()

    @staticmethod
    def _fact_value(facts: dict, name: str):
        if name in facts:
            return facts.get(name)
        return (facts.get("facts") or {}).get(name)

    @classmethod
    def _missing(cls, facts: dict, name: str) -> bool:
        value = cls._fact_value(facts, name)
        return value is None or value == "" or value == []

    @classmethod
    def _validate_conditions(cls, clause: dict, facts: dict) -> tuple[str, list[str], list[str]]:
        missing: list[str] = []
        mismatches: list[str] = []
        for name in clause.get("required_facts") or []:
            if cls._missing(facts, name):
                missing.append(name)
        for condition in clause.get("conditions") or []:
            name = str(condition.get("fact", "")).strip()
            if not name:
                continue
            actual = cls._fact_value(facts, name)
            if cls._missing(facts, name):
                missing.append(name)
                continue
            operator = str(condition.get("operator", "equals"))
            expected = condition.get("value")
            values = condition.get("values") or ([expected] if expected is not None else [])
            if operator == "contains":
                ok = expected in actual if isinstance(actual, (list, str)) else False
            elif operator == "intersects":
                ok = bool(set(actual if isinstance(actual, list) else [actual]) & set(values))
            elif operator == "in":
                ok = actual in values
            elif operator == "not_equals":
                ok = actual != expected
            else:
                ok = actual == expected
            if not ok:
                mismatches.append(name)
        if mismatches:
            return "not_applicable", sorted(set(missing)), sorted(set(mismatches))
        if missing:
            return "needs_information", sorted(set(missing)), []
        return "applicable", [], []

    @staticmethod
    def _candidate_score(text: str, clause: dict) -> tuple[float, list[str], float]:
        normalised = _normalise_text(text)
        keyword_hits = [item for item in clause.get("keywords") or [] if _normalise_text(item) in normalised]
        keywords = clause.get("keywords") or []
        keyword_score = len(keyword_hits) / max(1, min(3, len(keywords)))
        semantic_source = " ".join([
            str(clause.get("title", "")),
            str(clause.get("text", "")),
            *[str(item) for item in clause.get("semantic_terms") or []],
        ])
        semantic_score = _cosine(_ngrams(text), _ngrams(semantic_source))
        # 录入的同义表达若被原句直接命中，作为强语义召回信号。
        if any(_normalise_text(term) in normalised for term in clause.get("semantic_terms") or []):
            semantic_score = max(semantic_score, 0.8)
        score = min(1.0, keyword_score * 0.65 + semantic_score * 0.35)
        return score, keyword_hits, semantic_score

    def match(self, text: str, *, top_k: int = 5) -> dict:
        facts = self.extractor.extract(text)
        candidates: list[dict] = []
        for clause in self.knowledge.clauses:
            retrieval_score, keyword_hits, semantic_score = self._candidate_score(text, clause)
            # 候选召回阈值保持宽松，最终是否可引用还要经过适用条件和置信度阈值。
            if retrieval_score < 0.07:
                continue
            applicability, missing, mismatches = self._validate_conditions(clause, facts)
            if applicability == "not_applicable":
                continue
            confidence = retrieval_score * (1.0 if applicability == "applicable" else 0.72)
            candidates.append({
                "document_code": clause["document_code"],
                "document_title": clause["document_title"],
                "clause_id": clause["id"],
                "clause_no": clause["clause_no"],
                "clause_title": clause["title"],
                "clause_text": clause["text"],
                "source_file": clause.get("source_file", ""),
                "actions": list(clause.get("actions") or []),
                "applicability": applicability,
                "missing_facts": missing,
                "keyword_hits": keyword_hits,
                "semantic_score": round(semantic_score, 3),
                "confidence": round(confidence, 3),
            })
        candidates.sort(key=lambda item: item["confidence"], reverse=True)
        matches = candidates[:max(1, int(top_k))]
        applicable = [item for item in matches if item["applicability"] == "applicable"]
        best_confidence = max((item["confidence"] for item in applicable), default=0.0)
        missing_facts = sorted({name for item in matches for name in item["missing_facts"]})
        questions = []
        for name in missing_facts:
            custom = next(
                (
                    clause.get("questions", {}).get(name)
                    for clause in self.knowledge.clauses
                    if clause.get("questions", {}).get(name)
                ),
                "",
            )
            questions.append(custom or f"请补充{self.FACT_LABELS.get(name, name)}。")
        citations = [
            {
                "document_code": item["document_code"],
                "document_title": item["document_title"],
                "clause_no": item["clause_no"],
                "clause_title": item["clause_title"],
                "clause_text": item["clause_text"],
                "source_file": item["source_file"],
            }
            for item in applicable
        ]
        sourced_actions = list(dict.fromkeys(
            action for item in applicable for action in item.get("actions") or []
        ))
        source_unavailable = [
            item for item in self.knowledge.status()
            if item["source_status"] != "ready" or item["clause_count"] == 0
        ]
        reasons: list[str] = []
        if self.knowledge.errors:
            reasons.append("制度库存在加载错误")
        if not matches:
            reasons.append("未检索到候选条款")
        elif not applicable:
            reasons.append("候选条款适用条件尚未满足")
        if best_confidence < self.low_confidence_threshold:
            reasons.append("制度匹配置信度不足")
        if source_unavailable and not self.knowledge.clauses:
            reasons.append("目标制度原文尚未导入")
        requires_human = bool(reasons or missing_facts)
        return {
            "facts": facts,
            "matches": matches,
            "citations": citations,
            "sourced_actions": sourced_actions,
            "missing_facts": missing_facts,
            "follow_up_questions": questions,
            "confidence": round(best_confidence, 3),
            "threshold": self.low_confidence_threshold,
            "decision": "matched" if applicable and not requires_human else "human_review",
            "requires_human_confirmation": requires_human,
            "manual_review_reasons": list(dict.fromkeys(reasons)),
            "knowledge_status": self.knowledge.status(),
            "knowledge_errors": list(self.knowledge.errors),
        }
