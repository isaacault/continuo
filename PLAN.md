# Continuo — plan for a unified music backend and multi-platform clients

> Working name: **continuo** (the bass line that underpins polyphony). Rename freely; nothing
> below depends on the name. This document is the plan only; no code was written.

Written 2026-10-20 against: Polyphony `0.1.0-alpha.14` (commit `93b8346`), your Lidseeker fork
(`isaacault/lidseeker`, `master` + `fix/88-album-search` + `fix/keep-searching`), and the service
layout you described for `/ssd/homelab`. I could not read `/ssd/homelab` or the Jetson directly
(SSH was denied from this session) and the public `isaacault/homelab` repo is a year stale
(Traefik era, no navidrome/lidseeker/soulseek/torrent-cleanup), so everything about the compose
repo comes from your prompt. Items marked **(verify)** are things to confirm on your versions.

---

## 0. Summary and recommendation

**Yes, a separate project.** The backend is a new repo (`continuo`). Polyphony stays the client
and grows a requests feature; later it becomes the multi-platform client. The shared artefact
between them is the HTTP contract (OpenAPI), not code. A third place changes too: the homelab
compose repo gets `apps/continuo.yaml`, a Caddy site, a Navidrome config change, an Uptime Kuma
monitor and a Home Assistant automation.

**The one idea that makes it seamless:** the backend serves two "planes" on one hostname with one
login.

