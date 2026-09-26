"""Wiring of the adversarial review framework (docs/REVIEW.md).

The framework is four files that refer to each other by name: the policy, the
skill that applies it, and the two subagents the skill launches. Nothing runs
them in CI, so a rename or a dropped rule would only show up as a review that
quietly stops following the policy. These tests pin the references.
"""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
POLICY = ROOT / "docs" / "REVIEW.md"
SKILL = ROOT / ".claude" / "skills" / "adversarial-review" / "SKILL.md"
AGENTS = ROOT / ".claude" / "agents"
CLAUDE_MD = ROOT / "CLAUDE.md"

# The reviewer finds, the triager decides; neither may change the code.
EXPECTED_MODELS = {"adversarial-reviewer": "fable", "review-triager": "opus"}
READ_ONLY_TOOLS = {"Read", "Grep", "Glob"}

RULE_ID = re.compile(r"\b[EFD]\d+\b")
SECTION_REF = re.compile(r"§(\d+)")


def _frontmatter(path):
    text = path.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert match, f"{path} has no frontmatter"
    return yaml.safe_load(match.group(1)), text[match.end():]


def _defined_rules():
    return set(re.findall(r"\*\*([EFD]\d+)\b", POLICY.read_text(encoding="utf-8")))


def _defined_sections():
    return set(re.findall(r"^## (\d+)\.", POLICY.read_text(encoding="utf-8"), re.M))


def test_skill_name_matches_its_directory():
    meta, _ = _frontmatter(SKILL)
    assert meta["name"] == SKILL.parent.name


def test_skill_description_covers_the_phrases_it_should_trigger_on():
    """The description is what routes "get a fable adversarial review" here."""
    description = _frontmatter(SKILL)[0]["description"].lower()
    for phrase in ("adversarial review", "fable", "re-review", "findings"):
        assert phrase in description


@pytest.mark.parametrize("agent", sorted(EXPECTED_MODELS))
def test_agent_is_pinned_and_read_only(agent):
    meta, _ = _frontmatter(AGENTS / f"{agent}.md")
    assert meta["name"] == agent
    assert meta["model"] == EXPECTED_MODELS[agent]
    tools = {tool.strip() for tool in meta["tools"].split(",")}
    assert tools <= READ_ONLY_TOOLS


@pytest.mark.parametrize("agent", sorted(EXPECTED_MODELS))
def test_skill_launches_each_agent(agent):
    assert f"`{agent}`" in SKILL.read_text(encoding="utf-8")


def test_skill_lets_the_user_choose_the_reviewer_model():
    """The agent pins Fable; an "Opus adversarial review" must not silently get Fable."""
    text = SKILL.read_text(encoding="utf-8")
    assert "launch the same agent with that model as an\noverride" in text


def test_decision_rules_are_numbered_without_gaps():
    """§5 is applied in order, so a gap means a rule was lost."""
    numbers = sorted(int(rule[1:]) for rule in _defined_rules() if rule[0] == "D")
    assert numbers == list(range(1, len(numbers) + 1))


@pytest.mark.parametrize(
    "path",
    [SKILL, AGENTS / "adversarial-reviewer.md", AGENTS / "review-triager.md", POLICY],
    ids=lambda p: p.name,
)
def test_every_cited_rule_and_section_exists(path):
    text = path.read_text(encoding="utf-8")
    assert set(RULE_ID.findall(text)) <= _defined_rules()
    assert set(SECTION_REF.findall(text)) <= _defined_sections()


def test_claude_md_routes_reviews_through_the_skill():
    text = CLAUDE_MD.read_text(encoding="utf-8")
    assert "`adversarial-review`" in text
    assert "docs/REVIEW.md" in text
