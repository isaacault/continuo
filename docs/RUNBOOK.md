# continuo runbook

How to deploy each phase onto the Jetson stack, check it against the real services, and back out.
Design and rationale: [PLAN.md](../PLAN.md). Files referenced here live in [`deploy/`](../deploy).

## Facts about the live stack (checked 2026-10-20)

| | |
| --- | --- |
| Navidrome | 0.64.2. Reverse-proxy auth is **ExtAuth** now: `ND_EXTAUTH_TRUSTEDSOURCES` / `ND_EXTAUTH_USERHEADER` (the plan's `ND_REVERSEPROXY*` names are the deprecated aliases). Trusted `Remote-User` works on `/rest` **and** the native `/api`. The Subsonic path does **not** auto-create users: every continuo user needs an existing Navidrome account (default: same name). |
| Lidarr | 3.1.0, in gluetun's netns; continuo reaches it at `http://gluetun:8686` on `homelab_default`. Root folder `/data/music`. |
| slskd | `http://gluetun:5030`. |
| Caddy | on `homelab_default`; sites reach services via `host.docker.internal:<published port>`. `music.ault.me` is **public** (:8443) as well as private — the façade's header stripping matters. |
| Home Assistant | host network, `http://192.168.0.101:8123`. |
| Backups | `/ssd/appdata` is already in the restic set. |

## Build and test

```bash
docker build -t continuo-dev - <<'EOF'
FROM python:3.12-slim
RUN pip install fastapi==0.138.1 "uvicorn[standard]==0.49.0" httpx==0.28.1 pyjwt==2.13.0 bcrypt==5.0.0 pytest==8.4.2 ruff==0.14.0
EOF
docker run --rm -u $(id -u):$(id -g) -e HOME=/tmp -v $PWD:/src -w /src continuo-dev sh -c 'ruff check . && pytest -q'
docker build -t ghcr.io/isaacault/continuo:edge .     # local image until CI publishes one
```

## Phase 0/1 — continuo on the Jetson, search + requests

1. **Navidrome service user.** In Navidrome's UI create `continuo-svc`, admin (needed for
   `startScan`), long random password.
2. **Secrets.** `secrets/continuo.env` from [`deploy/continuo.env.example`](../deploy/continuo.env.example),
   `sops -e -i`, then add the `continuo` line to `make secrets-decrypt`.
3. **Compose.** `apps/continuo/continuo.yaml` = [`deploy/compose/continuo.yaml`](../deploy/compose/continuo.yaml)
   (+ `docker-proxy.conf` next to it), included from `docker-compose.yaml`.
4. **Caddy.** Add the `music-api.ault.me` block from [`deploy/caddy/Caddyfile.snippet`](../deploy/caddy/Caddyfile.snippet)
   (Phase 2 parts stay commented). `docker exec caddy caddy reload --config /etc/caddy/Caddyfile`.
5. `make up APP=continuo` (and `APP=continuo-docker-proxy`).
6. **Uptime Kuma.** (a) HTTP monitor `https://music-api.ault.me/v1/health`, 60 s, expects 200 —
   503 means continuo's DB or a loop is dead; (b) Push monitor (60 s heartbeat, 3 min grace) →
   put its URL in `UPTIME_KUMA_PUSH_URL` (use `http://uptime-kuma:3001/api/push/<token>`). Both
   notify via ntfy — the only ntfy path.
7. **Backups.** In `backup/profiles.yaml` `run-before` (both profiles):
   `docker exec continuo continuo backup || true` → consistent copy at
   `/ssd/appdata/continuo/backup/continuo.db`, inside the existing `/ssd/appdata` source.

Verify (PLAN.md §6 Phase 0/1):

