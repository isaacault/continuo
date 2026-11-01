# Installing continuo

continuo is one API in front of your media servers: search, requests that keep looking until they
land, and an inbox that says when they have. It has two parts, and you can run either or both:

| Part | Servers | Apps |
|---|---|---|
| `music` | Navidrome (library and streaming), Lidarr (albums), slskd (Soulseek) | Polyphony |
| `video` | Jellyfin (library and streaming), Radarr (films), Sonarr (series), Prowlarr, qBittorrent | Polyorama |

A part that's off loads none of its code and starts none of its servers.

## 1. Start it

On a Linux machine with Docker:

```sh
git clone https://github.com/isaacault/continuo && cd continuo/deploy/standalone
cp env.example .env           # then fill it in as you go (below)
docker compose --profile music --profile video up -d     # or just one --profile
```

`CONTINUO_MODULES` in `.env` must name the same parts as the profiles. Your files live in
`./data` (`music`, `movies`, `tv`, `downloads`) and each app's settings in `./appdata`.

Set `JWT_SECRET` (`openssl rand -hex 32`), `ADMIN_PASSWORD`, and `PUBLIC_BASE_URL`: the
address your apps will use for continuo. Its default is the author's own server.

## 2. Set up the music servers

1. **Navidrome** (http://<host>:4533): create your admin, then a user `continuo-svc`. Put its
   password in `NAVIDROME_SERVICE_PASSWORD`.
2. **Lidarr** (http://<host>:8686):
   - Settings, Media Management: add the root folder `/data/music`.
   - Settings, General: copy the API key to `LIDARR_API_KEY`.
   - Settings, Connect: add a **Webhook** for On Release Import, On Grab and the failure events.
     - URL: `http://continuo:8080/internal/webhooks/lidarr`.
     - Header: `X-Webhook-Secret` with your `LIDARR_WEBHOOK_SECRET`.
3. **slskd** (http://<host>:5030): sign in to Soulseek, then create an API key for
   `SLSKD_API_KEY`.

## 3. Set up the video servers

1. **Jellyfin** (http://<host>:8096):
   - Add libraries for `/data/movies` and `/data/tv`.
   - Dashboard, API Keys: create one for `JELLYFIN_API_KEY`.
   - Set `VIDEO_PUBLIC_BASE_URL` to the address your apps will use for Jellyfin.
2. **Prowlarr** (http://<host>:9696): add your indexers, then connect it to Radarr and Sonarr
   (Settings, Apps).
3. **qBittorrent** (http://<host>:8085): the first password is in its log
   (`docker compose logs qbittorrent`). Add it as the download client in Radarr and Sonarr, with
   the download path `/data/downloads`.
4. **Radarr** (http://<host>:7878) and **Sonarr** (:8989):
   - Root folders `/data/movies` and `/data/tv`.
   - API keys into `RADARR_API_KEY` and `SONARR_API_KEY`.
   - Set the quality profile IDs in `.env` if not 1.
5. **TMDB**: a free API key from themoviedb.org (Settings, API) for `TMDB_API_KEY`.
   `TMDB_LANGUAGE` and `TMDB_REGION` choose titles' language and what's trending (default UK
   English).

qBittorrent has no VPN here. Put it behind one (gluetun, for example) if you need to.

## 4. Restart continuo, check, add people

```sh
docker compose up -d continuo                            # picks up .env
curl -s http://localhost:8080/v1/health                  # each server you use should say "ok"
docker compose exec continuo continuo create-user alice  # asks for a password
```

- `--role admin` makes an admin.
- For music, a continuo user signs in to Navidrome as the Navidrome user of the same name;
  `--navidrome-username` picks another.
- For video, the first time someone opens Polyorama continuo makes them a Jellyfin user. A
  Jellyfin user who already exists is never reset: an admin links it once with its password
  (`POST /v1/admin/video/accounts` with `userId`, `jellyfinUsername`, `jellyfinPassword`).

## 5. Before going online

The ports above are for setting up. Before anything is reachable from outside, put a reverse
proxy with TLS in front of continuo, Jellyfin and Navidrome. Never expose `/internal/*` (only
Lidarr's webhook uses it, on the Docker network); see `deploy/caddy/Caddyfile.snippet`.

If the Polyorama web app is on another host, add that host to `CORS_ALLOW_ORIGINS`.
