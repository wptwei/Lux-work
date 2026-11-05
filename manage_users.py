"""H-Copilot 本地账号管理命令。密码通过交互输入，不进入命令历史。"""
from __future__ import annotations

import argparse
import getpass

from core.auth import ROLES, UserStore


def _password_twice() -> str:
    first = getpass.getpass("新密码：")
    second = getpass.getpass("再次输入：")
    if first != second:
        raise ValueError("两次输入的密码不一致")
    return first


def main() -> None:
    parser = argparse.ArgumentParser(description="H-Copilot 本地账号管理")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="新增账号")
    add.add_argument("username")
    add.add_argument("--name", default="", help="姓名/工号（显示用）")
    add.add_argument("--role", choices=ROLES, default="operator")
    reset = sub.add_parser("reset-password", help="重置密码")
    reset.add_argument("username")
    status = sub.add_parser("set-enabled", help="启用或停用账号")
    status.add_argument("username")
    status.add_argument("value", choices=("true", "false"))
    sub.add_parser("list", help="列出账号（不显示密码哈希）")
    args = parser.parse_args()
    store = UserStore()
    if args.command == "add":
        user = store.add_user(args.username, _password_twice(), display_name=args.name, role=args.role)
        print(f"已创建：{user['username']} · {user['display_name']} · {user['role_label']}")
    elif args.command == "reset-password":
        store.reset_password(args.username, _password_twice())
        print(f"已重置账号 {args.username} 的密码")
    elif args.command == "set-enabled":
        store.set_enabled(args.username, args.value == "true")
        print(f"账号 {args.username} 已{'启用' if args.value == 'true' else '停用'}")
    else:
        print("账号\t姓名/工号\t角色\t状态")
        for user in store.list_users():
            print(f"{user['username']}\t{user['display_name']}\t{user['role_label']}\t{'启用' if user['enabled'] else '停用'}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(f"错误：{exc}") from exc