```bash
curl -s https://music-api.ault.me/v1/health | jq            # status ok, upstream navidrome/lidarr/slskd ok
docker stats --no-stream continuo                            # RSS < 120 MB
T=$(curl -s https://music-api.ault.me/v1/auth/login -H 'content-type: application/json' \
     -d '{"username":"isaac","password":"…","device":{"name":"curl"}}' | jq -r .accessToken)
H="Authorization: Bearer $T"
curl -s -H "$H" https://music-api.ault.me/v1/me | jq
curl -s -H "$H" 'https://music-api.ault.me/v1/search?q=<album you own>&types=album' | jq '.results[0].availability'
curl -s -H "$H" 'https://music-api.ault.me/v1/search?q=<album you do not own>&types=album' | jq '.results[0]'
curl -s -H "$H" https://music-api.ault.me/v1/admin/policy-check | jq
# one cheap, legal album:
curl -s -H "$H" -H 'content-type: application/json' https://music-api.ault.me/v1/requests \
     -d '{"musicBrainzReleaseGroupId":"<rg mbid>"}' | jq
curl -s -H "$H" https://music-api.ault.me/v1/requests/<id>/events | jq
```

Then: Lidarr shows the artist with *Monitor New Albums: None* and exactly that one album
monitored; Lidarr → *Refresh all artists* → within 60 s the album is monitored again; an
unfindable album shows `stillLooking: true` after 6 cycles (30 min) and stays `searching`;
`POST /v1/requests/<id>/search-now` shows an AlbumSearch in Lidarr → System → Tasks. Stop the
container → Uptime Kuma → ntfy outage alert.

## Phase 2 — the Subsonic façade

1. Add the `music-auth` network (fixed `172.30.42.0/29`) and attach caddy `.2`, continuo `.3`,
   navidrome `.4` with alias `navidrome-trusted` — see the end of `deploy/compose/continuo.yaml`.
2. Navidrome: `ND_EXTAUTH_TRUSTEDSOURCES=172.30.42.2/32,172.30.42.3/32`,
   `ND_EXTAUTH_USERHEADER=Remote-User`. Recreate navidrome + caddy (brief playback blip).
3. continuo: `NAVIDROME_AUTH=header`, `NAVIDROME_URL=http://navidrome-trusted:4533`; drop
   `NAVIDROME_SERVICE_PASSWORD`.
4. Caddy: add the `/rest/*` handle from the snippet — inside `route { }`, because Caddy's default
   directive order runs `request_header` *after* `forward_auth` and would strip the user it just
   set — and `request_header -Remote-User` to both `music.ault.me` blocks. Reload.

Deployed 2026-10-20 (homelab `128155c`, `route` fix `096dc46`): verified that a forged
`Remote-User` is ignored on the public and private `music.ault.me` and on `:4533`; a façade
credential maps to the right Navidrome user even with a forged header; a 25 MB FLAC streamed with
continuo sending ~300 bytes (the auth answer); stars land on the mapped user; a revoked device is
refused.

Verify:

```bash
# header stripping on the public and private Navidrome sites → error 10 (missing u), NOT a login:
curl -s -H 'Remote-User: isaac' 'https://music.ault.me/rest/ping?v=1.16.1&c=x&f=json'
# no valid façade credential → 401 from forward_auth:
curl -s -o /dev/null -w '%{http_code}\n' 'https://music-api.ault.me/rest/ping?u=isaac&p=wrong&v=1.16.1&c=x'
# the auth check is not reachable from outside:
curl -s -o /dev/null -w '%{http_code}\n' https://music-api.ault.me/internal/auth/subsonic   # 404
```

Then Polyphony: add server `https://music-api.ault.me` with the `library` credential from the
login response (`GET /v1/me/library` shows it again) → library syncs, FLAC streams, cover art,
stars persist under *your* Navidrome user. Same in Cassette / Feishin. `DELETE /v1/devices/<id>`
→ the next stream is 401. `docker stats continuo` stays flat while streaming. Navidrome's web UI
on `music.ault.me` still works with passwords. Cast/DLNA still play.

## Phase 3 — closing the loop

