"""Licence, privacy policy and terms of service (issue #421).

Google's OAuth consent-screen verification and the Play Console both fetch the
privacy policy and terms URLs without running JavaScript, so ``/privacy`` and
``/terms`` must answer with the pages themselves — never with the Flutter web
app's ``index.html``, which the catch-all in ``api/router.py`` serves for every
path it does not recognise.

The licence is AGPL-3.0, whose section 13 obliges the hosted service to offer
its users the source: the pages link to the repository for that reason.
"""

import importlib
import os
import re
from fnmatch import fnmatch
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.brand import REPO_URL

ROOT = Path(__file__).resolve().parent.parent
LEGAL = ROOT / "legal"

# path -> (file under legal/, the page's <h1>)
PAGES = {
    "/privacy": ("privacy.html", "Privacy Policy"),
    "/terms": ("terms.html", "Terms of Service"),
}

_SPA_INDEX = "<!doctype html><html><body>spa shell</body></html>"


@pytest.fixture
def plain_client():
    """The app as tests normally see it: no web build, so no catch-all."""
    import api.router as router

    return TestClient(router.app)


@pytest.fixture
def spa_client(monkeypatch, tmp_path):
    """The app as production runs it, with the SPA catch-all registered.

    Same technique as tests/test_spa_catch_all_api_404.py: the catch-all is
    only registered when a web_client/ build exists at import time, so the
    router is reloaded with that directory reported present.
    """
    import api.router as router

    (tmp_path / "index.html").write_text(_SPA_INDEX)
    expected_web_dir = os.path.normpath(
        os.path.join(os.path.dirname(router.__file__), "..", "web_client")
    )
    real_isdir = os.path.isdir

    def _isdir(path):
        if os.path.normpath(str(path)) == expected_web_dir:
            return True
        return real_isdir(path)

    monkeypatch.setattr(os.path, "isdir", _isdir)
    importlib.reload(router)
    monkeypatch.setattr(os.path, "isdir", real_isdir)
    monkeypatch.setattr(router, "_web_dir", str(tmp_path))
    assert any(getattr(r, "path", None) == "/{full_path:path}" for r in router.app.routes), (
        "SPA catch-all was not registered; the fixture no longer reproduces production"
    )
    try:
        yield TestClient(router.app)
    finally:
        monkeypatch.undo()
        importlib.reload(router)


def _page(filename: str) -> str:
    return (LEGAL / filename).read_text(encoding="utf-8")


# ── Serving ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", PAGES)
def test_legal_page_is_served_as_html(plain_client, path):
    filename, heading = PAGES[path]

    resp = plain_client.get(path)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert f"<h1>{heading}</h1>" in resp.text
    assert resp.content == (LEGAL / filename).read_bytes()
    # Edits to the policy must reach readers on the next load.
    assert resp.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("path", PAGES)
def test_legal_page_is_not_the_web_app(spa_client, path):
    """The failure this guards: the catch-all answering with index.html and a 200."""
    _, heading = PAGES[path]

    resp = spa_client.get(path)

    assert resp.status_code == 200
    assert "spa shell" not in resp.text
    assert f"<h1>{heading}</h1>" in resp.text


@pytest.mark.parametrize("path", PAGES)
def test_legal_page_answers_head(spa_client, path):
    resp = spa_client.head(path)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


def test_the_legal_pages_ship_in_the_image():
    """``.dockerignore`` decides what ``COPY . .`` leaves out; the pages must not be."""
    patterns = [
        line.strip() for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith(("#", "!"))
    ]
    for rel in ("legal", *(f"legal/{f}" for f, _ in PAGES.values())):
        excluded = [p for p in patterns if fnmatch(rel, p.rstrip("/"))]
        assert not excluded, f"{rel} is excluded from the image by {excluded}"


# ── Content ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("filename", [f for f, _ in PAGES.values()])
def test_legal_page_links_the_source_code_and_the_other_page(filename):
    text = _page(filename)

    assert f'href="{REPO_URL}"' in text, "AGPL section 13: offer the source"
    assert f'href="{REPO_URL}/blob/main/LICENSE"' in text
    assert 'href="/privacy"' in text and 'href="/terms"' in text
    assert re.search(r"Last updated: \d{1,2} \w+ \d{4}", text)


def test_legal_pages_have_no_unfilled_placeholders():
    """THE MERGE GATE (issue #421). Fails until the owner fills in the drafts.

    The pages were drafted from what the code does, but some facts cannot be
    read from a repository: who operates the service, where, under which law,
    and so on. Each is written as ``{{NAME}}``. Replace every one, have the
    pages reviewed, and this test passes. Do not delete or skip it to get a
    green build: a published policy that names ``{{OPERATOR_NAME}}`` is worse
    than none.
    """
    unfilled = {
        f"{filename}: {m}"
        for filename, _ in PAGES.values()
        for m in re.findall(r"\{\{[A-Z0-9_]+\}\}", _page(filename))
    }
    assert not unfilled, "fill in before publishing:\n" + "\n".join(sorted(unfilled))
