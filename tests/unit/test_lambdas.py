"""The sign-in Lambda and the expiry watchdog, with mocked AWS and the fake Schwab."""

import asyncio
import base64
import json
import subprocess
import sys
import time
from urllib.parse import urlencode

import boto3
import pytest
from moto import mock_aws
from moto.core import DEFAULT_ACCOUNT_ID
from moto.sns import sns_backends

from tests.fakes.schwab_server import APP_KEY, APP_SECRET, query_of, redirect_url
from traider.lambdas import auth, watchdog
from traider.schwab.oauth import REFRESH_TOKEN_LIFETIME_S
from traider.schwab.tokens import Grant, SecretsManagerTokenStore

APP_SECRET_ID = "traider/test/schwab-app"
TOKEN_SECRET_ID = "traider/test/schwab-token"
PUBLIC_URL = "https://abc123.execute-api.us-west-2.amazonaws.com"
START_KEY = "start-key-0123456789"
STATE_SECRET = "state-secret-0123456789"
DAY = 86400


@pytest.fixture
def cloud():
    with mock_aws():
        secrets = boto3.client("secretsmanager")
        secrets.create_secret(
            Name=APP_SECRET_ID,
            SecretString=json.dumps({"app_key": APP_KEY, "app_secret": APP_SECRET}),
        )
        secrets.create_secret(Name=TOKEN_SECRET_ID)
        sns = boto3.client("sns")
        topic = sns.create_topic(Name="traider-alerts")["TopicArn"]
        yield {"secrets": secrets, "sns": sns, "topic": topic}


def alerts(cloud) -> list[tuple[str, str]]:
    topic = sns_backends[DEFAULT_ACCOUNT_ID]["us-west-2"].topics[cloud["topic"]]
    return [(n[2], n[1]) for n in topic.sent_notifications]


def stored_grant(cloud) -> Grant | None:
    return SecretsManagerTokenStore(TOKEN_SECRET_ID, cloud["secrets"]).load()


def make_app(cloud, schwab, *, callback_url=f"{PUBLIC_URL}/callback", now=time.time):
    schwab.redirect_uri = callback_url
    settings = auth.Settings(
        app_secret_id=APP_SECRET_ID,
        token_secret_id=TOKEN_SECRET_ID,
        public_url=PUBLIC_URL,
        callback_url=callback_url,
        start_key=START_KEY,
        state_secret=STATE_SECRET,
        alert_topic_arn=cloud["topic"],
        token_url=schwab.token_url,
    )
    return auth.AuthApp(settings, secrets=cloud["secrets"], sns=cloud["sns"], now=now)


def get(path: str, **query) -> dict:
    return {
        "version": "2.0",
        "rawPath": path,
        "queryStringParameters": query or None,
        "requestContext": {"http": {"method": "GET", "path": path}},
    }


def post(path: str, form: dict, *, encoded=False) -> dict:
    body = urlencode(form)
    return {
        "version": "2.0",
        "rawPath": path,
        "requestContext": {"http": {"method": "POST", "path": path}},
        "headers": {"content-type": "application/x-www-form-urlencoded"},
        "body": base64.b64encode(body.encode()).decode() if encoded else body,
        "isBase64Encoded": encoded,
    }


async def call(app, event) -> dict:
    """The handler blocks on HTTP; run it off the loop so the fake Schwab can answer."""
    return await asyncio.to_thread(app.handle, event)


async def start_state(app) -> str:
    response = await call(app, get("/start", k=START_KEY))
    return query_of(response["headers"]["location"])["state"]


# --- /start ----------------------------------------------------------------------------


@pytest.mark.parametrize("query", [{}, {"k": "wrong"}, {"k": ""}])
async def test_start_without_the_right_key_looks_like_nothing_is_there(cloud, schwab, query):
    response = await call(make_app(cloud, schwab), get("/start", **query))
    assert response["statusCode"] == 404
    assert "location" not in response["headers"]


async def test_start_redirects_to_schwab_with_this_apps_details(cloud, schwab):
    response = await call(make_app(cloud, schwab), get("/start", k=START_KEY))
    assert response["statusCode"] == 302
    location = response["headers"]["location"]
    assert location.startswith("https://api.schwabapi.com/v1/oauth/authorize?")
    query = query_of(location)
    assert query["client_id"] == APP_KEY
    assert query["redirect_uri"] == f"{PUBLIC_URL}/callback"
    assert query["response_type"] == "code"
    assert len(query["state"]) > 20


async def test_start_explains_when_the_app_key_has_not_been_stored_yet(cloud, schwab):
    cloud["secrets"].delete_secret(SecretId=APP_SECRET_ID, ForceDeleteWithoutRecovery=True)
    cloud["secrets"].create_secret(Name=APP_SECRET_ID)
    response = await call(make_app(cloud, schwab), get("/start", k=START_KEY))
    assert response["statusCode"] == 503
    assert "app key" in response["body"].lower()


