"""The README and the runbook are operating instructions. Keep them true."""

import re
from pathlib import Path

import pytest

from traider import cli
from traider.config import Config, RiskLimits
from traider.control import ControlMode

ROOT = Path(__file__).resolve().parents[2]
DOCS = [ROOT / "README.md", ROOT / "docs" / "runbook.md"]


def text_of(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def anchors(path: Path) -> set[str]:
    """Heading anchors the way GitHub makes them."""
    found = set()
    for line in text_of(path).splitlines():
        if line.startswith("#"):
            title = line.lstrip("#").strip().lower()
            found.add(re.sub(r"[^a-z0-9 -]", "", title).replace(" ", "-"))
    return found


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_links_inside_the_repository_lead_somewhere(doc):
    for target in re.findall(r"\]\(([^)\s]+)\)", text_of(doc)):
        if target.startswith(("http://", "https://")):
            continue
        path, _, anchor = target.partition("#")
        linked = (doc.parent / path).resolve() if path else doc
        assert linked.exists(), f"{doc.name}: {target} does not exist"
        if anchor:
            assert anchor in anchors(linked), f"{doc.name}: no heading for {target}"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_limits_named_in_the_docs_are_real_limits(doc):
    named = set(re.findall(r"`((?:max|min)_[a-z_]+|require_cash|settled_cash_only)`", text_of(doc)))
    assert named, "the docs should name at least one limit"
    assert named <= set(RiskLimits.model_fields)


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_commands_named_in_the_docs_exist(doc, capsys):
    # Only code is checked: prose such as "traider started" is not a command.
    spans = re.findall(r"`([^`\n]+)`", text_of(doc))
    blocks = re.findall(r"```sh\n(.*?)```", text_of(doc), flags=re.DOTALL)
    used = set(re.findall(r"\btraider ([a-z]+)\b", " ".join(spans + blocks)))
    assert used, "the docs should show at least one command"
    for command in used:
        with pytest.raises(SystemExit) as exit_:
            cli.main([command, "--help"])
        assert exit_.value.code == 0, f"{doc.name}: `traider {command}` is not a command"
    capsys.readouterr()


def test_runbook_lists_exactly_the_control_values_the_bot_understands():
    runbook = text_of(ROOT / "docs" / "runbook.md")
    table = runbook.split("| Value | Buys | Sells | Notes |", 1)[1].split("\n\n", 1)[0]
    listed = set(re.findall(r"^\| `([a-z_]+)` \|", table, flags=re.MULTILINE))
    assert listed == {mode.value for mode in ControlMode}


def test_runbook_explains_exactly_the_events_the_engine_records():
    engine = text_of(ROOT / "src" / "traider" / "engine.py")
    recorded = set(re.findall(r'self\._event\(\s*"([a-z_]+)"', engine))
    assert recorded, "no events found in the engine: has the call changed?"
    runbook = text_of(ROOT / "docs" / "runbook.md")
    table = runbook.split("| Event | Meaning |", 1)[1].split("\n\n", 1)[0]
    rows = [row for row in table.splitlines() if row.startswith("| `")]
    explained = {name for row in rows for name in re.findall(r"`([a-z_]+)`", row.split("|")[1])}
    assert explained == recorded


def test_runbook_explains_every_risk_code_it_gives_as_an_example():
    risk = text_of(ROOT / "src" / "traider" / "risk.py")
    codes = set(re.findall(r'reject\(\s*"([a-z_]+)"', risk))
    runbook = text_of(ROOT / "docs" / "runbook.md")
    line = next(row for row in runbook.splitlines() if row.startswith("| `order_blocked`"))
    examples = set(re.findall(r"`([a-z_]+)`", line)) - {"order_blocked", "codes"}
    assert examples and examples <= codes


def test_readme_defaults_match_the_code():
    readme = text_of(ROOT / "README.md")
    limits = RiskLimits()
    caps = f"{limits.max_order_usd} / {limits.max_position_usd} / {limits.max_total_exposure_usd}"
    assert f"({caps} dollars)" in readme
    assert Config(symbols=("SPY",)).trading_mode == "paper"
    assert "It starts in **paper mode**" in readme


def test_the_research_run_commands_in_the_docs_parse(capsys):
    shown = set()
    for doc in DOCS:
        shown |= set(re.findall(r"traider (research run [a-z -]+)", text_of(doc)))
    assert "research run --kind premarket --dry-run" in {s.strip() for s in shown}
    for command in shown:
        with pytest.raises(SystemExit) as exit_:
            cli.main([*command.split(), "--help"])
        assert exit_.value.code == 0, f"`traider {command}` does not parse"
    capsys.readouterr()


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_research_job_settings_named_in_the_docs_exist(doc):
    from pydantic import BaseModel

    from traider.research.job_settings import ResearchJobSettings

    named = re.findall(r"`research_jobs\.([a-z_.]+)`", text_of(doc))
    for path in named:
        model: type[BaseModel] | None = ResearchJobSettings
        for part in path.split("."):
            assert model is not None and part in model.model_fields, f"research_jobs.{path}"
            annotation = model.model_fields[part].annotation
            is_model = isinstance(annotation, type) and issubclass(annotation, BaseModel)
            model = annotation if is_model else None
