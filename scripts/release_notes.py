#!/usr/bin/env python3
"""Generate user-facing release notes for a commit range.

The release script used to paste `git log --pretty=format:- %s` under a
"What's changed" heading. For v0.46.9 that produced five bullets for two real
changes: three were merge commits, and the two that mattered were written in
commit grammar (``fix(people): ...``) rather than anything a user would read.

This module turns the same range into grouped, linked, audience-separated
notes. Three layers, each degrading to the one below:

1. **Parsing** — drop merges and version bumps, read Conventional Commit
   prefixes, group by type, map the scope to a user-facing area.
2. **Trailers** — a ``Release-Note:`` trailer in the commit body wins over the
   subject line. No parser can turn "enforce the API quota process-wide" into a
   user benefit; only the author knows that, so this is where they write it.
3. **Polish** (``--polish``) — asks Claude to translate any entry that isn't in
   English and to write a short intro paragraph. Entirely optional: no API key,
   no network, or any error means the notes are rendered without it. Whether or
   not it runs, non-English entries are reported on stderr.

Standalone use, to preview any range without cutting a release::

    python scripts/release_notes.py --from v0.46.9 --to HEAD
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, replace
from typing import Iterable, Optional

REPO_URL = "https://github.com/rui-nar/TraxJourney"

# Scope → (area shown to the reader, audience). Audience splits "what changed in
# the app" from "what changed on the server", because one tag ships both the
# Flutter client and the FastAPI backend and the two have different readers.
# `internal` never reaches the reader's sections at all.
APP, SERVER, INTERNAL = "app", "server", "internal"

AREAS: dict[str, tuple[str, str]] = {
    "e2ee": ("Encryption", APP),
    "encryption": ("Encryption", APP),
    "encounters": ("People & encounters", APP),
    "people": ("People & encounters", APP),
    "groups": ("People & encounters", APP),
    "poster": ("Posters", APP),
    "photos": ("Photos", APP),
    "immich": ("Immich", APP),
    "admin": ("Admin dashboard", APP),
    "editor": ("Track editor", APP),
    "track-edit": ("Track editor", APP),
    "track-save": ("Track editor", APP),
    "activity-edit": ("Track editor", APP),
    "activity-editor": ("Track editor", APP),
    "activity-split": ("Track editor", APP),
    "split": ("Track editor", APP),
    "activity-panel": ("Activity panel", APP),
    "strava": ("Strava", APP),
    "polarsteps": ("Polarsteps", APP),
    "gpx": ("GPX import", APP),
    "map": ("Map", APP),
    "geo": ("Map", APP),
    "view": ("Map", APP),
    "view-mode": ("Map", APP),
    "counters": ("Trip stats", APP),
    "stats": ("Trip stats", APP),
    "memory": ("Memories", APP),
    "memories": ("Memories", APP),
    "journal": ("Journal", APP),
    "translations": ("Translations", APP),
    "day-editor": ("Day editor", APP),
    "segment-dialog": ("Segments", APP),
    "segments": ("Segments", APP),
    "share": ("Sharing", APP),
    "social": ("Sharing", APP),
    "auth": ("Sign-in", APP),
    "companion": ("Companions", APP),
    "members": ("Companions", APP),
    "invites": ("Companions", APP),
    "projects": ("Trips", APP),
    "project_io": ("Trips", APP),
    "project-notifier": ("Trips", APP),
    "manage": ("Trips", APP),
    "splash": ("Splash screen", APP),
    "web": ("Web app", APP),
    "client": ("Web app", APP),
    "android": ("Android app", APP),
    "api": ("API", SERVER),
    "db": ("Database", SERVER),
    "alembic": ("Database", SERVER),
    "backup": ("Backups", SERVER),
    "email": ("Email", SERVER),
    "metrics": ("Monitoring", SERVER),
    "logging": ("Logging", SERVER),
    "release": ("Release tooling", INTERNAL),
    "ci": ("CI", INTERNAL),
    "deps": ("Dependencies", INTERNAL),
    "docker": ("Packaging", INTERNAL),
    "deploy": ("Deployment", INTERNAL),
    "test": ("Tests", INTERNAL),
    "tests": ("Tests", INTERNAL),
}

# Conventional Commit types that reach the reader, and the section each lands in.
SECTIONS = {"feat": "New", "fix": "Fixed", "perf": "Fixed"}

_COMMIT_RE = re.compile(
    r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]*)\))?(?P<breaking>!)?:\s*(?P<subject>.+)$"
)
_ISSUE_RE = re.compile(r"#(\d+)")
# A trailing "(#123)" / "(#125, #133)" group — the convention for tagging a
# subject with its issue and PR. Only these are stripped and turned into links;
# a reference woven into the sentence ("issue #45 follow-up") is prose and stays
# put, and GitHub auto-links it in a release body anyway.
_TRAILING_REFS_RE = re.compile(r"\s*\(\s*#\d+(?:[,\s]+#\d+)*\s*\)\s*$")
_TRAILER_RE = re.compile(r"^(Release-Note|Upgrade-Note):\s*(.+)$", re.IGNORECASE)
# The attribution trailers stacked under a note with no blank line
# (Co-Authored-By:, Signed-off-by:, Claude-Session:) end it. Only these: a
# wrapped line may itself start "Self-hosting: ..." and must stay in the note.
_OTHER_TRAILER_RE = re.compile(
    r"^(?:(?:[A-Za-z]+-)+by|Claude-Session|Change-Id):\s", re.IGNORECASE
)

# Commits that are pure bookkeeping — never interesting, in any section.
_SKIP_SUBJECTS = (
    re.compile(r"^chore: bump version", re.IGNORECASE),
    re.compile(r"^Merge ", re.IGNORECASE),
)

_RECORD_SEP = "\x1e"
_FIELD_SEP = "\x1f"


@dataclass(frozen=True)
class Change:
    """One user-visible change, ready to render."""

    kind: str
    area: str
    audience: str
    subject: str
    release_note: Optional[str]
    upgrade_note: Optional[str]
    refs: tuple[str, ...]

    @property
    def section(self) -> str:
        return SECTIONS.get(self.kind, "Internal")

    @property
    def text(self) -> str:
        """What the reader sees — the author's line if they wrote one."""
        return self.release_note or self.subject


