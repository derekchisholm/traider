import asyncio
import json
import stat
from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws

from tests.fakes.schwab_server import APP_KEY, APP_SECRET
from traider.schwab.oauth import REFRESH_TOKEN_LIFETIME_S, AppCredentials, TokenResponse
from traider.schwab.tokens import (
    AuthState,
    AuthUnavailable,
    CredentialsError,
    FileTokenStore,
    Grant,
    MemoryTokenStore,
    SecretsManagerCredentials,
    SecretsManagerTokenStore,
    StaticCredentials,
    TokenManager,
    TokenStoreError,
    new_grant,
)
from traider.timeutil import ManualClock

T0 = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)
EPOCH = int(T0.timestamp())
CREDS = AppCredentials(APP_KEY, APP_SECRET)
DAY = 86400


def grant(token="refresh-1", issued_at=EPOCH) -> Grant:
    return Grant(
        refresh_token=token,
        issued_at=issued_at,
        expires_at=issued_at + REFRESH_TOKEN_LIFETIME_S,
        grant_id="g-1",
    )


# --- grants ------------------------------------------------------------------------


def test_new_grant_expires_seven_days_after_the_sign_in():
    made = new_grant(TokenResponse("access", "refresh-9", 1800), now_epoch=EPOCH)
    assert (made.refresh_token, made.issued_at, made.expires_at) == (
        "refresh-9",
        EPOCH,
        EPOCH + 7 * DAY,
    )


def test_each_sign_in_gets_its_own_grant_id():
    response = TokenResponse("access", "refresh-9", 1800)
    assert new_grant(response, EPOCH).grant_id != new_grant(response, EPOCH).grant_id


def test_grant_reports_time_left():
    assert grant().seconds_left(EPOCH + DAY) == 6 * DAY
    assert grant().seconds_left(EPOCH + 8 * DAY) == -DAY


def test_grant_survives_json():
    assert Grant.from_json(grant().to_json()) == grant()


def test_grant_repr_hides_the_refresh_token():
    assert "refresh-1" not in repr(grant())


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        '{"refresh_token": ""}',
        '{"refresh_token": "r"}',
        '{"issued_at": 1, "expires_at": 2}',
    ],
)
def test_malformed_stored_grant_is_an_error(text):
    with pytest.raises(TokenStoreError):
        Grant.from_json(text)


# --- stores: one contract, three implementations ---------------------------------------


@pytest.fixture(params=["memory", "file", "secrets_manager"])
def store(request, tmp_path):
    if request.param == "memory":
        yield MemoryTokenStore()
    elif request.param == "file":
        yield FileTokenStore(tmp_path / "state" / "token.json")
    else:
        with mock_aws():
            client = boto3.client("secretsmanager")
            client.create_secret(Name="traider/test/schwab-token")  # exists, but has no value
            yield SecretsManagerTokenStore("traider/test/schwab-token", client)


def test_store_is_empty_before_the_first_sign_in(store):
    assert store.load() is None


def test_store_returns_what_was_saved(store):
    store.save(grant())
    assert store.load() == grant()


def test_store_keeps_only_the_latest_grant(store):
    store.save(grant("refresh-1"))
    store.save(grant("refresh-2", issued_at=EPOCH + 60))
    assert store.load().refresh_token == "refresh-2"


def test_file_store_is_private_to_the_user(tmp_path):
    path = tmp_path / "token.json"
    FileTokenStore(path).save(grant())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_file_store_reports_a_corrupt_file(tmp_path):
    path = tmp_path / "token.json"
    path.write_text("{oops")
    with pytest.raises(TokenStoreError):
        FileTokenStore(path).load()


def test_secret_that_does_not_exist_reads_as_empty():
    with mock_aws():
        client = boto3.client("secretsmanager")
        assert SecretsManagerTokenStore("missing", client).load() is None


def test_other_secrets_manager_errors_are_not_swallowed():
    class Denied:
        def get_secret_value(self, **_):
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetSecretValue"
            )

    with pytest.raises(TokenStoreError, match="AccessDenied"):
        SecretsManagerTokenStore("x", Denied()).load()


# --- app credentials ---------------------------------------------------------------------


@pytest.fixture
def secrets():
    with mock_aws():
        yield boto3.client("secretsmanager")


def test_app_credentials_are_read_from_the_secret(secrets):
    secrets.create_secret(
        Name="app", SecretString=json.dumps({"app_key": APP_KEY, "app_secret": APP_SECRET})
    )
    assert SecretsManagerCredentials("app", secrets).load() == CREDS


