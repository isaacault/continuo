# continuo

The server beneath [Polyphony](https://github.com/isaacault/polyphony): one private API that
fronts Navidrome (library), Lidarr + Prowlarr + qBittorrent and slskd + Soularr (requests), so a
music client can search once, play what is in the library, request what is not, and get a
tap-to-play notification when it lands.

*Basso continuo* is the continuous bass line that underpins polyphonic music; this is the
always-running service that underpins the client.

## Status

Backend for plan Phases 0–3 is implemented and tested against mocked upstreams; not yet
deployed. Design: **[PLAN.md](PLAN.md)**. Deploying and verifying each phase on the Jetson:
**[docs/RUNBOOK.md](docs/RUNBOOK.md)**.

## Shape

- **Library plane** — a Subsonic-compatible façade at `music-api.ault.me/rest/*`: Caddy
  `forward_auth` asks continuo (`/internal/auth/subsonic`) to validate a per-device credential,
  then proxies straight to Navidrome with `Remote-User` (Navidrome ExtAuth). Audio never passes
  through continuo; devices never hold Navidrome credentials; any Subsonic client works.
- **Request plane** — `/v1/*` JSON: auth and devices, unified search (library + Lidarr
  catalogue + MusicBrainz), album requests with a live pipeline that never gives up,
  notification inbox and SSE, Lidarr webhook, matching of imported albums to Navidrome albums,
  Home Assistant push with a deep link.
- Python 3.12 + FastAPI + httpx + SQLite, one ~45 MB-RSS container on the homelab Jetson.

## Layout

```
continuo/api/        routers: auth/devices, search/catalog, requests, notifications+SSE, admin, internal, health
continuo/auth/       passwords, access+refresh tokens, façade check, FastAPI deps
continuo/upstream/   lidarr, navidrome (Subsonic + native), slskd, soularr, musicbrainz, homeassistant
continuo/requests/   orchestrate (request_album), pipeline, matching, normalise, view
continuo/search/     unified search merge, TTL cache
continuo/workers/    status, reconcile, scan watcher, library index, heartbeat/nightly
continuo/db/         sqlite helpers, migrations runner, one module per table group
continuo/migrations/ numbered SQL migrations
deploy/              compose, Caddy, docker-proxy, Home Assistant automation, env template
```

## Develop

```bash
pip install -e '.[dev]'
ruff check . && pytest -q
docker build -t ghcr.io/isaacault/continuo:edge .
```

Portions are derived from [lidseeker](https://github.com/IvoryCobra-VC/lidseeker) (MIT, see
[LICENSES/lidseeker-MIT.txt](LICENSES/lidseeker-MIT.txt)).
