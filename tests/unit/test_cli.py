import asyncio
import io
import os
import subprocess
import sys
import time

import pytest

from tests.fakes.schwab_server import (
    APP_KEY,
    APP_SECRET,
    SECOND_ACCOUNT_HASH,
    SECOND_ACCOUNT_NUMBER,
    query_of,
    redirect_url,
)
from traider import cli
from traider.config import Config, RiskLimits
from traider.schwab.oauth import REFRESH_TOKEN_LIFETIME_S
from traider.schwab.tokens import FileTokenStore, Grant


def config(tmp_path, **overrides) -> Config:
    fields = {
        "symbols": ("SPY", "QQQ"),
        "schwab_app_key": APP_KEY,
        "schwab_app_secret": APP_SECRET,
        "schwab_token_file": str(tmp_path / "token.json"),
        "schwab_callback_url": "https://127.0.0.1",
    }
    fields.update(overrides)
    return Config.model_validate(fields)


def sign_in(schwab, tmp_path) -> None:
    now = int(time.time())
    grant = Grant(schwab.seed_refresh_token(), now, now + REFRESH_TOKEN_LIFETIME_S, "g")
    FileTokenStore(tmp_path / "token.json").save(grant)


async def run_check(schwab, cfg) -> tuple[int, str]:
    out = io.StringIO()
    code = await cli.check(cfg, out, schwab_base_url=schwab.base_url, token_url=schwab.token_url)
    return code, out.getvalue()


# --- check ----------------------------------------------------------------------------


async def test_check_passes_and_reports_what_the_bot_would_see(schwab, tmp_path):
    sign_in(schwab, tmp_path)
    schwab.set_quote("SPY", 512.30, 512.34)
    schwab.set_quote("QQQ", 440.10, 440.15)
    schwab.positions["SPY"] = (3.0, 500.0)
    code, text = await run_check(schwab, config(tmp_path))
    assert code == 0, text
    assert "...5678" in text  # the account, masked
    assert "12345678" not in text
    assert "11,536.90" in text  # equity: 10,000 cash plus 3 shares at the 512.30 bid
    assert "10,000.00" in text  # cash the bot may spend
    assert "SPY" in text and "512.30" in text and "512.34" in text
    assert "QQQ" in text and "440.10" in text
    assert "09:30" in text and "16:00" in text  # today's session, New York time
    assert "FAIL" not in text


async def test_check_only_reads(schwab, tmp_path):
    sign_in(schwab, tmp_path)
    schwab.set_quote("SPY", 512.30, 512.34)
    schwab.set_quote("QQQ", 440.10, 440.15)
    await run_check(schwab, config(tmp_path))
    writes = [r for r in schwab.requests if r["method"] != "GET" and r["path"] != "/v1/oauth/token"]
    assert writes == []
    assert schwab.sockets == [] and schwab.stream_logins == 0


async def test_check_never_prints_secrets(schwab, tmp_path):
    sign_in(schwab, tmp_path)
    _, text = await run_check(schwab, config(tmp_path))
    assert APP_SECRET not in text and APP_KEY not in text
    for token in [*schwab.refresh_tokens, *schwab.access_tokens]:
        assert token not in text


async def test_check_fails_clearly_when_not_signed_in(schwab, tmp_path):
    code, text = await run_check(schwab, config(tmp_path, reauth_url="https://x.test/start?k=1"))
    assert code == 1
    assert "FAIL" in text
    assert "sign in" in text.lower()
    assert "https://x.test/start?k=1" in text


async def test_check_fails_when_the_app_key_is_missing(schwab, tmp_path):
    cfg = config(tmp_path, schwab_app_key=None, schwab_app_secret=None)
    code, text = await run_check(schwab, cfg)
    assert code == 1
    assert "app key" in text.lower()


async def test_check_fails_on_delayed_quotes_because_the_bot_would_not_trade(schwab, tmp_path):
    sign_in(schwab, tmp_path)
    schwab.set_quote("SPY", 512.30, 512.34, realtime=False)
    schwab.set_quote("QQQ", 440.10, 440.15)
    code, text = await run_check(schwab, config(tmp_path))
    assert code == 1
    assert "delayed" in text.lower()


