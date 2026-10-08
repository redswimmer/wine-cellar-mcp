"""Manage users and API tokens: `uv run wine-tokens --help`.

Runs on your machine with your AWS credentials (e.g. AWS_PROFILE), not in Lambda.
"""

import argparse
import sys

import boto3

from .tokens import TokenStore
from .users import UserStore


def _table_name(stage: str) -> str:
    stack = boto3.client("cloudformation").describe_stacks(StackName=f"WineCellar-{stage}")["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    return outputs["TableName"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wine-tokens", description="Manage wine MCP users and API tokens.")
    parser.add_argument("--stage", default="dev", choices=["dev", "prod"])
    sub = parser.add_subparsers(dest="command", required=True)

    add_user = sub.add_parser("add-user", help="Create a user and their first token; prints the token once")
    add_user.add_argument("--name", required=True, help="Who this is, e.g. Andrew (a label only)")
    add_user.add_argument("--label", required=True, help="Where the token is used, e.g. andrew-nemoclaw")

    sub.add_parser("list-users", help="List users (ID, name, created)")

    create = sub.add_parser("create-token", help="Issue another token for an existing user; prints it once")
    create.add_argument("--user", required=True, help="User ID from list-users, e.g. usr_01j...")
    create.add_argument("--label", required=True, help="Where the token is used")

    sub.add_parser("list-tokens", help="List tokens (hash prefix, user, label, created)")

    revoke = sub.add_parser("revoke-token", help="Revoke a token by the hash prefix shown in list-tokens")
    revoke.add_argument("hash_prefix")

    args = parser.parse_args(argv)
    region = boto3.session.Session().region_name or "us-east-1"
    table = boto3.resource("dynamodb", region_name=region).Table(_table_name(args.stage))
    users, tokens = UserStore(table), TokenStore(table)

    if args.command == "add-user":
        user = users.create(args.name)
        print(tokens.create(user.user_id, args.label))
        print(f"Created {user.name} as {user.user_id}. The token above is not stored; save it now.", file=sys.stderr)
    elif args.command == "list-users":
        for u in users.list():
            print(f"{u.user_id}  {u.name:<24} {u.created_at}")
    elif args.command == "create-token":
        print(tokens.create(args.user, args.label))
        print(f"Token for {args.user} ({args.label}). It is not stored; save it now.", file=sys.stderr)
    elif args.command == "list-tokens":
        names = {u.user_id: u.name for u in users.list()}
        for r in tokens.list():
            print(f"{r.token_hash[:12]}  {r.user_id}  {names.get(r.user_id, '?'):<16} {r.label:<28} {r.created_at}")
    elif args.command == "revoke-token":
        r = tokens.revoke(args.hash_prefix)
        print(f"Revoked {r.token_hash[:12]} ({r.user_id}, {r.label}). Takes effect within a minute.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
