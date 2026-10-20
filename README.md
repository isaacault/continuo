# continuo

The server beneath [Polyphony](https://github.com/isaacault/polyphony): one private API that
fronts Navidrome (library), Lidarr + Prowlarr + qBittorrent and slskd + Soularr (requests), so a
music client can search once, play what is in the library, request what is not, and get a
tap-to-play notification when it lands.

*Basso continuo* is the continuous bass line that underpins polyphonic music; this is the
always-running service that underpins the client.

## Status

Planning. Nothing runs yet. The design, API sketch, porting plan from
[lidseeker](https://github.com/isaacault/lidseeker), phased roadmap and migration plan are in
**[PLAN.md](PLAN.md)**.

## Shape (from the plan)

- **Library plane** — a Subsonic-compatible façade at `music-api.ault.me/rest/*`: Caddy
  `forward_auth` asks continuo to validate a per-device token, then proxies straight to Navidrome
  with `Remote-User`. Audio never passes through continuo; devices never hold Navidrome
  credentials; any Subsonic client on any OS works unchanged.
- **Request plane** — `/v1/*` JSON: auth and devices, unified search (library + Lidarr
  catalogue), album requests with a live pipeline that never gives up, notification inbox and
  SSE, Lidarr webhook, matching of imported albums to Navidrome albums.
- Python 3.12 + FastAPI + httpx + SQLite, one small ARM64 container on the homelab Jetson.

## Where to start

Phase 0 in [PLAN.md §6](PLAN.md#6-phased-roadmap-with-verification-against-the-real-services):
repo skeleton, `/v1/health`, compose file, Caddy site, Uptime Kuma monitor. Merge the two
lidseeker fix branches first so the system you fall back to during migration is the fixed one.