def test_app_credentials_are_absent_until_the_secret_has_a_value(secrets):
    secrets.create_secret(Name="app")
    assert SecretsManagerCredentials("app", secrets).load() is None


@pytest.mark.parametrize(
    "value",
    ["not json", "{}", '{"app_key": "k"}', '{"app_key": "", "app_secret": "s"}', '["k", "s"]'],
)
def test_malformed_app_credentials_are_reported_without_leaking_them(secrets, value):
    secrets.create_secret(Name="app", SecretString=value)
    with pytest.raises(CredentialsError) as caught:
        SecretsManagerCredentials("app", secrets).load()
    assert "app_key" in str(caught.value) and "app_secret" in str(caught.value)


def test_surrounding_whitespace_in_pasted_credentials_is_ignored(secrets):
    secrets.create_secret(
        Name="app",
        SecretString=json.dumps({"app_key": f" {APP_KEY}\n", "app_secret": f"{APP_SECRET} "}),
    )
    assert SecretsManagerCredentials("app", secrets).load() == CREDS


# --- token manager -------------------------------------------------------------------------


class CountingStore(MemoryTokenStore):
    def __init__(self, initial=None):
        super().__init__(initial)
        self.saves = 0
        self.loads = 0

    def save(self, grant):
        self.saves += 1
        super().save(grant)

    def load(self):
        self.loads += 1
        return super().load()


@pytest.fixture
def clock():
    return ManualClock(T0)


@pytest.fixture
def linked(schwab, clock):
    """Tie the fake server's idea of time to the test clock."""
    schwab.now = lambda: clock.now().timestamp()
    return schwab


def manager(schwab, clock, store, credentials=None) -> TokenManager:
    return TokenManager(
        store=store,
        credentials=credentials if credentials is not None else StaticCredentials(CREDS),
        clock=clock,
        token_url=schwab.token_url,
    )


def signed_in_store(schwab, clock) -> CountingStore:
    token = schwab.seed_refresh_token()
    return CountingStore(grant(token, issued_at=int(clock.now().timestamp())))


def token_requests(schwab) -> int:
    return len(schwab.calls("POST", "/v1/oauth/token"))


async def test_access_token_is_obtained_from_the_stored_grant(linked, clock):
    tokens = manager(linked, clock, signed_in_store(linked, clock))
    assert await tokens.access_token() in linked.access_tokens
    assert tokens.state is AuthState.OK


async def test_access_token_is_reused_while_it_is_valid(linked, clock):
    tokens = manager(linked, clock, signed_in_store(linked, clock))
    first = await tokens.access_token()
    clock.advance(20 * 60)
    assert await tokens.access_token() == first
    assert token_requests(linked) == 1


async def test_access_token_is_renewed_shortly_before_it_expires(linked, clock):
    tokens = manager(linked, clock, signed_in_store(linked, clock))
    first = await tokens.access_token()
    clock.advance(26 * 60)  # inside the five-minute safety margin of a 30-minute token
    assert await tokens.access_token() != first


async def test_concurrent_callers_share_one_refresh(linked, clock):
    tokens = manager(linked, clock, signed_in_store(linked, clock))
    results = await asyncio.gather(*(tokens.access_token() for _ in range(5)))
    assert len(set(results)) == 1
    assert token_requests(linked) == 1


async def test_without_app_credentials_there_is_no_token(linked, clock):
    class NoCredentials:
        def load(self):
            return None

    tokens = manager(linked, clock, signed_in_store(linked, clock), NoCredentials())
    with pytest.raises(AuthUnavailable):
        await tokens.access_token()
    assert tokens.state is AuthState.NO_CREDENTIALS


async def test_credentials_added_later_are_picked_up(linked, clock):
    class Late:
        value = None

        def load(self):
            return self.value

    late = Late()
    tokens = manager(linked, clock, signed_in_store(linked, clock), late)
    with pytest.raises(AuthUnavailable):
        await tokens.access_token()
    late.value = CREDS
    clock.advance(61)
    assert await tokens.access_token() in linked.access_tokens


async def test_before_the_first_sign_in_there_is_no_token(linked, clock):
    tokens = manager(linked, clock, CountingStore())
    with pytest.raises(AuthUnavailable):
        await tokens.access_token()
    assert tokens.state is AuthState.NO_GRANT
    assert token_requests(linked) == 0


