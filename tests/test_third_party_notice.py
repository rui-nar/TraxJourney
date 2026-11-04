"""README's third-party notice must name the pillow-heif version we ship (INT-R1-B1).

The notice is the offer of source for libheif, libde265 and x265, which are
bundled inside the pillow-heif wheel. pyproject.toml allows `pillow-heif>=1.8.1`
and uv.lock pins the version actually installed in the image, but nothing tied
the README to the lock: a routine `uv lock --upgrade` would ship a different
wheel (with different bundled library versions) while the notice kept offering
the source of the old ones.
"""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _locked_pillow_heif_version() -> str:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    versions = [p["version"] for p in lock["package"] if p["name"] == "pillow-heif"]
    assert len(versions) == 1, f"expected one pillow-heif in uv.lock, found {versions}"
    return versions[0]


def test_readme_third_party_notice_names_the_locked_pillow_heif():
    version = _locked_pillow_heif_version()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    notice = readme[readme.index("### Third-party notice"):]
    # The README writes the name as a markdown link: [`pillow-heif`](url) 1.8.1
    pattern = rf"pillow-heif`?(?:\]\([^)]*\))?\s+{re.escape(version)}(?![\d.]*\d)"
    assert re.search(pattern, notice), (
        f"uv.lock pins pillow-heif {version} but the README's third-party notice "
        f"does not name 'pillow-heif {version}'. Update the README's library "
        "versions and source links to match the new pillow-heif wheel."
    )
