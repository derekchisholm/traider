import pytest


@pytest.fixture(autouse=True)
def _fake_aws_environment(monkeypatch):
    """Tests never touch real AWS. moto intercepts calls; these stop boto3 looking elsewhere."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@pytest.fixture
async def schwab():
    """A fake Schwab API listening on localhost."""
    from tests.fakes.schwab_server import FakeSchwab

    server = FakeSchwab()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
async def signed_in(schwab):
    """A token manager holding a valid sign-in against the fake Schwab."""
    import time

    from tests.fakes.schwab_server import APP_KEY, APP_SECRET
    from traider.schwab.oauth import REFRESH_TOKEN_LIFETIME_S, AppCredentials
    from traider.schwab.tokens import Grant, MemoryTokenStore, StaticCredentials, TokenManager
    from traider.timeutil import SystemClock

    now = int(time.time())
    grant = Grant(schwab.seed_refresh_token(), now, now + REFRESH_TOKEN_LIFETIME_S, "g-test")
    return TokenManager(
        store=MemoryTokenStore(grant),
        credentials=StaticCredentials(AppCredentials(APP_KEY, APP_SECRET)),
        clock=SystemClock(),
        token_url=schwab.token_url,
    )


@pytest.fixture
async def client(schwab, signed_in):
    """A Schwab REST client pointed at the fake, with short retry delays."""
    import aiohttp

    from traider.schwab.client import SchwabClient

    async with aiohttp.ClientSession() as session:
        yield SchwabClient(session, signed_in, base_url=schwab.base_url, backoff_s=0.01)


@pytest.fixture
async def finnhub():
    """A fake Finnhub API listening on localhost."""
    from tests.fakes.finnhub_server import FakeFinnhub

    server = FakeFinnhub()
    await server.start()
    yield server
    await server.stop()