async def test_check_fails_when_a_symbol_has_no_quote(schwab, tmp_path):
    sign_in(schwab, tmp_path)
    schwab.set_quote("SPY", 512.30, 512.34)
    code, text = await run_check(schwab, config(tmp_path))
    assert code == 1
    assert "QQQ" in text


async def test_check_fails_when_the_account_is_ambiguous(schwab, tmp_path):
    sign_in(schwab, tmp_path)
    schwab.accounts[SECOND_ACCOUNT_HASH] = SECOND_ACCOUNT_NUMBER
    code, text = await run_check(schwab, config(tmp_path))
    assert code == 1
    assert "2 accounts" in text
    assert "...4321" in text


async def checked(schwab, tmp_path, *, account_type, **risk) -> tuple[int, str]:
    sign_in(schwab, tmp_path)
    schwab.set_quote("SPY", 512.30, 512.34)
    schwab.set_quote("QQQ", 440.10, 440.15)
    schwab.account_type = account_type
    return await run_check(schwab, config(tmp_path, risk=RiskLimits(**risk)))


async def test_check_confirms_a_cash_account_only_spends_settled_cash(schwab, tmp_path):
    code, text = await checked(schwab, tmp_path, account_type="CASH")
    assert code == 0, text
    assert "ok    account type: cash" in text


async def test_check_fails_when_a_cash_account_would_spend_unsettled_money(schwab, tmp_path):
    code, text = await checked(schwab, tmp_path, account_type="CASH", settled_cash_only=False)
    assert code == 1
    assert "FAIL  account type: cash" in text
    assert "good-faith violation" in text
    assert "settled_cash_only" in text


async def test_check_fails_a_cash_account_that_does_not_check_cash_at_all(schwab, tmp_path):
    # With require_cash off the settled-cash rule has nothing to work with.
    code, text = await checked(schwab, tmp_path, account_type="CASH", require_cash=False)
    assert code == 1
    assert "FAIL  account type: cash" in text


async def test_check_tells_a_margin_account_the_settled_cash_rule_is_optional(schwab, tmp_path):
    code, text = await checked(schwab, tmp_path, account_type="MARGIN")
    assert code == 0, text
    assert "ok    account type: margin" in text
    assert "settled_cash_only" in text  # how to let it reuse the day's proceeds


async def test_check_has_nothing_to_add_for_a_margin_account_with_the_rule_off(schwab, tmp_path):
    code, text = await checked(schwab, tmp_path, account_type="MARGIN", settled_cash_only=False)
    assert code == 0, text
    assert "ok    account type: margin" in text
    assert "settled_cash_only" not in text


async def test_check_fails_safe_when_the_account_type_is_unknown(schwab, tmp_path):
    code, text = await checked(schwab, tmp_path, account_type=None, settled_cash_only=False)
    assert code == 1
    assert "FAIL  account type" in text
    code, text = await checked(schwab, tmp_path, account_type=None)
    assert code == 0, text


CALL = "SPY   261016C00500000"


async def options_check(schwab, tmp_path, *, allow=True) -> tuple[int, str]:
    sign_in(schwab, tmp_path)
    schwab.set_quote("SPY", 512.30, 512.34)
    schwab.set_quote("QQQ", 440.10, 440.15)
    return await run_check(schwab, config(tmp_path, risk=RiskLimits(allow_options=allow)))


async def test_check_reads_an_option_chain_and_quote_when_options_are_on(schwab, tmp_path):
    schwab.add_option(CALL, 2.00, 2.10)
    code, text = await options_check(schwab, tmp_path)
    assert code == 0, text
    assert "1 contracts for SPY" in text
    assert CALL in text and "2.00 x 2.10" in text


