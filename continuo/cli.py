"""continuo CLI: migrate, create-user, set-role, merge-artist, latin-names, import-lidseeker, backup,
policy-check, refresh-library.

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


def cmd_set_role(args) -> int:
    """Make a user an admin (to remove albums, see everyone's requests) or a user again."""
    from .db import users
    _migrate()
    u = users.get(args.username)
    if u is None:
        print(f"no user {args.username!r}", file=sys.stderr)
        return 1
    if u["role"] == "admin" and args.role != "admin" and users.count_admins() <= 1:
        print("can't demote the last admin", file=sys.stderr)
        return 1
    users.set_role(u["id"], args.role)
    print(f"{args.username} is now {args.role}")
    return 0


def cmd_merge_artist(args) -> int:
    """File every album Navidrome lists under [name] (e.g. 青葉市子, as Lidarr tagged an import)
    under the library's [into] (Ichiko Aoba): rewrite those files' artist and album artist tags,
    then rescan. Only the albums listed under [name] are touched."""
    import asyncio

    from .db import library as lib_db
    from .library import cache, tags
    from .requests.normalise import normalise
    from .upstream import navidrome
    _migrate()
    try:
        music = cache.music_dir()
    except cache.Unavailable:
        print("MUSIC_DIR isn't set or mounted", file=sys.stderr)
        return 1
    if args.mbid:
        # Saved first: with nothing to retag yet, future imports still get the library's name.
        from .db import identity
        identity.set_library_name(args.mbid, args.into)
    albums = [a for a in lib_db.all_albums() if normalise(a.get("artist")) == normalise(args.name)]
    if not albums:
        print(f"no albums by {args.name!r} in the library index (run refresh-library first?)", file=sys.stderr)
        return 1
    changed = 0
    for a in albums:
        if not a.get("folder"):
            print(f"  skipped {a['name']!r}: folder unknown", file=sys.stderr)
            continue
        folder = os.path.realpath(os.path.join(music, a["folder"]))
        if not folder.startswith(music + os.sep):
            continue
        n = tags.rename_artist(tags.audio_files(folder), {args.name}, args.into)
        changed += n
        print(f"  {a['name']}: {n} file(s)")
    try:
        asyncio.run(navidrome.start_scan())
    except Exception as e:  # noqa: BLE001
        print(f"rescan didn't start ({e}); Navidrome's watcher will catch up", file=sys.stderr)
    print(f"{changed} file(s) now by {args.into}")
    return 0


def cmd_latin_names(args) -> int:
    """Preview (or --apply) giving non-Latin artists their NAME_LOCALE name on the albums Lidarr
    downloaded; the user's own copies are listed and never touched."""
    import asyncio

    from .library import cache, latin_names
    from .upstream import navidrome
    _migrate()
    plans, notes = asyncio.run(latin_names.plan())
    print(latin_names.describe(plans, notes))
    if not args.apply:
        print("\n(preview only: run again with --apply to retag the albums marked 'retag')")
        return 0
    try:
        changed = latin_names.apply(plans)
    except cache.Unavailable:
        print("MUSIC_DIR isn't set or mounted", file=sys.stderr)
        return 1
    try:
        asyncio.run(navidrome.start_scan())
    except Exception as e:  # noqa: BLE001
        print(f"rescan didn't start ({e}); Navidrome's watcher will catch up", file=sys.stderr)
    print(f"\nretagged {changed} file(s)")
    return 0


def cmd_libraries(args) -> int:
    """Per-user libraries (#27): status, init <user> [--all] [--wait], carry <user> [--dry-run],
    grant <user>, revert <user> (back to the shared store)."""
    import asyncio

    from .db import account_libraries as acc_db
    from .db import library as lib_db
    from .db import members as members_db
    from .db import users
    from .library import accounts, links
    _migrate()
    if args.action == "status":
        for u in users.list_all():
            acc = acc_db.get(u["id"])
            n = len(members_db.folders(u["id"]))
            seen = lib_db.count_in_library(acc["navidrome_library_id"]) if acc else 0
            print(f"{u['username']:16} library {acc['navidrome_library_id'] if acc else '-':>3}  "
                  f"{n} album(s) in continuo, {seen} indexed by Navidrome")
        return 0
    user = users.get(args.username or "")
    if user is None:
        print(f"no user {args.username!r}", file=sys.stderr)
        return 1
    try:
        if args.action == "init":
            # One event loop for both steps: the HTTP clients belong to the loop that made them.
            async def init_and_wait() -> tuple[dict, int | None]:
                out = await accounts.init(user, everything=args.all)
                print(f"{user['username']}: Navidrome library {out['library']}, {out['albums']} album(s) "
                      f"({out['added']} added), {out['linked']} file(s) linked", flush=True)
                return out, (await accounts.scan_until(out["library"], out["expected"]) if args.wait else None)
            out, have = asyncio.run(init_and_wait())
            if have is not None:
                print(f"Navidrome indexed {have}/{out['expected']} album(s) in it")
                if have < out["expected"]:
                    return 3
        elif args.action == "carry":
            from .library import carry
            acc = acc_db.get(user["id"])
            if acc is None:
                print(f"{user['username']} has no library yet: run init first", file=sys.stderr)
                return 1
            async def run_carry() -> dict:
                if not args.dry_run:
                    await accounts.grant_store_and_own(user)   # to rate and list its new songs
                return await carry.carry(users.navidrome_name(user), acc["navidrome_library_id"], args.dry_run)
            out = asyncio.run(run_carry())
            print(("would carry" if args.dry_run else "carried") +
                  f": {out['songs']} song(s) matched ({out['unmatched_songs']} not in the new library), "
                  f"{out['ratings']} rating(s), {out['stars']} star(s), {out['playlists']} playlist(s) "
                  f"({out['playlist_tracks']} track(s); {out['unmatched_tracks']} unmatched, left as they are)")
        elif args.action == "grant":
            asyncio.run(accounts.grant_only_own(user))
            print(f"{user['username']} now sees only its own library")
        elif args.action == "revert":
            asyncio.run(accounts.revert(user))
            print(f"{user['username']} is back on the shared store library (its folder is kept)")
    except links.Unavailable as e:
        print(str(e), file=sys.stderr)
        return 1
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
        from .library import singles
        library = singles.whole(lib_db.all_albums())
        for row in req_db.list_requests(statuses=("available",), kind="album"):
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
    sr = sub.add_parser("set-role", help="make a user an admin or a user")
    sr.add_argument("username")
    sr.add_argument("role", choices=("admin", "user"))
    sr.set_defaults(fn=cmd_set_role)
    ma = sub.add_parser("merge-artist", help="file albums tagged with one artist name under another")
    ma.add_argument("name", help="the name the albums carry now (e.g. as Lidarr tagged them)")
    ma.add_argument("--into", required=True, help="the library's name for the artist")
    ma.add_argument("--mbid", help="the MusicBrainz artist id, so future imports use --into too")
    ma.set_defaults(fn=cmd_merge_artist)
    ln = sub.add_parser("latin-names", help="preview/apply NAME_LOCALE names for non-Latin artists")
    ln.add_argument("--apply", action="store_true", help="retag (default: preview only)")
    ln.set_defaults(fn=cmd_latin_names)
    lb = sub.add_parser("libraries", help="per-user libraries: status | init <user> [--all] [--wait] | "
                                          "carry <user> [--dry-run] | grant <user> | revert <user>")
    lb.add_argument("action", choices=("status", "init", "carry", "grant", "revert"))
    lb.add_argument("username", nargs="?")
    lb.add_argument("--all", action="store_true", help="init: the account gets every album in the store")
    lb.add_argument("--wait", action="store_true", help="init: wait until Navidrome has indexed the library")
    lb.add_argument("--dry-run", action="store_true", help="carry: count, change nothing")
    lb.set_defaults(fn=cmd_libraries)
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
