"""continuo CLI: migrate, create-user, import-lidseeker, backup, policy-check, refresh-library.

Run inside the container: `docker exec -it continuo continuo <command>`."""
import argparse
import asyncio
import getpass
import json
import os
import sqlite3
import sys

from . import config


def _migrate() -> None:
    from .db import migrate
    migrate.run()


def cmd_migrate(_args) -> int:
    from .db import migrate
    ran = migrate.run()
    print("applied: " + (", ".join(ran) if ran else "nothing (up to date)"))
    return 0


def cmd_create_user(args) -> int:
    from .auth import passwords
    from .db import users
    _migrate()
    if users.get(args.username):
        print(f"user {args.username!r} already exists", file=sys.stderr)
        return 1
    password = os.environ.get("CONTINUO_NEW_PASSWORD") or getpass.getpass("Password: ")
    passwords.validate(password)
    u = users.create(args.username, passwords.hash_password(password), args.role,
                     args.navidrome_username)
    print(f"created {u['username']} ({u['role']}), Navidrome user "
          f"{users.navidrome_name(u)!r}")
    return 0


# lidseeker status → continuo status. 'failed' never existed here: keep searching, flagged.
_STATUS_MAP = {"available": "available", "downloading": "downloading", "error": "error",
               "failed": "searching", "pending": "searching"}


def import_lidseeker(path: str, *, match_available: bool = True) -> dict:
    """Copy users and album requests from a lidseeker database (read-only). Idempotent: users by
    username, requests by (kind, release group) — existing rows are left alone, so it can be
    re-run at cut-over."""
    from .db import requests as req_db
    from .db import users as users_db
    from .db.conn import now
    _migrate()
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    report = {"users_added": 0, "users_skipped": 0, "requests_added": 0,
              "requests_skipped": 0, "artist_requests_skipped": 0, "matched": 0}

    id_map: dict[int, int] = {}
    for u in src.execute("SELECT * FROM users ORDER BY id"):
        existing = users_db.get(u["username"])
        if existing:
            report["users_skipped"] += 1
            id_map[u["id"]] = existing["id"]
            continue
        role = u["role"] if u["role"] in ("admin", "user") else "user"
        created = users_db.create(u["username"], u["password_hash"], role,
                                  created_at=u["created_at"])
        id_map[u["id"]] = created["id"]
        report["users_added"] += 1

    cols = {r[1] for r in src.execute("PRAGMA table_info(requests)")}
    for r in src.execute("SELECT * FROM requests ORDER BY id"):
        if r["type"] != "album":
            report["artist_requests_skipped"] += 1   # policy: artist requests aren't ported
            continue
        rg = (r["foreign_id"] or "").lower()
        if not rg or req_db.by_release_group(rg):
            report["requests_skipped"] += 1
            continue
        status = _STATUS_MAP.get(r["status"], "searching")
        if status == "searching" and not r["lidarr_album_id"]:
            status = "pending"                       # never reached Lidarr: re-run the add
        user_id = id_map.get(r["user_id"]) if "user_id" in cols and r["user_id"] else None
        row = req_db.create(mb_release_group_id=rg, title=r["title"], artist=r["artist"],
                            year=None, artwork_url=r["image_url"], user_id=user_id,
                            status=status, created_at=r["created_at"])
        fields = {"lidarr_artist_id": r["lidarr_artist_id"],
                  "lidarr_album_id": r["lidarr_album_id"], "error": r["error"]}
        if r["status"] == "failed":
            fields["still_looking"] = True
        if status == "available":
            # Already notified by lidseeker; never announce the backlog again.
            fields.update(available_at=r["created_at"], notified_at=now())
        req_db.update(row["id"], **fields)
        req_db.add_event(row["id"], "imported_from_lidseeker",
                         {"lidseekerId": r["id"], "status": r["status"]})
        report["requests_added"] += 1

    if match_available:
        report["matched"] = asyncio.run(_match_available())
    return report


async def _match_available() -> int:
    """Give imported 'available' rows their Navidrome album id (best-effort)."""
    from .db import library as lib_db
    from .db import requests as req_db
    from .requests import matching
    from .upstream import http, lidarr
    from .workers import index
    n = 0
    try:
        await index.refresh()
        library = lib_db.all_albums()
        for row in req_db.list_requests(statuses=("available",)):
            if row.get("navidrome_album_id"):
                continue
            album = None
            if row.get("lidarr_album_id"):
                try:
                    album = await lidarr.get_album(row["lidarr_album_id"])
                except Exception:  # noqa: BLE001
                    album = None
            m = matching.match(row, library, lidarr_album=album)
            if m.matched:
                req_db.update(row["id"], navidrome_album_id=m.navidrome_album_id)
                req_db.add_event(row["id"], "matched", {"strategy": m.strategy, "by": "import"})
                n += 1
    except Exception as e:  # noqa: BLE001
        print(f"warning: matching imported requests failed: {e}", file=sys.stderr)
    finally:
        await http.aclose()
    return n


def cmd_import_lidseeker(args) -> int:
    report = import_lidseeker(args.path, match_available=not args.no_match)
    print(json.dumps(report, indent=2))
    return 0


def backup(dest: str) -> str:
    """A consistent single-file snapshot regardless of WAL state (for the nightly restic run)."""
    from .db.conn import connect
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".tmp"
    if os.path.exists(tmp):
        os.unlink(tmp)
    with connect() as c:
        c.execute("VACUUM INTO ?", (tmp,))
    os.chmod(tmp, 0o600)
    os.replace(tmp, dest)
    return dest


def cmd_backup(args) -> int:
    dest = args.dest or os.path.join(os.path.dirname(config.settings.db_path), "backup",
                                     "continuo.db")
    print(backup(dest))
    return 0


def cmd_policy_check(_args) -> int:
    from .api.admin import policy_check
    from .upstream import http

    async def run():
        try:
            return await policy_check()
        finally:
            await http.aclose()
    _migrate()
    report = asyncio.run(run())
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 2


def cmd_refresh_library(_args) -> int:
    from .upstream import http
    from .workers import index

    async def run():
        try:
            return await index.refresh()
        finally:
            await http.aclose()
    _migrate()
    print(json.dumps(asyncio.run(run())))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="continuo")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)
    cu = sub.add_parser("create-user", help="password from $CONTINUO_NEW_PASSWORD or a prompt")
    cu.add_argument("username")
    cu.add_argument("--role", choices=("admin", "user"), default="user")
    cu.add_argument("--navidrome-username")
    cu.set_defaults(fn=cmd_create_user)
    il = sub.add_parser("import-lidseeker")
    il.add_argument("path")
    il.add_argument("--no-match", action="store_true",
                    help="skip matching imported available requests to Navidrome")
    il.set_defaults(fn=cmd_import_lidseeker)
    b = sub.add_parser("backup")
    b.add_argument("dest", nargs="?")
    b.set_defaults(fn=cmd_backup)
    sub.add_parser("policy-check").set_defaults(fn=cmd_policy_check)
    sub.add_parser("refresh-library").set_defaults(fn=cmd_refresh_library)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
