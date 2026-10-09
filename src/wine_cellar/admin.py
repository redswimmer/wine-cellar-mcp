"""Manage users and API tokens: `uv run wine-tokens --help`.

Runs on your machine with your AWS credentials (e.g. AWS_PROFILE), not in Lambda.
"""

import argparse
import sys

import boto3

from .bootstrap import build_service
from .config import Settings
from .tokens import TokenStore
from .users import UserStore


def _stack_outputs(stage: str) -> dict[str, str]:
    stack = boto3.client("cloudformation").describe_stacks(StackName=f"WineCellar-{stage}")["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wine-tokens", description="Manage wine MCP users and API tokens.")
    parser.add_argument("--stage", default="dev", choices=["dev", "prod"])
    sub = parser.add_subparsers(dest="command", required=True)

    add_user = sub.add_parser("add-user", help="Create a user and their first token; prints the token once")
    add_user.add_argument("--username", required=True, help="Unique username, e.g. andrew")
    add_user.add_argument("--label", required=True, help="Where the token is used, e.g. andrew-laptop")

    sub.add_parser("list-users", help="List users (ID, username, status, created)")

    for name, help_text in [
        ("deactivate-user", "Block a user's tokens; their wines and tokens are kept"),
        ("reactivate-user", "Let a deactivated user's existing tokens work again"),
    ]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--user", required=True, help="Username or user ID (usr_...)")

    create = sub.add_parser("create-token", help="Issue another token for an existing user; prints it once")
    create.add_argument("--user", required=True, help="Username or user ID (usr_...)")
    create.add_argument("--label", required=True, help="Where the token is used")

    sub.add_parser("list-tokens", help="List tokens (hash prefix, user, label, created)")

    revoke = sub.add_parser("revoke-token", help="Revoke a token by the hash prefix shown in list-tokens")
    revoke.add_argument("hash_prefix")

    sub.add_parser("reindex", help="Rewrite every wine's text and photo vectors, e.g. after changing models")

    args = parser.parse_args(argv)
    region = boto3.session.Session().region_name
    outputs = _stack_outputs(args.stage)
    table = boto3.resource("dynamodb", region_name=region).Table(outputs["TableName"])
    users, tokens = UserStore(table), TokenStore(table)

    if args.command == "add-user":
        user = users.create(args.username)
        print(tokens.create(user.user_id, args.label))
        print(
            f"Created {user.username} as {user.user_id}. The token above is not stored; save it now.", file=sys.stderr
        )
    elif args.command == "list-users":
        for u in users.list():
            print(f"{u.user_id}  {u.username:<24} {'active' if u.active else 'deactivated':<12} {u.created_at}")
    elif args.command in ("deactivate-user", "reactivate-user"):
        user = users.find(args.user)
        if user is None:
            parser.error(f"No user {args.user!r}; see list-users")
        user = users.set_active(user.user_id, args.command == "reactivate-user")
        state = "active" if user.active else "deactivated (takes effect within a minute)"
        print(f"{user.username} ({user.user_id}) is now {state}.")
    elif args.command == "create-token":
        user = users.find(args.user)
        if user is None:
            parser.error(f"No user {args.user!r}; see list-users")
        print(tokens.create(user.user_id, args.label))
        print(f"Token for {user.username} ({args.label}). It is not stored; save it now.", file=sys.stderr)
    elif args.command == "list-tokens":
        names = {u.user_id: u.username for u in users.list()}
        for r in tokens.list():
            print(f"{r.token_hash[:12]}  {r.user_id}  {names.get(r.user_id, '?'):<16} {r.label:<28} {r.created_at}")
    elif args.command == "revoke-token":
        r = tokens.revoke(args.hash_prefix)
        print(f"Revoked {r.token_hash[:12]} ({r.user_id}, {r.label}). Takes effect within a minute.")
    elif args.command == "reindex":
        settings = Settings(
            table_name=outputs["TableName"],
            vector_bucket=outputs["VectorBucketName"],
            text_index=outputs["TextIndexName"],
            photo_index=outputs["PhotoIndexName"],
            photo_bucket=outputs["PhotoBucketName"],
        )
        service = build_service(settings, table, region)
        for u in users.list():
            n_wines, n_photos = service.reindex(u.user_id)
            print(f"{u.username}: {n_wines} wines and {n_photos} photos indexed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