1. **Lidarr** → Settings → Connect → Webhook: URL
   `https://music-api.ault.me/internal/webhooks/lidarr`, method POST, header
   `X-Webhook-Secret: <LIDARR_WEBHOOK_SECRET>` (or put it in the password field — Basic auth is
   accepted too, or `?secret=` as a last resort); events: On Grab, On Release Import, On Upgrade,
   On Download Failure, On Import Failure, On Health Issue. Press **Test** → continuo logs
   `eventType=Test`. (Lidarr is inside gluetun: if Test can't connect, gluetun's
   `FIREWALL_OUTBOUND_SUBNETS` must include `192.168.0.0/24`. Without the webhook, requests
   still complete via polling, just slower.)
2. **MusicBrainz tags: deliberately not written** (decided 2026-10-21). Lidarr's *Tag Audio
   Files* stays **No** — the plan's "new files only" was wrong for this stack:
   - Lidarr edits tags in place, and a torrent's library file is a hardlink of the file still
     seeding, so tagging would corrupt the torrent (tagscrub exists to avoid exactly this).
   - Navidrome 0.64 derives album/track IDs from `musicbrainz_albumid` / `musicbrainz_trackid`
     when present, so adding them later re-identifies albums: Polyphony re-syncs them as new,
     offline copies re-download, stars/plays/playlist entries are likely lost, and a
     just-imported album's id changes under continuo's "ready to play" link.
   - Matching doesn't need them: folder paths match first and cover the whole library.
   Extending tagscrub to add them (safe for hardlinks) was considered and rejected for the
   ID churn. Albums whose uploader already tagged them (e.g. via Picard) keep their tags; the
   policy check warns if Lidarr tagging is ever switched on.
3. **Home Assistant**: automation from [`deploy/homeassistant/music-requests.yaml`](../deploy/homeassistant/music-requests.yaml);
   put its webhook URL in `HA_WEBHOOK_URL`.
4. Polyphony: handle `polyphony://album/{server}/{albumId}` (server = `music-api.ault.me`).

Lidarr webhook (done 2026-10-21): connection "continuo" → `http://continuo:8080/internal/webhooks/lidarr`
with header `X-Webhook-Secret`. Not `https://music-api.ault.me`: Lidarr is in gluetun's netns, whose
DNS refuses private answers (rebinding protection) and whose firewall blocks the LAN; the container
name resolves there and follows continuo across recreates.

Verify: request an album → `GET /v1/requests/<id>/events` shows `webhook_grab`,
`webhook_import`, `status → scanning`, `matched` (expect `"strategy": "paths"`), `notified`; the
phone gets "Ready to play"; tapping opens the album. Negative: move an imported folder aside
before the scan → after 10 min `needs_attention` (`import_not_visible`) + HA alert; move it back
→ `available` after the next scan. Nothing reaches ntfy.

## Phase 5 — cut-over from lidseeker, and rollback

```bash
cp /ssd/appdata/lidseeker/lidseeker.db /ssd/appdata/lidseeker/lidseeker.db.pre-continuo
docker cp /ssd/appdata/lidseeker/lidseeker.db continuo:/tmp/lidseeker.db
docker exec continuo continuo import-lidseeker /tmp/lidseeker.db     # idempotent; re-run at cut-over
make stop APP=lidseeker                                              # keep its compose + data
```

Status mapping: `failed` → `searching` + `stillLooking`; `pending` with a Lidarr album →
`searching`, without → re-added; `available` → `available` (already notified, never announced
again) and matched to its Navidrome album; artist requests are skipped (policy).

**Rollback** at any point: `make up APP=lidseeker`; clear Lidarr's webhook. continuo never
writes lidseeker's DB, and every request made meanwhile exists in Lidarr either way.

## Operations

```bash
docker exec continuo continuo policy-check          # exit 2 if a policy check warns
docker exec continuo continuo refresh-library
docker exec -it continuo continuo create-user charlie --navidrome-username charlie
docker exec continuo continuo backup
```

**Request latency** (issue #10, evidence for "play now"): `GET /v1/admin/latency?days=30` (admin
token) gives median/p90 seconds per source for request → download start → first complete file →
available in Navidrome, plus the recent requests. Milestones are recorded by the 45 s status loop
and the Lidarr webhook; requests made before 0.2's migration were backfilled from the event log,
where "first file" is only known as "all files in" (an upper bound).