# ── Parsing ───────────────────────────────────────────────────────────────────


def _trailers(body: str) -> dict[str, str]:
    """Read ``Release-Note:`` / ``Upgrade-Note:`` trailers from a commit body.

    A trailer runs until a blank line or the next trailer, so it can wrap across
    lines the way the rest of the commit body does.
    """
    found: dict[str, str] = {}
    key: Optional[str] = None
    for line in body.splitlines():
        match = _TRAILER_RE.match(line.strip())
        if match:
            key = match.group(1).lower()
            found[key] = match.group(2).strip()
        elif key and line.strip() and not _OTHER_TRAILER_RE.match(line.strip()):
            found[key] = f"{found[key]} {line.strip()}"
        else:
            key = None
    return found


def _classify(scope: Optional[str], files: Iterable[str]) -> tuple[str, str]:
    """Resolve (area, audience) from the scope, falling back to changed paths.

    The scope table is the accurate source when it has an entry. When it
    doesn't, which tree the commit touched is the next best signal — anything
    under flutter_client/ is something a user can see.
    """
    if scope:
        # `fix(map,stats)` — take the first, that's the primary area.
        primary = scope.split(",")[0].strip().lower()
        if primary in AREAS:
            return AREAS[primary]

    # No scope means no area label — under an "In the app" heading, a generic
    # "**App** —" prefix on every bullet is noise.
    label = scope.split(",")[0].strip().title() if scope else ""
    paths = list(files)
    if any(p.startswith("flutter_client/") for p in paths):
        return label, APP
    if paths:
        return label, SERVER
    return label, INTERNAL


def parse_commits(raw: str) -> list[Change]:
    """Turn ``git log`` output (see :func:`git_log`) into changes, newest last.

    Merge commits and version bumps are dropped: they duplicate or bury the
    real change, which is what made the old flat list unreadable.
    """
    changes: list[Change] = []
    for record in raw.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record.strip():
            continue
        subject, body, files_blob = (record.split(_FIELD_SEP) + ["", ""])[:3]
        subject = subject.strip()
        if not subject or any(p.match(subject) for p in _SKIP_SUBJECTS):
            continue

        match = _COMMIT_RE.match(subject)
        kind = match.group("type") if match else "other"
        scope = match.group("scope") if match else None
        text = match.group("subject").strip() if match else subject

        trailers = _trailers(body)
        area, audience = _classify(scope, (f for f in files_blob.split("\n") if f))
        if match and match.group("breaking"):
            trailers.setdefault("upgrade-note", text)

        refs: list[str] = []
        while (trailing := _TRAILING_REFS_RE.search(text)) is not None:
            refs = _ISSUE_RE.findall(trailing.group(0)) + refs
            text = text[: trailing.start()]

        changes.append(
            Change(
                kind=kind,
                area=area,
                audience=audience,
                subject=text.strip(),
                release_note=trailers.get("release-note"),
                upgrade_note=trailers.get("upgrade-note"),
                refs=tuple(dict.fromkeys(refs)),
            )
        )
    return list(reversed(changes))


