import os
import sys
import json
import configparser
from pathlib import Path

USER_AGENT = "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/119.0"

base_dir = Path.cwd()
config_path = Path(os.path.join(base_dir, "settings/config.ini"))
config = configparser.ConfigParser(interpolation=None)


def credentials():
    """Return (email, password) for a standalone/CLI use of pyquotex.

    Not used by the Telegram bot itself (each user's credentials live,
    encrypted, in users_data.json — see shared.py). This helper is only
    for the example script trade_bot.py. It reads QUOTEX_EMAIL /
    QUOTEX_PASSWORD from the environment first, then falls back to
    settings/config.ini, and only prompts interactively as a last resort
    (never silently falls back to a hard-coded value).
    """
    email = os.getenv("QUOTEX_EMAIL")
    password = os.getenv("QUOTEX_PASSWORD")
    if email and password:
        return email, password

    if config_path.exists():
        config.read(config_path, encoding="utf-8")
        email = config.get("settings", "email", fallback=None)
        password = config.get("settings", "password", fallback=None)
        if email and password:
            return email, password

    if not sys.stdin.isatty():
        print("QUOTEX_EMAIL/QUOTEX_PASSWORD não configurados no ambiente.")
        sys.exit(1)

    email = input("Enter your account email: ")
    password = input("Enter your account password: ")
    if not email or not password:
        print("Email and password cannot be left blank...")
        sys.exit(1)

    return email, password


def resource_path(relative_path: str | Path) -> Path:
    global base_dir
    """Get absolute path to resource, works for dev and for PyInstaller"""
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        base_dir = Path(sys._MEIPASS)
    return base_dir / relative_path


def load_session(user_agent, base_path: Path | str | None = None):
    """Load the cached session (cookies/token) from disk.

    ``base_path`` MUST be a directory unique to the account being logged
    in (see ``Quotex(root_path=...)``). Without it, every ``Quotex``
    instance falls back to the single global ``session.json`` next to the
    process's cwd — which means, in a bot that logs in multiple different
    Quotex accounts (e.g. one per Telegram user), every account would read
    and overwrite the SAME session file. Whichever account logged in most
    recently silently poisons every other account's next connection
    attempt with its own token/cookies, which Quotex's server then rejects
    without a clear error (exactly the "Login failed. Nenhum erro
    específico informado" symptom).
    """
    output_file = Path(base_path) / "session.json" if base_path else Path(
        resource_path(
            "session.json"
        )
    )
    if os.path.isfile(output_file):
        with open(output_file) as file:
            session_data = json.loads(
                file.read()
            )
    else:
        output_file.parent.mkdir(
            exist_ok=True,
            parents=True
        )
        session_dict = {
            "cookies": None,
            "token": None,
            "user_agent": user_agent
        }
        session_result = json.dumps(session_dict, indent=4)
        output_file.write_text(
            session_result
        )
        session_data = json.loads(
            session_result
        )
    return session_data


def update_session(session_data, base_path: Path | str | None = None):
    """Persist the session (cookies/token) to disk. See ``load_session``
    for why ``base_path`` must be passed for any multi-account use."""
    output_file = Path(base_path) / "session.json" if base_path else Path(
        resource_path(
            "session.json"
        )
    )
    output_file.parent.mkdir(
        exist_ok=True,
        parents=True
    )
    session_result = json.dumps(session_data, indent=4)
    output_file.write_text(
        session_result
    )
    session_data = json.loads(
        session_result
    )
    return session_data
