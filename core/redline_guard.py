"""
数据红线守卫 —— 所有大模型调用的唯一出口。

铁律：业务代码【禁止】直接 import openai / 直接调模型 API，
必须通过 RedlineGuard.call_llm() 调用，否则红线机制形同虚设。

三道闸：
  闸1 输入识别  → scan()
  闸2 脱敏/路由  → desensitize() / block
  闸3 输出复检  → call_llm 内部自动再 scan 一遍
"""
from __future__ import annotations

import json
import re
import hashlib
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

from core.region_policy import RegionPolicy
from core.paths import config_path as default_config_path, log_path


_LOG_LOCK = threading.Lock()


class RedlineGuard:
    def __init__(
        self,
        config_path: str | Path | None = None,
        log_dir: str | Path | None = None,
        region_policy: RegionPolicy | None = None,
    ):
        self.config_path = Path(config_path) if config_path else (
            default_config_path("redline_rules.json")
        )
        with open(self.config_path, encoding="utf-8") as f:
            self.config = json.load(f)

        self.log_dir = Path(log_dir) if log_dir else log_path()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.region_policy = region_policy or RegionPolicy()

        # 预编译正则
        self._regexes = []
        for name, rule in self.config.get("regex_patterns", {}).items():
            self._regexes.append({
                "name": name,
                "prog": re.compile(rule["pattern"]),
                "action": rule["action"],
                "label": rule["label"],
                "exclude_date_like": bool(rule.get("exclude_date_like", False)),
            })

    # ---------------- 闸1：输入扫描 ----------------
    def scan(
        self,
        text: str = "",
        file_path: str | None = None,
        source_type: str = "text",
        region: str | None = None,
    ):
        """
        返回 (level, hits, mapping)
          level  : 'block' | 'review' | 'desensitize' | 'allow'
          hits   : 命中明细（用于日志和展示）
          mapping: {占位符: 原文}，用于脱敏后还原
        """
        text = text or ""
        hits: list[dict] = []
        mapping: dict[str, str] = {}
        block_hit = False
        review_hit = False
        desensitize_hit = False

        # 1) 正则命中（优先，因为要做占位符替换）
        for r in self._regexes:
            for m in r["prog"].finditer(text):
                original = m.group()
                if r["exclude_date_like"] and self._looks_like_date(original):
                    continue
                placeholder = f"【{r['label']}_{len(mapping) + 1}】"
                mapping[placeholder] = original
                hits.append({"category": r["label"], "term": original, "action": r["action"]})
                if r["action"] == "block":
                    block_hit = True
                else:
                    desensitize_hit = True

        # 2) 词表命中（长词优先，避免"H区"破坏"H区3F"）
        cat_label = {"customers": "客户", "production_lines": "区域", "company_header": "单位"}
        term_hits = []
        for category, terms in self.config.get("sensitive_terms", {}).items():
            for term in terms:
                if term and term in text:
                    term_hits.append((cat_label.get(category, category), term))
        term_hits.sort(key=lambda x: -len(x[1]))
        for label, term in term_hits:
            hits.append({"category": label, "term": term, "action": "desensitize"})
            desensitize_hit = True
            if term not in mapping.values():  # 同一原文只建一个占位符
                mapping[f"【{label}_{len(mapping) + 1}】"] = term

        # 3) 文字硬红线。资产类名词不放在这里，避免正常讨论被误拦。
        for term in self.config.get("block_terms", []):
            if term and term.lower() in text.lower():
                hits.append({"category": "高敏内容", "term": term, "action": "block"})
                block_hit = True

        # 4) 文件型资产只在上传链路识别。文件名是风险证据，不冒充内容识别。
        asset_hits = self._scan_asset(text, file_path, source_type, region)
        if asset_hits:
            hits.extend(asset_hits)
            review_hit = True

        if block_hit:
            level = "block"
        elif review_hit:
            level = "review"
        elif desensitize_hit or mapping:
            level = "desensitize"
        else:
            level = "allow"

        return level, hits, mapping

    @staticmethod
    def _looks_like_date(value: str) -> bool:
        """8 位数字若是有效的 YYYYMMDD（1900—2100 年），视为日期而非工号。"""
        if not re.fullmatch(r"\d{8}", value):
            return False
        year, month, day = int(value[:4]), int(value[4:6]), int(value[6:])
        if not (1900 <= year <= 2100 and 1 <= month <= 12):
            return False
        days = (31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                31, 30, 31, 30, 31, 31, 30, 31, 30, 31)[month - 1]
        return 1 <= day <= days

    def check_outbound(
        self,
        text: str,
        purpose: str,
        *,
        region: str | None = None,
        file_path: str | None = None,
        source_type: str = "text",
    ) -> dict:
        """所有“送出系统/落盘成文件”的文字统一过这一个入口（邮件、导出等）。

        只拦截 block / review；脱敏级（部门名、区域名等）在内部邮件和周报里是正常内容，
        不拦截。日志只记元数据与摘要哈希，不记原文。
        """
        level, hits, _ = self.scan(text, file_path=file_path, source_type=source_type, region=region)
        ok = level not in {"block", "review"}
        categories = sorted({h.get("category", "未知") for h in hits if h.get("action") in {"block", "review"}})
        message = "" if ok else (
            f"已拦截：{purpose}内容含{'、'.join(categories) or '受控信息'}，"
            "请删除或打码后重试。"
        )
        self._log(purpose, text, level, hits, rejected=not ok)
        return {"ok": ok, "level": level, "hits": hits, "categories": categories, "message": message}

    def _scan_asset(self, text: str, file_path: str | None, source_type: str, region: str | None) -> list[dict]:
        """识别疑似受控文件；当前能力只升为人工复核，不作自动定罪。"""
        if source_type not in {"file_metadata", "document", "image_ocr"}:
            return []

        rules = self.config.get("asset_rules", {})
        policy = self.region_policy.get(region)
        hits: list[dict] = []
        if file_path:
            fname = Path(file_path).name.lower()
            keywords = set(rules.get("filename_risk_keywords", [])) | set(policy.get("filename_risk_keywords", []))
            for keyword in keywords:
                if keyword.lower() in fname:
                    hits.append({
                        "category": "受控资产文件名",
                        "term": keyword,
                        "action": "review",
                    })

        if source_type == "file_metadata" or not text:
            return hits

        lowered = text.lower()

        def found_terms(key: str) -> list[str]:
            return [term for term in rules.get(key, []) if term.lower() in lowered]

        # 单个名词可能只是制度讨论；至少两个相互印证的特征才进入复核。
        for key, category, minimum in (
            ("monitoring_indicators", "疑似监控画面/录像", int(policy.get("monitoring_indicator_min", 2))),
            ("layout_indicators", "疑似厂区布局图", int(policy.get("layout_indicator_min", 2))),
        ):
            found = found_terms(key)
            if len(found) >= minimum:
                hits.append({"category": category, "term": "、".join(found[:3]), "action": "review"})

        # 花名册、门禁记录属于结构化材料，需要字段组合和多行记录同时成立。
        line_count = len([line for line in text.splitlines() if line.strip()])
        for key, category in (
            ("roster_markers", "疑似完整花名册"),
            ("access_log_markers", "疑似门禁原始记录"),
        ):
            found = found_terms(key)
            if len(found) >= 3 and line_count >= 3:
                hits.append({"category": category, "term": "、".join(found[:4]), "action": "review"})

        letterhead = found_terms("letterhead_markers")
        if len(letterhead) >= 2:
            hits.append({"category": "疑似带公司抬头原件", "term": "、".join(letterhead[:3]), "action": "review"})
        return hits

    # ---------------- 脱敏 / 还原 ----------------
    def desensitize(self, text: str, mapping: dict[str, str]) -> str:
        # 统一按 mapping 替换；长原文优先，避免短词破坏长词
        for placeholder, original in sorted(mapping.items(), key=lambda kv: -len(kv[1])):
            text = text.replace(original, placeholder)
        return text

    @staticmethod
    def restore(text: str, mapping: dict[str, str]) -> str:
        """把所有占位符（工号/邮箱/区域/客户等）还原成原文。"""
        for placeholder, original in mapping.items():
            text = text.replace(placeholder, original)
        return text

    # ---------------- 统一出口 ----------------
    def call_llm(
        self,
        prompt: str,
        task_desc: str = "",
        file_path: str | None = None,
        system_prompt: str = "",
        llm_fn: Callable[..., str] | None = None,
        external: bool = True,
    ) -> dict:
        """
        所有模型调用走这里。
        system_prompt: 固定角色/规范。外网模式下与用户输入一起检查、脱敏。
        llm_fn: 真正调大模型的函数，签名 f(prompt: str, system: str = "") -> str
        external: 是否调用公网模型；本地模型不需要替换 desensitize 级内容。
        """
        if llm_fn is None:
            raise RuntimeError("必须注入 llm_fn（大模型调用函数），见 core/providers.py 的 configured_llm")

        # 系统提示词也可能含部门/区域代号，不能绕过外网脱敏。
        combined = f"{system_prompt}\n{prompt}" if system_prompt else prompt
        level, hits, mapping = self.scan(combined, file_path)

        # block：直接拒绝，不送公网（本部门无内网模型，此类只能人工）
        if level == "block":
            self._log(task_desc, prompt, level, hits, rejected=True)
            blocked_categories = sorted({h["category"] for h in hits})
            return {
                "status": "rejected",
                "reason": self._reject_advice(blocked_categories),
                "hits": hits,
                "blocked_categories": blocked_categories,
            }

        if level == "review":
            self._log(task_desc, prompt, level, hits, rejected=True)
            return {
                "status": "rejected",
                "reason": "内容疑似受控文件资产，需人工复核，未送模型。",
                "hits": hits,
                "blocked_categories": sorted({h["category"] for h in hits}),
            }

        should_mask = external and level == "desensitize"
        safe_prompt = self.desensitize(prompt, mapping) if should_mask else prompt
        safe_system = self.desensitize(system_prompt, mapping) if should_mask else system_prompt

        raw_output = llm_fn(safe_prompt, system=safe_system)

        # 闸3：输出复检（防止模型把敏感信息写出来）
        out_level, out_hits, _ = self.scan(raw_output)
        if out_level == "block":
            # 输出里又冒出红线词，宁可不给
            self._log(task_desc, prompt, level, hits, output="<已拦截：输出含敏感信息>", out_level=out_level)
            return {
                "status": "rejected",
                "reason": "模型输出中检出敏感信息，已拦截。请缩小任务范围重试。",
                "hits": out_hits,
            }

        final_output = self.restore(raw_output, mapping) if should_mask else raw_output

        self._log(task_desc, prompt, level, hits, output=final_output, out_level=out_level)

        return {
            "status": "ok",
            "level": level,
            "output": final_output,
            "masked": should_mask,
            "input_hits": hits,
            "output_leak_check": out_hits,
        }

    # ---------------- 统一出口（流式） ----------------
    def call_llm_stream(
        self,
        prompt: str,
        task_desc: str = "",
        system_prompt: str = "",
        llm_stream: Callable[..., object] | None = None,
        external: bool = True,
    ):
        """流式版本，逐段产出事件。

        产出 {"type": "chunk", "text": ...} 用于边生成边显示，
        最后产出 {"type": "final", "result": {...}}（结构与 call_llm 一致）。
        输入先扫红线；输出边流边扫，命中 block 级内容立即中止。
        """
        if llm_stream is None:
            raise RuntimeError("必须注入 llm_stream（流式模型调用函数）")

        combined = f"{system_prompt}\n{prompt}" if system_prompt else prompt
        level, hits, mapping = self.scan(combined)

        if level == "block":
            self._log(task_desc, prompt, level, hits, rejected=True)
            blocked_categories = sorted({h["category"] for h in hits})
            yield {
                "type": "final",
                "result": {
                    "status": "rejected",
                    "reason": self._reject_advice(blocked_categories),
                    "hits": hits,
                    "blocked_categories": blocked_categories,
                },
            }
            return

        if level == "review":
            self._log(task_desc, prompt, level, hits, rejected=True)
            yield {
                "type": "final",
                "result": {
                    "status": "rejected",
                    "reason": "内容疑似受控文件资产，需人工复核，未送模型。",
                    "hits": hits,
                    "blocked_categories": sorted({h["category"] for h in hits}),
                },
            }
            return

        should_mask = external and level == "desensitize"
        safe_prompt = self.desensitize(prompt, mapping) if should_mask else prompt
        safe_system = self.desensitize(system_prompt, mapping) if should_mask else system_prompt

        accumulated = ""
        out_hits: list[dict] = []
        out_level = "allow"

        for chunk in llm_stream(safe_prompt, system=safe_system):
            if not chunk:
                continue
            accumulated += chunk

            # 闸3：增量复检，命中高危立即停止
            out_level, out_hits, _ = self.scan(accumulated)
            if out_level == "block":
                self._log(
                    task_desc, prompt, level, hits,
                    output="<已拦截：输出含敏感信息>", out_level=out_level,
                )
                yield {
                    "type": "final",
                    "result": {
                        "status": "rejected",
                        "reason": "模型输出中检出敏感信息，已拦截。请缩小任务范围重试。",
                        "hits": out_hits,
                    },
                }
                return

            yield {"type": "chunk", "text": chunk}

        final_output = self.restore(accumulated, mapping) if should_mask else accumulated
        self._log(task_desc, prompt, level, hits, output=final_output, out_level=out_level)

        yield {
            "type": "final",
            "result": {
                "status": "ok",
                "level": level,
                "output": final_output,
                "masked": should_mask,
                "input_hits": hits,
                "output_leak_check": out_hits,
            },
        }

    # ---------------- 带角色的消息出口 ----------------
    @staticmethod
    def _messages_text(messages) -> str:
        """把消息序列拼成一个字符串供扫描；角色本身不参与扫描。"""
        return "\n".join(m.get("content", "") for m in messages or [] if isinstance(m, dict))

    def _mask_messages(self, messages, mapping) -> list[dict]:
        """按条脱敏，保持角色顺序不变；占位符只在内容里替换。"""
        return [
            {"role": m["role"], "content": self.desensitize(m["content"], mapping)}
            for m in messages
        ]

    def call_llm_stream_messages(
        self,
        messages,
        task_desc: str = "",
        llm_stream: Callable[..., object] | None = None,
        external: bool = True,
    ):
        """流式消息版：整段消息先扫红线，再按角色脱敏后送出。

        与 call_llm_stream 的区别只在于入口形态：system 作为 messages 中的一项，
        不再拼进单个 prompt 字符串。
        """
        if llm_stream is None:
            raise RuntimeError("必须注入 llm_stream（流式模型调用函数）")
        messages = [m for m in messages or [] if isinstance(m, dict) and m.get("content", "").strip()]
        if not messages:
            raise RuntimeError("messages 不能为空")

        combined = self._messages_text(messages)
        level, hits, mapping = self.scan(combined)

        if level in {"block", "review"}:
            self._log(task_desc, combined, level, hits, rejected=True)
            blocked_categories = sorted({h["category"] for h in hits})
            reason = (self._reject_advice(blocked_categories) if level == "block"
                      else "内容疑似受控文件资产，需人工复核，未送模型。")
            yield {"type": "final", "result": {
                "status": "rejected", "reason": reason, "hits": hits,
                "blocked_categories": blocked_categories}}
            return

        should_mask = external and level == "desensitize"
        safe_messages = self._mask_messages(messages, mapping) if should_mask else messages

        accumulated = ""
        out_hits: list[dict] = []
        out_level = "allow"
        for chunk in llm_stream(messages=safe_messages):
            if not chunk:
                continue
            accumulated += chunk
            out_level, out_hits, _ = self.scan(accumulated)
            if out_level == "block":
                self._log(task_desc, combined, level, hits,
                          output="<已拦截：输出含敏感信息>", out_level=out_level)
                yield {"type": "final", "result": {
                    "status": "rejected",
                    "reason": "模型输出中检出敏感信息，已拦截。请缩小任务范围重试。",
                    "hits": out_hits}}
                return
            yield {"type": "chunk", "text": chunk}

        final_output = self.restore(accumulated, mapping) if should_mask else accumulated
        self._log(task_desc, combined, level, hits, output=final_output, out_level=out_level)
        yield {"type": "final", "result": {
            "status": "ok", "level": level, "output": final_output,
            "masked": should_mask, "input_hits": hits, "output_leak_check": out_hits}}

    # ---------------- 拒绝时给人话建议 ----------------
    @staticmethod
    def _reject_advice(categories: list[str]) -> str:
        tips = []
        for c in categories:
            if c in ("身份证号", "手机号"):
                tips.append(f"发现{c}——请把这些号码删掉或打码后再试，不要直接发我")
            elif c in ("监控", "camera", "厂区布局", "layout", "花名册", "门禁记录", "文件名"):
                tips.append("发现监控/花名册/厂区类文件——本阶段这类敏感文件不进公网AI，请人工处理")
            else:
                tips.append(f"发现{c}——请移除该内容后再试")
        return "已拦截，未送公网。" + "；".join(tips) + "。"

    # ---------------- 留痕 ----------------
    def _log(self, task_desc, prompt, level, hits, output=None, out_level=None, rejected=False):
        """只记元数据与摘要哈希，绝不把业务原文写入日志。"""
        prompt_bytes = (prompt or "").encode("utf-8", errors="replace")
        output_bytes = (output or "").encode("utf-8", errors="replace")
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "task": task_desc,
            "level": level,
            "rejected": rejected,
            "hit_categories": sorted({h.get("category", "未知") for h in hits}),
            "hit_count": len(hits),
            "prompt_chars": len(prompt or ""),
            "prompt_sha256_12": hashlib.sha256(prompt_bytes).hexdigest()[:12],
            "output_chars": len(output or ""),
            "output_sha256_12": hashlib.sha256(output_bytes).hexdigest()[:12] if output else "",
            "output_level": out_level,
        }
        log_file = self.log_dir / f"redline_{datetime.now():%Y-%m-%d}.jsonl"
        with _LOG_LOCK:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
