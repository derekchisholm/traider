"""The Schwab sign-in endpoint (Lambda behind an HTTP API).

Schwab's refresh token dies seven days after each sign-in, and renewing it
needs a person to log in. This function makes that a tap on a link:

``GET /start?k=<key>``
    Sends the browser to Schwab's sign-in page. The key keeps strangers from
    starting the flow.
``GET /callback?code=..&state=..``
    Where Schwab sends the browser back. The one-time code is exchanged for a
    refresh token, which goes straight into Secrets Manager. The bot picks it
    up within a minute; no restart.

If Schwab will only accept a ``https://127.0.0.1`` callback for your app, the
same link works in *paste mode*: after signing in, the browser lands on a
127.0.0.1 address that fails to load, and you paste that address into the form
on the start page (``POST /exchange``).

Tokens are never written to the page, the logs or the alert.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets as random_secrets
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from traider.alerts import publish
from traider.lambdas.common import escape, message, not_found, page, redirect
from traider.schwab.oauth import (
    AUTHORIZE_URL,
    TOKEN_URL,
    OAuthError,
    authorize_url,
    code_from_redirect,
    exchange_code,
)
from traider.schwab.tokens import (
    CredentialsError,
    SecretsManagerCredentials,
    SecretsManagerTokenStore,
    TokenStoreError,
    new_grant,
)

STATE_LIFETIME_S = 600


@dataclass(frozen=True)
class Settings:
    app_secret_id: str
    token_secret_id: str
    public_url: str  # this API's base URL, no trailing slash
    callback_url: str  # the redirect URI registered with Schwab
    start_key: str
    state_secret: str
    alert_topic_arn: str | None = None
    token_url: str = TOKEN_URL
    authorize_url: str = AUTHORIZE_URL

    @property
    def hosted(self) -> bool:
        """True when Schwab redirects straight back to this API."""
        return self.callback_url == f"{self.public_url}/callback"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        return cls(
            app_secret_id=env["APP_SECRET_ID"],
            token_secret_id=env["TOKEN_SECRET_ID"],
            public_url=env["PUBLIC_URL"].rstrip("/"),
            callback_url=env["CALLBACK_URL"],
            start_key=env["START_KEY"],
            state_secret=env["STATE_SECRET"],
            alert_topic_arn=env.get("ALERT_TOPIC_ARN") or None,
        )


class AuthApp:
    def __init__(
        self,
        settings: Settings,
        *,
        secrets: Any,
        sns: Any | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self._secrets = secrets
        self._sns = sns
        self._now = now

    # ---------------------------------------------------------------- routing

    def handle(self, event: Mapping[str, Any]) -> dict[str, Any]:
        http = event.get("requestContext", {}).get("http", {})
        route = (http.get("method", ""), event.get("rawPath", ""))
        query = event.get("queryStringParameters") or {}
        if route == ("GET", "/start"):
            return self._start(query)
        if route == ("GET", "/callback"):
            return self._callback(query)
        if route == ("POST", "/exchange"):
            return self._exchange(_form(event))
        return not_found()

    def _key_ok(self, supplied: object) -> bool:
        if not isinstance(supplied, str) or not supplied:
            return False
        return hmac.compare_digest(supplied.encode(), self.settings.start_key.encode())

    # ------------------------------------------------------------------ /start

    def _start(self, query: Mapping[str, str]) -> dict[str, Any]:
        if not self._key_ok(query.get("k")):
            return not_found()
        try:
            creds = SecretsManagerCredentials(self.settings.app_secret_id, self._secrets).load()
        except CredentialsError as exc:
            print(f"start: {exc}")
            return message(500, "Sign-in is not set up correctly", str(exc))
        if creds is None:
            return message(
                503,
                "Not ready yet",
                "The Schwab app key and secret have not been stored yet. Put them in the "
                "app-credentials secret, then open this link again.",
            )
        link = authorize_url(
            creds.app_key,
            self.settings.callback_url,
            self._new_state(),
            base=self.settings.authorize_url,
        )
        if self.settings.hosted:
            return redirect(link)
        return page(
            200,
            "Sign in to Schwab",
            _PASTE_PAGE.format(
                link=escape(link),
                callback=escape(self.settings.callback_url),
                key=escape(self.settings.start_key),
            ),
        )

    # --------------------------------------------------------------- /callback

    def _callback(self, query: Mapping[str, str]) -> dict[str, Any]:
        # State first: without ours, nothing in the request came from a sign-in we started,
        # and none of it is shown.
        if not self._state_ok(query.get("state")):
            return message(
                400,
                "This sign-in link has expired",
                "Open the sign-in link again and complete the Schwab login within ten minutes.",
            )
        if query.get("error"):
            detail = f"{query['error']} {query.get('error_description', '')}".strip()[:300]
            return message(400, "Schwab did not complete the sign-in", f"Schwab said: {detail}")
        code = query.get("code")
        if not code:
            return message(400, "No authorization code", "Schwab did not send a code. Start again.")
        return self._complete(code)

    # --------------------------------------------------------------- /exchange

    def _exchange(self, form: Mapping[str, str]) -> dict[str, Any]:
        if not self._key_ok(form.get("k")):
            return not_found()
        try:
            code, state = code_from_redirect(form.get("redirect", ""))
        except OAuthError as exc:
            return message(
                400,
                "That is not the address Schwab sent you to",
                f"{exc}. Paste the whole address from the browser's address bar after "
                "signing in; it contains code=...",
            )
        # The key already proves who is asking. A state, when Schwab echoes one, must be ours.
        if state is not None and not self._state_ok(state):
            return message(400, "This sign-in attempt has expired", "Start again from the link.")
        return self._complete(code)

    # ------------------------------------------------------------------ shared

    def _complete(self, code: str) -> dict[str, Any]:
        settings = self.settings
        try:
            creds = SecretsManagerCredentials(settings.app_secret_id, self._secrets).load()
            if creds is None:
                return message(
                    503, "Not ready yet", "The Schwab app key and secret are not stored."
                )
            tokens = exchange_code(creds, code, settings.callback_url, token_url=settings.token_url)
            grant = new_grant(tokens, self._now())
            SecretsManagerTokenStore(settings.token_secret_id, self._secrets).save(grant)
        except OAuthError as exc:
            print(f"exchange failed: {exc}")
            return message(
                502,
                "Schwab did not accept the code",
                "Codes are single-use and expire within about 30 seconds. Open the sign-in "
                "link and try again.",
            )
        except (CredentialsError, TokenStoreError) as exc:
            print(f"storage problem: {exc}")
            return message(500, "Could not save the sign-in", str(exc))

        expires = time.strftime("%A %d %B %Y, %H:%M UTC", time.gmtime(grant.expires_at))
        print(f"sign-in stored, grant {grant.grant_id}, expires {expires}")
        self._announce(expires)
        return message(
            200,
            "Signed in",
            f"The bot is connected to Schwab. This sign-in lasts 7 days, until {expires}. "
            "You can close this page.",
        )

    def _announce(self, expires: str) -> None:
        if not (self._sns and self.settings.alert_topic_arn):
            return
        try:
            publish(
                self._sns,
                self.settings.alert_topic_arn,
                "Schwab sign-in completed",
                f"A new Schwab sign-in was stored. It is valid until {expires}.\n"
                "If this was not you, set the control switch to halt and change your "
                "Schwab password.",
            )
        except Exception as exc:  # the sign-in itself succeeded; do not fail the page
            print(f"could not publish the sign-in notice: {type(exc).__name__}")

    # ------------------------------------------------------------------- state

    def _sign(self, payload: str) -> str:
        key = self.settings.state_secret.encode()
        return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()

    def _new_state(self) -> str:
        payload = f"{int(self._now()) + STATE_LIFETIME_S}.{random_secrets.token_urlsafe(12)}"
        return f"{payload}.{self._sign(payload)}"

    def _state_ok(self, state: object) -> bool:
        if not isinstance(state, str) or state.count(".") != 2:
            return False
        payload, _, signature = state.rpartition(".")
        if not hmac.compare_digest(signature.encode(), self._sign(payload).encode()):
            return False
        try:
            expires = int(payload.split(".", 1)[0])
        except ValueError:
            return False
        return self._now() <= expires


_PASTE_PAGE = """
<ol>
<li><a class="button" href="{link}" target="_blank" rel="noopener noreferrer">Sign in with
Schwab</a><br>It opens in a new tab. Log in and approve access.</li>
<li>Your browser will then try to open an address starting with <code>{callback}</code> and
show an error page. That is expected. Copy the <strong>whole address</strong> from the
address bar.</li>
<li>Paste it here within about 30 seconds:
<form action="/exchange" method="post">
<input type="hidden" name="k" value="{key}">
<p><input type="text" name="redirect" placeholder="{callback}/?code=..." autocomplete="off"
required></p>
<button type="submit">Finish sign-in</button>
</form></li>
</ol>
"""


def _form(event: Mapping[str, Any]) -> dict[str, str]:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode()
        except (ValueError, UnicodeDecodeError):
            return {}
    return {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}


_app: AuthApp | None = None


def handler(event: Mapping[str, Any], _context: object) -> dict[str, Any]:
    global _app  # noqa: PLW0603 - reuse clients across warm invocations
    if _app is None:
        import boto3  # noqa: PLC0415 - provided by the Lambda runtime

        _app = AuthApp(
            Settings.from_env(os.environ),
            secrets=boto3.client("secretsmanager"),
            sns=boto3.client("sns"),
        )
    return _app.handle(event)