async def test_a_grant_past_seven_days_is_not_even_tried(linked, clock):
    store = signed_in_store(linked, clock)
    tokens = manager(linked, clock, store)
    clock.advance(7 * DAY + 1)
    with pytest.raises(AuthUnavailable):
        await tokens.access_token()
    assert tokens.state is AuthState.EXPIRED
    assert token_requests(linked) == 0


async def test_grant_rejected_by_schwab_means_sign_in_again(linked, clock):
    tokens = manager(linked, clock, signed_in_store(linked, clock))
    linked.revoke_refresh_tokens()
    with pytest.raises(AuthUnavailable):
        await tokens.access_token()
    assert tokens.state is AuthState.EXPIRED


async def test_rejected_grant_is_not_retried_on_every_call(linked, clock):
    tokens = manager(linked, clock, signed_in_store(linked, clock))
    linked.revoke_refresh_tokens()
    for _ in range(5):
        with pytest.raises(AuthUnavailable):
            await tokens.access_token()
        clock.advance(1)
    assert token_requests(linked) == 1


async def test_new_sign_in_after_expiry_is_picked_up_without_a_restart(linked, clock):
    store = signed_in_store(linked, clock)
    tokens = manager(linked, clock, store)
    linked.revoke_refresh_tokens()
    with pytest.raises(AuthUnavailable):
        await tokens.access_token()

    store.save(grant(linked.seed_refresh_token(), issued_at=int(clock.now().timestamp())))
    clock.advance(31)
    await tokens.poll()
    assert tokens.state is AuthState.OK
    assert await tokens.access_token() in linked.access_tokens


async def test_new_sign_in_replacing_a_live_grant_is_adopted_when_the_old_one_stops_working(
    linked, clock
):
    store = signed_in_store(linked, clock)
    tokens = manager(linked, clock, store)
    await tokens.access_token()

    # The user signs in again early. Schwab voids the old refresh token.
    clock.advance(DAY)
    store.save(grant(linked.seed_refresh_token(), issued_at=int(clock.now().timestamp())))
    linked.expire_access_tokens()
    clock.advance(31 * 60)
    assert await tokens.access_token() in linked.access_tokens
    assert tokens.seconds_left() == pytest.approx(7 * DAY - 31 * 60)


async def test_sign_in_renewed_just_before_expiry_is_used_the_moment_the_old_one_runs_out(
    linked, clock
):
    store = signed_in_store(linked, clock)
    tokens = manager(linked, clock, store)
    await tokens.access_token()

    # An hour before the seven days are up the user signs in again. Nothing has told
    # the bot yet when its own copy of the old sign-in expires.
    clock.advance(7 * DAY - 3600)
    store.save(grant(linked.seed_refresh_token(), issued_at=int(clock.now().timestamp())))
    clock.advance(3601)
    assert await tokens.access_token() in linked.access_tokens
    assert tokens.state is AuthState.OK


async def test_early_sign_in_is_noticed_by_the_periodic_poll(linked, clock):
    store = signed_in_store(linked, clock)
    tokens = manager(linked, clock, store)
    await tokens.access_token()
    clock.advance(3 * DAY)
    store.save(grant(linked.seed_refresh_token(), issued_at=int(clock.now().timestamp())))
    clock.advance(301)
    await tokens.poll()
    assert tokens.seconds_left() == pytest.approx(7 * DAY - 301)


async def test_an_older_grant_in_the_store_is_ignored(linked, clock):
    store = signed_in_store(linked, clock)
    tokens = manager(linked, clock, store)
    await tokens.access_token()
    # Say someone restores a previous version of the secret.
    store.save(grant("refresh-from-last-week", issued_at=int(clock.now().timestamp()) - 3 * DAY))
    clock.advance(301)
    await tokens.poll()
    assert tokens.seconds_left() == pytest.approx(7 * DAY - 301)


async def test_temporary_schwab_outage_keeps_the_grant(linked, clock):
    tokens = manager(linked, clock, signed_in_store(linked, clock))
    linked.fail("POST", "/v1/oauth/token", 503)
    with pytest.raises(AuthUnavailable):
        await tokens.access_token()
    assert tokens.state is AuthState.ERROR
    clock.advance(31)
    assert await tokens.access_token() in linked.access_tokens
    assert tokens.state is AuthState.OK


