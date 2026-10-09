import asyncio
import base64

import pytest

from tests.fakes.schwab_server import APP_KEY, APP_SECRET, query_of, redirect_url
from traider.schwab.oauth import (
    AppCredentials,
    OAuthError,
    authorize_url,
    code_from_redirect,
    exchange_code,
    refresh_access_token,
)

CREDS = AppCredentials(APP_KEY, APP_SECRET)


async def in_thread(fn, *args, **kwargs):
    """The OAuth calls are blocking; the fake server needs the event loop free to answer."""
    return await asyncio.to_thread(fn, *args, **kwargs)


# --- building the sign-in link and reading the redirect ------------------------


def test_authorize_url_carries_client_redirect_and_state():
    url = authorize_url(APP_KEY, "https://example.test/callback", "state-123")
    assert url.startswith("https://api.schwabapi.com/v1/oauth/authorize?")
    assert query_of(url) == {
        "response_type": "code",
        "client_id": APP_KEY,
        "redirect_uri": "https://example.test/callback",
        "state": "state-123",
    }


def test_code_is_read_from_a_pasted_redirect_url_and_url_decoded():
    code, state = code_from_redirect(
        "https://127.0.0.1/?code=C0.abc%40&session=xyz&state=state-123"
    )
    assert (code, state) == ("C0.abc@", "state-123")


def test_code_is_read_from_a_bare_query_string():
    code, state = code_from_redirect("code=C0.abc%40&session=xyz")
    assert (code, state) == ("C0.abc@", None)


def test_redirect_without_a_code_is_an_error():
    with pytest.raises(OAuthError, match="no authorization code"):
        code_from_redirect("https://127.0.0.1/?session=xyz")


def test_redirect_carrying_an_error_reports_it():
    with pytest.raises(OAuthError, match="access_denied"):
        code_from_redirect("https://127.0.0.1/?error=access_denied&error_description=nope")


# --- exchanging the code ---------------------------------------------------------


async def test_code_is_exchanged_for_tokens(schwab):
    code = schwab.issue_auth_code()
    tokens = await in_thread(
        exchange_code, CREDS, code, schwab.redirect_uri, token_url=schwab.token_url
    )
    assert tokens.refresh_token in schwab.refresh_tokens
    assert tokens.access_token in schwab.access_tokens
    assert tokens.expires_in == 1800


async def test_exchange_sends_what_schwab_expects(schwab):
    code = schwab.issue_auth_code()
    await in_thread(exchange_code, CREDS, code, schwab.redirect_uri, token_url=schwab.token_url)
    (request,) = schwab.calls("POST", "/v1/oauth/token")
    expected = base64.b64encode(f"{APP_KEY}:{APP_SECRET}".encode()).decode()
    assert request["headers"]["Authorization"] == f"Basic {expected}"
    assert request["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert request["form"] == {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": schwab.redirect_uri,
    }


async def test_pasted_redirect_round_trips_through_the_exchange(schwab):
    pasted = redirect_url(schwab, schwab.issue_auth_code(), state="s1")
    code, _ = code_from_redirect(pasted)
    tokens = await in_thread(
        exchange_code, CREDS, code, schwab.redirect_uri, token_url=schwab.token_url
    )
    assert tokens.refresh_token


async def test_a_code_cannot_be_used_twice(schwab):
    code = schwab.issue_auth_code()
    await in_thread(exchange_code, CREDS, code, schwab.redirect_uri, token_url=schwab.token_url)
    with pytest.raises(OAuthError) as caught:
        await in_thread(exchange_code, CREDS, code, schwab.redirect_uri, token_url=schwab.token_url)
    assert caught.value.rejected is True


async def test_wrong_app_secret_is_a_rejection(schwab):
    code = schwab.issue_auth_code()
    with pytest.raises(OAuthError) as caught:
        await in_thread(
            exchange_code,
            AppCredentials(APP_KEY, "wrong"),
            code,
            schwab.redirect_uri,
            token_url=schwab.token_url,
        )
    assert caught.value.rejected is True
    assert caught.value.status == 401


# --- refreshing ---------------------------------------------------------------------


async def test_refresh_returns_a_new_access_token_and_the_same_refresh_token(schwab):
    refresh = schwab.seed_refresh_token()
    tokens = await in_thread(refresh_access_token, CREDS, refresh, token_url=schwab.token_url)
    assert tokens.access_token in schwab.access_tokens
    assert tokens.refresh_token == refresh


async def test_refresh_sends_the_refresh_grant(schwab):
    refresh = schwab.seed_refresh_token()
    await in_thread(refresh_access_token, CREDS, refresh, token_url=schwab.token_url)
    (request,) = schwab.calls("POST", "/v1/oauth/token")
    assert request["form"] == {"grant_type": "refresh_token", "refresh_token": refresh}


async def test_revoked_refresh_token_is_a_rejection(schwab):
    refresh = schwab.seed_refresh_token()
    schwab.revoke_refresh_tokens()
    with pytest.raises(OAuthError) as caught:
        await in_thread(refresh_access_token, CREDS, refresh, token_url=schwab.token_url)
    assert (caught.value.rejected, caught.value.status) == (True, 400)


async def test_server_error_is_not_a_rejection(schwab):
    refresh = schwab.seed_refresh_token()
    schwab.fail("POST", "/v1/oauth/token", 503)
    with pytest.raises(OAuthError) as caught:
        await in_thread(refresh_access_token, CREDS, refresh, token_url=schwab.token_url)
    assert (caught.value.rejected, caught.value.status) == (False, 503)


async def test_unreachable_server_is_not_a_rejection():
    with pytest.raises(OAuthError) as caught:
        await in_thread(
            refresh_access_token, CREDS, "r", token_url="http://127.0.0.1:9/token", timeout_s=2
        )
    assert caught.value.rejected is False


async def test_redirects_are_not_followed_so_credentials_cannot_be_forwarded(schwab):
    refresh = schwab.seed_refresh_token()
    schwab.fail(
        "POST", "/v1/oauth/token", 302, headers={"Location": f"{schwab.base_url}/somewhere-else"}
    )
    with pytest.raises(OAuthError) as caught:
        await in_thread(refresh_access_token, CREDS, refresh, token_url=schwab.token_url)
    assert caught.value.status == 302
    assert [r["path"] for r in schwab.requests] == ["/v1/oauth/token"]


async def test_garbled_success_response_is_an_error_not_a_crash(schwab):
    refresh = schwab.seed_refresh_token()
    schwab.fail("POST", "/v1/oauth/token", 200, body={"unexpected": True})
    with pytest.raises(OAuthError) as caught:
        await in_thread(refresh_access_token, CREDS, refresh, token_url=schwab.token_url)
    assert caught.value.rejected is False


async def test_errors_never_contain_the_secret_or_the_token(schwab):
    schwab.revoke_refresh_tokens()
    with pytest.raises(OAuthError) as caught:
        await in_thread(
            refresh_access_token, CREDS, "super-secret-refresh-token", token_url=schwab.token_url
        )
    text = f"{caught.value} {caught.value!r}"
    assert "super-secret-refresh-token" not in text
    assert APP_SECRET not in text


@pytest.mark.parametrize(
    "url", ["http://api.schwabapi.com/v1/oauth/token", "ftp://x/y", "file:///etc/passwd"]
)
def test_tokens_are_only_ever_sent_over_https(url):
    with pytest.raises(ValueError, match="https"):
        refresh_access_token(CREDS, "r", token_url=url)
