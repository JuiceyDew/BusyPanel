"""Settings: business identity, web bind, and where the books live.

All the values the web UI can edit live in settings.json (see busypanel/state.py)
and are layered over these defaults.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from busypanel.state import resolve_state_dir

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """Precedence: init > environment > settings.json > .env > secrets.

        `settings.json` is what the web UI writes (see busypanel/state.py). It
        sits below the real environment on purpose: an operator, or a NixOS
        `EnvironmentFile`, must always be able to override whatever the UI
        stored. Earlier sources in this tuple win.
        """
        from pydantic_settings import JsonConfigSettingsSource

        from busypanel.state import settings_file

        path = settings_file()
        if path.exists():
            json_source = JsonConfigSettingsSource(settings_cls, json_file=path)
            return (init_settings, env_settings, json_source,
                    dotenv_settings, file_secret_settings)
        return (init_settings, env_settings, dotenv_settings, file_secret_settings)

    # --- business identity (printed on every invoice) -------------------------
    business_name: str = ""
    business_address: str = ""
    business_email: str = ""
    # Days from issue date to due date on a new invoice.
    payment_terms_days: int = 30
    invoice_footer: str = "Thank you for your business."

    # --- web UI ---------------------------------------------------------------
    # 0.0.0.0 binds every interface so the panel is reachable from the LAN, which
    # is the point of a self-hosted business panel. Set auth_password (or the
    # NixOS `authPasswordFile`) before exposing it beyond a trusted subnet: this
    # is a LAN gate, not internet-grade auth, and the password crosses plain HTTP.
    #
    # 0.0.0.0 rather than the machine's LAN IP on purpose: the address here comes
    # from DHCP and changes, a bind to a stale IP fails silently on reboot.
    web_host: str = "0.0.0.0"
    # 8090: 8000, 8080 and 3030 are already bound on this box.
    web_port: int = 8090

    # --- web auth -------------------------------------------------------------
    # One password gates the whole UI. Empty disables the gate (the default, so
    # development and the offline tests are unaffected). Set AUTH_PASSWORD in the
    # environment -- the NixOS module's environmentFile is the clean way -- or via
    # `busypanel passwd` / the Settings page once logged in.
    auth_password: str = ""

    # --- paths ----------------------------------------------------------------
    # All state lives under the state dir, which defaults to ../data (gitignored)
    # and is overridden by BUSYPANEL_STATE_DIR (the NixOS module sets this to the
    # systemd StateDirectory). Resolved at instantiation so a relocated state dir
    # moves the database with it.
    db_path: Path = DATA_DIR / "busypanel.db"

    @model_validator(mode="after")
    def _relocate_paths(self) -> "Settings":
        """Move the default database path when BUSYPANEL_STATE_DIR relocates it.

        The field default is anchored at `ROOT/data`. If the operator pointed the
        state dir somewhere else, the database follows it -- but a path set
        explicitly via env/json/dotenv is left alone.
        """
        state = resolve_state_dir()
        if state == DATA_DIR:
            return self
        if self.db_path == DATA_DIR / "busypanel.db":
            object.__setattr__(self, "db_path", state / "busypanel.db")
        return self

    @property
    def web_url(self) -> str:
        """A human-facing URL. 0.0.0.0 is a bind address, not a destination, so
        it is shown as localhost -- the operator reaches it via the machine's
        real IP or hostname."""
        shown = "127.0.0.1" if self.web_host in ("0.0.0.0", "::") else self.web_host
        return f"http://{shown}:{self.web_port}"

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