async def test_check_does_not_touch_options_while_they_are_off(schwab, tmp_path):
    schwab.add_option(CALL, 2.00, 2.10)
    code, text = await options_check(schwab, tmp_path, allow=False)
    assert code == 0, text
    assert "option" not in text.lower()
    assert schwab.calls("GET", "/marketdata/v1/chains") == []


async def test_check_fails_when_the_option_chain_is_empty(schwab, tmp_path):
    code, text = await options_check(schwab, tmp_path)
    assert code == 1
    assert "FAIL" in text and "no option contracts" in text


async def test_check_fails_when_the_option_chain_cannot_be_read(schwab, tmp_path):
    schwab.fail("GET", "/marketdata/v1/chains", 500, times=3)
    code, text = await options_check(schwab, tmp_path)
    assert code == 1
    assert "option chain" in text and "FAIL" in text


async def test_check_fails_on_delayed_option_quotes(schwab, tmp_path):
    schwab.add_option(CALL, 2.00, 2.10, realtime=False)
    code, text = await options_check(schwab, tmp_path)
    assert code == 1
    assert "delayed" in text


async def test_check_fails_on_an_option_quote_schwab_does_not_call_normal(schwab, tmp_path):
    schwab.add_option(CALL, 2.00, 2.10)
    schwab.quotes[CALL]["quote"]["securityStatus"] = "Unknown"
    code, text = await options_check(schwab, tmp_path)
    assert code == 1
    assert "Unknown" in text and "would not trade" in text


async def test_check_fails_on_an_option_with_no_bid(schwab, tmp_path):
    schwab.add_option(CALL, 0.0, 0.05)
    code, text = await options_check(schwab, tmp_path)
    assert code == 1
    assert "no bid" in text


async def test_check_fails_when_an_option_has_no_quote(schwab, tmp_path):
    schwab.add_option(CALL, 2.00, 2.10)
    del schwab.quotes[CALL]
    code, text = await options_check(schwab, tmp_path)
    assert code == 1
    assert "no quote" in text


async def test_check_keeps_going_after_a_failed_step(schwab, tmp_path):
    sign_in(schwab, tmp_path)
    schwab.set_quote("SPY", 512.30, 512.34)
    schwab.set_quote("QQQ", 440.10, 440.15)
    schwab.fail("GET", "/markets", 503, times=3)
    code, text = await run_check(schwab, config(tmp_path))
    assert code == 1
    assert "512.30" in text  # quotes were still checked


# --- login ----------------------------------------------------------------------------------


async def run_login(schwab, cfg, answer) -> tuple[int, str]:
    out = io.StringIO()
    code = await asyncio.to_thread(
        cli.login,
        cfg,
        out,
        read_line=lambda _prompt: answer(out.getvalue()),
        token_url=schwab.token_url,
    )
    return code, out.getvalue()


def pasted_redirect(schwab, **kwargs):
    """Simulates the user: reads the link the CLI printed, signs in, pastes the redirect."""

    def answer(printed: str) -> str:
        link = next(word for word in printed.split() if word.startswith("https://api.schwabapi"))
        state = kwargs.get("state", query_of(link)["state"])
        return redirect_url(schwab, schwab.issue_auth_code(), state=state)

    return answer


async def test_login_prints_the_link_and_stores_the_sign_in(schwab, tmp_path):
    code, text = await run_login(schwab, config(tmp_path), pasted_redirect(schwab))
    assert code == 0, text
    link = next(word for word in text.split() if word.startswith("https://api.schwabapi"))
    assert query_of(link)["client_id"] == APP_KEY
    assert query_of(link)["redirect_uri"] == "https://127.0.0.1"
    grant = FileTokenStore(tmp_path / "token.json").load()
    assert grant.refresh_token in schwab.refresh_tokens
    assert grant.refresh_token not in text


async def test_login_rejects_a_paste_that_is_not_the_redirect(schwab, tmp_path):
    code, _ = await run_login(schwab, config(tmp_path), lambda _printed: "hello")
    assert code == 1
    assert FileTokenStore(tmp_path / "token.json").load() is None


