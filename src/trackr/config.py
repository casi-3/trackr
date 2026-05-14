from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

import tomli_w
from platformdirs import user_config_dir

APP_NAME = "trackr"
CONFIG_FILENAME = "config.toml"


@dataclass
class Config:
    # C411
    c411_api_key: str = ""
    c411_passkey: str = ""
    c411_username: str = ""
    c411_session: str = ""
    c411_session_expires_at: str = ""
    # Torr9
    torr9_username: str = ""
    torr9_password: str = ""
    torr9_jwt: str = ""
    torr9_jwt_expires_at: str = ""
    torr9_passkey: str = ""
    # qBittorrent (client BitTorrent)
    qbt_url: str = ""
    qbt_auth_mode: str = ""  # "api_key" | "login"
    qbt_api_key: str = ""
    qbt_username: str = ""
    qbt_password: str = ""
    qbt_sid_cookie: str = ""  # cache du cookie de session (mode login)
    # Réseau / Proxy
    proxy_url: str = ""  # ex: socks5://user:pass@host:1080 ou http://host:8080
    # Common
    default_screen_host: str = "ask"

    def is_qbt_ready(self) -> bool:
        if not self.qbt_url:
            return False
        if self.qbt_auth_mode == "api_key":
            return bool(self.qbt_api_key)
        if self.qbt_auth_mode == "login":
            return bool(self.qbt_username and self.qbt_password)
        return False

    def is_c411_ready(self) -> bool:
        return bool(self.c411_api_key and self.c411_passkey)

    def is_torr9_ready(self) -> bool:
        return bool(self.torr9_jwt and self.torr9_passkey)

    def c411_session_valid(self) -> bool:
        if not self.c411_session or not self.c411_session_expires_at:
            return False
        try:
            exp = datetime.fromisoformat(self.c411_session_expires_at)
            return exp > datetime.now(timezone.utc)
        except ValueError:
            return False

    def torr9_jwt_valid(self) -> bool:
        if not self.torr9_jwt or not self.torr9_jwt_expires_at:
            return False
        try:
            exp = datetime.fromisoformat(self.torr9_jwt_expires_at)
            return exp > datetime.now(timezone.utc)
        except ValueError:
            return False


def config_dir() -> Path:
    return Path(user_config_dir(APP_NAME))


def config_path() -> Path:
    return config_dir() / CONFIG_FILENAME


def load_config() -> Config:
    path = config_path()
    if not path.exists():
        return Config()
    with path.open("rb") as f:
        data = tomllib.load(f)
    known = {f.name for f in fields(Config)}
    return Config(**{k: v for k, v in data.items() if k in known})


def save_config(cfg: Config) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        tomli_w.dump(asdict(cfg), f)
    if sys.platform != "win32":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return path
