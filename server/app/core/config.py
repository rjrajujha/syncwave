from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = 'SyncWave Signaling Server'
    app_version: str = '1.1.5'
    app_env: str = 'development'
    app_host: str = '0.0.0.0'
    app_port: int = 8000
    websocket_path: str = '/ws'
    protocol_version: str = '1'
    require_server_connection_pin: bool = False
    server_connection_pin: str = ''
    # Legacy setting; listener-only handshakes always skip Server PIN (see handlers).
    allow_listener_only_without_server_pin: bool = True
    redis_url: str = ''
    room_ttl_seconds: int = 21600
    max_participants_per_room: int = 20
    pin_hash_secret: str = 'change-this-in-production'
    allowed_origins: str = '*'
    log_level: str = 'INFO'
    github_redirect: str = 'https://github.com/OpenCodeQuark/syncwave'
    # Uvicorn CLI flags (Dockerfile/Procfile read these from the process env).
    forwarded_allow_ips: str = '*'
    ws_ping_interval: int = 20
    ws_ping_timeout: int = 20
    keep_alive_timeout: int = 10

    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore',
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
