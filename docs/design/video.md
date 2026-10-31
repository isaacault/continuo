# Video in continuo: design

Status: proposal, for review. Nothing here is built yet.

continuo gives music one place for everything: what's in the library and what isn't, played or
requested from the same search, with one account, one inbox and one request list. This document
designs the same for film and TV, as a second, optional half of continuo, so that someone can run
music only, video only, or both, and pay on their server only for what they run.

## 1. Goals, in the user's order

**Clients:** Samsung TV (Tizen), web browser, Android, iOS (iPhone and iPad), Mac, Windows.

**Running:** efficiency on the server first (the homelab is one Jetson).

**Portability:**
- easy for others to set up;
- what someone doesn't use isn't installed, started or loaded;
- running both halves shares as much as possible: one process, one database, one login.

**Non-goals for now:**
- streaming a title while it downloads (music's Play now); see §9;
- live TV and DVR;
- Jellyfin's plugins and SyncPlay;
- multiple video libraries per account.

## 2. Decisions

1. **One continuo, made of parts.**
   - `core` is always on.
   - `music` and `video` are switched on by configuration (`CONTINUO_MODULES=music,video`).
   - A part that's off registers no routes, starts no background loops, opens no upstream clients and imports none of its code.
2. **Jellyfin stays the video server; Navidrome stays the music server.** Each is best at its job.
   - Jellyfin handles scanning, metadata, transcoding, HLS, subtitles, trickplay thumbnails, watch state and resume points.
   - continuo doesn't redo any of that. It sits in front, as it does for Navidrome.
3. **continuo is never in the video data path.** Clients stream from Jellyfin directly, through Caddy, with a per-device Jellyfin token that continuo issued. continuo answers questions and authorises; it never proxies bytes.
4. **Sonarr and Radarr are the wanted list; continuo never talks to Prowlarr or qBittorrent.** It asks Sonarr and Radarr, as it asks Lidarr. Bazarr fetches subtitles on its own.
5. **TMDB is the catalogue,** as MusicBrainz is for music. TMDB IDs are the key across Jellyfin, Sonarr and Radarr (§5).
6. **Direct play by design.** The Jetson can't use its hardware encoder from Jellyfin's ffmpeg, so live transcoding is a CPU last resort. Quality profiles, subtitle choices and client device profiles are all set up to avoid it (§8).
7. **Three client codebases for six targets:**
   - web (TypeScript) for Tizen, browsers and Windows;
   - Android (Kotlin);
   - Apple (SwiftUI) for iPhone, iPad and Mac.
   They live in their own repository and talk to continuo through SDKs that continuo publishes (§10).

## 3. The parts

```
continuo/
  core/      accounts, devices, sessions and refresh tokens, the request lifecycle (states, events,
             retries, inbox and notifications), events (SSE), settings, admin, health, rate limits,
             the module registry
  music/     MusicBrainz, Lidarr, Soulseek/slskd, Soularr, Navidrome (index, façade, forward-auth),
             Play now, ListenBrainz/Last.fm scrobbling, lyrics, singles, completeness
  video/     TMDB, Radarr, Sonarr, Jellyfin (index, users and tokens, forward-auth), title pages,
             video requests
```

**Module registry.** Each module exposes one object:
- its routers;
- its background loops (name, interval, function);
- its migrations;
- what it adds to the sign-in answer;
- its search source (§6).

At start-up, `core` reads `CONTINUO_MODULES`, imports only the modules listed, and mounts what they give. Imports stay inside the module (as `continuo/api/library.py` already does locally), so a disabled module's Python is never loaded.

**One process, shared work.** With both modules on:
- one uvicorn process, one SQLite database and one HTTP connection pool (`upstream/http.py`);
- one SSE stream per device;
- one heartbeat and one nightly job; each module's nightly work is a step of the same job.

Memory is the base process plus what each module caches.

**Migrations.** Each module has its own numbered folder (`migrations/core`, `migrations/music`, `migrations/video`), recorded in `schema_migrations` with the module name. A disabled module's tables simply don't exist. The existing migrations become `core` and `music`, by a one-time renumbering migration that changes no schema (§11, step 1).

## 4. Upstreams

| Service | continuo's use | Notes |
|---|---|---|
| Jellyfin | the library: what's there (movies, series, seasons, episodes with ProviderIds), users and per-device tokens, playback info for clients, watch state (pass-through) | `/Items` with `StartIndex`/`Limit`, checked to really paginate (Lidarr's `/album` didn't, which crash-looped continuo: #84); one admin API key in secrets |
| Radarr | movies: lookup by TMDB ID, add (monitored, search), queue and history for request states | the Lidarr client's patterns: never follow an artist or series for everything by accident |
| Sonarr | series: lookup (TMDB ID → TVDB ID through TMDB's `external_ids`), add with `monitor` set to what was asked (one season, future episodes, a pilot), queue, history | Sonarr v4 keys on TVDB |
| TMDB | search, title and season details, trending/popular for Explore, posters and backdrops (fetched through continuo's artwork proxy and cached, as music artwork is) | free API key; rate limit about 50/s; cached on disk like `musicbrainz_cache` |
| Bazarr, Prowlarr, qBittorrent | none | behind Sonarr and Radarr |

## 5. Identity and matching

- **The key is the TMDB ID:**
  - a movie: `tmdb:movie:603`;
  - a series: `tmdb:tv:1396`, with seasons `…:s1` and episodes `…:s1e3`.
- Jellyfin items carry `ProviderIds` (`Tmdb`, `Tvdb`, `Imdb`). Radarr keys movies on TMDB, and Sonarr gets TVDB IDs through TMDB.
- **The video index** caches Jellyfin's library, refreshed incrementally like the music index (by `DateLastSaved`): movies, and series with per-season episode counts. Matching is by provider ID.
- Items without one (home videos, oddly named files) are library-only. They're shown, but can't be requested or completed.
- **Availability states,** the music states carried over:
  - `in_library`;
  - `partial`: a series with missing episodes per season, counted against TMDB's aired episodes and ignoring specials, the video version of the standard-edition rule (#87);
  - `requested` (with progress);
  - `requestable`;
  - `catalog` (not in Radarr or Sonarr's reach, e.g. unreleased).

## 6. Search

`GET /v1/search?q=` stays one endpoint. Each enabled module contributes a source, and `core` merges and ranks them:
- music's search as today;
- `video`: Jellyfin's library search plus TMDB's multi-search, matched by TMDB ID.

The answer gains `movie`, `series` and `person` result kinds. A video-only server returns only those; a music client asking for `types=album,artist,track` never pays for video.

## 7. API (`/v1/video/…`)

**Titles**
- `GET /v1/video/movies/{tmdb}` returns the details, availability, the Jellyfin item ID when it's in the library, resume position and played state (from Jellyfin), and trailers.
- `GET /v1/video/series/{tmdb}` returns the details and seasons, each with episode counts, have/expected and availability.
- `GET /v1/video/series/{tmdb}/seasons/{n}` returns the episodes, each with availability, Jellyfin ID and watch state.

**Home and Explore**
- `GET /v1/video/home`:
  - Continue Watching (Jellyfin `Resume`) and Next Up (Jellyfin `NextUp`);
  - Recently Added;
  - requests on their way.
  Built per account and cached for a minute.
- `GET /v1/video/discover` returns trending, popular and "because you watched", from TMDB, marked with availability and built ahead like music's `/v1/discover`.

**Requests** (the existing `/v1/requests` lifecycle, generalised in §11)
- `POST /v1/requests` with `{"kind": "movie", "tmdbId": 603}`, `{"kind": "series", "tmdbId": 1396, "seasons": [1], "future": true}`, or `{"kind": "episodes", …}`.
- The same states and inbox notifications, plus `video.available` with a deep link to the title.

**Sign-in**
- The login and refresh answers gain `video: {baseUrl, userId, accessToken, deviceId}` alongside `library` (Subsonic), only when the module is on (§7.1).

### 7.1 Jellyfin users and tokens

- Each continuo account is linked to one Jellyfin user. continuo creates it through the admin API on first sign-in, or links an existing one by name (`video_accounts` table).
- Each continuo device gets its own Jellyfin token:
  - continuo signs in to Jellyfin as that user, with a device ID equal to continuo's device ID;
  - it returns the token in the sign-in answer;
  - it revokes it (`/Sessions/Logout`) when the device is signed out, in `core`'s device sign-out hook, which both modules use.
- Clients then talk to Jellyfin directly for playback info, streams, images, subtitles, trickplay and progress reports. Jellyfin's own resume and Next Up stay accurate, and continuo stores no watch state.
- From outside, Jellyfin is reached through Caddy at the public video host, with the same `forward_auth` pattern as Navidrome so that only continuo-issued tokens get through. Jellyfin's own login page isn't exposed publicly.

## 8. Playback and server efficiency

- **Direct play first.** Clients send a device profile (codecs, containers, maximum bitrate) to Jellyfin's `PlaybackInfo`. They're built to accept H.264, HEVC (where the TV and device can), AAC, AC-3 and E-AC-3, in MP4 or MKV, so that most library files play untouched.
- **Remux next.** A container change costs almost no CPU.
- **Transcode last, and on the CPU.** It's a policy setting: allow, cap at one at a time, or refuse and suggest a download.
- **Quality profiles** in Sonarr and Radarr prefer releases that direct-play on the household's devices. A shipped recommendation lives in the docs.
- **Subtitles** from Bazarr are text (SRT), which clients render. Image subtitles (PGS) force a burn-in transcode on most clients, so the profiles avoid them.
- **Optional overnight conversion** of the few files that can't direct-play. That's a separate tool (Tdarr or a small worker), not continuo.
- **The TV's Wi-Fi:** the household's Samsung TV has a weak link (431 ms ping, homelab notes 2026-10-25). The TV client exposes a quality cap and shows the measured throughput.

**Downloads (offline).** Jellyfin downloads the original file (`/Items/{id}/Download`), or a transcode made on demand, which counts as a transcode. Download rules, as in Polyphony:
- keep the next N unwatched episodes of a series;
- keep this movie;
- a storage limit, removing watched items first.
They're evaluated on the device, using continuo's title pages for what's next.

## 9. Not "Play now" (yet)

Music's Play now streams a song while Soulseek fetches it. A film from a torrent can't do that reliably: playing while downloading needs pieces in order, healthy seeding and a container that plays incomplete. So video requests are request → notified when ready (usually minutes for popular titles), with the progress shown live.

"Watch while downloading" can be tried later as an experiment, behind a setting: qBittorrent's sequential mode plus Jellyfin playing a growing file. It isn't in the first version.

## 10. Clients and SDKs

**SDKs live in this repository,** generated from the OpenAPI document and checked by contract tests:
- `sdk/typescript`;
- `sdk/kotlin`;
- `sdk/swift`.

They cover sign-in and refresh (with the race-safe rotation Polyphony learned), search, title pages, requests, the inbox and events.

**A new repository** for the video apps (the name is the user's choice):

```
web/       TypeScript: one app, a TV layout (D-pad, 10-foot) and a desktop layout; Tizen (.wgt,
           AVPlay), browsers, Windows (installable PWA; Tauri if media keys or a tray icon are needed)
android/   Kotlin, Media3: phone and tablet first; Android TV later
apple/     SwiftUI: iPhone, iPad, Mac from one app (AVPlayer, AirPlay, downloads)
contract/  continuo's OpenAPI and shared rule files (resume, next-up, availability), as Polyphony does
```

The web app is built first: it covers three of the six targets.

**Tizen specifics:**
- developer-mode installs (`tizen`/`sdb`);
- AVPlay for HLS and MP4;
- spatial navigation for the remote;
- tested on the household's TV model;
- also packaged for LG webOS, since it's the same web app.

## 11. Getting there without disturbing music

Each step is a PR, with music's tests green throughout.

1. **Module registry, music only.**
   - `core` and the registry, `CONTINUO_MODULES` defaulting to `music`, migrations recorded per module (renumbered, no schema change).
   - No behaviour changes; music's routes and loops register through the registry.
2. **Move music code under `continuo/music/`.** Pure moves; imports updated. Each folder in `upstream/`, `library/`, `search/`, `requests/` and `fetch/` is assigned to `core` or `music`.
3. **Generalise requests.**
   - Add `domain` (`music` or `video`) and an `ids` JSON column.
   - Music's `mb_release_group_id` and Lidarr columns stay for existing rows, nullable for video.
   - The state machine and inbox are in `core`; each domain's pipeline (Lidarr and Soulseek, Radarr and Sonarr) is a module.
4. **`video` module, movies.**
   - Jellyfin link, users and tokens, the video index, TMDB search, movie pages, movie requests through Radarr.
   - Sign-in answer gains `video`.
   - Caddy `forward_auth` for Jellyfin on the homelab, as its own homelab change with the user's go-ahead.
5. **Series:** Sonarr, seasons and episodes, partial seasons, Next Up and Continue Watching.
6. **Web client MVP** (TV layout first): sign in, home, search, title pages, play (direct play), request, inbox.
7. **Compose profiles and docs for others:**
   - `--profile music`: continuo, Navidrome, Lidarr, slskd.
   - `--profile video`: continuo, Jellyfin, Radarr, Sonarr, Prowlarr, qBittorrent, Bazarr.
   - Both together, with one continuo.
8. **Android, then Apple** clients on the SDKs.

**Measured at each step:**
- idle memory and CPU of continuo with `music`, with `video` and with both;
- request latency for search and title pages.
The budget is continuo under 200 MB resident with both modules on the homelab, inside its 256 MiB limit.

## 12. Open questions

1. A public video host name (`watch.ault.me`?), and whether Jellyfin's own clients (Findroid, Swiftfin, the TV app) should keep working directly while the new clients are built. That's easy: they'd just need a Jellyfin login.
2. Which TMDB region and language for titles and trending (en-GB?).
3. Who may request video: everyone, or per-account quotas like Jellyseerr's?
4. Should series requests default to "future episodes too" or "just what's asked"?
5. The video apps' repository name.
