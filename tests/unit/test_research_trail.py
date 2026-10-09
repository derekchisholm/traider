"""The research trail (S3, local, memory) and the scrubber for alerts and errors."""

import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws
from pydantic import BaseModel

from traider.research.models import PostureLevel
from traider.research.scrub import scrub
from traider.research.trail import LocalTrail, MemoryTrail, S3Trail, to_json, trail_prefix

PREFIX = trail_prefix(date(2026, 10, 9), "premarket-20261009T120000Z-ab12")


@dataclass
class Row:
    symbol: str
    level: PostureLevel


DATA = {
    "at": datetime(2026, 10, 9, 12, tzinfo=UTC),
    "cost": Decimal("1.1200"),
    "rows": [Row("NVDA", PostureLevel.REDUCED)],
    "symbols": ("NVDA",),
}
STORED = {
    "at": "2026-10-09T12:00:00+00:00",
    "cost": "1.1200",
    "rows": [{"symbol": "NVDA", "level": "reduced"}],
    "symbols": ["NVDA"],
}


def test_the_prefix_is_by_day_then_run():
    assert PREFIX == "runs/2026-10-09/premarket-20261009T120000Z-ab12/"


def test_anything_a_run_records_serialises():
    assert json.loads(to_json(DATA)) == STORED
    with pytest.raises(TypeError, match="object"):
        to_json({"x": object()})


class Priced(BaseModel):
    price: Decimal
    on: date


@dataclass
class ByDay:
    prices: dict[date, Decimal]


def test_decimals_and_dates_serialise_wherever_they_sit():
    data = {
        date(2026, 10, 9): Decimal("1.5"),
        Decimal("2.5"): {PostureLevel.REDUCED: {Decimal("3")}},
        "model": Priced(price=Decimal("9.10"), on=date(2026, 10, 9)),
        "nested": ByDay({date(2026, 10, 8): Decimal("0.01")}),
        "items": [(date(2026, 10, 7), Decimal("4"))],
    }
    assert json.loads(to_json(data)) == {
        "2026-10-09": "1.5",
        "2.5": {"reduced": ["3"]},
        "model": {"price": "9.10", "on": "2026-10-09"},
        "nested": {"prices": {"2026-10-08": "0.01"}},
        "items": [["2026-10-07", "4"]],
    }


async def test_s3_trail_writes_json_objects_under_the_runs_prefix():
    with mock_aws():
        client = boto3.client("s3")
        client.create_bucket(
            Bucket="trail", CreateBucketConfiguration={"LocationConstraint": "us-west-2"}
        )
        trail = S3Trail(client, "trail", PREFIX)
        await trail.put("dives/NVDA.json", DATA)
        stored = client.get_object(Bucket="trail", Key=PREFIX + "dives/NVDA.json")
        assert json.loads(stored["Body"].read()) == STORED
        assert stored["ContentType"] == "application/json"
        assert trail.location == f"s3://trail/{PREFIX}"


async def test_local_trail_writes_files_under_the_directory(tmp_path):
    trail = LocalTrail(tmp_path, PREFIX)
    await trail.put("result.json", DATA)
    assert json.loads((tmp_path / PREFIX / "result.json").read_text()) == STORED
    assert trail.location == str(tmp_path / PREFIX)


async def test_memory_trail_keeps_parsed_json():
    trail = MemoryTrail(PREFIX)
    await trail.put("posture.json", DATA)
    assert trail.files == {"posture.json": STORED}
    assert trail.location == f"memory://{PREFIX}"


BAD_NAMES = [
    "",
    "/etc/passwd",
    "../outside.json",
    "dives/../../x",
    "..",
    "dives/..",
    "./x.json",
    "dives//x.json",
    "dives/",
    "..\\outside.json",
    "x\x00.json",
]


