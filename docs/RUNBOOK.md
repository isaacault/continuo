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

## Build and test (local CI)

```bash
scripts/ci.sh            # ruff + pytest in the pinned CI image (Dockerfile.dev), no network
scripts/ci.sh --image    # … and build the production image (native ARM64)
scripts/merge_when_green.sh <pr>   # the merge gate: ci.sh --image on the PR head → "local-ci" status → merge
docker build -t ghcr.io/isaacault/continuo:edge .   # the image the Jetson runs (pull_policy: never)
```

GitHub Actions only runs on demand (`gh workflow run ci --ref <branch>`, for outside
contributors). `ci.sh` serialises with Polyphony's local CI through the shared lock
`/ssd/Personal/repositories/.locks/docker-ci.lock` and waits for `CI_MIN_FREE_MB` (700) of free
memory before starting, because the Jetson shares RAM with the homelab. The CI image is tagged
by a hash of `pyproject.toml` + `Dockerfile.dev` and rebuilt only when they change.

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

**Clients that send credentials in the body** (issue #4, decided 2026-10-21). forward_auth
passes only the request line and headers to continuo, never the body, so a client using the
OpenSubsonic `formPost` extension (POST with `u`/`t`/`s` in an `x-www-form-urlencoded` body) got
error 10 even with a valid app password. Navidrome 0.64 advertises `formPost`, and Feishin switches
to POST exactly when it's advertised. Fix: continuo answers `getOpenSubsonicExtensions` on
`music-api.ault.me` itself, with Navidrome's list minus `formPost` and `apiKeyAuthentication`
(Navidrome's master branch adds it; an `apiKey` isn't a façade credential either), so compliant
clients stay on query-string auth. continuo asks Navidrome for the list on each call (no
credentials, no `Remote-User`) and returns it as XML, JSON or JSONP, whichever the client asked
for; if Navidrome is down it answers with a Subsonic error (HTTP 502). It fetches the list rather
than hard-coding it, so other extensions (`transcodeOffset`, `songLyrics`, `sonicSimilarity` when
configured, …) stay correct across Navidrome upgrades. The route is **unauthenticated**, as the
spec requires ("must be publicly accessible"); Navidrome serves it without auth too. It reveals
no more than Navidrome's own public answer, and continuo never forwards or logs the client's
parameters. Rejected: accepting credentials in a header (no Subsonic client sends
them there, so it fixes nothing); sending POST `/rest/*` through continuo so it can read the
body (audio would then pass through continuo, against the 256 MB budget and the design); a Caddy
plugin that copies the body into headers (custom Caddy build). A client that POSTs without
checking the list still fails. forward_auth logs `POST … without query credentials (formPost?)`
for that case.

Deploy a continuo image that has the route **first**: older images answer this path with a 404.
Then make the Caddy change, in the `music-api.ault.me` block, **above** `handle /rest/*` (as in the
snippet):

```caddyfile
	handle /rest/getOpenSubsonicExtensions* {
		reverse_proxy continuo:8080
	}
```

It has a single directive, so it needs no `route { }`. If a second directive is ever added
(e.g. `request_header`), wrap them in `route { }`, the same lesson as the façade block. Caddy
tries `handle` blocks with a longer path first, so it wins over `/rest/*` wherever it sits, but
keep it above so the file reads in the order Caddy applies it. The `*` also covers the `.view`
form. Reload, then:

```bash
# formPost (and apiKeyAuthentication) gone, the rest of Navidrome's list intact, no credentials:
curl -s 'https://music-api.ault.me/rest/getOpenSubsonicExtensions?f=json' \
  | jq -c '."subsonic-response" | [.type, .serverVersion, [.openSubsonicExtensions[].name]]'
# every other endpoint still needs a façade credential:
curl -s -o /dev/null -w '%{http_code}\n' 'https://music-api.ault.me/rest/ping?v=1.16.1&c=x'   # 401
```

Then Feishin: it reads the extension list when it connects to the server. If it still POSTs
(look for the forward_auth log line above), remove and re-add the server. It should sign in,
browse and play.

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

**Removing albums and the media cache** (issue #23, decided 2026-10-23 with the user): an admin
can remove an album from Polyphony (`POST /v1/library/albums/{navidromeId}/remove`). continuo
unmonitors it in Lidarr (so it never re-downloads), moves its folder with one rename into
`<MUSIC_DIR>/.continuo-cache/removed-<ts>-<id>/`, and has Lidarr and Navidrome rescan. Restore
(`POST /v1/library/removed/{id}/restore`) moves it back to the same path and re-monitors it if it
was monitored. Needs the music folder mounted read-write at the path Navidrome reports
(`/ssd/data/media/music:/music`, `MUSIC_DIR=/music`); without it the capability
`library_removal` is absent and the routes answer 503. The cache is a dot-folder with
`.ndignore`, so Navidrome skips it and Lidarr skips it as hidden. Eviction is least recently used
under `MEDIA_CACHE_MAX_GB` (25), never inside `REMOVED_MIN_DAYS` (7, the undo window), and always
after `REMOVED_MAX_DAYS` (30); it runs nightly and after each removal. The same cache will hold
streamed and prefetched music ("play now", #12) under one eviction policy. Refused: folders shared
with another album, an artist root, anything outside `MUSIC_DIR`. Restic's music backup includes
the cache, so a removed album also survives in older snapshots. Check: `ls -la
/ssd/data/media/music/.continuo-cache` after a removal; `GET /v1/library/removed`.
**Which artist a library artist is** (decided 2026-10-23): files without MusicBrainz tags (e.g.
the Peach Pit albums, which bypassed Lidarr's tagging) give Navidrome artists no MBID, and names are
ambiguous (four "Peach Pit"s in MusicBrainz). `GET /v1/catalog/artists/resolve?name=` picks among
same-name catalogue artists: one already in Lidarr, else the one whose discography shares the most
album titles with the library's albums by that name, else Lidarr's order. Polyphony's artist page
uses it before loading the discography.

**Catalogue artist pages** (decided 2026-10-22, the user's choice): `GET /v1/catalog/artists/{mbid}`
gives any artist what a library artist page shows — name, picture, biography, top songs, similar
artists — for Polyphony's catalogue artist page (the discography stays on `/albums`). Sources, each
best-effort and cached 6 h per artist: Lidarr's metadata server (name, picture via
`images.lidarr.audio`, overview); Wikipedia's lead paragraph via the artist's Wikidata link when
Lidarr has no overview (`www.wikidata.org`, `en.wikipedia.org`); ListenBrainz similar artists
(`labs.api.listenbrainz.org`, open). **Top songs** come from ListenBrainz's popularity API, which
since 2025 needs a user token: set `LISTENBRAINZ_TOKEN` (a free ListenBrainz account's token, as a
sops secret) to enable them; without it `topTracks` is empty and `sources.topTracks` says
`needs_token`. A failed source empties its section (`sources.* = unavailable`), never the page;
only an artist no source knows is a 404. Check: `curl -s -H "Authorization: Bearer <token>"
https://music-api.ault.me/v1/catalog/artists/69158f97-4c07-4c4e-baf8-4e4ab1ed666e | jq .sources`.

**Catalogue artwork** (issue #13, decided 2026-10-21): search, catalogue and request outputs
never point at third-party hosts; catalogue art is `{PUBLIC_BASE_URL}/v1/artwork?src=<url>[&size=px]`
(library art stays Subsonic `getCoverArt`). The route is **unauthenticated** on purpose: Coil
loads images with Polyphony's base OkHttp client, which sends no auth headers, and
`music-api.ault.me` is LAN/tailnet only (Caddy `import private`; no Caddy change needed, the
catch-all `handle` already proxies `/v1/*`). It is not an open proxy: `src` must be https on
`coverartarchive.org`, `images.lidarr.audio` or `imagecache.lidarr.audio`; redirects are followed
by hand (max 5) and only to those or `archive.org` / `*.archive.org` (CAA → 307 archive.org → 302
`iaNNN.us.archive.org`), http redirects upgraded to https; only jpeg/png/webp/gif (type header
and magic bytes), at most `ARTWORK_MAX_BYTES` (10 MB); upstream fetches are rate-limited per
client (300/min). `size` picks CAA's 250/500/1200 thumbnail. **archive.org fallback** (issue #15,
decided 2026-10-21): every CAA image goes through archive.org's front door, which sometimes goes dark
(an 8 s ConnectTimeout was seen from the Jetson; DNS, route and IPv4 were fine, and there's no
AAAA). So when the direct chain fails (connect timeout 4 s), continuo fetches Lidarr's cached copy
`https://images.lidarr.audio/cache/<CAA URL>` (Cloudflare; same bytes and sizes, 404 for no art).
It then tries that mirror first for 10 min (log line `using Lidarr's mirror first`). CAA's own 404 is
final; the mirror's isn't. CAA's JSON index doesn't help here: it also redirects through
archive.org. Responses: `Cache-Control: public,
max-age=604800` + ETag. Disk cache: `/data/artwork` (`ARTWORK_CACHE_DIR`), capped at
`ARTWORK_CACHE_MB` (200), least-recently-used files evicted down to 90 %; upstream 404s ("no
art") are remembered in memory for an hour. The cache is disposable (`rm -rf
/ssd/appdata/continuo/artwork` clears stale covers) and can be left out of restic. Check:
`curl -sI "https://music-api.ault.me/v1/artwork?src=https%3A%2F%2Fcoverartarchive.org%2Frelease-group%2F<rg>%2Ffront-500"`
→ 200 `image/jpeg`; `?src=https%3A%2F%2Fexample.com%2Fx.jpg` → 400 `artwork_not_allowed`.

**Request latency** (issue #10, evidence for "play now"): `GET /v1/admin/latency?days=30` (admin
token) gives median/p90 seconds per source for request → download start → first complete file →
available in Navidrome, plus the recent requests. Milestones are recorded by the 45 s status loop
and the Lidarr webhook; requests made before 0.2's migration were backfilled from the event log,
where "first file" is only known as "all files in" (an upper bound).
