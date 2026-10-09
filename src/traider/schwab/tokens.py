"""Where the Schwab login lives, and how the bot keeps an access token fresh.

There are two secrets:

* the **app credentials** (app key and secret from the Schwab developer portal)
* the **grant**: the refresh token from the user's last sign-in, with the time
  it was issued. It dies seven days after sign-in whatever happens.

The sign-in Lambda (or ``traider login``) writes the grant. The bot only reads
it, with one exception: if Schwab returns a different refresh token on refresh,
the bot saves that, unless a newer sign-in is already stored. Access tokens are
kept in memory only.

Imported by the Lambda functions: standard library and botocore only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from botocore.exceptions import BotoCoreError, ClientError

from traider.schwab.oauth import (
    REFRESH_TOKEN_LIFETIME_S,
    TOKEN_URL,
    AppCredentials,
    OAuthError,
    TokenResponse,
    refresh_access_token,
)

log = logging.getLogger(__name__)


class TokenStoreError(Exception):
    """The stored grant could not be read or written."""


class CredentialsError(Exception):
    """The app credentials are present but unusable."""


class AuthUnavailable(Exception):
    """There is no usable Schwab access token right now."""


# --------------------------------------------------------------------------- grant


@dataclass(frozen=True, slots=True)
class Grant:
    refresh_token: str
    issued_at: int  # epoch seconds: when the user signed in
    expires_at: int  # issued_at plus seven days
    grant_id: str

    def seconds_left(self, now_epoch: float) -> float:
        return self.expires_at - now_epoch

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": 1,
                "grant_id": self.grant_id,
                "refresh_token": self.refresh_token,
                "issued_at": self.issued_at,
                "expires_at": self.expires_at,
            }
        )

    @classmethod
    def from_json(cls, text: str) -> Grant:
        try:
            data = json.loads(text)
            token = data["refresh_token"]
            if not isinstance(token, str) or not token:
                raise ValueError("empty refresh_token")
            return cls(
                refresh_token=token,
                issued_at=int(data["issued_at"]),
                expires_at=int(data["expires_at"]),
                grant_id=str(data.get("grant_id", "")),
            )
        except (ValueError, KeyError, TypeError) as exc:
            # Say what is wrong without echoing the stored content.
            raise TokenStoreError(f"stored grant is malformed ({type(exc).__name__})") from None

    def __repr__(self) -> str:
        return f"Grant(grant_id={self.grant_id!r}, issued_at={self.issued_at}, refresh_token=***)"


def new_grant(tokens: TokenResponse, now_epoch: float) -> Grant:
    """The grant to store right after the user signs in."""
    issued = int(now_epoch)
    return Grant(
        refresh_token=tokens.refresh_token,
        issued_at=issued,
        expires_at=issued + REFRESH_TOKEN_LIFETIME_S,
        grant_id=uuid.uuid4().hex,
    )


# -------------------------------------------------------------------------- stores


class TokenStore(Protocol):
    def load(self) -> Grant | None: ...

    def save(self, grant: Grant) -> None: ...


class MemoryTokenStore:
    def __init__(self, grant: Grant | None = None) -> None:
        self._grant = grant

    def load(self) -> Grant | None:
        return self._grant

    def save(self, grant: Grant) -> None:
        self._grant = grant


class FileTokenStore:
    """A local file, readable only by its owner. For running on your own machine."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)

    def load(self) -> Grant | None:
        try:
            text = self._path.read_text()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise TokenStoreError(f"cannot read {self._path}: {exc.strerror}") from None
        return Grant.from_json(text)

    def save(self, grant: Grant) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temp = self._path.with_name(self._path.name + ".tmp")
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(grant.to_json())
            os.chmod(temp, 0o600)
            os.replace(temp, self._path)
        except OSError as exc:
            raise TokenStoreError(f"cannot write {self._path}: {exc.strerror}") from None


def _read_secret(client: Any, secret_id: str) -> str | None:
    """The secret's current value, or None if it has none yet."""
    try:
        response = client.get_secret_value(SecretId=secret_id)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        if code == "ResourceNotFoundException":
            return None  # no such secret, or a secret with no value yet
        raise TokenStoreError(f"cannot read secret {secret_id}: {code}") from None
    except BotoCoreError as exc:
        raise TokenStoreError(f"cannot read secret {secret_id}: {type(exc).__name__}") from None
    value = response.get("SecretString")
    return value if isinstance(value, str) else None


class SecretsManagerTokenStore:
    def __init__(self, secret_id: str, client: Any) -> None:
        self._secret_id = secret_id
        self._client = client

    def load(self) -> Grant | None:
        text = _read_secret(self._client, self._secret_id)
        return None if text is None else Grant.from_json(text)

    def save(self, grant: Grant) -> None:
        try:
            self._client.put_secret_value(SecretId=self._secret_id, SecretString=grant.to_json())
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            raise TokenStoreError(f"cannot write secret {self._secret_id}: {code}") from None
        except BotoCoreError as exc:
            raise TokenStoreError(
                f"cannot write secret {self._secret_id}: {type(exc).__name__}"
            ) from None