async def test_outage_does_not_cause_a_request_storm(linked, clock):
    tokens = manager(linked, clock, signed_in_store(linked, clock))
    linked.fail("POST", "/v1/oauth/token", 503, times=100)
    for _ in range(10):
        with pytest.raises(AuthUnavailable):
            await tokens.access_token()
        clock.advance(1)
    assert token_requests(linked) == 1


async def test_a_still_valid_token_keeps_working_through_a_refresh_outage(linked, clock):
    tokens = manager(linked, clock, signed_in_store(linked, clock))
    first = await tokens.access_token()
    clock.advance(26 * 60)  # refresh is due, but the token has four minutes left
    linked.fail("POST", "/v1/oauth/token", 503)
    assert await tokens.access_token() == first


async def test_token_rejected_by_the_api_is_replaced(linked, clock):
    tokens = manager(linked, clock, signed_in_store(linked, clock))
    first = await tokens.access_token()
    await tokens.invalidate(first)
    assert await tokens.access_token() != first


async def test_invalidating_an_old_token_does_not_discard_the_current_one(linked, clock):
    tokens = manager(linked, clock, signed_in_store(linked, clock))
    first = await tokens.access_token()
    await tokens.invalidate(first)
    second = await tokens.access_token()
    await tokens.invalidate(first)  # a late 401 from a request that used the old token
    assert await tokens.access_token() == second


async def test_bot_does_not_write_the_store_when_the_refresh_token_is_unchanged(linked, clock):
    store = signed_in_store(linked, clock)
    tokens = manager(linked, clock, store)
    await tokens.access_token()
    clock.advance(26 * 60)
    await tokens.access_token()
    assert store.saves == 0


async def test_rotated_refresh_token_is_saved_without_resetting_the_seven_days(linked, clock):
    linked.rotate_refresh_token = True
    store = signed_in_store(linked, clock)
    original = store.load()
    tokens = manager(linked, clock, store)
    clock.advance(DAY)
    await tokens.access_token()
    saved = store.load()
    assert saved.refresh_token != original.refresh_token
    assert saved.refresh_token in linked.refresh_tokens
    assert (saved.issued_at, saved.expires_at) == (original.issued_at, original.expires_at)


async def test_rotated_token_keeps_working_for_the_next_refresh(linked, clock):
    linked.rotate_refresh_token = True
    tokens = manager(linked, clock, signed_in_store(linked, clock))
    first = await tokens.access_token()
    clock.advance(26 * 60)
    assert await tokens.access_token() != first


async def test_rotation_never_overwrites_a_newer_sign_in(linked, clock):
    linked.rotate_refresh_token = True
    store = signed_in_store(linked, clock)
    tokens = manager(linked, clock, store)
    # The bot has loaded the grant but not refreshed yet (Schwab was briefly down).
    linked.fail("POST", "/v1/oauth/token", 503)
    with pytest.raises(AuthUnavailable):
        await tokens.access_token()

    # A new sign-in lands in the store before the bot's next refresh.
    newer = grant("refresh-from-new-sign-in", issued_at=int(clock.now().timestamp()) + 5)
    store.save(newer)
    store.saves = 0

    clock.advance(31)
    await tokens.access_token()  # refreshes with the old grant, which Schwab rotates
    assert store.saves == 0
    assert store.load() == newer


async def test_time_left_is_zero_before_any_sign_in(linked, clock):
    assert manager(linked, clock, CountingStore()).seconds_left() == 0


async def test_time_left_counts_down_to_the_seven_day_limit(linked, clock):
    tokens = manager(linked, clock, signed_in_store(linked, clock))
    await tokens.access_token()
    clock.advance(2 * DAY)
    assert tokens.seconds_left() == 5 * DAY


async def test_poll_keeps_the_access_token_fresh_in_the_background(linked, clock):
    tokens = manager(linked, clock, signed_in_store(linked, clock))
    first = await tokens.access_token()
    clock.advance(26 * 60)
    await tokens.poll()
    assert token_requests(linked) == 2
    assert await tokens.access_token() != first


async def test_poll_never_raises(linked, clock):
    tokens = manager(linked, clock, CountingStore())
    await tokens.poll()
    assert tokens.state is AuthState.NO_GRANT


async def test_unreadable_store_is_reported_as_an_error_state(linked, clock):
    class Broken:
        def load(self):
            raise TokenStoreError("AccessDeniedException")

        def save(self, _grant):
            raise TokenStoreError("AccessDeniedException")

    tokens = manager(linked, clock, Broken())
    with pytest.raises(AuthUnavailable):
        await tokens.access_token()
    assert tokens.state is AuthState.ERROR
