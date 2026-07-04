#!/usr/bin/env python
"""Admin CLI: manage Archivist teams and API keys.

    team-create <name>
    key-issue   --team <id|name> --label <label>
    key-revoke  --key-id <id>
    key-rotate  --team <id|name> --label <label>   (issue new; revoke old(s) with --revoke-old)

On key issue the plaintext is printed exactly ONCE (stdout); only its SHA-256
hash is stored. Rotation issues a new key on the SAME team, so the archive is
untouched; run with --revoke-old to revoke the team's other keys after clients
have switched.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, update  # noqa: E402

from app import db  # noqa: E402
from app.auth import create_api_key, create_team, revoke_api_key  # noqa: E402
from app.models import ApiKey, Team  # noqa: E402


async def _resolve_team(team: str) -> int:
    """Accept a numeric id or a team name; return the team id or exit."""
    if team.isdigit():
        return int(team)
    async with db.SessionFactory() as session:
        tid = (await session.execute(select(Team.id).where(Team.name == team))).scalar_one_or_none()
    if tid is None:
        raise SystemExit(f"no team named {team!r}")
    return tid


async def team_create(name: str) -> int:
    async with db.SessionFactory() as session:
        team_id = await create_team(session, name)
    print(team_id)
    return team_id


async def key_issue(team: str, label: str) -> str:
    team_id = await _resolve_team(team)
    async with db.SessionFactory() as session:
        plaintext = await create_api_key(session, label, team_id)
    print(plaintext)
    print("Store this now: only its hash is saved; the key cannot be recovered.", file=sys.stderr)
    return plaintext


async def key_revoke(key_id: int) -> None:
    async with db.SessionFactory() as session:
        ok = await revoke_api_key(session, key_id)
    if not ok:
        raise SystemExit(f"no api key with id {key_id}")
    print(f"revoked api key {key_id}")


async def key_rotate(team: str, label: str, revoke_old: bool) -> str:
    team_id = await _resolve_team(team)
    async with db.SessionFactory() as session:
        existing = (
            await session.execute(select(ApiKey.id).where(ApiKey.team_id == team_id, ApiKey.revoked.is_(False)))
        ).scalars().all()
    plaintext = await key_issue(str(team_id), label)
    if revoke_old and existing:
        async with db.SessionFactory() as session:
            await session.execute(update(ApiKey).where(ApiKey.id.in_(existing)).values(revoked=True))
            await session.commit()
        print(f"revoked {len(existing)} previous key(s) for team {team_id}", file=sys.stderr)
    return plaintext


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Archivist teams and API keys.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_tc = sub.add_parser("team-create")
    p_tc.add_argument("name")

    p_ki = sub.add_parser("key-issue")
    p_ki.add_argument("--team", required=True)
    p_ki.add_argument("--label", required=True)

    p_kr = sub.add_parser("key-revoke")
    p_kr.add_argument("--key-id", type=int, required=True)

    p_kt = sub.add_parser("key-rotate")
    p_kt.add_argument("--team", required=True)
    p_kt.add_argument("--label", required=True)
    p_kt.add_argument("--revoke-old", action="store_true")

    args = parser.parse_args()
    if args.cmd == "team-create":
        asyncio.run(team_create(args.name))
    elif args.cmd == "key-issue":
        asyncio.run(key_issue(args.team, args.label))
    elif args.cmd == "key-revoke":
        asyncio.run(key_revoke(args.key_id))
    elif args.cmd == "key-rotate":
        asyncio.run(key_rotate(args.team, args.label, args.revoke_old))


if __name__ == "__main__":
    main()
