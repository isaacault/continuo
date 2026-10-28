"""Environment-driven settings.

Everything is read once into `settings` at import; tests (and the CLI) call `reload()` after
changing the environment. Code reads `config.settings.X` at call time, never copies values at
import, so a reload takes effect everywhere.
"""
import logging
import os
import secrets
from dataclasses import dataclass

log = logging.getLogger("continuo")


def _bool(env: dict, name: str, default: bool) -> bool:
    val = env.get(name)
    if val is None or val == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _int(env: dict, name: str, default: int) -> int:
    val = env.get(name)
    return int(val) if val not in (None, "") else default


def _str(env: dict, name: str, default: str = "") -> str:
    return env.get(name, default) or default


def _url(env: dict, name: str, default: str = "") -> str:
    return _str(env, name, default).rstrip("/")


@dataclass(frozen=True)
class Settings:
    # --- storage ---
    db_path: str = "/data/continuo.db"

    # --- public face ---
    public_base_url: str = "https://music-api.ault.me"
    # The deep link a "ready to play" notification opens. {server} is the host of
    # public_base_url (how Polyphony recognises the Continuo-linked server), {albumId} the
    # Navidrome album id.
    deep_link_template: str = "polyphony://album/{server}/{albumId}"
    trust_proxy: bool = True

    # --- auth ---
    jwt_secret: str = ""
    access_token_ttl: int = 3600
    refresh_token_ttl_days: int = 90
    admin_user: str = ""
    admin_password: str = ""
    login_rate_limit_attempts: int = 10
    login_rate_limit_window_seconds: int = 300

    # --- Lidarr ---
    lidarr_url: str = "http://localhost:8686"
    lidarr_api_key: str = ""
    lidarr_webhook_secret: str = ""
    root_folder_path: str = "/music"
    quality_profile_id: int = 1
    metadata_profile_id: int = 1
    trigger_album_search: bool = True

    # --- Navidrome ---
    navidrome_url: str = "http://localhost:4533"
    navidrome_music_root: str = "/music"
    navidrome_service_user: str = "continuo-svc"
    # "header": Navidrome trusts continuo's address (ND_EXTAUTH_TRUSTEDSOURCES) and calls carry
    # Remote-User. "password": until that is enabled, calls authenticate as the service user
    # with NAVIDROME_SERVICE_PASSWORD (a temporary Phase-1 crutch).
    navidrome_auth: str = "password"
    navidrome_service_password: str = ""
    navidrome_user_header: str = "Remote-User"

    # --- slskd / Soularr ---
    slskd_url: str = "http://localhost:5030"
    slskd_api_key: str = ""
    soularr_config_path: str = "/soularr/config.ini"
    soularr_denylist_path: str = "/soularr/failed_imports.json"
    soularr_container: str = "soularr"
    docker_proxy_url: str = "http://127.0.0.1:2375"
    soularr_enabled: bool = False

    # --- media cache (issue #23): removed albums now, streamed music later ---
    # The music folder as continuo sees it, mounted read-write. Empty = removal disabled. The cache
    # is <music_dir>/.continuo-cache: same filesystem and mount, so moves are instant renames.
    music_dir: str = ""
    # Per-user libraries (#27): each account's library is <libraries_dir>/<Navidrome username>/,
    # hardlinks into the store (MUSIC_DIR) on the same filesystem and mount. Empty = off.
    # Single-song requests (#24): how often the track loop runs, and how long one Soulseek
    # copy may take before the next one is tried.
    track_poll_seconds: int = 30
    # Where single songs are filed, relative to MUSIC_DIR: not an artist folder, so Lidarr
    # never sees a partial album.
    singles_folder: str = "_singles"
    track_download_give_up_seconds: int = 2700
    libraries_dir: str = ""
    library_sync_seconds: int = 300
    # Accounts (continuo usernames) that get albums reaching the store outside a request (Lidarr's
    # own UI, a manual copy), which would otherwise be in nobody's library.
    library_unclaimed_to: tuple[str, ...] = ()
    # The same folder as Navidrome sees it, and the id of Navidrome's library for the store.
    navidrome_libraries_root: str = "/libraries"
    store_library_id: int = 1
    # Artists named in a non-Latin script get MusicBrainz's primary alias in this language as
    # their library name (青葉市子 -> Ichiko Aoba); "original" keeps names as they are.
    name_locale: str = "en"
    # Lidarr's downloads folder as Lidarr sees it: albums imported from here were downloaded
    # (and may be retagged); anything else is the user's own and is never touched.
    lidarr_downloads_path: str = "/data/downloads"
    # Who may remove and restore albums: "admin" (the default, while the library is one shared set
    # of files) or "user" (every signed-in account). Per-user libraries (#27) make it everyone's.
    library_removal_role: str = "admin"
    media_cache_max_gb: int = 25
    removed_min_days: int = 7      # undo window: never evicted sooner
    removed_max_days: int = 30     # dropped after this whatever the cache size

    # --- MusicBrainz ---
    musicbrainz_contact: str = "https://github.com/isaacault/continuo"
    # ListenBrainz user token: only its popularity data (an artist's top songs) needs one; the
    # rest of the artist page works without. Empty = no top songs.
    listenbrainz_token: str = ""
    # Last.fm, for scrobbling Play now songs (the same key Navidrome uses for library songs).
    lastfm_api_key: str = ""
    lastfm_secret: str = ""
    # Search ranked by Deezer's public search (search.ranked); off: MusicBrainz's search alone.
    search_deezer: bool = True

    # --- notifications ---
    ha_webhook_url: str = ""
    uptime_kuma_push_url: str = ""

    # --- loops / policy ---
    status_poll_seconds: int = 45
    reconcile_poll_seconds: int = 60
    scan_poll_seconds: int = 30
    scan_timeout_seconds: int = 600
    # How long after Lidarr reports the files to wait for Navidrome's own watcher before
    # continuo asks for a scan.
    scan_watcher_grace_seconds: int = 60
    search_cycle_seconds: int = 300
    still_looking_after_cycles: int = 6
    stalled_download_hours: int = 6
    # Racing slow downloads (#31): after RACE_GRACE_MIN minutes downloading, a download that
    # won't finish within RACE_SLOW_MIN minutes at its speed so far, or got nothing for
    # RACE_IDLE_MIN minutes, gets Soulseek racing it; the first imported wins.
    race_enabled: bool = True
    race_grace_min: int = 10
    race_slow_min: int = 60
    race_idle_min: int = 10
    # slskd's downloads folder as continuo sees it (to clear a losing racer's files; "" = leave
    # them) and as Lidarr sees it (where Lidarr imports a winning racer from).
    slskd_downloads_dir: str = ""
    # Where slskd writes a file while it downloads (Play now plays from it); default: the
    # downloads folder's sibling "<name>-incomplete", as the homelab mounts them.
    slskd_incomplete_dir: str = ""
    lidarr_soulseek_path: str = "/data/downloads/soulseek"
    heartbeat_seconds: int = 60
    loop_stale_seconds: int = 300
    library_index_hour: int = 4

    # --- catalogue artwork proxy (/v1/artwork) ---
    # Disk cache directory; empty = "artwork" next to the database.
    artwork_cache_dir: str = ""
    artwork_cache_mb: int = 200
    artwork_max_bytes: int = 10 * 1024 * 1024

    # --- server ---
    port: int = 8080
    log_json: bool = True

    @property
    def musicbrainz_user_agent(self) -> str:
        from . import __version__
        return f"continuo/{__version__} ( {self.musicbrainz_contact} )"

    @property
    def public_host(self) -> str:
        from urllib.parse import urlparse
        return urlparse(self.public_base_url).hostname or self.public_base_url