1. **Library plane = a Subsonic-compatible façade.** `https://music-api.ault.me/rest/*` looks like
   Navidrome to any client. Caddy runs `forward_auth` against the backend for every `/rest/*`
   request; the backend validates a backend-issued, per-device token and answers with the
   Navidrome username; Caddy proxies the request to Navidrome with `Remote-User` set. Navidrome's
   `/rest/*` endpoints accept reverse-proxy auth and ignore `u/t/s` when the header is present
   (official docs). Result: audio bytes go Navidrome → Caddy → device, never through the backend
   process; the device never holds a Navidrome credential; the backend holds **no** Navidrome
   passwords at all; and *every existing Subsonic client on every OS works today* (Polyphony
   unchanged, Cassette on iOS/macOS, Feishin/Supersonic on desktop, Navidrome's web UI).
2. **Request plane = a small JSON API** (`/v1/*`): auth, unified search (Navidrome + Lidarr
   catalogue merged), requests with a live pipeline, notifications, Lidarr webhook, matching
   "Lidarr album → Navidrome album" so a request ends as a tap-to-play notification.

This replaces your "signed stream URLs" idea. Section 8.2 has the argument; the HMAC-signed-URL
design is kept as the fallback.

**Shape of the work**

| Phase | Outcome | Rough size |
| --- | --- | --- |
| 0 | Repo, container on the Jetson, Caddy, health, Uptime Kuma | 1 evening |
| 1 | Auth + unified search + request one album + live pipeline (ported Lidseeker core) | 1–2 weekends |
| 2 | Subsonic façade: one login, Polyphony + any client plays through `music-api.ault.me` | 1 weekend |
| 3 | Closing the loop: Lidarr webhook, Navidrome matching, notification → tap → album | 1–2 weekends |
| 4 | Polyphony UX: unified search, request buttons, requests screen, deep links | 2 weekends |
| 5 | Migrate off Lidseeker, archive it | 1 evening + 2 weeks soak |
| 6 | Multi-platform clients and library expansion (KMP track, fileshares, Jellyfin) | ongoing |

Backend language: **Python 3.12 + FastAPI + httpx + SQLite**, the same stack as Lidseeker and
`torrent-cleanup`, so the port is near-verbatim and the image stays small on ARM64 (~80–120 MB
RSS). Section 8.1 covers Go/Kotlin alternatives.

---

## 1. Questions and the assumptions I made instead

None of these block the plan; each has a stated assumption. Correct any that are wrong and the
affected section is noted.

| # | Question | Assumption used | Affects |
| --- | --- | --- | --- |
| Q1 | Navidrome version on the Jetson? | ≥ 0.56 (new scanner + folder watcher, `mbzReleaseGroupId`, reverse-proxy auth on `/rest`, CVE-2025-27112 fixed in 0.54.5) | §2.5, §2.6, §8.16 |
| Q2 | Does `*.ault.me` resolve on the tailnet (split DNS) and does Caddy already terminate TLS for `music.ault.me`? | Yes to both | §7.1 |
| Q3 | Is Caddy on the same compose network as Navidrome (bridge) or on host networking? | Same bridge network; Navidrome's whitelist can name Caddy's container IP/CIDR | §2.5, §7.1 |
| Q4 | Can a bridge-network container reach Lidarr/slskd at `http://gluetun:8686` / `:5030`? (Lidseeker uses `network_mode: host` + `localhost`) | Yes if gluetun is on that network; otherwise run the backend with `network_mode: host` like Lidseeker | §7.1 |
| Q5 | Household or just you? | Multi-user capable (Lidseeker already is), you are admin; most flows single-user | §2.4 |
| Q6 | Is the Home Assistant companion app on your phone, and is the existing torrent-cleanup → HA webhook path working? | Yes | §2.8 |
| Q7 | Lidarr quality profile used for requests: is "Upgrades Allowed" off? | It should be; the backend will check and warn | §2.9 |
| Q8 | "Fileshare systems" in your first message = SMB/NFS shares holding music (NAS, other people's collections)? | Yes | §9.3 |
| Q9 | Backend language preference if not Python? | Python | §8.1 |
| Q10 | Which phone/app currently receives torrent-cleanup alerts, and do you want request notifications grouped with them or separate? | HA companion; separate notification channel | §2.8 |

---

## 2. Architecture

### 2.1 Components

```
                    tailnet / LAN (never public)
   ┌────────────┐   ┌──────────────┐  ┌──────────────┐  ┌─────────────┐
   │ Polyphony  │   │ Cassette /   │  │ Feishin etc. │  │ HA companion│
   │ (Android)  │   │ iOS, macOS   │  │ desktop, web │  │ (push)      │
   └─────┬──────┘   └──────┬───────┘  └──────┬───────┘  └──────▲──────┘
         │ /v1 (JSON)      │ /rest           │ /rest           │ notify
         │ /rest           │                 │                 │
   ══════╪═════════════════╪═════════════════╪═════════════════╪═══════ Caddy
         │                 │                 │                 │
   music-api.ault.me ──────┴─────────────────┘                 │
     ├─ /rest/*  ── forward_auth ─▶ continuo /internal/auth/subsonic
     │              └─ reverse_proxy ─▶ navidrome:4533  (+ Remote-User)
     └─ /v1/*    ── reverse_proxy ─▶ continuo:8080
   music.ault.me ── reverse_proxy ─▶ navidrome:4533 (Remote-User stripped; unchanged)
                                                               │
   ┌──────────────────────── continuo (FastAPI, SQLite) ───────┼──────────────┐
   │ auth · façade token check · unified search · requests · matching · inbox │
   │ loops: status / reconcile / scan-watch · webhooks: lidarr · events (SSE) │
   └──┬───────────┬──────────────┬──────────────┬──────────────┬─────────────┘
      │ Remote-User│ X-Api-Key    │ X-API-Key    │ docker-proxy │ webhook
      ▼            ▼              ▼              ▼              ▼
   navidrome   lidarr (gluetun) slskd (gluetun) soularr restart Home Assistant
   /rest,/api  /api/v1          /api/v0         (nginx, 1 route)
                     ▲
                     │ Connect → Webhook (On Release Import, On Grab, failures)
                     └──────────────────────────────── continuo /internal/webhooks/lidarr

   unchanged: Prowlarr, qBittorrent, Soularr, torrent-cleanup (rules 9–11), tagscrub,
              restic nightly backup of /ssd/data/media/music
```

The backend never touches the music files. Lidarr imports into `/ssd/data/media/music` exactly as
today, Navidrome's watcher (or a scan the backend triggers) picks the album up, and the backend
only *observes* both sides and links them.

### 2.2 Planes and responsibilities

| Plane | What it does | Who implements |
| --- | --- | --- |
| Library (`/rest/*`) | Browse, search-in-library, stream, download, cover art, playlists, stars, scrobbles — all of Subsonic | Navidrome, behind Caddy; backend only authenticates |
| Catalogue | "Everything that exists": Lidarr `/album/lookup`, `/artist/lookup` (SkyHook metadata), MusicBrainz for tracks and tracklists, Cover Art Archive | Backend (ported `lidarr.py`) |
| Unified search (`/v1/search`) | Library + catalogue merged; each hit carries an `availability` | Backend |
| Requests (`/v1/requests`) | Lidarr orchestration, monitoring reconciliation, pipeline status from Lidarr queue + slskd + Soularr denylist, never-give-up | Backend (ported `lidarr.py`, `main.py`, `slskd.py`, `soularr_*`) |
| Matching | Lidarr album ⇄ Navidrome album identity; `available` = playable in Navidrome | Backend (new) |
| Notifications | Inbox table + SSE for foreground + HA companion push with deep link; later UnifiedPush | Backend (new) + HA automation |
| Identity | Backend users, JWT + refresh tokens per device, per-device façade tokens, Navidrome username mapping | Backend (ported `auth.py`, extended) |

### 2.3 Data flow walkthroughs

**Search "boards of canada geogaddi"**

1. App → `GET /v1/search?q=…`.
2. Backend fans out concurrently: Navidrome `search3` as the user (Remote-User), Lidarr
   `/album/lookup` + `/artist/lookup` (10-minute cache per normalised term; SkyHook is
   rate-limited), active requests from SQLite.
3. Merge: key = MusicBrainz release-group ID when both sides have one, else normalised
   `artist|title` (NFKC, curly→straight quotes, strip punctuation, casefold — the fix/88 lesson).
   Library hits win; catalogue hits that resolve to a library album are folded into it.
4. Each result gets `availability`: `in_library` (navidromeAlbumId), `requested`/`searching`/
   `downloading`/`importing` (requestId + percent), `partial` (Navidrome has it but fewer tracks
   than the release group), or `requestable`.
5. One list back to the app: play buttons on the first kind, request buttons on the last.

**Request an album**

1. App → `POST /v1/requests {kind:"album", musicBrainzReleaseGroupId}`. Backend inserts the row
   (`pending`), returns immediately with title/art from `lookup_album_by_id`, dispatches the add.
2. Ported `request_album`: `GET /artist?mbId=` (replaces the full-artist-list scan), add artist
   with `monitor=none`, `monitorNewItems=none`, `searchForMissingAlbums=false` if absent; ensure
   artist `monitored=true`; wait for the album row; `_monitor_album` (monitor → blocking
   RefreshArtist → re-monitor → verify track list); `AlbumSearch`.
3. Status `searching`. From here Lidarr/Prowlarr/qBittorrent and Soularr/slskd do what they do
   today; torrent-cleanup's rules 9–11 keep doing what they do today.
4. Reconcile loop (60 s) re-asserts monitoring until files exist. Status loop (45 s) refreshes
   pipeline; SSE pushes changes to any connected app.

**Download lands → "tap to play"**

1. Lidarr imports → Connect webhook `On Release Import` → `POST /internal/webhooks/lidarr`
   (shared-secret header). Payload has `album.MBId` (release-group), `artist.MBId`,
   `trackFiles[].path`. Backend sets `importing → scanning`, records the import paths.
2. Backend asks Navidrome to scan (`/rest/startScan` as the admin service user) if the watcher
   hasn't already; polls `getScanStatus`, then matches (§2.6). On match: stores
   `navidrome_album_id`, status `available`, `available_at`.
3. Notification: inbox row + SSE `request.available` + HA webhook → HA automation →
   `notify.mobile_app_<phone>` with `clickAction` = `polyphony://album/{serverId}/{albumId}`.
4. Tap → Polyphony opens the album; the next library sync (or the deep-link handler's
   `refreshAlbum`) has it in Room.
5. If Lidarr says 100 % but Navidrome still lacks the album after 10 minutes: status
   `needs_attention`, detail "imported but not visible in Navidrome", HA alert (not ntfy).

**Play**

Unchanged for the client: `GET https://music-api.ault.me/rest/stream?id=…&u=isaac&t=…&s=…` →
Caddy `forward_auth` → backend verifies `t == md5(facadeSecret(u, device) + s)` → `200` with
`Remote-User: isaac` → Caddy → Navidrome streams. Validation is a hash compare, sub-millisecond.

### 2.4 Auth, identity, secrets

- **Backend users**: ported `users` table (bcrypt, roles admin/user). Add `devices`
  (per-device refresh token hash, façade secret, platform, push registration, last seen,
  revoked_at). Login returns a short-lived access JWT (1 h), a rotating refresh token per device,
  and the façade credential for that device. Revoking a device kills API access *and* playback.
- **Façade tokens** are random per-device secrets, not the user's password. Subsonic token auth
  needs the plaintext on the server side to verify `md5(secret+salt)`, so they live in SQLite in
  plaintext — acceptable because they are random, per-device, revocable, and only grant Navidrome
  access as that user via Caddy. Also accept `p=enc:…` for old clients.
- **Navidrome mapping**: one Navidrome username per backend user (same name by default). With
  reverse-proxy auth Navidrome auto-creates missing users on first request **(verify)**; stars,
  playlists, play counts and scrobbles stay per person. A separate admin service user
  (`continuo-svc`) is used only for `startScan`, `getScanStatus` and the album index.
- **Upstream secrets** (Lidarr key, slskd key, HA webhook URL/id, JWT secret, Lidarr webhook
  secret) come from `secrets/continuo.env` (sops+age) exactly like the other apps.
- **Caddy is the trust boundary**: it must strip any incoming `Remote-User` on *every* route to
  Navidrome and only set it from the `forward_auth` response. Navidrome's `ReverseProxyWhitelist`
  lists only Caddy's and the backend's addresses. Navidrome's docs warn that enabling proxy auth
  does not disable the other methods; that is what we want (`music.ault.me` keeps working for
  existing clients with their Navidrome passwords).

### 2.5 Streaming

Target design (Phase 2): the façade above. What you gain over backend-minted signed URLs:

- No client changes anywhere; Polyphony's Subsonic provider, Cassette, Feishin all just work.
- No expiry problem. Polyphony resolves stream URLs at load time, but Cast and DLNA receivers hold
  real URLs for the remaining queue and Polyphony re-sends them (ARCHITECTURE.md). Expiring
  signatures would break long queues and downloads; permanent signatures are just credentials.
- Offline downloads and the Coil artwork cache keep stable cache keys (Polyphony's fixed
  per-server salt exists for exactly that).

Fallback (if proxy-auth on `/rest` misbehaves on your version): backend-signed URLs on a second
route `GET /v1/stream/{trackId}?u&exp&sig&format&maxBitRate` validated by the same `forward_auth`
handler (HMAC over `trackId|user|exp|format|maxBitRate`), with Caddy rewriting to `/rest/stream`.
Still no audio through the backend. Polyphony would need a `provider:continuo` module for it.

Rejected: proxying audio bytes through the FastAPI process (Jetson CPU/memory, extra hop, backend
becomes a playback single point of failure).

### 2.6 Album identity and matching

Three ID spaces meet here:

| System | Album identity | Where |
| --- | --- | --- |
| Lidarr | `foreignAlbumId` = MusicBrainz **release-group** ID; each album has `releases[]` with `foreignReleaseId` (release MBIDs), one `monitored` | `/api/v1/album/{id}` |
| MusicBrainz | release-group ⊃ releases ⊃ recordings | `musicbrainz.org/ws/2` |
| Navidrome | internal MD5 id; Subsonic `AlbumID3.musicBrainzId` = **release** MBID (`mbzAlbumId`); native API also has `mbzReleaseGroupId`, and tracks have `path` | `/rest/getAlbum`, `/api/album` |

Matching order, each step deterministic before the next heuristic:

1. **Paths** (webhook-driven): Lidarr's `trackFiles[].path` → album directory → Navidrome tracks
   whose `path` is under it (same filesystem; `/music` in both containers; strip the root prefix).
   Most reliable signal and needs no tags.
2. **Release-group MBID**: Navidrome native `/api/album` `mbzReleaseGroupId` == Lidarr
   `foreignAlbumId` (needs the tag `MUSICBRAINZ_RELEASEGROUPID` in the files).
3. **Release MBID**: Subsonic `musicBrainzId` ∈ Lidarr `releases[].foreignReleaseId`.
4. **Normalised artist + title (+ year ±1, + track count)**, with a confidence score; below the
   threshold the request becomes `needs_attention` with the candidates listed, never a silent
   wrong link.

The backend keeps a small `library_albums` index (navidrome id, name, artist, year, track count,
`mbzAlbumId`, `mbzReleaseGroupId`, folder path, updated_at), refreshed after each scan completes
and nightly. For 90 albums this is nothing; it still works at 10k.

**Making MBIDs appear in files.** Prefer Lidarr's own tagging over extending tagscrub: Lidarr
Settings → Metadata → "Tag Audio Files with Metadata" set to the *new files only* option
(internal enum `WriteAudioTags = NewFiles`), "Scrub Existing Tags" **off**. Lidarr then writes
`MUSICBRAINZ_ALBUMID`, `MUSICBRAINZ_RELEASEGROUPID`, artist/album-artist/track IDs on import and
never touches files it did not import — consistent with "never touch my purchased music". Audit
`music-tagscrub` so it never strips `MUSICBRAINZ_*` tags; keep it only for files that bypass
Lidarr. **(verify** the option labels on your Lidarr build and inspect one imported file with
`metaflac --list` / `ffprobe`.**)**

### 2.7 State

SQLite with WAL, one file in the app's data dir, numbered SQL migrations (Lidseeker's inline
`ALTER TABLE` checks don't scale past a few columns). Tables:

| Table | Purpose | Notes |
| --- | --- | --- |
| `users` | ported | + `navidrome_username` |
| `devices` | per-device refresh token hash, façade secret, platform, push kind/endpoint, created/last_seen/revoked | new |
| `requests` | ported, extended: `mb_release_group_id`, `lidarr_artist_id`, `lidarr_album_id`, `navidrome_album_id`, `status`, `still_looking`, `search_cycles`, `last_search_at`, `last_source` (torrent/soulseek), `import_paths` (JSON), `error`, `available_at`, `notified_at`, `user_id` | statuses: `pending, searching, downloading, importing, scanning, available, error, needs_attention` — **no terminal `failed`** |
| `request_events` | append-only log per request (status transitions, webhook hits, searches fired, matching decisions) | powers the pipeline detail and debugging |
| `library_albums` | Navidrome album index for matching | rebuilt from Navidrome, disposable |
| `notifications` | inbox: user_id, type, title, body, action JSON, created_at, read_at, delivered_via | new |
| `catalog_cache` | term → Lidarr lookup JSON, TTL | optional, could be in-memory |
| `settings` | key/value: Soularr quality, feature flags | replaces env for runtime-tunable bits |

Why not Postgres: one writer, tiny data, 8 GB shared RAM, restic-friendly single file.

### 2.8 Background work

| Worker | Interval | Ported from | Change |
| --- | --- | --- | --- |
| status loop | 45 s | `_status_loop` | no give-up; computes pipeline incl. torrent/soulseek source; emits SSE on any pipeline change, not only status; writes `request_events`; triggers matching when Lidarr reports files |
| reconcile loop | 60 s | `_reconcile_loop` + `ensure_album_monitored` | unchanged logic; also re-asserts `monitorNewItems=none` on the artist |
| scan watcher | event-driven + 30 s while any request is `scanning` | new | `startScan` once per import burst; poll `getScanStatus`; run matching; `needs_attention` after 10 min |
| library index refresh | after scans + nightly 04:00 | new | repopulate `library_albums` |
| stranded requeue | at startup | `_requeue_stranded_requests` | unchanged |
| heartbeat | 60 s | new | POSTs to an Uptime Kuma *push* monitor; `/v1/health` returns 503 if any loop is older than 5 min (Lidseeker's health only checked the DB, so dead loops went unnoticed) |
| Lidarr webhook | push | new | `Grab` → `downloading` + source; `Download` (import) → `importing/scanning`; `DownloadFailure`/`ImportFailure` → detail + keep searching; `Retag`, `Health*` ignored. Polling remains the source of truth; the webhook only makes it fast |

### 2.9 Notifications

Policy from your constraints: ntfy only for outages (Uptime Kuma owns that); everything about
requests goes to Home Assistant / in-app.

| Channel | When | Mechanism |
| --- | --- | --- |
| In-app, foreground | always | SSE `GET /v1/events` (ported `_broadcast`); events `request.updated`, `request.available`, `notification.new` |
| In-app, background | always | inbox `GET /v1/notifications?since=` polled by a WorkManager periodic job (15 min minimum) and on app open; local notification with the deep link |
| OS push | Phase 3 | backend → HA webhook (same pattern as torrent-cleanup) → automation → `notify.mobile_app_<phone>` with `data.clickAction` = the deep link (if a bare custom scheme is refused by the companion app, use the `intent://album/{id}#Intent;scheme=polyphony;package=app.polyphony;end` form) and a dedicated channel "Music requests" |
| OS push, later | Phase 6 | UnifiedPush (Polyphony registers with a distributor; your ntfy server would be transport only, invisible to you as alerts — your call whether that still violates the ntfy rule). FCM is not recommended: Firebase project, Google dependency, only needed for a Play release |

Events that notify: `available` (always), `needs_attention` (always), `error` on add (always),
weekly digest of `still_looking` requests (optional, off by default). Never per-search-cycle.

### 2.10 Hard constraints → mechanisms

| Constraint | Mechanism |
| --- | --- |
| Torrent/arr/Soulseek traffic stays on the home IP | Backend runs on the Jetson and only speaks to Lidarr/slskd APIs; all download traffic still originates from Lidarr/qBittorrent/slskd inside gluetun. Dev instances off-site may point at the real APIs over the tailnet; they still never move download traffic. Test downloads always run on the Jetson instance |
| Nothing follows artists, nothing grabs automatically | `add_artist(monitor="none", monitorNewItems="none", searchForMissingAlbums=False)`; reconcile re-asserts `monitorNewItems=none`; **`request_artist` is not ported**; no "monitor all" UI |
| Never auto-upgrade or replace purchased music | Startup policy check reads the quality profile and warns if `upgradeAllowed`; webhook `IsUpgrade=true` raises `needs_attention` + HA alert; backend never calls rename/retag/upgrade; Lidarr tagging restricted to new files |
| Never give up | `SEARCH_GIVE_UP_ATTEMPTS` concept removed; `still_looking=true` after N fruitless cycles is a *display* flag; album stays monitored; torrent-cleanup rule 10 keeps re-searching daily; manual "search now" always available |
| ntfy only for outages | Backend has no ntfy integration; Uptime Kuma → ntfy |
| Nightly backup covers downloaded music | Library path and Lidarr root folder unchanged; backend writes nothing into the library; backend data dir added to the restic include list (§7.5) |
| Container on the Jetson, small memory | python-slim ARM64 image, single uvicorn worker, `mem_limit: 256m`, SQLite |
| Private `*.ault.me` only | Caddy site on the internal/tailnet listener only; no public DNS; backend binds to the compose network |
| Don't break Lidseeker; cut-over + rollback | §7.3 |

### 2.11 Known Lidarr pitfalls → mechanisms

| Pitfall | Handling |
| --- | --- |
| Scheduled artist refresh un-monitors requested albums | ported `ensure_album_monitored` loop (60 s) + `_monitor_album`'s monitor → blocking refresh → re-monitor sequence |
| Album rows without releases/statistics after metadata-profile changes | in `request_album`, after `_wait_for_album`: if `releases` empty or `statistics` missing → blocking `RefreshArtist`, re-fetch, then monitor; pipeline shows "waiting for Lidarr metadata" instead of 0 % |
| `/album/lookup` returns ids without statistics; curly-quote titles | port fix/88 `_attach_album_statistics` (batch `/album?albumIds=`); one `normalise()` used by search merge, matching and dedupe |
| Soularr skips albums already in the download queue | pipeline reads Lidarr `/queue` `protocol`, `trackedDownloadStatus`, `sizeleft/timeleft`; a torrent with no progress for > N h shows "torrent stalled — torrent-cleanup replaces after 24 h, Soulseek fallback after"; Phase 4 action **Drop this torrent now** = `DELETE /queue/{id}?removeFromClient=true&blocklist=true`, which frees Soularr immediately |

---

## 3. API sketch

Base `https://music-api.ault.me`. JSON, camelCase, `Authorization: Bearer <access JWT>` on
`/v1/*` except login/refresh. Errors: `{ "error": { "code": "lidarr_unavailable", "message":
"…", "retryable": true } }` with 401/403/404/409/429/502/503. OpenAPI is generated by FastAPI and
published per release; Polyphony's DTOs are hand-written against it (small surface) and checked in
CI with a contract test (schemathesis or a recorded-fixture test).

### 3.1 Auth and devices

```
POST /v1/auth/login
  { "username": "isaac", "password": "…", "device": { "name": "Pixel 9", "platform": "android" } }
→ 200 {
  "accessToken": "…", "expiresIn": 3600, "refreshToken": "…",
  "user": { "id": 1, "username": "isaac", "role": "admin" },
  "device": { "id": "dev_7f3a", "name": "Pixel 9" },
  "library": {                       # what a Subsonic client needs — Polyphony adds it as a Server
    "type": "subsonic", "baseUrl": "https://music-api.ault.me",
    "username": "isaac", "password": "<façade secret for this device>"
  }
}
POST /v1/auth/refresh   { "refreshToken": "…" } → new access + rotated refresh (old one revoked)
POST /v1/auth/logout    → revokes this device (API + façade)
GET  /v1/me             → { user, device, capabilities: ["requests","notifications","events"] }
GET  /v1/devices        → [ { id, name, platform, lastSeenAt, current } ]
DELETE /v1/devices/{id} → revoke another device
PUT  /v1/devices/{id}/push  { "kind": "unifiedpush", "endpoint": "…" }   # Phase 6
```

### 3.2 Unified search and catalogue

```
GET /v1/search?q=geogaddi&types=album,artist,track&limit=25
→ 200 {
  "query": "geogaddi",
  "sources": { "library": "ok", "catalog": "ok" | "degraded" | "unavailable" },
  "results": [
    {
      "kind": "album",
      "key": "mb:rg:4a1b…",                       # stable merge key; "nd:<id>" when no MBID
      "title": "Geogaddi", "artist": "Boards of Canada", "year": 2002, "albumType": "Album",
      "artwork": { "url": "https://music-api.ault.me/rest/getCoverArt?id=…" | "https://music-api.ault.me/v1/artwork?src=https%3A%2F%2Fcoverartarchive.org%2F…" },
      # library art: Subsonic getCoverArt (client adds its façade auth); catalogue art: continuo's
      # unauthenticated, allow-listed artwork proxy (issue #13), never a third-party host
      "ids": { "musicBrainzReleaseGroupId": "4a1b…", "navidromeAlbumId": "e9c…", "lidarrAlbumId": 412 },
      "availability": { "state": "in_library", "trackCount": 23 }
      # or { "state": "requestable" }
      # or { "state": "downloading", "requestId": 17, "percent": 42.0, "source": "soulseek" }
      # or { "state": "partial", "have": 9, "expected": 23, "navidromeAlbumId": "…" }
    },
    { "kind": "artist", "key": "mb:artist:69158…", "name": "Boards of Canada",
      "ids": { "musicBrainzArtistId": "…", "navidromeArtistId": "…" },
      "availability": { "state": "in_library", "albumCount": 4 } },
    { "kind": "track", "key": "mb:rec:…", "title": "Music Is Math", "artist": "…",
      "album": { "title": "Geogaddi", "musicBrainzReleaseGroupId": "4a1b…" },
      "availability": { "state": "in_library", "navidromeTrackId": "…" } }
  ]
}
GET /v1/catalog/artists/{mbid}/albums        # ported artist_albums; each with availability
GET /v1/catalog/albums/{mbid}                 # tracklist (ported album_tracks) + availability
GET /v1/library/albums/by-mbid/{mbid}         # resolve release-group or release MBID → navidrome id
```

### 3.3 Requests

```
POST /v1/requests
  { "kind": "album", "musicBrainzReleaseGroupId": "4a1b…", "note": "optional" }
→ 201 Request      # 409 { code: "already_requested", requestId } if live; 409 "in_library" with navidromeAlbumId

GET  /v1/requests?scope=active|all&mine=true|false&limit=&cursor=
GET  /v1/requests/{id}
DELETE /v1/requests/{id}                       # unmonitor + remove (ported)
POST /v1/requests/{id}/retry                   # ported: clear denylist, re-monitor, search, kick Soularr
POST /v1/requests/{id}/search-now              # ported
POST /v1/requests/{id}/drop-download           # Phase 4: blocklist + remove current queue item
POST /v1/requests/search-now                   # ported global
POST /v1/requests/clear-available              # ported

Request = {
  "id": 17, "kind": "album",
  "title": "Geogaddi", "artist": "Boards of Canada", "year": 2002,
  "artwork": { "url": "…" },
  "ids": { "musicBrainzReleaseGroupId": "…", "lidarrArtistId": 9, "lidarrAlbumId": 412, "navidromeAlbumId": null },
  "status": "searching",                         # pending|searching|downloading|importing|scanning|available|error|needs_attention
  "stillLooking": true,                          # display flag, never terminal
  "pipeline": {
    "stages": ["requested","searching","downloading","importing","scanning","available"],
    "stage": "searching", "stageIndex": 1,
    "percent": 0, "trackFiles": 0, "trackCount": 23,
    "source": null | "torrent" | "soulseek",
    "detail": "Still looking — 14 search cycles, last 2h ago. Searching continues automatically.",
    "lastSearchAt": "2026-10-20T07:12:00Z", "searchCycles": 14,
    "download": { "client": "qBittorrent", "sizeLeft": 123456789, "timeLeft": "00:12:30", "stalled": false } | null,
    "attention": null | { "reason": "import_not_visible" | "upgrade_detected" | "ambiguous_match", "candidates": [...] }
  },
  "error": null | { "message": "Couldn't find this album in Lidarr…", "retryable": true },
  "requestedBy": "isaac", "createdAt": "…", "updatedAt": "…", "availableAt": null,
  "play": null | { "navidromeAlbumId": "e9c…", "deepLink": "polyphony://album/{serverId}/e9c…" }
}
GET /v1/requests/{id}/events → [ { at, type, detail } ]   # the request_events log
```

### 3.4 Notifications and events

```
GET  /v1/events                  # SSE; auth via Bearer header, or ?token= for EventSource
     event: request.updated   data: Request (slim)
     event: request.available data: { requestId, play }
     event: notification.new  data: Notification
GET  /v1/notifications?since=<cursor>&unread=true
→ { "items": [ { "id": 88, "type": "request_available", "title": "Ready to play",
                 "body": "Geogaddi — Boards of Canada", "createdAt": "…", "readAt": null,
                 "action": { "type": "open_album", "navidromeAlbumId": "…", "requestId": 17, "deepLink": "polyphony://album/…" } } ],
    "cursor": "…", "unreadCount": 1 }
POST /v1/notifications/read   { "ids": [88] } | { "all": true }
```

### 3.5 Admin, health, internal

```
GET  /v1/health              → 200 { status:"ok", db:true, loops:{ status:"3s ago", reconcile:"41s ago", scan:"idle" },
                                     upstream:{ navidrome:"ok", lidarr:"ok", slskd:"ok" }, version } ; 503 when degraded
GET  /v1/admin/policy-check  → quality profile upgrade flag, artists with monitorNewItems≠none, Lidarr tagging setting, Navidrome proxy-auth reachable
GET  /v1/admin/stats         → ported counts
GET/POST/DELETE /v1/admin/users…   → ported
GET/PUT /v1/admin/settings   → Soularr quality (ported), feature flags
GET  /internal/auth/subsonic                 # Caddy forward_auth; reads X-Forwarded-Uri; 200 + Remote-User, or 401
POST /internal/webhooks/lidarr               # X-Webhook-Secret header; eventType Test|Grab|Download|DownloadFailure|ImportFailure|…
POST /internal/events/search                 # optional: torrent-cleanup reports re-searches { lidarrAlbumId, at, rule }
```

### 3.6 The façade (`/rest/*`)

Not implemented by the backend beyond `/internal/auth/subsonic`. Caddy proxies everything to
Navidrome. Polyphony's `getOpenSubsonicExtensions`, `getLyricsBySongId`, `search3`, `stream`,
`getCoverArt`, `scrobble`, playlists, stars — all Navidrome's. If you ever want the façade to
*augment* a Subsonic call (e.g. append catalogue hits to `search3`), that becomes a backend route
that proxies that one endpoint; not planned.

---

## 4. Porting plan from Lidseeker

Licence: keep `LICENSES/lidseeker-MIT.txt` (the upstream MIT text with the IvoryCobra-VC copyright
line) and a one-line header in each ported file: `# Portions derived from lidseeker
(https://github.com/IvoryCobra-VC/lidseeker), MIT License.` The new repo can be MIT or GPL-3.0
(Polyphony is GPL-3.0; the two never link, so either is fine). Merge `fix/88-album-search` and
`fix/keep-searching` into your fork's `master` first so the Lidseeker you run during the
transition is the fixed one.

### 4.1 Module map

| Lidseeker | → continuo | Verdict |
| --- | --- | --- |
| `app/lidarr.py` | `continuo/upstream/lidarr.py` (client + normalisers), `continuo/requests/orchestrate.py` (request flow), `continuo/requests/pipeline.py` (status) | port with changes (below) |
| `app/main.py` | `continuo/api/*.py` routers, `continuo/workers/*.py` loops, `continuo/events.py` (SSE) | port; split; drop SPA serving and CSP |
| `app/db.py` | `continuo/db/{conn,migrations,requests,users,devices,notifications,library}.py` + `migrations/0001_init.sql…` | port; schema v1 = Lidseeker's + new tables |
| `app/slskd.py` | `continuo/upstream/slskd.py` | port; improve matching |
| `app/soularr_cfg.py`, `app/soularr_ctl.py` | `continuo/upstream/soularr.py` | port as-is (atomic write, quality presets, denylist, docker-proxy restart) |
| `app/auth.py` | `continuo/auth/{passwords,jwt,devices,facade}.py` | port; add refresh tokens, devices, façade secrets |
| `app/ratelimit.py` | `continuo/ratelimit.py` | port as-is |
| `app/notify.py` (ntfy) | `continuo/notify/{inbox,homeassistant}.py` | **replace** (no ntfy) |
| `app/config.py` | `continuo/config.py` | port the pattern; new variables (§7.2) |
| `app/schemas.py` | `continuo/api/schemas.py` | evolve to §3 shapes |
| `backend/docker-proxy/` (nginx allow-list) | `deploy/docker-proxy/` | port as-is |
| `backend/tests/` | `tests/` | port `test_pipeline.py`, `test_db.py`, `test_main.py` (give-up tests become never-give-up), `test_search_in_library.py` (fix/88), auth tests; drop SPA/security-header tests |
| `web/` React UI, `android/` | — | drop (per your decision; see §8.10 about a minimal page) |

### 4.2 Function-level notes for `lidarr.py`

Port unchanged: `_best_image`, `_year`, `normalize_artist`, `search_tracks`, `_pick_release_group`,
`_norm_text` (→ promote to the shared `normalise()`), `album_tracks` + `_pick_release` +
tracklist cache, `lookup_album_by_id`, `lookup_artist_by_id`, `artist_albums`,
`_metadata_artist_albums`, `_coverart_url`, `add_artist`, `_albums_for_artist_id`,
`_wait_for_album`, `set_album_monitored`, `set_artist_monitored`, `album_search`, `_run_command`,
`refresh_artist`, `_monitor_album`, `ensure_album_monitored`, `queue_index`, `_build_pipeline`,
`PIPELINE_STAGES` (+ `scanning`), the shared httpx client pattern.

Port with changes:

- `normalize_album` + `search`: take the fix/88 versions (`_attach_album_statistics`); add
  `musicBrainzReleaseGroupId` to the normalised shape; run titles through `normalise()` for merge
  keys but keep the display title as-is.
- `find_library_artist`: use `GET /artist?mbId=<foreignArtistId>` instead of listing every artist
  per call.
- `_get_album`: Lidseeker defines it twice (lines 600 and 784; the cached second one wins). Keep
  the cached one only.
- `request_album`: add the "no releases / no statistics → blocking refresh → retry" step (§2.11);
  write `request_events`; return `mb_release_group_id` and the monitored `foreignReleaseId`.
- `request_status`: keep as the coarse fallback only; the pipeline is authoritative.
- `request_pipeline`: add `scanning` (between Lidarr 100 % and Navidrome match), `source`
  detection from queue `protocol` vs slskd activity, stalled-torrent detection, `still_looking`
  instead of `failed`; remove the `config.SEARCH_GIVE_UP_ATTEMPTS` message; `_is_stuck` becomes
  "cycles since last progress" rather than a 25-minute wall clock.
- `queue_index`: also capture `protocol`, `downloadClient`, `sizeleft`, `timeleft`,
  `trackedDownloadStatus`, `statusMessages`, queue `id` (needed for drop-download).

Do not port: `request_artist` (policy), `discover`, `discover_categories`, `_discover_pool`
(policy-neutral browse feature; park it — it's 90 lines if you want it in Phase 6).

### 4.3 Notes for `main.py`

- `_status_loop`: keep structure; replace ntfy with inbox + HA; call matching when Lidarr reports
  `trackFileCount == trackCount` or on webhook; broadcast on pipeline change (hash the pipeline
  dict); the "clean up slskd searches" side effect stays.
- `_track_search_attempts` → `_count_search_cycles`: same counting, no terminal state; sets
  `still_looking` when `cycles >= STILL_LOOKING_AFTER_CYCLES` (display only).
- `_reconcile_loop`: unchanged; add `monitorNewItems` re-assertion on the artist.
- `create_request`, `_process_request`, `list_requests` (semaphore + batched queue index),
  `delete_request`, `retry_request`, `search_request_now`, `_search_now`, `clear_available`,
  `stats`, users CRUD, `_upstream_error`, rate limiters: port.
- SSE `_broadcast`/`sse_stream`: port; add per-user filtering (events only to the requester and
  admins); keep `?token=` for EventSource clients, prefer the header.
- Drop: `_security_headers` CSP (no SPA), static mounts, `/api/services`, `/api/settings` ntfy
  fields.

### 4.4 Notes for `slskd.py`

`artist_progress` matches transfers by artist name only; two concurrent requests from one artist
collide. Match on `normalise(artist)` **and** `normalise(album title)` in the path, falling back to
artist-only when nothing matches. Keep the 5 s transfer cache and lock. `delete_searches_for`
unchanged.

### 4.5 Bugs and sharp edges seen while reading (fix in the port)

- Duplicate `_get_album` (above).
- `find_library_artist` O(all artists) per call.
- `_is_stuck` ignores progress; a request that downloaded 9/23 tracks then stalled looks "stuck"
  by wall clock only.
- `health` returns 200 with dead loops.
- `search_tracks` hits MusicBrainz with a fresh client per call and no caching; add a 10-minute
  cache and reuse one client with the required User-Agent (`continuo/<ver> (your contact)`).
- SSE token in the query string is logged by proxies; keep but prefer the header and add
  `?token=` to Caddy's log redaction.

---

## 5. Polyphony integration, incrementally

Facts that shape this (from the code): the Subsonic provider puts all auth in the query string
with a fixed per-server salt; stream and Coil artwork fetches use the base OkHttp client (custom
headers do not reach them — a header-based backend auth would not work for playback, which is
another reason the façade uses Subsonic auth); routes carry no `serverId`; the only deep link is
`polyphony://player`; `core:domain` is pure Kotlin; the roadmap's M3 already names "Lidarr add
to home library requests", "slskd requests" and "Unified sources".

### Stage 0 — nothing (backend Phase 1)

Drive the backend with curl/HTTPie. Optional: an "Experimental → Request server" settings entry
that stores the backend URL + token and shows `/v1/health` in Diagnostics.

### Stage 1 — one login, no feature code (backend Phase 2)

Add `music-api.ault.me` as a Subsonic server in Polyphony with the façade credential. That is the
existing Connect flow; zero code. Everything plays through the façade; Cast/DLNA receivers get
`https://music-api.ault.me/rest/stream?…` URLs, which they can fetch because the tailnet/LAN
resolves the name (HTTPS hosts are never rewritten by `RemoteUrlResolver`, so the receiver must
be able to reach that name — same as today for `music.ault.me`).

Small optional code: a "Sign in with Continuo" onboarding path: `POST /v1/auth/login`, store
backend tokens, auto-create the Subsonic `Server` from `library` in the response, link
`server.options["continuoAccount"] = deviceId`. This is what makes "one login" literal.

### Stage 2 — requests feature (backend Phase 3–4)

New modules, following the existing dependency rule:

```
core/domain      + model: CatalogAlbum, CatalogArtist, Availability, MusicRequest, RequestPipeline,
                   InboxNotification; contracts: RequestRepository, CatalogSearch, NotificationInbox,
                   ContinuoAccountRepository; Capability.REQUESTS, Capability.NOTIFICATIONS
core/requests    (Android lib) OkHttp + kotlinx.serialization client for /v1, SSE via okhttp-sse,
                   token refresh interceptor, Room table for inbox + request cache, WorkManager
                   poller (15 min), local notifications on a "Music requests" channel
app              Search: third section "Not in your library" from /v1/search when the active server
                   is Continuo-linked; Album/Artist screens: Request / Requested / Downloading chip;
                   RequestsRoute (list + stepper, retry, search now, drop download);
                   InboxRoute; Settings → Account (devices, sign out everywhere);
                   deep links polyphony://album/{serverId}/{albumId}, polyphony://requests/{id},
                   polyphony://inbox via navDeepLink on AlbumRoute / new routes; if serverId ≠ active,
                   switch active server first (routes carry no serverId today)
```

Search merge in the app: local Room FTS results first (instant), remote `/v1/search` on submit
replaces the provider `search3` call for Continuo-linked servers (the backend already includes
library hits), so the UI shows one list with play/request affordances. Keep `searchRemote` via
`search3` for plain Subsonic servers.

### Stage 3 — unified sources (roadmap M3)

Downloaded / Library / Catalogue toggle; "partial" albums show missing tracks greyed with a
request affordance; request state visible in album lists via a small chip. `Capability.REQUESTS`
gates all of it so other servers are untouched.

### Stage 4 — multi-platform (§9)

---

## 6. Phased roadmap with verification against the real services

Each phase ends with a check you run on the Jetson stack. Branch per phase in `continuo`, PR,
CI = `ruff` + `pytest` + ARM64 image build.

### Phase 0 — skeleton on the Jetson

Deliver: repo layout (§Appendix A), FastAPI app with `/v1/health`, config loader, migrations
runner with `0001_init.sql`, Dockerfile (multi-stage `python:3.12-slim`, non-root, `arm64` +
`amd64` via buildx in GitHub Actions → `ghcr.io/isaacault/continuo`), `apps/continuo.yaml`,
`secrets/continuo.env`, Caddy site `music-api.ault.me` → backend (no `/rest` yet), Uptime Kuma
HTTP monitor on `/v1/health` with JSON keyword `ok`, push monitor for the heartbeat.

Verify: `make up APP=continuo`; `curl -s https://music-api.ault.me/v1/health` from a tailnet
device shows upstream `navidrome/lidarr/slskd: ok`; stop the container → Uptime Kuma → ntfy
outage alert arrives (this is the one ntfy use); `docker stats` RSS < 120 MB.

### Phase 1 — unified search and one album request

Deliver: ported auth (login, JWT, refresh, devices), ported `lidarr.py`/`slskd.py`/`soularr.py`,
`requests` orchestration and pipeline with never-give-up, `/v1/search` merge against Navidrome
`search3` (service user via Remote-User **or**, until Phase 2 enables proxy auth, a dedicated
Navidrome service account password — a temporary env var), `/v1/requests*`, SSE, policy check
endpoint, Lidseeker tests ported and green.

Verify (all on the Jetson instance, downloads originate there):

1. `POST /v1/auth/login` → tokens; `GET /v1/me`.
2. `GET /v1/search?q=<album you own>` → `in_library` with the right `navidromeAlbumId`;
   `q=<album you don't own>` → `requestable` with a release-group MBID; `q=<curly-quote title>`
   → merged correctly (regression for fix/88).
3. Request one cheap, legal, obtainable album. Watch `GET /v1/requests/{id}` move
   `pending → searching → downloading → importing`; `GET …/events` shows each transition; Lidarr
   shows the artist added with `monitorNewItems=none` and exactly one album monitored.
4. Force Lidarr's "Refresh all artists"; within 60 s the album is monitored again (reconcile).
5. Request an album that cannot be found; after `STILL_LOOKING_AFTER_CYCLES` the row says
   `stillLooking=true`, status still `searching`, album still monitored; `search-now` fires an
   `AlbumSearch` (visible in Lidarr → System → Tasks/Events).
6. `GET /v1/admin/policy-check` reports your quality profile's upgrade flag.
7. Lidseeker is still running and untouched.

### Phase 2 — the façade

Deliver: Navidrome `ND_REVERSEPROXYUSERHEADER=Remote-User`,
`ND_REVERSEPROXYWHITELIST=<caddy ip/cidr>,<continuo ip/cidr>`; Caddy `music-api.ault.me`
`/rest/*` → `request_header -Remote-User` → `forward_auth continuo /internal/auth/subsonic`
(`copy_headers Remote-User`) → `reverse_proxy navidrome:4533`; `music.ault.me` gains
`request_header -Remote-User`; backend `/internal/auth/subsonic` (parse `u`,`t`,`s`|`p`, verify
against the device's façade secret, map to Navidrome username, 200/401); per-device façade secret
issued at login; the Phase-1 temporary service password removed (service calls now use
Remote-User too).

Verify:

1. Polyphony: add server `https://music-api.ault.me` with the façade user/secret → library syncs,
   a FLAC streams, cover art loads, star/unstar persists in Navidrome under *your* user.
2. Cassette (your fork) on iOS/macOS and Feishin on a laptop log in the same way and play.
3. Revoke the device (`DELETE /v1/devices/{id}`) → next stream request is 401 in Caddy's log.
4. `curl -H 'Remote-User: admin' https://music.ault.me/rest/ping?v=1.16.1&c=x` → Navidrome
   error 10 (missing `u`), proving Caddy strips the header; same against `music-api` without a
   valid token → 401 from forward_auth.
5. While streaming, `docker stats continuo` shows no bandwidth/CPU rise; Caddy ↔ Navidrome does.
6. Cast or DLNA to a receiver on the LAN; queue re-send after a transcode change still works.
7. Navidrome's own web UI at `music.ault.me` still works with passwords.

### Phase 3 — closing the loop

Deliver: Lidarr Connect → Webhook to `https://music-api.ault.me/internal/webhooks/lidarr` (or the
internal compose address) with the secret header, events Grab, Release Import, Download Failure,
Import Failure, Health; `library_albums` index; matching (§2.6) with `request_events` decisions;
scan watcher; `available` = matched in Navidrome; notification inbox + SSE; HA webhook + HA
automation YAML (notify + clickAction + channel); Lidarr tagging set to new-files-only; tagscrub
audited for `MUSICBRAINZ_*` preservation; Polyphony Stage 2 deep link handler for
`polyphony://album/{serverId}/{albumId}` (minimal, so the notification tap has somewhere to go).

Verify:

1. Press "Test" on the Lidarr webhook → backend logs `eventType=Test` with the secret accepted.
2. Request an album → on import, backend logs the webhook within seconds, `scanning` appears,
   Navidrome scan status cycles, request becomes `available` with `navidromeAlbumId`;
   `GET /v1/requests/{id}/events` shows which matching strategy fired (expect `paths`).
3. Phone receives the HA notification; tapping opens the album in Polyphony and it plays.
4. Inspect one imported file: `metaflac --list` shows `MUSICBRAINZ_RELEASEGROUPID`; after
   tagscrub runs it is still there; Navidrome native `/api/album/<id>` shows `mbzReleaseGroupId`
   and matching strategy 2 would also have matched.
5. Negative: move an imported album folder aside before the scan → after 10 min the request is
   `needs_attention` with an HA alert; move it back → recovers to `available` on next scan.
6. Confirm nothing went to ntfy.

### Phase 4 — Polyphony UX

Deliver: Polyphony Stage 2 in full (search section, request chips, Requests screen with stepper,
inbox, devices/sign-out, WorkManager poller, drop-download action), backend `drop-download`,
stalled-torrent detection, `partial` availability.

Verify: full flow on the phone without touching a browser or Lidarr's UI: search → request →
watch progress → notification → play; drop a deliberately stalled torrent and see Soularr pick
the album up on its next cycle (slskd shows the transfer, pipeline shows `source: soulseek`).

### Phase 5 — migration off Lidseeker (§7.3)

### Phase 6 — expansion (§9)

---

## 7. Deployment, migration, rollback, monitoring, backups

### 7.1 Compose and Caddy (sketches, adapt to your conventions)

`apps/continuo.yaml` (mirrors the other app files; secrets via sops env file):

```yaml
services:
  continuo:
    image: ghcr.io/isaacault/continuo:0.1.0@sha256:…
    container_name: continuo
    restart: unless-stopped
    user: "${PUID}:${PGID}"
    env_file: [secrets/continuo.env]
    environment:
      TZ: ${TZ}
      NAVIDROME_URL: http://navidrome:4533
      LIDARR_URL: http://gluetun:8686          # or http://localhost:8686 with network_mode: host
      SLSKD_URL: http://gluetun:5030
      DOCKER_PROXY_URL: http://continuo-docker-proxy:2375
      SOULARR_CONFIG_PATH: /soularr/config.ini
      SOULARR_DENYLIST_PATH: /soularr/failed_imports.json
    volumes:
      - ${DOCKER_DIRECTORY}/appdata/continuo:/data
      - ${DOCKER_DIRECTORY}/appdata/soularr:/soularr
    networks: [internal]                        # same network as navidrome and caddy
    mem_limit: 256m
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/v1/health').status==200 else 1)"]
      interval: 1m
      timeout: 5s
      retries: 3
  continuo-docker-proxy:                        # ported from lidseeker: allows only POST /containers/soularr/restart
    image: nginx:alpine
    volumes:
      - ./apps/continuo/docker-proxy/nginx.conf:/etc/nginx/nginx.conf:ro
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks: [internal]
```

Navidrome additions: `ND_REVERSEPROXYUSERHEADER=Remote-User`,
`ND_REVERSEPROXYWHITELIST=<caddy>/32,<continuo>/32` (use the compose network's fixed IPs or a
tight CIDR; never `0.0.0.0/0`).

Caddy:

```
music-api.ault.me {
    @subsonic path /rest/*
    handle @subsonic {
        request_header -Remote-User
        forward_auth continuo:8080 {
            uri /internal/auth/subsonic
            copy_headers Remote-User
        }
        reverse_proxy navidrome:4533
    }
    handle {
        reverse_proxy continuo:8080
    }
    log { format json }     # redact ?token= and t= if you keep access logs
}

music.ault.me {
    request_header -Remote-User
    reverse_proxy navidrome:4533
}
```

### 7.2 Configuration variables

Required: `JWT_SECRET`, `LIDARR_URL`, `LIDARR_API_KEY`, `LIDARR_WEBHOOK_SECRET`,
`ROOT_FOLDER_PATH=/music`, `QUALITY_PROFILE_ID`, `METADATA_PROFILE_ID`, `NAVIDROME_URL`,
`NAVIDROME_SERVICE_USER=continuo-svc`, `DB_PATH=/data/continuo.db`, `ADMIN_USER`/`ADMIN_PASSWORD`
(first boot only). Optional: `SLSKD_URL`, `SLSKD_API_KEY`, `SOULARR_*`, `DOCKER_PROXY_URL`,
`HA_WEBHOOK_URL`, `UPTIME_KUMA_PUSH_URL`, `MUSICBRAINZ_CONTACT`, `STILL_LOOKING_AFTER_CYCLES=6`,
`SEARCH_CYCLE_SECONDS=300`, `STATUS_POLL_SECONDS=45`, `RECONCILE_POLL_SECONDS=60`,
`SCAN_TIMEOUT_SECONDS=600`, `ACCESS_TOKEN_TTL=3600`, `REFRESH_TOKEN_TTL_DAYS=90`,
`TRUST_PROXY=true`.

### 7.3 Migration from Lidseeker and rollback

1. **Before anything**: merge both fix branches into your Lidseeker fork and redeploy it, so the
   system you fall back to is the fixed one. Snapshot `appdata/lidseeker/lidseeker.db`.
2. **Shadow period (Phases 1–3)**: both run. Lidseeker keeps its port/hostname and its UI; the
   backend runs on `music-api.ault.me`. Two reconcile loops asserting the same monitoring is
   idempotent. Two notification senders would duplicate, so Lidseeker's `NTFY_*` stays empty (it
   already must, per the ntfy rule). Lidarr's webhook points only at the backend.
3. **Import**: a one-off CLI `continuo import-lidseeker /path/lidseeker.db` copies `users`
   (bcrypt hashes are compatible) and `requests` (status mapping: `failed` → `searching` +
   `stillLooking`; `error` → `error`; everything else as-is; `foreign_id` → `mb_release_group_id`;
   `lidarr_*` ids as-is; `available` rows get `navidrome_album_id` via matching on first run).
   Idempotent on `(kind, mb_release_group_id)`, so it can be re-run at cut-over.
4. **Cut-over (Phase 5)**: re-run the import; switch the phone to Polyphony's requests UI; stop
   Lidseeker (`make stop APP=lidseeker`) but keep its compose include commented and its data dir.
   Soak two weeks.
5. **Rollback** at any point: `make up APP=lidseeker`, point Lidarr's webhook back to nothing
   (Lidseeker polls), uninstall nothing. Lidseeker's DB was never written by the backend. Requests
   created during the shadow period exist in Lidarr either way, so nothing is lost; Lidseeker will
   simply not list the ones it did not create (re-request them if you care).
6. **Archive**: move `apps/lidseeker/` to `apps/archive/`, keep the DB snapshot in appdata for a
   month, then delete.

### 7.4 Monitoring

- Uptime Kuma: HTTP(s)-JSON monitor on `/v1/health` (`status == ok`, 60 s) and a Push monitor fed
  by the heartbeat worker (so a wedged loop is an outage); both notify via ntfy — the only ntfy
  path.
- Backend logs: JSON lines to stdout; `request_events` is the audit trail per request.
- Diagnostics in Polyphony: add "Request server" to the existing Diagnostics screen (backend
  health, last SSE event, inbox cursor).
- Policy check nightly → HA notification only if something is wrong.

### 7.5 Backups

- Add `appdata/continuo/` to the restic include list. Before the nightly run, a pre-backup hook
  runs `sqlite3 /data/continuo.db ".backup /data/backup/continuo.db"` (or the backend's
  `continuo backup` CLI doing `VACUUM INTO`) so the snapshot is consistent regardless of WAL state.
- `library_albums` and `catalog_cache` are disposable; everything else is small.
- Music files: unchanged paths, unchanged Lidarr root folder, nothing new is excluded. Verify once
  that a freshly requested album appears in the next restic snapshot (`restic ls latest | grep`).

---

## 8. Risks and open decisions (with recommendations)

| # | Decision | Options | Recommendation |
| --- | --- | --- | --- |
| 8.1 | Backend language | Python/FastAPI · Go · Kotlin/Ktor | **Python**: near-verbatim port of 2.4k proven lines, same stack as torrent-cleanup, small ARM64 image. Go would halve memory but means rewriting everything; Kotlin could share models with Polyphony but a JVM on the Jetson costs 200–300 MB and the shared artefact is the API anyway |
| 8.2 | Streaming auth | Subsonic façade + Caddy `forward_auth` + Remote-User · HMAC-signed URLs · proxy audio | **Façade.** Zero client code, no expiry edge cases for Cast/DLNA/downloads, every Subsonic client on every OS works day one. Keep HMAC as fallback. Risk: depends on Navidrome's proxy-auth on `/rest` behaving on your version (official docs say yes; an old issue said no for 0.49) → verified in Phase 2 step 4 before anything depends on it |
| 8.3 | Navidrome user mapping | per-user via Remote-User · one shared service user | **Per-user**; preserves stars/playlists/history and the backend stores no Navidrome passwords |
| 8.4 | Push | HA companion + in-app · UnifiedPush · FCM | **HA + in-app now** (fits the stated policy and existing plumbing); UnifiedPush in Phase 6 if you want Polyphony standalone; no FCM |
| 8.5 | Album identity key | release-group MBID · release MBID · title match | **Release-group** as the canonical key with paths as the webhook-time matcher; release MBID and fuzzy as fallbacks; ambiguous → `needs_attention`, never a silent link |
| 8.6 | Writing MBIDs | Lidarr tagging (new files only) · extend tagscrub | **Lidarr**, scrub off; audit tagscrub to preserve tags |
| 8.7 | Artist requests | port · drop | **Drop** (policy). Keep `artist_albums` for browsing a discography and requesting albums one by one |
| 8.8 | Who re-searches | torrent-cleanup rules 9–11 · backend | **torrent-cleanup stays authoritative** through Phase 5; backend fires searches only on request/retry/manual. Optional `POST /internal/events/search` lets rule 10 report so the app shows "last searched". Consider moving rule 10 into the backend in Phase 6 if you want per-request cadence |
| 8.9 | Terminal failure state | keep `failed` · never | **Never** (constraint). `error` remains only for "could not add to Lidarr", always retryable |
| 8.10 | Web UI | drop · keep Lidseeker's React UI · minimal page | **Drop the React app**, but note the gap: until a Polyphony desktop/web client exists, requesting from a laptop means curl or the phone. A ~300-line server-rendered page (`/ui`) is cheap insurance; decide in Phase 4 |
| 8.11 | Discover feed | port · defer | **Defer** |
| 8.12 | Jellyfin/Plex libraries | abstract the streaming plane · keep Navidrome canonical | **Keep Navidrome canonical** for music (§9.2). Polyphony already speaks Jellyfin natively for a Jellyfin music library; the backend's *matching/search* plane gets a `LibraryProvider` protocol with only a Navidrome implementation for now |
| 8.13 | Multi-platform clients | build five UIs · KMP from Polyphony · existing Subsonic clients | **Existing clients immediately (via façade), KMP track after Phase 4** (§9.1) |
| 8.14 | Memory | — | `mem_limit 256m`; single worker; httpx connection pools bounded; no in-process caches > a few MB |
| 8.15 | Security | — | Caddy strips `Remote-User` on all Navidrome routes; whitelist is two addresses; webhook secret; refresh-token rotation; façade secrets per device; DB file 0600; tailnet-only DNS |
| 8.16 | Version floors | — | Navidrome ≥ 0.54.5 (CVE), ≥ 0.55 for watcher + `mbzReleaseGroupId` **(verify)**; Lidarr v2 (`/artist?mbId=`, webhook payload fields confirmed in `develop`) |
| 8.17 | Lidarr webhook reliability | — | Polling loops stay the source of truth; webhook only accelerates. Lidarr retries failed webhooks but not forever |
| 8.18 | Navidrome scan latency | watcher · scheduled · triggered | watcher first (local disk, inotify works); backend triggers `startScan` when a webhook arrives and the watcher has not produced the album within 60 s |
| 8.19 | Request from a device that is off-tailnet | — | Not supported by design (private names only). Polyphony's existing offline mode covers playback of downloads |

---

## 9. Track B — multi-platform clients and library expansion

This answers your first message. Sequence it **after** Phase 4; the façade already gives you
every platform through existing clients, so nothing here is on the critical path for "seamless".

### 9.1 Clients

**Immediately (after Phase 2), per platform, through the façade and zero new code:**

| Platform | Playback | Requests |
| --- | --- | --- |
| Android | Polyphony | Polyphony (Phase 4) |
| iOS, macOS | Cassette (your fork) or any Subsonic client | phone, or the optional `/ui` page |
| Windows, Linux | Feishin / Supersonic / Sonixd | `/ui` page |
| Web | Navidrome's web UI at `music.ault.me` (or Feishin web) | `/ui` page |

**Then your own client, as one codebase — Kotlin Multiplatform from Polyphony:**

1. `core:domain` → KMP `commonMain`. It is already pure Kotlin; replace `javax.inject` with
   constructor injection or `kotlin-inject`, and use the multiplatform `paging-common` artefact.
2. `core:network` → Ktor client (Retrofit is declared but unused today, so nothing is lost);
   `provider:subsonic` → common (pure HTTP + JSON); new `core:requests` written common from day one.
3. `core:data` → Room KMP + DataStore KMP (both support Android/iOS/JVM).
4. `core:ui` → Compose Multiplatform. Risk: Material 3 Expressive parity lags on CMP; keep
   `PolyphonyTheme` tokens so screens compile with a non-expressive fallback.
5. Playback behind `expect/actual`: Media3 on Android; JVM desktop via VLCJ or GStreamer;
   iOS via AVPlayer. Cast/DLNA stay Android-only at first (the DLNA control point is pure Kotlin
   and could move later).
6. Targets in order: **desktop JVM** (one build for Linux/macOS/Windows; biggest payoff per
   effort), **iOS** (CMP is stable; or keep Cassette and skip), **web** last (Kotlin/Wasm is still
   beta as of Aug 2026; a thin React page against `/v1` + the façade is the pragmatic web client).

Separate repo or Polyphony? Keep it **in Polyphony**: the modules are already split along the
right seams; KMP conversion is a module-by-module refactor, not a rewrite. Polyphony becomes
"the client" for all platforms; `continuo` is "the server".

### 9.2 Jellyfin / Plex libraries

Do not put the backend in front of Jellyfin or Plex streaming. Polyphony already has a Jellyfin
provider; a Jellyfin music library is a second `Server` in the app. For the request/search plane,
`LibraryProvider` (list albums with MBIDs + paths, search) can get a Jellyfin implementation later
(Jellyfin exposes `ProviderIds.MusicBrainzReleaseGroup`). Plex: only if you actually run it for
music; it has no Subsonic API and the least useful MBID exposure.

### 9.3 Fileshares as libraries

Treat a fileshare as *another Navidrome library*, not as something the backend indexes:

- Mount SMB/NFS on the Jetson (autofs/systemd mount), read-only.
- Navidrome multi-library (0.56+ **(verify)**): add it as a library; per-user library access
  controls who sees it. inotify does not work over NFS/SMB, so give it a scan schedule.
- Optional: a tagscrub pass into a staging copy if tags are poor; never write to the share.
- Requests fulfilled from a share (Phase 6+): a second `FulfillmentSource` next to Lidarr —
  "copy album from share X into the library" — using the same matching code to confirm the result.

---

## Appendix A — repo layout for `continuo`

```
continuo/
├── continuo/
│   ├── api/            routers: auth, devices, search, catalog, requests, notifications, events, admin, internal
│   ├── auth/           passwords, jwt, devices, facade (Subsonic token check), deps
│   ├── upstream/       lidarr.py, navidrome.py (Subsonic + native, Remote-User), slskd.py, soularr.py, musicbrainz.py, homeassistant.py
│   ├── requests/       orchestrate.py (request_album), pipeline.py, matching.py, normalise.py
│   ├── search/         merge.py (unified search), cache.py
│   ├── notify/         inbox.py, dispatch.py (SSE + HA)
│   ├── workers/        status.py, reconcile.py, scan.py, index.py, heartbeat.py
│   ├── db/             conn.py, migrate.py, repositories per table
│   ├── events.py       SSE hub
│   ├── config.py
│   └── cli.py          import-lidseeker, backup, policy-check, create-user
├── migrations/0001_init.sql …
├── tests/              ported + new (matching fixtures from real Navidrome/Lidarr JSON)
├── deploy/             compose snippet, Caddy snippet, docker-proxy nginx.conf, HA automation yaml, Uptime Kuma notes
├── docs/               this plan, API.md (generated OpenAPI), RUNBOOK.md
├── LICENSES/lidseeker-MIT.txt
├── Dockerfile, pyproject.toml, ruff.toml, .github/workflows/ci.yml (ruff, pytest, buildx arm64+amd64 → ghcr)
└── README.md
```

## Appendix B — settings checklist in the existing services

- Lidarr: Connect → Webhook → URL, secret header, events (Grab, Release Import, Download
  Failure, Import Failure, Health); Metadata → tag new files only, scrub off; quality profile
  used for requests → Upgrades Allowed off; confirm `/artist?mbId=` works on your version.
- Navidrome: `ND_REVERSEPROXYUSERHEADER`, `ND_REVERSEPROXYWHITELIST`; create `continuo-svc`
  admin user; confirm `startScan` requires admin on your version; scanner watcher enabled.
- Caddy: new site; `request_header -Remote-User` on both sites; log redaction.
- Home Assistant: webhook trigger automation → `notify.mobile_app_<phone>` with `clickAction`,
  `channel: Music requests`, `tag: request-<id>` (replaces rather than stacks).
- Uptime Kuma: two monitors, ntfy notification.
- restic: include `appdata/continuo`, pre-backup SQLite snapshot hook.
- Soularr: unchanged; confirm `delete_searches=False` still (the slskd cleanup assumes it).
- torrent-cleanup: unchanged; optionally POST to `/internal/events/search` from rule 10.

## Appendix C — glossary of identifiers

| Name | Meaning |
| --- | --- |
| `foreignAlbumId` (Lidarr) / `album.MBId` (webhook) | MusicBrainz release-group ID — the canonical album key |
| `foreignReleaseId` (Lidarr `releases[]`) | MusicBrainz release ID of one pressing; the monitored one is what gets tagged |
| `musicBrainzId` on Subsonic `AlbumID3` | Navidrome's `mbzAlbumId` = release ID |
| `mbzReleaseGroupId` (Navidrome native API) | release-group ID, present when the tag exists |
| `musicBrainzId` on Subsonic `Child` | track/recording ID |
| `navidromeAlbumId` | Navidrome's internal MD5 id, what Polyphony's `Album.id` is for a Subsonic server |
| `polyphony://album/{serverId}/{albumId}` | the deep link the notification carries |
