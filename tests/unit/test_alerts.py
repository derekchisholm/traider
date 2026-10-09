import boto3
import pytest
from moto import mock_aws
from moto.core import DEFAULT_ACCOUNT_ID
from moto.sns import sns_backends

from tests.unit.helpers import T0
from traider.alerts import LogAlerter, SnsAlerter, publish
from traider.timeutil import ManualClock


@pytest.fixture
def sns():
    with mock_aws():
        client = boto3.client("sns")
        arn = client.create_topic(Name="traider-alerts")["TopicArn"]
        yield client, arn


def sent(arn):
    topic = sns_backends[DEFAULT_ACCOUNT_ID]["us-west-2"].topics[arn]
    return [(n[2], n[1]) for n in topic.sent_notifications]  # (subject, message)


async def test_alert_is_published_with_a_recognisable_subject(sns):
    client, arn = sns
    await SnsAlerter(arn, client, ManualClock(T0)).send("k", "Daily loss limit hit", "down 120")
    assert sent(arn) == [("[traider] Daily loss limit hit", "down 120")]


async def test_repeat_of_the_same_alert_is_suppressed_for_a_while(sns):
    client, arn = sns
    clock = ManualClock(T0)
    alerter = SnsAlerter(arn, client, clock, min_interval_s=900)
    await alerter.send("auth", "Login expired", "one")
    clock.advance(600)
    await alerter.send("auth", "Login expired", "two")
    assert len(sent(arn)) == 1


async def test_same_alert_is_sent_again_after_the_quiet_period(sns):
    client, arn = sns
    clock = ManualClock(T0)
    alerter = SnsAlerter(arn, client, clock, min_interval_s=900)
    await alerter.send("auth", "Login expired", "one")
    clock.advance(901)
    await alerter.send("auth", "Login expired", "two")
    assert len(sent(arn)) == 2


async def test_different_alerts_do_not_suppress_each_other(sns):
    client, arn = sns
    alerter = SnsAlerter(arn, client, ManualClock(T0))
    await alerter.send("auth", "Login expired", "one")
    await alerter.send("loss", "Daily loss limit hit", "two")
    assert len(sent(arn)) == 2


async def test_a_failed_publish_never_raises_into_the_bot():
    class Broken:
        def publish(self, **_):
            raise RuntimeError("sns unreachable")

    await SnsAlerter("arn:aws:sns:us-west-2:1:x", Broken(), ManualClock(T0)).send("k", "s", "m")


async def test_a_failed_publish_is_retried_on_the_next_send(sns):
    client, arn = sns

    class FailOnce:
        def __init__(self):
            self.failed = False

        def publish(self, **kwargs):
            if not self.failed:
                self.failed = True
                raise RuntimeError("throttled")
            return client.publish(**kwargs)

    alerter = SnsAlerter(arn, FailOnce(), ManualClock(T0))
    await alerter.send("k", "s", "first")
    await alerter.send("k", "s", "second")
    assert [message for _, message in sent(arn)] == ["second"]


def test_subject_is_cut_to_what_sns_accepts(sns):
    client, arn = sns
    publish(client, arn, "x" * 300, "body")
    ((subject, _),) = sent(arn)
    assert len(subject) == 100


def test_subject_line_breaks_are_flattened(sns):
    client, arn = sns
    publish(client, arn, "line one\nline two", "body")
    ((subject, _),) = sent(arn)
    assert subject == "[traider] line one line two"


async def test_log_alerter_remembers_what_it_was_asked_to_send():
    alerter = LogAlerter()
    await alerter.send("k", "subject", "message")
    assert alerter.sent == [("k", "subject", "message")]


LINK = "https://abc.execute-api.us-east-1.amazonaws.com/start?k=SECRETKEY123"


async def test_the_sign_in_key_is_published_but_never_written_to_the_log(sns, caplog):
    client, arn = sns
    caplog.set_level("WARNING")
    await SnsAlerter(arn, client, ManualClock(T0)).send("auth", "Sign in", f"Go to:\n{LINK}")
    assert LINK in sent(arn)[0][1]  # the person needs the whole link
    assert "SECRETKEY123" not in caplog.text
    assert "https://abc.execute-api.us-east-1.amazonaws.com/start?[redacted]" in caplog.text


async def test_log_alerter_keeps_the_sign_in_key_out_of_the_log_too(caplog):
    caplog.set_level("WARNING")
    await LogAlerter().send("auth", "Sign in", f"Go to {LINK} now")
    assert "SECRETKEY123" not in caplog.text
    assert "Go to" in caplog.text and "now" in caplog.text