def _resolve_jwt_secret(env: dict, db_path: str) -> str:
    """Use JWT_SECRET when set; otherwise generate one once and persist it next to the database
    (0600). A baked-in default would let anyone forge tokens; a per-boot one would sign everyone
    out on every restart. Ported from lidseeker."""
    configured = env.get("JWT_SECRET", "")
    if configured and configured != "change-me":
        return configured
    data_dir = os.path.dirname(db_path) or "."
    path = os.path.join(data_dir, ".jwt_secret")
    try:
        with open(path) as f:
            existing = f.read().strip()
        if existing:
            return existing
    except OSError:
        pass
    secret = secrets.token_hex(32)
    try:
        os.makedirs(data_dir, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secret)
        log.info("JWT_SECRET not set — generated one at %s", path)
    except OSError as e:
        log.warning("couldn't persist a generated JWT secret (%s); tokens won't survive a restart", e)
    return secret


def load(env: dict | None = None) -> Settings:
    e = dict(os.environ if env is None else env)
    db_path = _str(e, "DB_PATH", "/data/continuo.db")
    slskd_api_key = _str(e, "SLSKD_API_KEY")
    soularr_config_path = _str(e, "SOULARR_CONFIG_PATH", "/soularr/config.ini")
    return Settings(
        db_path=db_path,
        public_base_url=_url(e, "PUBLIC_BASE_URL", "https://music-api.ault.me"),
        deep_link_template=_str(e, "DEEP_LINK_TEMPLATE", "polyphony://album/{server}/{albumId}"),
        trust_proxy=_bool(e, "TRUST_PROXY", True),
        jwt_secret=_resolve_jwt_secret(e, db_path),
        access_token_ttl=_int(e, "ACCESS_TOKEN_TTL", 3600),
        refresh_token_ttl_days=_int(e, "REFRESH_TOKEN_TTL_DAYS", 90),
        admin_user=_str(e, "ADMIN_USER"),
        admin_password=_str(e, "ADMIN_PASSWORD"),
        login_rate_limit_attempts=_int(e, "LOGIN_RATE_LIMIT_ATTEMPTS", 10),
        login_rate_limit_window_seconds=_int(e, "LOGIN_RATE_LIMIT_WINDOW_SECONDS", 300),
        lidarr_url=_url(e, "LIDARR_URL", "http://localhost:8686"),
        lidarr_api_key=_str(e, "LIDARR_API_KEY"),
        lidarr_webhook_secret=_str(e, "LIDARR_WEBHOOK_SECRET"),
        root_folder_path=_url(e, "ROOT_FOLDER_PATH", "/music"),
        quality_profile_id=_int(e, "QUALITY_PROFILE_ID", 1),
        metadata_profile_id=_int(e, "METADATA_PROFILE_ID", 1),
        trigger_album_search=_bool(e, "TRIGGER_ALBUM_SEARCH", True),
        navidrome_url=_url(e, "NAVIDROME_URL", "http://localhost:4533"),
        navidrome_music_root=_url(e, "NAVIDROME_MUSIC_ROOT", "/music"),
        navidrome_service_user=_str(e, "NAVIDROME_SERVICE_USER", "continuo-svc"),
        navidrome_auth=_str(e, "NAVIDROME_AUTH", "password").lower(),
        navidrome_service_password=_str(e, "NAVIDROME_SERVICE_PASSWORD"),
        navidrome_user_header=_str(e, "NAVIDROME_USER_HEADER", "Remote-User"),
        slskd_url=_url(e, "SLSKD_URL", "http://localhost:5030"),
        slskd_api_key=slskd_api_key,
        soularr_config_path=soularr_config_path,
        soularr_denylist_path=_str(e, "SOULARR_DENYLIST_PATH", "/soularr/failed_imports.json"),
        soularr_container=_str(e, "SOULARR_CONTAINER", "soularr"),
        docker_proxy_url=_url(e, "DOCKER_PROXY_URL", "http://127.0.0.1:2375"),
        # Auto-enabled (as in lidseeker) when slskd is keyed and Soularr's config is mounted.
        soularr_enabled=_bool(
            e, "SOULARR_ENABLED", bool(slskd_api_key) and os.path.exists(soularr_config_path)
        ),
        musicbrainz_contact=_str(e, "MUSICBRAINZ_CONTACT", "https://github.com/isaacault/continuo"),
        listenbrainz_token=_str(e, "LISTENBRAINZ_TOKEN"),
        search_deezer=_bool(e, "SEARCH_DEEZER", True),
        lastfm_api_key=_str(e, "LASTFM_API_KEY"),
        lastfm_secret=_str(e, "LASTFM_SECRET"),
        music_dir=_url(e, "MUSIC_DIR"),  # a path; _url only trims the trailing slash
        library_removal_role=_str(e, "LIBRARY_REMOVAL_ROLE", "admin").lower(),
        name_locale=_str(e, "NAME_LOCALE", "en").lower(),
        track_poll_seconds=_int(e, "TRACK_POLL_SECONDS", 30),
        singles_folder=_str(e, "SINGLES_FOLDER", "_singles").strip("/") or "_singles",
        track_download_give_up_seconds=_int(e, "TRACK_DOWNLOAD_GIVE_UP_SECONDS", 2700),
        libraries_dir=_url(e, "LIBRARIES_DIR"),
        library_sync_seconds=_int(e, "LIBRARY_SYNC_SECONDS", 300),
        library_unclaimed_to=tuple(n.strip().lower() for n in _str(e, "LIBRARY_UNCLAIMED_TO", "").split(",") if n.strip()),
        navidrome_libraries_root=_url(e, "NAVIDROME_LIBRARIES_ROOT", "/libraries"),
        store_library_id=_int(e, "STORE_LIBRARY_ID", 1),
        lidarr_downloads_path=_url(e, "LIDARR_DOWNLOADS_PATH", "/data/downloads"),
        media_cache_max_gb=_int(e, "MEDIA_CACHE_MAX_GB", 25),
        removed_min_days=_int(e, "REMOVED_MIN_DAYS", 7),
        removed_max_days=_int(e, "REMOVED_MAX_DAYS", 30),
        ha_webhook_url=_str(e, "HA_WEBHOOK_URL"),
        uptime_kuma_push_url=_str(e, "UPTIME_KUMA_PUSH_URL"),
        status_poll_seconds=_int(e, "STATUS_POLL_SECONDS", 45),
        reconcile_poll_seconds=_int(e, "RECONCILE_POLL_SECONDS", 60),
        scan_poll_seconds=_int(e, "SCAN_POLL_SECONDS", 30),
        scan_timeout_seconds=_int(e, "SCAN_TIMEOUT_SECONDS", 600),
        scan_watcher_grace_seconds=_int(e, "SCAN_WATCHER_GRACE_SECONDS", 60),
        search_cycle_seconds=_int(e, "SEARCH_CYCLE_SECONDS", 300),
        still_looking_after_cycles=_int(e, "STILL_LOOKING_AFTER_CYCLES", 6),
        stalled_download_hours=_int(e, "STALLED_DOWNLOAD_HOURS", 6),
        race_enabled=_bool(e, "RACE_ENABLED", True),
        race_grace_min=_int(e, "RACE_GRACE_MIN", 10),
        race_slow_min=_int(e, "RACE_SLOW_MIN", 60),
        race_idle_min=_int(e, "RACE_IDLE_MIN", 10),
        slskd_downloads_dir=_url(e, "SLSKD_DOWNLOADS_DIR"),
        slskd_incomplete_dir=_url(e, "SLSKD_INCOMPLETE_DIR") or (
            _url(e, "SLSKD_DOWNLOADS_DIR").rstrip("/") + "-incomplete" if _url(e, "SLSKD_DOWNLOADS_DIR") else ""),
        lidarr_soulseek_path=_url(e, "LIDARR_SOULSEEK_PATH", "/data/downloads/soulseek"),
        heartbeat_seconds=_int(e, "HEARTBEAT_SECONDS", 60),
        loop_stale_seconds=_int(e, "LOOP_STALE_SECONDS", 300),
        library_index_hour=_int(e, "LIBRARY_INDEX_HOUR", 4),
        artwork_cache_dir=_str(e, "ARTWORK_CACHE_DIR"),
        artwork_cache_mb=_int(e, "ARTWORK_CACHE_MB", 200),
        artwork_max_bytes=_int(e, "ARTWORK_MAX_BYTES", 10 * 1024 * 1024),
        port=_int(e, "PORT", 8080),
        log_json=_bool(e, "LOG_JSON", True),
    )


settings: Settings = load()


def reload(env: dict | None = None) -> Settings:
    global settings
    settings = load(env)
    return settings
