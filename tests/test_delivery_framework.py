"""Wiring of the delivery framework (docs/DELIVERY.md).

Like the review framework, this is a policy, two skills and two agents that
refer to each other by name, and nothing runs them in CI. These tests pin the
references, and the properties the policy relies on: the implementer cannot
start agents of its own, and the verifier cannot edit.
"""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
POLICY = ROOT / "docs" / "DELIVERY.md"
SKILLS = ROOT / ".claude" / "skills"
DELIVER = SKILLS / "deliver-plan" / "SKILL.md"
WRITE = SKILLS / "write-plan" / "SKILL.md"
AGENTS = ROOT / ".claude" / "agents"
CLAUDE_MD = ROOT / "CLAUDE.md"

RULE_ID = re.compile(r"\b[SXW]\d+\b")
SECTION_REF = re.compile(r"§(\d+)")


def _frontmatter(path):
    text = path.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert match, f"{path} has no frontmatter"
    return yaml.safe_load(match.group(1)), text[match.end():]


def _tools(agent):
    meta, _ = _frontmatter(AGENTS / f"{agent}.md")
    return {tool.strip() for tool in meta["tools"].split(",")}


def _defined_rules():
    return set(re.findall(r"\*\*([SXW]\d+)\b", POLICY.read_text(encoding="utf-8")))


def _defined_sections():
    return set(re.findall(r"^## (\d+)\.", POLICY.read_text(encoding="utf-8"), re.M))


@pytest.mark.parametrize("skill", [DELIVER, WRITE], ids=lambda p: p.parent.name)
def test_skill_name_matches_its_directory(skill):
    assert _frontmatter(skill)[0]["name"] == skill.parent.name


@pytest.mark.parametrize(
    "skill, phrases",
    [
        (DELIVER, ("implement", "deliver the plan", "orchestrate", "not for small")),
        (WRITE, ("plan a feature", "draft a plan", "write a plan")),
    ],
    ids=["deliver-plan", "write-plan"],
)
def test_skill_description_covers_its_triggers(skill, phrases):
    description = _frontmatter(skill)[0]["description"].lower()
    for phrase in phrases:
        assert phrase in description


def test_implementer_model_is_chosen_per_unit():
    """Routing (§2) picks Sonnet or Opus at launch; a pinned model would hide that."""
    assert "model" not in _frontmatter(AGENTS / "implementer.md")[0]


def test_implementer_cannot_start_agents_or_skills():
    """An implementer that can delegate is how a unit's scope quietly grows."""
    assert not _tools("implementer") & {"Agent", "Task", "Skill", "Workflow"}


def test_verifier_is_sonnet_and_cannot_edit():
    meta, _ = _frontmatter(AGENTS / "verifier.md")
    assert meta["model"] == "sonnet"
    assert not _tools("verifier") & {"Edit", "Write", "NotebookEdit", "Agent", "Task"}


@pytest.mark.parametrize("name", ["`implementer`", "`verifier`", "`adversarial-review`", "`write-plan`"])
def test_deliver_plan_names_what_it_uses(name):
    assert name in DELIVER.read_text(encoding="utf-8")


def test_the_skills_it_hands_off_to_exist():
    for skill in ("adversarial-review", "write-plan", "deliver-plan"):
        assert (SKILLS / skill / "SKILL.md").exists()


def test_rules_are_numbered_without_gaps():
    rules = _defined_rules()
    for prefix in "SXW":
        numbers = sorted(int(rule[1:]) for rule in rules if rule[0] == prefix)
        assert numbers == list(range(1, len(numbers) + 1)), prefix


@pytest.mark.parametrize(
    "path",
    [DELIVER, WRITE, AGENTS / "implementer.md", AGENTS / "verifier.md", POLICY],
    ids=lambda p: f"{p.parent.name}/{p.name}",
)
def test_every_cited_rule_and_section_exists(path):
    text = path.read_text(encoding="utf-8")
    assert set(RULE_ID.findall(text)) <= _defined_rules()
    # §N in these files means DELIVERY.md, except where REVIEW.md is named on
    # the same line.
    for line in text.splitlines():
        if "REVIEW.md" not in line:
            assert set(SECTION_REF.findall(line)) <= _defined_sections(), line


def test_plans_carry_the_heading_the_reviewer_looks_for():
    """write-plan must produce the envelope heading adversarial-review reads."""
    review_skill = (SKILLS / "adversarial-review" / "SKILL.md").read_text(encoding="utf-8")
    assert "`## Review envelope`" in review_skill
    assert "`## Review envelope`" in WRITE.read_text(encoding="utf-8")


def test_claude_md_routes_plans_through_the_skills():
    text = CLAUDE_MD.read_text(encoding="utf-8")
    assert "`deliver-plan`" in text
    assert "`write-plan`" in text
    assert "docs/DELIVERY.md" in text
