"""周报 Agent V8：状态机驱动的 AI 语义分析、事实血缘与证据编译引擎。"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from core.weekly_report_agent import (
    CRITICAL_CATEGORIES,
    build_weekly_analysis_prompt,
    build_weekly_compact_analysis_prompt,
    compile_grounded_weekly_page,
    critical_fact_coverage_issues,
    extract_weekly_facts,
    make_offline_analysis,
    normalize_weekly_analysis,
    parse_json_object,
    validate_report_grounding,
    validate_weekly_report,
    weekly_content_quality_issues,
    weekly_page_generation_issues,
)


class Stage(str, Enum):
    INGEST = "素材理解"
    SEMANTIC = "第一阶段：AI 语义分析"
    COMPILE = "第二阶段：事实证据编译"
    VALIDATE = "第三阶段：结构、来源与质量核验"
    COMPLETE = "完成"


class WeeklyEngineError(RuntimeError):
    def __init__(self, stage: str, reason: str, facts=None, analysis=None, model_attempted=False):
        super().__init__(reason)
        self.stage = stage
        self.reason = reason
        self.facts = facts or []
        self.analysis = analysis or {}
        self.model_attempted = bool(model_attempted)


@dataclass
class EngineResult:
    report: str
    facts: list[dict]
    analysis: dict
    quality_issues: list[str]
    status: str
    page_count: int
    evidence_compiled_pages: int
    ai_summary_pages: int
    trace: list[dict]
    lineage: dict[str, dict]
    validation_matrix: dict[str, object]
    quality_score: int


class WeeklyReportEngine:
    """纯业务引擎；模型调用由上层注入，便于测试、审计和更换提供商。"""

    def __init__(self, model_call: Callable[[str, int], dict], external_model: bool = False):
        self.model_call = model_call
        self.external_model = bool(external_model)
        self.trace: list[dict] = []

    def _record(self, stage: Stage, started: float, **details) -> None:
        self.trace.append({
            "stage": stage.value,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            **details,
        })

    @staticmethod
    def _critical_coverage(facts: list[dict], analysis: dict) -> list[str]:
        return critical_fact_coverage_issues(facts, analysis)

    @staticmethod
    def _stable_page_body(page: dict, facts: list[dict], period: str, page_plan: list[dict]) -> str | None:
        page_type = page.get("page_type")
        if page_type == "cover":
            lines = [f"统计周期：{period}" if period else "统计周期：请确认"]
            for fact in facts:
                if fact.get("category") == "context" and re.search(r"所服务\s*BU|服务\s*BU", fact.get("text", ""), re.I):
                    lines.append(fact["text"])
            return "\n".join(lines)
        if page_type == "contents":
            pages = [item for item in page_plan if item.get("page_type") not in {"cover", "contents", "closing"}]
            return "\n".join(f"{idx:02d}  {item.get('title', '工作进展')}" for idx, item in enumerate(pages, 1))
        if page_type == "closing":
            return "华东安保二部 · H区\nThankyou"
        return None

    def _semantic_analysis(self, facts: list[dict], period: str) -> dict:
        fallback = make_offline_analysis(facts, period)
        fixed_plan = None if self.external_model else fallback.get("page_plan", [])
        if fixed_plan is None:
            prompt = build_weekly_analysis_prompt(facts, period)
            token_limit = 1536
        else:
            prompt = build_weekly_compact_analysis_prompt(facts, fixed_plan, period)
            token_limit = 640

        last_reason = "模型未返回结果"
        for attempt in range(2):
            result = self.model_call(prompt, token_limit)
            if result.get("status") != "ok":
                raise WeeklyEngineError(
                    Stage.SEMANTIC.value,
                    str(result.get("reason", "模型调用被安全策略拒绝"))[:220],
                    facts,
                    fallback,
                    model_attempted=result.get("status") != "rejected",
                )
            try:
                payload = parse_json_object(result.get("output", ""))
                if fixed_plan is not None:
                    raw_summary = payload.get("executive_summary")
                    summary_text = raw_summary.get("text", "") if isinstance(raw_summary, dict) else raw_summary
                    if not (str(summary_text or "").strip() or any(payload.get(key) for key in ("highlights", "data_insights", "risks", "next_week"))):
                        raise ValueError("[schema] 模型返回的语义分析为空")
                    payload["page_plan"] = fixed_plan
                analysis, errors = normalize_weekly_analysis(payload, facts)
                if errors:
                    raise ValueError("[schema] " + "；".join(errors[:3]))
                coverage = self._critical_coverage(facts, analysis)
                if coverage:
                    raise ValueError("[coverage] " + "；".join(coverage))
                if fixed_plan is not None:
                    analysis["warnings"] = list(analysis.get("warnings", []))
                    analysis["warnings"].append(
                        "本地 CPU 稳定模式：AI 只做一次语义分析；业务正文由事实血缘编译。"
                    )
                return analysis
            except (ValueError, TypeError) as exc:
                last_reason = str(exc).strip().replace("\n", " ")[:240] or type(exc).__name__
                if "[schema]" not in last_reason and "[coverage]" not in last_reason:
                    last_reason = "[non_json] " + last_reason
                if attempt == 0:
                    prompt += (
                        "\n\n只修复结构化语义分析。上一版失败原因：" + last_reason
                        + "。请输出单个完整 JSON 对象；不得写正文或解释。"
                    )
                    continue
        raise WeeklyEngineError(Stage.SEMANTIC.value, last_reason, facts, fallback, model_attempted=True)

    def run(self, source: str, period: str = "", on_progress=None) -> EngineResult:
        ingest_started = time.perf_counter()
        facts = extract_weekly_facts(source)
        if not facts or not any(fact.get("category") != "context" for fact in facts):
            raise WeeklyEngineError(Stage.INGEST.value, "未识别到足够的业务事实；请补充工作事项、数据或明确计划", facts)
        self._record(Stage.INGEST, ingest_started, fact_count=len(facts))

        semantic_started = time.perf_counter()
        try:
            analysis = self._semantic_analysis(facts, period)
        except WeeklyEngineError:
            raise
        except Exception as exc:
            reason = str(exc).strip().replace("\n", " ")[:220] or type(exc).__name__
            raise WeeklyEngineError(Stage.SEMANTIC.value, reason, facts, make_offline_analysis(facts, period), True) from exc
        self._record(Stage.SEMANTIC, semantic_started, warning_count=len(analysis.get("warnings", [])))

        page_plan = analysis.get("page_plan", [])
        if on_progress:
            on_progress("", analysis, facts, 0, len(page_plan))
        compile_started = time.perf_counter()
        blocks, lineage = [], {}
        evidence_pages = ai_summary_pages = 0
        for page in page_plan:
            page_no = int(page["page"])
            page_type = str(page.get("page_type", "generic"))
            title = str(page.get("title") or f"P{page_no}")
            body = self._stable_page_body(page, facts, period, page_plan)
            ai_summary = False
            compiler = "stable_template"
            if body is None:
                body, ai_summary = compile_grounded_weekly_page(facts, analysis, page)
                compiler = "evidence_compiler"
                evidence_pages += 1
                ai_summary_pages += int(ai_summary)
                issues = weekly_page_generation_issues(body, page, facts, analysis)
                if issues:
                    raise WeeklyEngineError(
                        f"{Stage.COMPILE.value}：P{page_no}「{title}」",
                        "；".join(issues[:3]), facts, analysis, True,
                    )
            if not body.strip():
                raise WeeklyEngineError(f"{Stage.COMPILE.value}：P{page_no}", "页面正文为空", facts, analysis, True)
            blocks.append(f"## P{page_no} [type:{page_type}] | {title}\n{body.strip()}")
            lineage[f"P{page_no}"] = {
                "page_type": page_type,
                "fact_ids": list(page.get("fact_ids", [])),
                "compiler": compiler,
                "ai_summary": ai_summary,
            }
            analysis["completed_pages"] = len(blocks)
            if on_progress:
                on_progress("\n\n".join(blocks), analysis, facts, len(blocks), len(page_plan))
        report = "\n\n".join(blocks)
        self._record(Stage.COMPILE, compile_started, page_count=len(blocks), evidence_pages=evidence_pages)

        validate_started = time.perf_counter()
        structure = validate_weekly_report(report)
        grounding = validate_report_grounding(report, facts, analysis) if structure.get("valid") else ["周报结构不完整"]
        quality = weekly_content_quality_issues(report) if structure.get("valid") and not grounding else []
        matrix = {
            "structure_valid": bool(structure.get("valid")),
            "grounding_valid": not grounding,
            "quality_valid": not quality,
            "critical_coverage_valid": not self._critical_coverage(facts, analysis),
            "grounding_errors": grounding[:8],
            "quality_issues": quality[:8],
        }
        if not structure.get("valid"):
            raise WeeklyEngineError(Stage.VALIDATE.value, "页面编号不连续、页数无效或存在空白内容页", facts, analysis, True)
        if grounding:
            raise WeeklyEngineError(Stage.VALIDATE.value, "；".join(grounding[:3]), facts, analysis, True)
        self._record(Stage.VALIDATE, validate_started, quality_issue_count=len(quality))

        assigned = {fid for page in page_plan for fid in page.get("fact_ids", [])}
        critical = [fact for fact in facts if fact.get("category") in CRITICAL_CATEGORIES]
        coverage_ratio = (sum(fact["id"] in assigned for fact in critical) / len(critical)) if critical else 1.0
        score = round(100 * coverage_ratio - min(20, len(quality) * 10) - min(10, len(analysis.get("warnings", []))))
        score = max(0, min(100, score))
        status = "needs_review" if quality else "completed"
        return EngineResult(
            report=report,
            facts=facts,
            analysis=analysis,
            quality_issues=quality,
            status=status,
            page_count=int(structure.get("page_count", 0)),
            evidence_compiled_pages=evidence_pages,
            ai_summary_pages=ai_summary_pages,
            trace=self.trace,
            lineage=lineage,
            validation_matrix=matrix,
            quality_score=score,
        )