# --- /callback -------------------------------------------------------------------------


async def test_callback_stores_the_new_sign_in(cloud, schwab):
    app = make_app(cloud, schwab)
    state = await start_state(app)
    before = int(time.time())
    response = await call(app, get("/callback", code=schwab.issue_auth_code(), state=state))
    assert response["statusCode"] == 200
    grant = stored_grant(cloud)
    assert grant.refresh_token in schwab.refresh_tokens
    assert before <= grant.issued_at <= int(time.time())
    assert grant.expires_at == grant.issued_at + REFRESH_TOKEN_LIFETIME_S


async def test_callback_page_says_when_the_sign_in_expires_but_shows_no_token(cloud, schwab):
    app = make_app(cloud, schwab)
    response = await call(
        app, get("/callback", code=schwab.issue_auth_code(), state=await start_state(app))
    )
    grant = stored_grant(cloud)
    assert grant.refresh_token not in response["body"]
    assert "7 days" in response["body"]
    for token in schwab.access_tokens:
        assert token not in response["body"]


async def test_successful_sign_in_is_announced(cloud, schwab):
    app = make_app(cloud, schwab)
    await call(app, get("/callback", code=schwab.issue_auth_code(), state=await start_state(app)))
    ((subject, message),) = alerts(cloud)
    assert "sign-in" in subject.lower()
    assert stored_grant(cloud).refresh_token not in message


async def test_a_second_sign_in_replaces_the_first(cloud, schwab):
    app = make_app(cloud, schwab)
    await call(app, get("/callback", code=schwab.issue_auth_code(), state=await start_state(app)))
    first = stored_grant(cloud)
    await call(app, get("/callback", code=schwab.issue_auth_code(), state=await start_state(app)))
    second = stored_grant(cloud)
    assert second.refresh_token != first.refresh_token
    assert second.grant_id != first.grant_id


async def refused(cloud, schwab, app, event) -> dict:
    response = await call(app, event)
    assert response["statusCode"] == 400
    assert stored_grant(cloud) is None
    assert schwab.calls("POST", "/v1/oauth/token") == []  # Schwab was never contacted
    return response


async def test_callback_without_state_is_refused(cloud, schwab):
    app = make_app(cloud, schwab)
    await refused(cloud, schwab, app, get("/callback", code=schwab.issue_auth_code()))


async def test_callback_with_a_tampered_state_is_refused(cloud, schwab):
    app = make_app(cloud, schwab)
    state = await start_state(app)
    tampered = state[:-2] + ("aa" if not state.endswith("aa") else "bb")
    await refused(cloud, schwab, app, get("/callback", code="c", state=tampered))


async def test_callback_with_a_state_from_another_deployment_is_refused(cloud, schwab):
    other = make_app(cloud, schwab)
    other.settings = auth.Settings(**{**other.settings.__dict__, "state_secret": "different"})
    foreign_state = await start_state(other)
    app = make_app(cloud, schwab)
    await refused(cloud, schwab, app, get("/callback", code="c", state=foreign_state))


async def test_callback_with_an_expired_state_is_refused(cloud, schwab):
    clock = {"now": time.time()}
    app = make_app(cloud, schwab, now=lambda: clock["now"])
    state = await start_state(app)
    clock["now"] += 11 * 60  # the link is good for ten minutes
    await refused(cloud, schwab, app, get("/callback", code="c", state=state))


async def test_state_is_still_valid_just_inside_its_lifetime(cloud, schwab):
    clock = {"now": time.time()}
    app = make_app(cloud, schwab, now=lambda: clock["now"])
    state = await start_state(app)
    clock["now"] += 9 * 60
    response = await call(app, get("/callback", code=schwab.issue_auth_code(), state=state))
    assert response["statusCode"] == 200


async def test_callback_without_a_code_is_refused(cloud, schwab):
    app = make_app(cloud, schwab)
    await refused(cloud, schwab, app, get("/callback", state=await start_state(app)))


async def test_code_schwab_does_not_accept_stores_nothing(cloud, schwab):
    app = make_app(cloud, schwab)
    response = await call(app, get("/callback", code="bogus@", state=await start_state(app)))
    assert response["statusCode"] == 502
    assert stored_grant(cloud) is None
    assert alerts(cloud) == []


async def test_error_text_from_a_stranger_is_not_shown_at_all(cloud, schwab):
    app = make_app(cloud, schwab)
    response = await call(
        app, get("/callback", error="access_denied", error_description="Call 555-0100", state="s")
    )
    assert response["statusCode"] == 400
    assert "555-0100" not in response["body"] and "access_denied" not in response["body"]