async def test_login_rejects_a_redirect_from_a_different_attempt(schwab, tmp_path):
    code, text = await run_login(schwab, config(tmp_path), pasted_redirect(schwab, state="other"))
    assert code == 1
    assert "state" in text.lower()
    assert FileTokenStore(tmp_path / "token.json").load() is None


async def test_login_needs_somewhere_to_store_the_sign_in(schwab, tmp_path):
    cfg = config(tmp_path, schwab_token_file=None)
    code, text = await run_login(schwab, cfg, pasted_redirect(schwab))
    assert code == 1
    assert "TRAIDER_SCHWAB_TOKEN" in text


async def test_login_reports_a_code_schwab_refuses(schwab, tmp_path):
    def stale(_printed: str) -> str:
        return "https://127.0.0.1/?code=expired%40&session=x"

    code, text = await run_login(schwab, config(tmp_path), stale)
    assert code == 1
    assert "30 seconds" in text


# --- backtest and dispatch ---------------------------------------------------------------------


async def test_history_for_a_backtest_can_be_downloaded_from_schwab(schwab, tmp_path):
    sign_in(schwab, tmp_path)
    minute = (int(time.time()) // 60 - 10) * 60 * 1000
    schwab.candles["SPY"] = [
        {"open": 1, "high": 1, "low": 1, "close": 1, "volume": 5, "datetime": minute + i * 60000}
        for i in range(3)
    ]
    bars = await cli.download_bars(
        config(tmp_path, symbols=("SPY",)),
        days=2,
        schwab_base_url=schwab.base_url,
        token_url=schwab.token_url,
    )
    assert [bar.symbol for bar in bars] == ["SPY", "SPY", "SPY"]
    (request,) = schwab.calls("GET", "/pricehistory")
    span = int(request["query"]["endDate"]) - int(request["query"]["startDate"])
    assert span == 2 * 86400 * 1000


CSV = """timestamp,open,high,low,close,volume
2026-10-08T15:00:00Z,100,100,100,100,1000
2026-10-08T15:01:00Z,101,101,101,101,1000
2026-10-08T15:02:00Z,102,102,102,102,1000
2026-10-08T15:03:00Z,99,99,99,99,1000
"""


@pytest.fixture
def env(monkeypatch):
    for name in list(os.environ):
        if name.startswith("TRAIDER_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("TRAIDER_SYMBOLS", "SPY")
    monkeypatch.setenv("TRAIDER_STRATEGY_PARAMS", '{"fast": 1, "slow": 2, "position_usd": 300}')


def test_backtest_command_prints_a_summary(env, tmp_path, capsys):
    path = tmp_path / "bars.csv"
    path.write_text(CSV)
    code = cli.main(["backtest", "--csv", str(path), "--symbol", "SPY", "--spread-bps", "0"])
    text = capsys.readouterr().out
    assert code == 0
    assert "4 bars" in text
    assert "-0.04%" in text
    assert "BUY" in text and "SELL" in text
    assert "9,996.00" in text


def test_backtest_command_reports_bad_input_without_a_traceback(env, tmp_path, capsys):
    path = tmp_path / "bars.csv"
    path.write_text("timestamp,open\n1,2\n")
    code = cli.main(["backtest", "--csv", str(path), "--symbol", "SPY"])
    captured = capsys.readouterr()
    assert code == 1
    assert "missing column" in captured.err
    assert "Traceback" not in captured.err


def test_missing_configuration_is_explained(monkeypatch, capsys):
    for name in list(os.environ):
        if name.startswith("TRAIDER_"):
            monkeypatch.delenv(name)
    code = cli.main(["check"])
    assert code == 2
    assert "TRAIDER_SYMBOLS" in capsys.readouterr().err


def test_a_command_is_required(env):
    with pytest.raises(SystemExit) as caught:
        cli.main([])
    assert caught.value.code == 2


def test_the_package_runs_as_a_module():
    result = subprocess.run(
        [sys.executable, "-m", "traider", "--help"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    for command in ("run", "check", "login", "backtest"):
        assert command in result.stdout