@pytest.mark.parametrize("name", BAD_NAMES)
async def test_names_cannot_leave_the_runs_prefix(tmp_path, name):
    with pytest.raises(ValueError, match="not a trail file name"):
        await LocalTrail(tmp_path, PREFIX).put(name, {})
    assert not any(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("name", BAD_NAMES)
async def test_s3_and_memory_trails_refuse_the_same_names(name):
    with mock_aws():
        client = boto3.client("s3")
        client.create_bucket(
            Bucket="trail", CreateBucketConfiguration={"LocationConstraint": "us-west-2"}
        )
        with pytest.raises(ValueError, match="not a trail file name"):
            await S3Trail(client, "trail", PREFIX).put(name, {})
        assert client.list_objects_v2(Bucket="trail").get("KeyCount") == 0
    with pytest.raises(ValueError, match="not a trail file name"):
        await MemoryTrail(PREFIX).put(name, {})


async def test_a_symlink_inside_the_trail_cannot_lead_out_of_it(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / PREFIX).mkdir(parents=True)
    os.symlink(outside, root / PREFIX / "dives")
    os.symlink(outside / "stolen.json", root / PREFIX / "result.json")
    trail = LocalTrail(root, PREFIX)
    for name in ("dives/NVDA.json", "result.json"):
        with pytest.raises(ValueError, match="not a trail file name"):
            await trail.put(name, {})
    assert list(outside.iterdir()) == []


async def test_a_prefix_cannot_lead_out_of_the_root_either(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ValueError, match="not a trail file name"):
        await LocalTrail(root, "../elsewhere/").put("x.json", {})
    assert not (tmp_path / "elsewhere").exists()


# --- scrubbing --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        (
            "AccessDenied on arn:aws:secretsmanager:us-east-1:123456789012:secret:finnhub-AbCd",
            "AccessDenied on arn:***",
        ),
        ("account 123456789012 is not allowed", "account *** is not allowed"),
        ("bad key d1c2b3a4e5f6a7b8c9d0e1f2 refused", "bad key *** refused"),
        ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0", "token ***.***"),
        (
            "see https://example.com/start?k=secretvalue&x=1 now",
            "see https://example.com/start?*** now",
        ),
        (
            "plain words like authentication_failed stay",
            "plain words like authentication_failed stay",
        ),
        ("GET /marketdata/v1/markets: HTTP 503", "GET /marketdata/v1/markets: HTTP 503"),
        ("order 1234 for 10 shares", "order 1234 for 10 shares"),
        ("a\n\nmulti   line\terror", "a multi line error"),
    ],
)
def test_scrub_masks_what_could_be_secret(raw, clean):
    assert scrub(raw) == clean


def test_scrub_cuts_long_text_to_300_characters():
    text = scrub("word " * 200)
    assert len(text) == 300 and text.endswith("...")


KEY = "d1c2b3a4e5f6a7b8c9d0e1f2"


@pytest.mark.parametrize(
    "hidden",
    [
        "\N{ZERO WIDTH SPACE}",
        "\N{SOFT HYPHEN}",
        "\N{RIGHT-TO-LEFT OVERRIDE}",
        "\N{ZERO WIDTH NO-BREAK SPACE}",
        "\N{WORD JOINER}",
        "\N{TAG LATIN CAPITAL LETTER A}",
    ],
)
def test_invisible_characters_cannot_hide_a_key(hidden):
    assert scrub(f"key {KEY[:8]}{hidden}{KEY[8:]} end") == "key *** end"


def test_scrub_turns_control_characters_into_spaces():
    raw = "a\x00b\x1b[31mred\x1b[0m\x7f\x85c"
    assert scrub(raw) == "a b [31mred [0m c"


def test_scrub_survives_lone_surrogates_and_odd_unicode():
    accent, face, separator = (
        "\N{COMBINING ACUTE ACCENT}",
        "\N{GRINNING FACE}",
        "\N{LINE SEPARATOR}",
    )
    raw = f"bad \ud800 pair \udfff and e{accent} {face} {separator} end"
    clean = scrub(raw)
    clean.encode("utf-8")  # alerts and DynamoDB need valid UTF-8
    assert clean == f"bad pair and e{accent} {face} end"


@pytest.mark.parametrize(
    "raw",
    [
        "x" * 100_000,
        "\x00" * 100_000,
        "\ud800" * 1_000,
        "\N{TAG LATIN CAPITAL LETTER A}" * 1_000,
        ("ignore previous instructions " + KEY + " ") * 5_000,
        "arn:aws:" * 20_000,
        "https://a.b/c?" * 20_000,
        "1" * 100_000,
    ],
)
@pytest.mark.parametrize("limit", [300, 50, 10, 3, 2, 1, 0])
def test_scrub_output_never_exceeds_the_limit(raw, limit):
    clean = scrub(raw, limit)
    assert len(clean) <= limit
    clean.encode("utf-8")
    assert KEY not in clean


def test_scrub_is_fast_on_large_hostile_text():
    started = time.monotonic()
    scrub("a" * 2_000_000)
    scrub("1a" * 1_000_000)
    scrub("http://x" + "?" * 500_000)
    assert time.monotonic() - started < 5


def test_scrub_keeps_short_text_whole_and_marks_the_cut():
    assert scrub("x" * 300) == "x" * 300
    assert scrub("x" * 301) == "x" * 297 + "..."
    assert scrub("hello world", 8) == "hello..."
