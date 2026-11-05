"""
邮件发送 —— 标准库 smtplib，零依赖。
配置存本地 config/email_config.json（含授权码，请勿外传）。
红线：发送为外发动作，必须人工预览确认后才发送。
"""
from __future__ import annotations

import json
import os
import re
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.header import Header
from pathlib import Path
from core.paths import config_path

CONFIG_PATH = config_path("email_config.json")
FORMAT_PATH = config_path("email_format.json")


# 首次使用的示例配置（请改成你自己的企业邮箱）
DEFAULT_CONFIG = {
    "_说明": "发件邮箱配置。常见：腾讯企业邮 smtp.exmail.qq.com:465；阿里企业邮 smtp.mxhichina.com:465；Office365 smtp.office365.com:587",
    "smtp_host": "smtp.exmail.qq.com",
    "smtp_port": 465,
    "use_ssl": True,
    "use_starttls": False,
    "sender": "your-name@company.com",
    "sender_name": "H区安保二部"
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def is_configured() -> tuple[bool, str]:
    cfg = load_config()
    if cfg.get("sender", "").startswith("your-") or not os.getenv("LUX_SMTP_PASSWORD"):
        return False, "邮件未配置：请填写发件邮箱，并通过 LUX_SMTP_PASSWORD 设置授权码"
    return True, "已配置"


# ---------- 固定邮件格式 ----------
DEFAULT_FORMAT = {
    "_说明": "固定邮件正文格式。{body} 会自动替换成AI生成的正文，日期自动取当天。",
    "salutation": "各位同事：",
    "closing": "特此通知。",
    "signature_unit": "华东安保二部",
    "use_date": True
}


def load_format() -> dict:
    if not FORMAT_PATH.exists():
        FORMAT_PATH.write_text(json.dumps(DEFAULT_FORMAT, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    return json.loads(FORMAT_PATH.read_text(encoding="utf-8"))


def wrap_email(body: str) -> str:
    """把正文套进固定邮件格式（称呼 + 正文 + 结尾 + 落款单位 + 日期）。"""
    f = load_format()
    body = body.strip()
    date_line = datetime.now().strftime("%Y年%m月%d日") if f.get("use_date") else ""
    parts = [f.get("salutation", "各位同事："), "", body, "", f.get("closing", "特此通知。"), ""]
    if f.get("signature_unit"):
        parts.append(f["signature_unit"])
    if date_line:
        parts.append(date_line)
    return "\n".join(parts)


def send_email(to: str, subject: str, body: str, cc: str = "") -> str:
    """发送纯文本邮件。to/cc 用逗号分隔多人。返回成功/失败提示。"""
    cfg = load_config()
    ok, msg = is_configured()
    if not ok:
        return f"❌ {msg}"
    if not to.strip():
        return "❌ 收件人不能为空"

    recipients = [x.strip() for x in to.split(",") if x.strip()]
    if cc.strip():
        recipients += [x.strip() for x in cc.split(",") if x.strip()]
    email_pattern = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    invalid = [x for x in recipients if not email_pattern.fullmatch(x)]
    if invalid:
        return f"❌ 邮箱格式无效：{', '.join(invalid)}"
    if not subject.strip() or not body.strip():
        return "❌ 主题和正文不能为空"

    mime = MIMEText(body, "plain", "utf-8")
    mime["Subject"] = Header(subject, "utf-8")
    mime["From"] = Header(f"{cfg['sender_name']} <{cfg['sender']}>", "utf-8")
    mime["To"] = to
    if cc.strip():
        mime["Cc"] = cc

    try:
        if cfg.get("use_ssl"):
            server = smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], timeout=20)
        else:
            server = smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=20)
            if cfg.get("use_starttls"):
                server.starttls()
        server.login(cfg["sender"], os.environ["LUX_SMTP_PASSWORD"])
        server.sendmail(cfg["sender"], recipients, mime.as_string())
        server.quit()
        return f"✅ 已发送给：{to}{('、' + cc) if cc.strip() else ''}"
    except Exception as e:
        return f"❌ 发送失败：{e}"


if __name__ == "__main__":
    print(is_configured())