# ── Rendering ─────────────────────────────────────────────────────────────────


def _bullet(change: Change, repo_url: str) -> str:
    # One parenthesised group, however many refs: a squash commit carries both
    # its issue and its PR number, and "(#125) (#133)" reads like two changes.
    # /issues/N redirects to /pull/N when N is a PR, so one URL shape covers both.
    links = ", ".join(f"[#{n}]({repo_url}/issues/{n})" for n in change.refs)
    text = change.text.rstrip(".")
    if change.area:
        prefix = f"**{change.area}** — "
    else:
        # Nothing in front of it, so the subject starts the sentence — commit
        # subjects are lowercase by convention.
        prefix = ""
        text = text[:1].upper() + text[1:]
    return f"- {prefix}{text}." + (f" ({links})" if links else "")


def _section(changes: list[Change], repo_url: str) -> list[str]:
    """Render one section, splitting by audience only when both are present.

    A lone "In the app" heading over an all-app section is noise, so the
    sub-headings appear only when they're actually telling the reader apart.
    """
    app = [c for c in changes if c.audience == APP]
    server = [c for c in changes if c.audience == SERVER]
    lines: list[str] = []
    if app and server:
        lines.append("### In the app\n")
        lines += [_bullet(c, repo_url) for c in app]
        lines.append("\n### Server & deployment\n")
        lines += [_bullet(c, repo_url) for c in server]
    else:
        lines += [_bullet(c, repo_url) for c in app + server]
    return lines


def render(
    changes: list[Change],
    *,
    version: str,
    previous: str,
    repo_url: str = REPO_URL,
    highlights: Optional[str] = None,
) -> str:
    """Render the full release body as GitHub-flavoured markdown."""
    visible = [c for c in changes if c.audience != INTERNAL and c.section != "Internal"]
    internal = [c for c in changes if c not in visible]

    out: list[str] = [f"# TraxJourney {version}", ""]
    count = len(visible)
    noun = "change" if count == 1 else "changes"
    out += [f"_{count} {noun} since {previous}_", ""]

    if highlights:
        out += ["## ✨ Highlights", "", highlights.strip(), ""]

    for title, emoji in (("New", "🚀"), ("Fixed", "🐛")):
        section = [c for c in visible if c.section == title]
        if section:
            out += [f"## {emoji} {title}", ""]
            out += _section(section, repo_url)
            out.append("")

    upgrades = [c for c in changes if c.upgrade_note]
    if upgrades:
        out += ["## ⚠️ Upgrade notes", ""]
        for c in upgrades:
            note = c.upgrade_note.rstrip(".")
            # Same rule as _bullet: no area, no empty "****" prefix.
            out.append(f"- **{c.area}** — {note}." if c.area
                       else f"- {note[:1].upper()}{note[1:]}.")
        out.append("")

    if internal:
        out += [
            f"<details><summary>Internal changes ({len(internal)})</summary>",
            "",
        ]
        out += [f"- {c.subject}" for c in internal]
        out += ["", "</details>", ""]

    if not visible and not internal:
        out += [f"_No changes since {previous}._", ""]

    out.append(f"**Full changelog**: [`{previous}...{version}`]"
               f"({repo_url}/compare/{previous}...{version})")
    return "\n".join(out) + "\n"


# ── Git plumbing ──────────────────────────────────────────────────────────────


def _git(*args: str) -> str:
    """Run git and decode as UTF-8.

    Explicit encoding, not the platform default: this repo's commit messages
    are full of em-dashes and accented characters, and on Windows the default
    cp1252 decode raises UnicodeDecodeError on them.
    """
    return subprocess.run(
        ["git", *args], capture_output=True, check=True,
        encoding="utf-8", errors="replace",
    ).stdout


def previous_tag(ref: str = "HEAD") -> Optional[str]:
    """The most recent tag reachable from *ref*.

    Asked of git rather than derived from pubspec.yaml: a tag cut by hand, or a
    pubspec that drifted, would otherwise silently produce the wrong range.
    """
    try:
        return _git("describe", "--tags", "--abbrev=0", ref).strip() or None
    except subprocess.CalledProcessError:
        return None


def git_log(from_ref: Optional[str], to_ref: str = "HEAD") -> str:
    """Commits in the range, with subject, body and changed paths per record."""
    spec = f"{from_ref}..{to_ref}" if from_ref else to_ref
    # The record separator leads each entry, not trails it: --name-only appends
    # the changed paths *after* the pretty format, so a trailing separator would
    # push every commit's file list into the next record.
    return _git(
        "log", spec, "--no-merges", "--name-only",
        f"--pretty=format:{_RECORD_SEP}%s{_FIELD_SEP}%b{_FIELD_SEP}",
    )


