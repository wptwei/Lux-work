"""启动时的安全提示：只提示，不阻止启动（阻止类规则在 Settings.validate 里）。"""
from __future__ import annotations

from core.auth import validate_password

LOCAL_HOSTS = {"127.0.0.1", "localhost"}


def security_warnings(*, host: str, share: bool, app_password: str, has_users: bool) -> list[str]:
    warnings: list[str] = []
    if app_password and has_users:
        warnings.append(
            ".env 中仍保留明文 LUX_APP_PASSWORD，而独立账号库已建立。"
            "请删除 LUX_APP_USERNAME 和 LUX_APP_PASSWORD 两行。"
        )
    if app_password:
        try:
            validate_password(app_password)
        except ValueError as exc:
            warnings.append(
                f"LUX_APP_PASSWORD 不满足密码策略（{exc}）。初始管理员迁移时不检查强度，"
                "请登录后在账号管理中改为强密码。"
            )
    if host not in LOCAL_HOSTS:
        warnings.append(
            "当前监听局域网地址，页面和登录密码通过明文 HTTP 传输。"
            "请改为只监听 127.0.0.1，并放在 HTTPS 反向代理之后（见 docs/DEPLOY_HTTPS.md）。"
        )
    if share:
        warnings.append("已启用公网分享链接（LUX_SHARE=true）：请确认确实需要，并只在必要时临时开启。")
    return warnings
