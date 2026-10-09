"""Schwab OAuth: the sign-in link, the code exchange and the token refresh.

Standard library only, and blocking. The Lambda functions import this module
directly; the bot calls it from a worker thread. Request and response details
follow the two open-source Schwab clients this project was checked against
(schwab-py and schwabdev):

* authorize: ``GET /v1/oauth/authorize?response_type=code&client_id=..&redirect_uri=..``
* token:     ``POST /v1/oauth/token``, HTTP Basic auth with the app key and
  secret, form-encoded body
* access tokens last 30 minutes; a refresh token lasts 7 days from the moment
  the user signed in, and refreshing does not extend it

Nothing in here logs or raises a token or the app secret.
"""

from __future__ import annotations

import base64
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

AUTHORIZE_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"  # noqa: S105
REFRESH_TOKEN_LIFETIME_S = 7 * 24 * 3600
_LOOPBACK = ("http://127.0.0.1:", "http://localhost:")  # tests only


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would resend the Authorization header to the target."""

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


@dataclass(frozen=True, slots=True)
class AppCredentials:
    app_key: str
    app_secret: str

    def __repr__(self) -> str:  # keep the secret out of logs and tracebacks
        return f"AppCredentials(app_key={self.app_key[:4]}..., app_secret=***)"


@dataclass(frozen=True, slots=True)
class TokenResponse:
    access_token: str
    refresh_token: str
    expires_in: int

    def __repr__(self) -> str:
        return f"TokenResponse(expires_in={self.expires_in})"


class OAuthError(Exception):
    """A token request failed.

    ``rejected`` is True when Schwab definitively refused it (bad or expired grant, bad
    app credentials): retrying will not help and the user has to sign in again. It is
    False for anything that might be temporary.
    """

    def __init__(self, message: str, *, status: int | None = None, rejected: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.rejected = rejected


def authorize_url(app_key: str, redirect_uri: str, state: str, *, base: str = AUTHORIZE_URL) -> str:
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": app_key,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{base}?{query}"


def code_from_redirect(url_or_query: str) -> tuple[str, str | None]:
    """Pull the authorization code (and state) out of the URL Schwab redirected to."""
    text = url_or_query.strip()
    query = urllib.parse.urlsplit(text).query if "://" in text else text.lstrip("?")
    params = urllib.parse.parse_qs(query)
    if "error" in params:
        detail = params.get("error_description", [""])[0]
        raise OAuthError(f"Schwab returned an error: {params['error'][0]} {detail}".strip())
    code = params.get("code", [None])[0]
    if not code:
        raise OAuthError("no authorization code in the redirect URL")
    return code, params.get("state", [None])[0]


def exchange_code(
    creds: AppCredentials,
    code: str,
    redirect_uri: str,
    *,
    token_url: str = TOKEN_URL,
    timeout_s: float = 15.0,
) -> TokenResponse:
    """Trade a fresh authorization code for tokens. Codes expire within seconds."""
    return _token_request(
        creds,
        {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
        token_url,
        timeout_s,
    )


def refresh_access_token(
    creds: AppCredentials,
    refresh_token: str,
    *,
    token_url: str = TOKEN_URL,
    timeout_s: float = 15.0,
) -> TokenResponse:
    return _token_request(
        creds, {"grant_type": "refresh_token", "refresh_token": refresh_token}, token_url, timeout_s
    )


def _token_request(
    creds: AppCredentials, form: dict[str, str], token_url: str, timeout_s: float
) -> TokenResponse:
    if not (token_url.startswith("https://") or token_url.startswith(_LOOPBACK)):
        raise ValueError("token URL must be https")
    basic = base64.b64encode(f"{creds.app_key}:{creds.app_secret}".encode()).decode()
    request = urllib.request.Request(  # noqa: S310 - scheme checked above
        token_url,
        data=urllib.parse.urlencode(form).encode(),
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    grant = form["grant_type"]
    try:
        with _OPENER.open(request, timeout=timeout_s) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        # 400 and 401 are Schwab saying no. The body can echo request details, so only
        # its error code is passed on.
        rejected = exc.code in (400, 401)
        raise OAuthError(
            f"token request ({grant}) failed with HTTP {exc.code}{_error_code(exc.read())}",
            status=exc.code,
            rejected=rejected,
        ) from None
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise OAuthError(f"token request ({grant}) could not reach Schwab: {reason}") from None
    try:
        data = json.loads(raw)
        return TokenResponse(
            access_token=str(data["access_token"]),
            # A refresh response may omit the refresh token; then the current one stays.
            refresh_token=str(data.get("refresh_token") or form.get("refresh_token") or ""),
            expires_in=int(data.get("expires_in", 1800)),
        )
    except (ValueError, KeyError, TypeError):
        raise OAuthError(f"token request ({grant}) returned an unexpected response") from None


def _error_code(body: bytes) -> str:
    try:
        error = json.loads(body).get("error")
    except (ValueError, AttributeError):
        return ""
    return f" ({error})" if isinstance(error, str) and len(error) < 60 else ""