# ── Language ──────────────────────────────────────────────────────────────────

# The notes are published in English, but a `Release-Note:` trailer is written
# in whatever language the author was thinking in — v0.48.0 shipped with sixty
# Portuguese bullets between the English ones. Detection is deliberately blunt:
# it only has to spot a whole sentence in another language, and a warning that
# fires on good English would be worse than one that occasionally stays quiet.

# Function words that are common in Portuguese/Spanish/French and are not
# English words. Ambiguous ones are left out on purpose: "no", "a", "de", "os"
# and "com" all appear in English text (or in "github.com") and would flag it.
_ROMANCE_MARKERS = frozenset("""
    não nao já ja agora também tambem uma umas uns que quando como para por
    pelo pela dos das aos num numa mais muito mas sem depois antes onde porque
    ainda apenas está estão esta este esse essa isso seu sua seus suas cada
    entre sempre todos todas les une dans pour avec cette aux nous vous los las
    pero ahora desde hasta muy della delle sono anche
""".split())

# English function words, to weigh against the above: a bullet naming a
# Portuguese-looking product or place is still English if it reads as English.
_ENGLISH_MARKERS = frozenset("""
    the and is are was were when with from this that these those now longer
    instead you your of to for but on in at it its has have been will would
    could than while after before every any
""".split())

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
# Accents English doesn't use. One anywhere in the sentence counts as a marker,
# which is what catches a short bullet like "produção e validação" that carries
# no function word at all.
_ACCENT_RE = re.compile(r"[áàâãéèêíìóòôõúùûçñ]", re.IGNORECASE)


def looks_non_english(text: str) -> bool:
    """True when *text* reads as Portuguese/Spanish/French rather than English.

    Ties go to "non-English": a sentence with markers of both is usually a
    translated line with an English product name in it, and the cost of a false
    positive is one line re-checked, not a broken release.
    """
    words = {w.lower() for w in _WORD_RE.findall(text)}
    romance = len(words & _ROMANCE_MARKERS) + bool(_ACCENT_RE.search(text))
    return romance > 0 and romance >= len(words & _ENGLISH_MARKERS)


def _reader_fields(changes: list[Change]) -> list[tuple[int, str]]:
    """(index, attribute) for every string the reader actually sees.

    The internal-changes block is raw commit subjects by design, so it is left
    alone — nobody reads it for the prose.
    """
    fields: list[tuple[int, str]] = []
    for i, change in enumerate(changes):
        if change.audience != INTERNAL and change.section != "Internal":
            fields.append((i, "release_note" if change.release_note else "subject"))
        if change.upgrade_note:
            fields.append((i, "upgrade_note"))
    return fields


def _stray_fields(changes: list[Change]) -> list[tuple[int, str]]:
    return [(i, f) for i, f in _reader_fields(changes)
            if looks_non_english(getattr(changes[i], f))]


def warn_non_english(changes: list[Change]) -> int:
    """Report entries that aren't in English. Returns how many there were."""
    stray = _stray_fields(changes)
    if not stray:
        return 0
    print(f"warning: {len(stray)} entr{'y' if len(stray) == 1 else 'ies'} "
          f"look non-English — release notes are published in English "
          f"(see docs/RELEASING.md); re-run with --polish to translate them:",
          file=sys.stderr)
    for i, field in stray[:5]:
        print(f"  - {getattr(changes[i], field)[:90]}", file=sys.stderr)
    if len(stray) > 5:
        print(f"  ... and {len(stray) - 5} more", file=sys.stderr)
    return len(stray)


# ── Optional: LLM polish ──────────────────────────────────────────────────────

_TRANSLATE_PROMPT = """\
Below are release-note lines for TraxJourney, an app for mapping and sharing \
multi-week trips. Some are not in English.

Translate each line into English. Keep the meaning exactly — do not add, drop, \
merge or reorder anything, and do not change the tone. Leave product, brand and \
feature names as they are (Strava, Polarsteps, Immich, Grafana, Alloy, ...), and \
keep any markdown, numbers and units untouched. A line already in English must \
be returned unchanged.

Return exactly {count} line(s), numbered "1." to "{count}." in the same order, \
one per line, and nothing else.

Lines:
{lines}
"""