async def test_error_from_schwab_is_shown_safely(cloud, schwab):
    app = make_app(cloud, schwab)
    state = await start_state(app)
    response = await call(
        app,
        get("/callback", error="<script>alert(1)</script>", error_description="x<b>y", state=state),
    )
    assert response["statusCode"] == 400
    assert "<script>" not in response["body"]
    assert "<b>" not in response["body"]
    assert "&lt;script&gt;" in response["body"]


# --- paste mode: Schwab redirects to https://127.0.0.1 and the user pastes the address -------


async def test_start_page_in_paste_mode_offers_the_link_and_a_form(cloud, schwab):
    app = make_app(cloud, schwab, callback_url="https://127.0.0.1")
    response = await call(app, get("/start", k=START_KEY))
    assert response["statusCode"] == 200
    assert response["headers"]["content-type"].startswith("text/html")
    body = response["body"]
    assert "https://api.schwabapi.com/v1/oauth/authorize?" in body
    assert "redirect_uri=https%3A%2F%2F127.0.0.1" in body
    assert 'action="/exchange"' in body and 'method="post"' in body


async def paste_state(app) -> str:
    body = (await call(app, get("/start", k=START_KEY)))["body"]
    link = body.split('href="', 1)[1].split('"', 1)[0].replace("&amp;", "&")
    return query_of(link)["state"]


@pytest.mark.parametrize("encoded", [False, True])
async def test_pasted_redirect_address_completes_the_sign_in(cloud, schwab, encoded):
    app = make_app(cloud, schwab, callback_url="https://127.0.0.1")
    state = await paste_state(app)
    pasted = redirect_url(schwab, schwab.issue_auth_code(), state=state)
    response = await call(
        app, post("/exchange", {"k": START_KEY, "redirect": pasted}, encoded=encoded)
    )
    assert response["statusCode"] == 200
    assert stored_grant(cloud).refresh_token in schwab.refresh_tokens
    (request,) = schwab.calls("POST", "/v1/oauth/token")
    assert request["form"]["redirect_uri"] == "https://127.0.0.1"


async def test_paste_without_the_key_is_refused(cloud, schwab):
    app = make_app(cloud, schwab, callback_url="https://127.0.0.1")
    pasted = redirect_url(schwab, schwab.issue_auth_code(), state=await paste_state(app))
    response = await call(app, post("/exchange", {"k": "wrong", "redirect": pasted}))
    assert response["statusCode"] == 404
    assert stored_grant(cloud) is None


async def test_paste_with_a_forged_state_is_refused(cloud, schwab):
    app = make_app(cloud, schwab, callback_url="https://127.0.0.1")
    pasted = redirect_url(schwab, schwab.issue_auth_code(), state="forged.state")
    response = await call(app, post("/exchange", {"k": START_KEY, "redirect": pasted}))
    assert response["statusCode"] == 400
    assert stored_grant(cloud) is None


async def test_paste_of_something_that_is_not_the_redirect_address_is_explained(cloud, schwab):
    app = make_app(cloud, schwab, callback_url="https://127.0.0.1")
    response = await call(
        app, post("/exchange", {"k": START_KEY, "redirect": "https://www.schwab.com/"})
    )
    assert response["statusCode"] == 400
    assert "code" in response["body"].lower()


# --- everything else ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event",
    [get("/"), get("/admin"), get("/exchange", k=START_KEY), post("/start", {"k": START_KEY})],
)
async def test_other_paths_and_methods_are_not_found(cloud, schwab, event):
    assert (await call(make_app(cloud, schwab), event))["statusCode"] == 404


async def test_every_page_forbids_caching_and_referrers(cloud, schwab):
    app = make_app(cloud, schwab)
    pages = [
        await call(app, get("/start", k=START_KEY)),
        await call(app, get("/start")),
        await call(
            app, get("/callback", code=schwab.issue_auth_code(), state=await start_state(app))
        ),
    ]
    for page in pages:
        assert page["headers"]["cache-control"] == "no-store"
        assert page["headers"]["referrer-policy"] == "no-referrer"


