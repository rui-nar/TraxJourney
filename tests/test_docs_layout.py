"""Layout of docs/: plans live in docs/plans/, and every relative link resolves.

Code comments and tests cite plans by path (``docs/plans/X_PLAN.md, U3``), so a
plan saved at the top level, or a doc moved without its links, silently breaks
those pointers.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
PLANS = DOCS / "plans"

LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")


def test_plans_live_in_the_plans_folder():
    assert not list(DOCS.glob("*_PLAN.md"))


def test_every_plan_is_in_the_index():
    index = (PLANS / "README.md").read_text(encoding="utf-8")
    missing = [p.name for p in PLANS.glob("*.md") if p.name != "README.md" and f"({p.name})" not in index]
    assert not missing, f"docs/plans/README.md does not list {missing}"


def test_relative_links_resolve():
    broken = []
    for doc in DOCS.rglob("*.md"):
        for target in LINK.findall(doc.read_text(encoding="utf-8")):
            if re.match(r"[a-z]+:", target):
                continue
            if not (doc.parent / target).exists():
                broken.append(f"{doc.relative_to(ROOT)} -> {target}")
    assert not broken, broken
