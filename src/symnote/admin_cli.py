"""招待制ベータの管理コマンド（symnote-admin）。

使用例:
  symnote-admin invite friend@gmail.com --note "ベータ招待"
  symnote-admin invite owner@gmail.com --owner
  symnote-admin list
  symnote-admin revoke friend@gmail.com
"""

from __future__ import annotations

import argparse
import sys

from symnote.core.accounts import (
    invite_user,
    list_invites,
    list_users,
    revoke_invite,
    system_db_path,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="symnote-admin",
        description="SymNote 招待制ベータの利用者・招待リストを管理する",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_invite = sub.add_parser("invite", help="メールアドレスを招待リストに追加する")
    p_invite.add_argument("email", help="招待する Google アカウントのメールアドレス")
    p_invite.add_argument("--owner", action="store_true", help="オーナー権限を付与する")
    p_invite.add_argument("--note", default="", help="メモ（任意）")

    p_revoke = sub.add_parser("revoke", help="招待を取り消し、セッションを無効化する")
    p_revoke.add_argument("email", help="取り消すメールアドレス")

    sub.add_parser("list", help="招待リストと登録済み利用者を表示する")

    args = parser.parse_args(argv)

    if args.command == "invite":
        rec = invite_user(args.email, is_owner=bool(args.owner), note=args.note)
        role = "オーナー" if rec.is_owner else "招待メンバー"
        print(f"招待を登録しました: {rec.email} ({role})")
        print(f"管理DB: {system_db_path()}")
        return 0

    if args.command == "revoke":
        removed = revoke_invite(args.email)
        if removed:
            print(f"招待を取り消しました: {args.email}")
        else:
            print(f"該当する招待は見つかりませんでした: {args.email}", file=sys.stderr)
        return 0

    if args.command == "list":
        invites = list_invites()
        users = list_users()
        print(f"=== 招待リスト ({len(invites)} 件) ===")
        for inv in invites:
            role = "owner" if inv.is_owner else "member"
            status = f"参加済 ({inv.accepted_at})" if inv.accepted_at else "未ログイン"
            note = f" [{inv.note}]" if inv.note else ""
            print(f"  - {inv.email} ({role}) : {status}{note}")
        print(f"\n=== 利用者一覧 ({len(users)} 件) ===")
        for u in users:
            role = "owner" if u.is_owner else "member"
            print(f"  - {u.email} (id={u.id}, {role}) -> DB: {u.db_path}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