def test_handler_builds_itself_from_the_environment(cloud, monkeypatch):
    env = {
        "APP_SECRET_ID": APP_SECRET_ID,
        "TOKEN_SECRET_ID": TOKEN_SECRET_ID,
        "PUBLIC_URL": PUBLIC_URL + "/",
        "CALLBACK_URL": f"{PUBLIC_URL}/callback",
        "START_KEY": START_KEY,
        "STATE_SECRET": STATE_SECRET,
        "ALERT_TOPIC_ARN": cloud["topic"],
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(auth, "_app", None)
    response = auth.handler(get("/start", k=START_KEY), None)
    assert response["statusCode"] == 302
    assert query_of(response["headers"]["location"])["redirect_uri"] == f"{PUBLIC_URL}/callback"


# --- the expiry watchdog --------------------------------------------------------------------------

REAUTH = "https://abc123.execute-api.us-west-2.amazonaws.com/start?k=start-key"


def run_watchdog(cloud, *, now, warn_hours=48):
    settings = watchdog.Settings(
        token_secret_id=TOKEN_SECRET_ID,
        alert_topic_arn=cloud["topic"],
        reauth_url=REAUTH,
        warn_hours=warn_hours,
    )
    return watchdog.check(settings, secrets=cloud["secrets"], sns=cloud["sns"], now=now)


def save_grant(cloud, issued_at: int) -> None:
    grant = Grant("refresh-x", issued_at, issued_at + REFRESH_TOKEN_LIFETIME_S, "g")
    SecretsManagerTokenStore(TOKEN_SECRET_ID, cloud["secrets"]).save(grant)


def test_watchdog_is_quiet_while_the_sign_in_has_days_left(cloud):
    save_grant(cloud, 1_000_000)
    result = run_watchdog(cloud, now=1_000_000 + 3 * DAY)
    assert result["status"] == "ok"
    assert alerts(cloud) == []


def test_watchdog_warns_when_expiry_is_near_and_includes_the_link(cloud):
    save_grant(cloud, 1_000_000)
    result = run_watchdog(cloud, now=1_000_000 + 7 * DAY - 30 * 3600)
    assert result["status"] == "expiring"
    ((subject, message),) = alerts(cloud)
    assert "30 hours" in subject
    assert REAUTH in message


def test_watchdog_warning_starts_exactly_at_the_threshold(cloud):
    save_grant(cloud, 1_000_000)
    assert run_watchdog(cloud, now=1_000_000 + 7 * DAY - 48 * 3600 - 60)["status"] == "ok"
    assert run_watchdog(cloud, now=1_000_000 + 7 * DAY - 48 * 3600)["status"] == "expiring"


def test_watchdog_reports_an_expired_sign_in(cloud):
    save_grant(cloud, 1_000_000)
    result = run_watchdog(cloud, now=1_000_000 + 7 * DAY + 5)
    assert result["status"] == "expired"
    ((subject, message),) = alerts(cloud)
    assert "expired" in subject.lower()
    assert REAUTH in message


def test_watchdog_reports_that_nobody_has_signed_in_yet(cloud):
    result = run_watchdog(cloud, now=1_000_000)
    assert result["status"] == "missing"
    ((_, message),) = alerts(cloud)
    assert REAUTH in message


def test_watchdog_reports_a_secret_it_cannot_read(cloud):
    cloud["secrets"].put_secret_value(SecretId=TOKEN_SECRET_ID, SecretString="{broken")
    result = run_watchdog(cloud, now=1_000_000)
    assert result["status"] == "error"
    ((subject, _),) = alerts(cloud)
    assert "cannot" in subject.lower()


def test_watchdog_never_puts_the_token_in_an_alert(cloud):
    save_grant(cloud, 1_000_000)
    run_watchdog(cloud, now=1_000_000 + 7 * DAY - 3600)
    ((subject, message),) = alerts(cloud)
    assert "refresh-x" not in subject + message


def test_watchdog_handler_reads_its_settings_from_the_environment(cloud, monkeypatch):
    monkeypatch.setenv("TOKEN_SECRET_ID", TOKEN_SECRET_ID)
    monkeypatch.setenv("ALERT_TOPIC_ARN", cloud["topic"])
    monkeypatch.setenv("REAUTH_URL", REAUTH)
    monkeypatch.setenv("WARN_HOURS", "24")
    assert watchdog.handler({}, None)["status"] == "missing"


# --- packaging boundary ------------------------------------------------------------


def test_lambda_code_imports_only_the_standard_library_and_boto():
    """The functions are deployed as plain source with no dependencies bundled, so they
    must not import anything the Lambda runtime does not already have."""
    script = """
import importlib.abc, sys
ALLOWED = {"boto3", "botocore"}
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        top = name.split(".")[0]
        if top in ("aiohttp", "pydantic", "pydantic_core", "tzdata", "zoneinfo", "moto"):
            raise ImportError(f"{name} is not available in the Lambda runtime")
        return None
sys.meta_path.insert(0, Block())
import traider.lambdas.auth, traider.lambdas.watchdog
print(sorted(m for m in sys.modules if m.startswith("traider")))
"""
    result = subprocess.run(  # noqa: S603 - fixed arguments, our own interpreter
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    loaded = set(json.loads(result.stdout.replace("'", '"')))
    assert loaded == {
        "traider",
        "traider.alerts",
        "traider.lambdas",
        "traider.lambdas.auth",
        "traider.lambdas.common",
        "traider.lambdas.watchdog",
        "traider.schwab",
        "traider.schwab.oauth",
        "traider.schwab.tokens",
    }