_HIGHLIGHTS_PROMPT = """\
Write the "Highlights" paragraph for a release of TraxJourney, an app for mapping \
and sharing multi-week trips (Flutter client, FastAPI server).

Below are the changes in this release, already grouped. Write 2-3 sentences in \
English for someone who uses the app, in plain language: what is now possible \
or fixed, and why it matters. Lead with whatever is most significant.

Do not use a heading, a bullet list, issue numbers, or the words "this release". \
Do not invent anything that is not in the list. If the changes are all minor, \
say so briefly rather than inflating them.

Changes:
{changes}
"""


def _ask(prompt: str, *, model: str, max_tokens: int, what: str) -> Optional[str]:
    """Send one prompt to Claude; None if the answer is unavailable.

    Every failure path returns None so the caller renders the deterministic
    notes instead. A release must never be blocked by a missing API key, an
    offline machine, or a bad day at the API — this whole layer is a garnish,
    not the notes.
    """
    try:
        import anthropic
    except ImportError:
        print("note: `pip install anthropic` to enable --polish", file=sys.stderr)
        return None

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001 — never fail a release over a garnish
        print(f"note: {what} unavailable ({exc.__class__.__name__}); "
              f"continuing without", file=sys.stderr)
        return None

    if response.stop_reason == "refusal":
        print(f"note: {what} declined by the model; continuing without",
              file=sys.stderr)
        return None

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return text or None


_NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s*")


def translate(changes: list[Change], *, model: str = "claude-opus-5") -> list[Change]:
    """Return *changes* with every non-English reader-facing entry in English.

    Only the entries :func:`looks_non_english` flags are sent, so a line that
    already reads as English is never rewritten. Anything unexpected in the
    answer — a short count, a blank line — means the originals are kept, and
    :func:`warn_non_english` still reports them.
    """
    stray = _stray_fields(changes)
    if not stray:
        return changes

    lines = "\n".join(f"{n}. {getattr(changes[i], f)}"
                      for n, (i, f) in enumerate(stray, 1))
    answer = _ask(_TRANSLATE_PROMPT.format(count=len(stray), lines=lines),
                  model=model, max_tokens=8192, what="translation")
    if answer is None:
        return changes

    translated = [_NUMBERED_RE.sub("", line).strip()
                  for line in answer.splitlines() if line.strip()]
    if len(translated) != len(stray) or not all(translated):
        print(f"note: translation returned {len(translated)} usable lines for "
              f"{len(stray)} entries; keeping the originals", file=sys.stderr)
        return changes

    updates: dict[int, dict[str, str]] = {}
    for (index, field), text in zip(stray, translated):
        updates.setdefault(index, {})[field] = text
    return [replace(c, **updates[i]) if i in updates else c
            for i, c in enumerate(changes)]


def polish(changes: list[Change], *, model: str = "claude-opus-5") -> Optional[str]:
    """Ask Claude for a short highlights paragraph; None if unavailable."""
    visible = [c for c in changes if c.audience != INTERNAL and c.section != "Internal"]
    if not visible:
        return None

    listing = "\n".join(f"- [{c.section}] {c.area}: {c.text}" for c in visible)
    return _ask(_HIGHLIGHTS_PROMPT.format(changes=listing),
                model=model, max_tokens=1024, what="highlights")


# ── CLI ───────────────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="from_ref", default=None,
                        help="start ref (default: the most recent tag)")
    parser.add_argument("--to", dest="to_ref", default="HEAD", help="end ref")
    parser.add_argument("--version", default=None,
                        help="version being released (default: the --to ref)")
    parser.add_argument("--repo-url", default=REPO_URL)
    parser.add_argument("--polish", action="store_true",
                        help="translate non-English entries and add an "
                             "LLM-written highlights paragraph")
    parser.add_argument("--model", default="claude-opus-5")
    args = parser.parse_args(argv)

    # The warnings quote the offending entries, which are exactly the ones full
    # of accented characters; a Windows console defaults to cp1252 and would
    # mangle them. Same reason the body is written as UTF-8 bytes below.
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

    from_ref = args.from_ref or previous_tag(args.to_ref)
    changes = parse_commits(git_log(from_ref, args.to_ref))
    if args.polish:
        # Before the highlights, so the paragraph is written from English notes.
        changes = translate(changes, model=args.model)
    warn_non_english(changes)
    highlights = polish(changes, model=args.model) if args.polish else None

    body = render(
        changes,
        version=args.version or args.to_ref,
        previous=from_ref or "the beginning",
        repo_url=args.repo_url,
        highlights=highlights,
    )
    # Written as UTF-8 bytes rather than through sys.stdout: the section
    # headings carry emoji, and a Windows console defaults to cp1252, which
    # raises UnicodeEncodeError on them.
    sys.stdout.buffer.write(body.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
