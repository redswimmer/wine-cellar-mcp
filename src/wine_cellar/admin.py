"""Manage API tokens: `uv run wine-tokens --help`.

Runs on your machine with your AWS credentials (e.g. AWS_PROFILE), not in Lambda.
"""

import argparse
import sys

import boto3

from .tokens import TokenStore


def _table_name(stage: str) -> str:
    stack = boto3.client("cloudformation").describe_stacks(StackName=f"WineCellar-{stage}")["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    return outputs["TableName"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wine-tokens", description="Manage wine MCP API tokens.")
    parser.add_argument("--stage", default="dev", choices=["dev", "prod"])
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="Issue a token for a user; prints it once")
    create.add_argument("--user", required=True, help="User ID the token acts for, e.g. andrew")
    create.add_argument("--label", required=True, help="Where the token is used, e.g. andrew-nemoclaw")

    sub.add_parser("list", help="List tokens (hash prefix, user, label, created)")

    revoke = sub.add_parser("revoke", help="Revoke a token by the hash prefix shown in `list`")
    revoke.add_argument("hash_prefix")

    args = parser.parse_args(argv)
    region = boto3.session.Session().region_name or "us-east-1"
    store = TokenStore(boto3.resource("dynamodb", region_name=region).Table(_table_name(args.stage)))

    if args.command == "create":
        token = store.create(args.user, args.label)
        print(token)
        print(f"Token for user '{args.user}' ({args.label}). It is not stored; save it now.", file=sys.stderr)
    elif args.command == "list":
        for r in store.list():
            print(f"{r.token_hash[:12]}  {r.user_id:<20} {r.label:<30} {r.created_at}")
    elif args.command == "revoke":
        r = store.revoke(args.hash_prefix)
        print(f"Revoked {r.token_hash[:12]} ({r.user_id}, {r.label}). Takes effect within a minute.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