# --------------------------------------------------------------------- credentials


class CredentialsProvider(Protocol):
    def load(self) -> AppCredentials | None: ...


class StaticCredentials:
    def __init__(self, credentials: AppCredentials | None) -> None:
        self._credentials = credentials

    def load(self) -> AppCredentials | None:
        return self._credentials


class SecretsManagerCredentials:
    """App key and secret from a secret holding ``{"app_key": "...", "app_secret": "..."}``."""

    def __init__(self, secret_id: str, client: Any) -> None:
        self._secret_id = secret_id
        self._client = client

    def load(self) -> AppCredentials | None:
        try:
            text = _read_secret(self._client, self._secret_id)
        except TokenStoreError as exc:
            raise CredentialsError(str(exc)) from None
        if text is None:
            return None
        try:
            data = json.loads(text)
            key, secret = data["app_key"].strip(), data["app_secret"].strip()
            if not key or not secret:
                raise ValueError("empty")
        except (ValueError, KeyError, TypeError, AttributeError):
            raise CredentialsError(
                f"secret {self._secret_id} must be JSON like "
                '{"app_key": "...", "app_secret": "..."}'
            ) from None
        return AppCredentials(key, secret)


# ------------------------------------------------------------------- token manager


class AuthState(StrEnum):
    OK = "ok"
    NO_CREDENTIALS = "no_credentials"  # app key and secret not set yet
    NO_GRANT = "no_grant"  # nobody has signed in yet
    EXPIRED = "expired"  # the sign-in is past seven days or Schwab refused it
    ERROR = "error"  # something temporary: Schwab or AWS unreachable


class _Clock(Protocol):
    def now(self) -> datetime: ...


