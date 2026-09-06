"""Credential loading. Credentials never live in code.

Resolution order: environment variables, then ./.env, then ~/.reddit_feed.env.
The feed token comes from https://www.reddit.com/prefs/feeds (open any RSS link there and
copy the ``feed=`` and ``user=`` query parameters). It grants read access to the account's
private feeds, so treat it like a password. Changing the account password revokes it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

TOKEN_VAR = "REDDIT_FEED_TOKEN"
USER_VAR = "REDDIT_FEED_USER"
DEFAULT_ENV_FILES: tuple[Path, ...] = (Path(".env"), Path.home() / ".reddit_feed.env")


class MissingCredentials(RuntimeError):
    """Raised when neither the environment nor any env file supplies both values."""


@dataclass(frozen=True)
class Credentials:
    token: str
    user: str

    def as_params(self) -> dict[str, str]:
        """Query parameters Reddit expects on authenticated .rss URLs."""
        return {"feed": self.token, "user": self.user}


def parse_env_file(text: str) -> dict[str, str]:
    """Minimal KEY=VALUE parser: ignores blanks and comments, strips matching quotes."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_credentials(
    environ: Mapping[str, str] | None = None,
    env_files: Sequence[Path] = DEFAULT_ENV_FILES,
) -> Credentials:
    environ = os.environ if environ is None else environ
    merged: dict[str, str] = {}
    for path in reversed(env_files):          # earlier files win, env wins over all
        if path.exists():
            merged.update(parse_env_file(path.read_text()))
    merged.update({k: v for k, v in environ.items() if k in (TOKEN_VAR, USER_VAR) and v})
    token, user = merged.get(TOKEN_VAR), merged.get(USER_VAR)
    if not (token and user):
        raise MissingCredentials(
            f"set {TOKEN_VAR} and {USER_VAR} (environment, ./.env, or ~/.reddit_feed.env). "
            "Get the token from https://www.reddit.com/prefs/feeds -> any RSS link -> feed= and user= params."
        )
    return Credentials(token=token, user=user)