class TokenManager:
    ACCESS_MARGIN_S = 300.0  # refresh this long before the access token expires
    ACCESS_GRACE_S = 30.0  # an access token is usable until this close to expiry
    RELOAD_OK_S = 300.0  # how often to look for a newer sign-in while all is well
    RELOAD_WAITING_S = 30.0  # ...and while waiting for one
    RETRY_TEMPORARY_S = 30.0
    MIN_REPLACE_S = 30.0  # a token the API rejects is replaced at most this often
    RETRY_REJECTED_S = 300.0

    def __init__(
        self,
        *,
        store: TokenStore,
        credentials: CredentialsProvider,
        clock: _Clock,
        token_url: str = TOKEN_URL,
    ) -> None:
        self._store = store
        self._credentials = credentials
        self._clock = clock
        self._token_url = token_url
        self._lock = asyncio.Lock()
        self._grant: Grant | None = None
        self._creds: AppCredentials | None = None
        self._access: str | None = None
        self._access_expires_at = 0.0
        self._state = AuthState.NO_GRANT
        self._detail = "not started"
        self._retry_at = 0.0
        self._reload_at = 0.0
        self._invalidated_at = float("-inf")

    # ---------------------------------------------------------------- public

    @property
    def state(self) -> AuthState:
        return self._state

    def describe(self) -> str:
        return f"{self._state.value}: {self._detail}"

    def seconds_left(self) -> float:
        """Seconds until the sign-in expires. Zero when there is none."""
        if self._grant is None:
            return 0.0
        return max(0.0, self._grant.seconds_left(self._now()))

    async def access_token(self) -> str:
        """A valid access token, refreshing if needed. Raises AuthUnavailable."""
        async with self._lock:
            now = self._now()
            if self._access is not None and now < self._access_expires_at - self.ACCESS_MARGIN_S:
                return self._access
            try:
                await self._refresh(now)
            except AuthUnavailable:
                usable = now < self._access_expires_at - self.ACCESS_GRACE_S
                if self._access is not None and usable and self._state is AuthState.ERROR:
                    return self._access  # refresh hiccup; the current token still works
                raise
            if self._access is None:
                raise AuthUnavailable(self.describe())
            return self._access

    async def invalidate(self, token: str) -> None:
        """Call when the API answered 401 for ``token``. The next call fetches a new one."""
        async with self._lock:
            now = self._now()
            if token != self._access or now - self._invalidated_at < self.MIN_REPLACE_S:
                return  # an old token, or one replaced moments ago: not the token's fault
            self._invalidated_at = now
            self._access = None
            self._access_expires_at = 0.0

    async def poll(self) -> None:
        """Background upkeep: notice a new sign-in, keep the access token warm. Never raises."""
        async with self._lock:
            now = self._now()
            try:
                if now >= self._reload_at and await self._reload(now):
                    self._retry_at = 0.0
                stale = (
                    self._access is None or now >= self._access_expires_at - self.ACCESS_MARGIN_S
                )
                if stale:
                    await self._refresh(now)
            except AuthUnavailable:
                pass
            except Exception:
                log.exception("token upkeep failed")

    # -------------------------------------------------------------- internals

    def _now(self) -> float:
        return self._clock.now().timestamp()

    def _fail(self, state: AuthState, retry_at: float, detail: str) -> AuthUnavailable:
        if state is not self._state:
            log.warning("Schwab auth is now %s: %s", state.value, detail)
        self._state, self._retry_at, self._detail = state, retry_at, detail
        return AuthUnavailable(self.describe())

    async def _load_credentials(self, now: float) -> AppCredentials:
        if self._creds is not None:
            return self._creds
        try:
            creds = await asyncio.to_thread(self._credentials.load)
        except CredentialsError as exc:
            raise self._fail(AuthState.ERROR, now + self.RETRY_TEMPORARY_S, str(exc)) from None
        if creds is None:
            raise self._fail(
                AuthState.NO_CREDENTIALS,
                now + self.RETRY_TEMPORARY_S,
                "the Schwab app key and secret have not been stored yet",
            )
        self._creds = creds
        return creds

    async def _reload(self, now: float) -> bool:
        """Re-read the stored grant. Returns True if a different one was adopted."""
        waiting = self._state is not AuthState.OK
        self._reload_at = now + (self.RELOAD_WAITING_S if waiting else self.RELOAD_OK_S)
        try:
            stored = await asyncio.to_thread(self._store.load)
        except TokenStoreError as exc:
            raise self._fail(AuthState.ERROR, now + self.RETRY_TEMPORARY_S, str(exc)) from None
        if stored is None:
            return False
        current = self._grant
        if current is not None and (
            stored.issued_at < current.issued_at
            or (
                stored.issued_at == current.issued_at
                and stored.refresh_token == current.refresh_token
            )
        ):
            return False
        if current is not None:
            log.info("adopting a newer Schwab sign-in from the store")
            # The new sign-in may have voided tokens from the old one.
            self._access = None
            self._access_expires_at = 0.0
        self._grant = stored
        return True

    async def _refresh(self, now: float) -> None:
        if now < self._retry_at:
            raise AuthUnavailable(self.describe())
        creds = await self._load_credentials(now)
        if self._grant is None:
            await self._reload(now)
        if self._grant is None:
            raise self._fail(
                AuthState.NO_GRANT,
                now + self.RELOAD_WAITING_S,
                "nobody has signed in to Schwab yet",
            )
        if self._grant.seconds_left(now) <= 0:
            await self._reload(now)
            if self._grant.seconds_left(now) <= 0:
                raise self._fail(
                    AuthState.EXPIRED,
                    now + self.RELOAD_WAITING_S,
                    "the Schwab sign-in is more than seven days old",
                )
        try:
            response = await self._request(creds, self._grant)
        except OAuthError as exc:
            if not exc.rejected:
                raise self._fail(AuthState.ERROR, now + self.RETRY_TEMPORARY_S, str(exc)) from None
            if exc.status == 401:
                self._creds = None  # the app secret may have been changed: re-read it next time
            response = await self._retry_with_newer_grant(creds, self._grant, now, exc)

        self._access = response.access_token
        self._access_expires_at = now + response.expires_in
        if self._state is not AuthState.OK:
            log.info("Schwab auth is ok")
        self._state, self._detail, self._retry_at = AuthState.OK, "access token is valid", 0.0
        if response.refresh_token and response.refresh_token != self._grant.refresh_token:
            await self._save_rotated(response.refresh_token)

    async def _request(self, creds: AppCredentials, grant: Grant) -> TokenResponse:
        return await asyncio.to_thread(
            refresh_access_token, creds, grant.refresh_token, token_url=self._token_url
        )

    async def _retry_with_newer_grant(
        self, creds: AppCredentials, rejected: Grant, now: float, error: OAuthError
    ) -> TokenResponse:
        """Schwab refused the grant. If the user has signed in again since, use that."""
        await self._reload(now)
        current = self._grant
        if current is None or current.refresh_token == rejected.refresh_token:
            raise self._fail(AuthState.EXPIRED, now + self.RETRY_REJECTED_S, str(error)) from None
        try:
            return await self._request(creds, current)
        except OAuthError as exc:
            state = AuthState.EXPIRED if exc.rejected else AuthState.ERROR
            retry = self.RETRY_REJECTED_S if exc.rejected else self.RETRY_TEMPORARY_S
            raise self._fail(state, now + retry, str(exc)) from None

    async def _save_rotated(self, refresh_token: str) -> None:
        """Schwab handed back a different refresh token: persist it, unless a newer
        sign-in is already in the store. Rotation does not restart the seven days."""
        assert self._grant is not None
        rotated = replace(self._grant, refresh_token=refresh_token)
        try:
            stored = await asyncio.to_thread(self._store.load)
            if stored is not None and stored.issued_at > rotated.issued_at:
                self._grant = stored
                return
            await asyncio.to_thread(self._store.save, rotated)
        except TokenStoreError as exc:
            # Keep trading on the in-memory token, but say so: a restart would lose it.
            log.error("could not save the rotated refresh token: %s", exc)
        self._grant = rotated
