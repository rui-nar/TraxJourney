"""Tests for the release-notes generator.

Fixtures are real subjects from this repo's history, because the failures that
motivated this script were all shape problems with real commits: merge commits
outnumbering the changes, version bumps in the list, and refs woven into a
sentence rather than tagged on the end.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from scripts.release_notes import (
    APP,
    INTERNAL,
    SERVER,
    Change,
    looks_non_english,
    parse_commits,
    polish,
    render,
    translate,
    warn_non_english,
)


def _log(*records: tuple[str, str, str]) -> str:
    """Build a fake `git log` blob: (subject, body, files) per commit."""
    return "".join(f"\x1e{s}\x1f{b}\x1f\n{f}\n" for s, b, f in records)


def _one(subject: str, body: str = "", files: str = "api/x.py") -> Change:
    changes = parse_commits(_log((subject, body, files)))
    assert len(changes) == 1
    return changes[0]


class TestFiltering:
    def test_merge_commits_are_dropped(self):
        """Three of the five bullets in the v0.46.9 notes were merge commits,
        and they duplicated changes already listed by their own commit."""
        raw = _log(
            ("Merge pull request #129 from rui-nar/fix/123-polarsteps", "", ""),
            ("Merge remote-tracking branch 'origin/main' into fix/123", "", ""),
            ("fix(people): show the trip on the map (#123)", "", "api/people.py"),
        )
        assert [c.subject for c in parse_commits(raw)] == ["show the trip on the map"]

    def test_version_bumps_are_dropped(self):
        raw = _log(
            ("chore: bump version to v0.46.9", "", ""),
            ("fix(map): restore the fit-to-trip zoom", "", "flutter_client/lib/a.dart"),
        )
        assert len(parse_commits(raw)) == 1

    def test_commits_are_ordered_oldest_first(self):
        """git log is newest-first; release notes read the other way."""
        raw = _log(
            ("fix(map): second", "", "flutter_client/a.dart"),
            ("fix(map): first", "", "flutter_client/a.dart"),
        )
        assert [c.subject for c in parse_commits(raw)] == ["first", "second"]


class TestSubjectParsing:
    def test_conventional_prefix_is_stripped(self):
        change = _one("fix(poster): keep emoji presentation selectors")
        assert change.kind == "fix"
        assert change.subject == "keep emoji presentation selectors"
        assert change.area == "Posters"

    def test_trailing_refs_become_links_not_text(self):
        change = _one("fix(people): a person inherits their group's encounters (#124)")
        assert change.subject == "a person inherits their group's encounters"
        assert change.refs == ("124",)

    def test_multiple_trailing_ref_groups_are_collected(self):
        """A squash-merged PR carries both its issue and its PR number."""
        change = _one("feat(metrics): Prometheus instrumentation (#125) (#133)")
        assert change.subject == "Prometheus instrumentation"
        assert change.refs == ("125", "133")

    def test_inline_references_stay_in_the_prose(self):
        """`_ISSUE_RE.sub` over the whole subject turned this into
        "(issue  follow-up)" — only a trailing group may be stripped."""
        change = _one("fix(editor): defer elevation profile (issue #45 follow-up)")

        assert change.subject == "defer elevation profile (issue #45 follow-up)"
        assert change.refs == ()

    def test_non_conventional_subject_survives(self):
        change = _one("update the deployment runbook")
        assert change.kind == "other"
        assert change.subject == "update the deployment runbook"


class TestTrailers:
    def test_release_note_wins_over_the_subject(self):
        """The whole point of the trailer: no parser turns "enforce the API
        quota process-wide" into something a user would recognise."""
        change = _one(
            "fix(strava): enforce the API quota process-wide, not per request",
            body="Release-Note: Strava import no longer risks exceeding the "
                 "request quota.",
        )
        assert change.text == "Strava import no longer risks exceeding the request quota."
        assert change.subject.startswith("enforce the API quota")

    def test_trailer_wraps_across_lines(self):
        change = _one("feat(map): x", body="Release-Note: first line\n  second line\n")
        assert change.release_note == "first line second line"

    def test_trailer_stops_at_a_blank_line(self):
        change = _one(
            "feat(map): x",
            body="Release-Note: the note\n\nUnrelated paragraph.\n",
        )
        assert change.release_note == "the note"

    def test_trailer_stops_at_the_next_git_trailer(self):
        """Trailers stack without blank lines; a Co-Authored-By line directly
        under the note used to be glued onto the published sentence."""
        change = _one(
            "feat(map): x",
            body="Release-Note: the note\n  wraps here.\n"
                 "Co-Authored-By: Someone <someone@example.com>\n"
                 "Signed-off-by: Someone <someone@example.com>\n",
        )
        assert change.release_note == "the note wraps here."

    def test_wrapped_line_that_looks_like_a_label_stays_in_the_note(self):
        change = _one(
            "feat(docker): x",
            body="Release-Note: The image moved.\n"
                 "Self-hosting: pull ghcr.io/rui-nar/traxjourney instead.\n"
                 "Claude-Session: https://example.com/s/1\n",
        )
        assert change.release_note == (
            "The image moved. Self-hosting: pull ghcr.io/rui-nar/traxjourney instead.")

    def test_subject_is_used_when_no_trailer(self):
        change = _one("fix(map): restore the fit-to-trip zoom")
        assert change.text == "restore the fit-to-trip zoom"

    def test_upgrade_note_trailer_is_captured(self):
        change = _one("feat(metrics): /metrics endpoint",
                      body="Upgrade-Note: Set METRICS_TOKEN to enable it.")
        assert change.upgrade_note == "Set METRICS_TOKEN to enable it."

    def test_breaking_marker_implies_an_upgrade_note(self):
        change = _one("feat(api)!: drop the legacy export route")
        assert change.upgrade_note == "drop the legacy export route"


class TestAudience:
    def test_known_scope_sets_area_and_audience(self):
        assert _one("fix(poster): x").audience == APP
        assert _one("fix(db): x").audience == SERVER

    def test_unknown_scope_falls_back_to_changed_paths(self):
        app = _one("feat: invite join flow", files="flutter_client/lib/a.dart")
        server = _one("feat: invite links", files="api/members.py")

        assert (app.audience, server.audience) == (APP, SERVER)

    def test_a_commit_touching_both_trees_counts_as_app(self):
        """It shipped something the user can see; that's the stronger signal."""
        change = _one("feat: companions", files="api/members.py\nflutter_client/a.dart")
        assert change.audience == APP

    def test_tooling_scopes_are_internal(self):
        assert _one("fix(ci): x").audience == INTERNAL
        assert _one("fix(release): x").audience == INTERNAL

    def test_scopeless_commit_gets_no_area_label(self):
        """Under an "In the app" heading, a generic "**App** —" on every bullet
        is noise."""
        assert _one("feat: invite join flow", files="flutter_client/a.dart").area == ""


class TestRendering:
    def _render(self, raw: str, **kwargs) -> str:
        return render(parse_commits(raw), version="v0.47.0",
                      previous="v0.46.9", **kwargs)

    def test_groups_by_type_with_links_and_compare(self):
        body = self._render(_log(
            ("feat(metrics): Prometheus instrumentation (#125)", "", "api/metrics.py"),
            ("fix(editor): compound track edits (#127)", "", "flutter_client/a.dart"),
        ))

        assert body.startswith("# TraxJourney v0.47.0\n")
        assert "## 🚀 New" in body
        assert "## 🐛 Fixed" in body
        assert "[#125](https://github.com/rui-nar/TraxJourney/issues/125)" in body
        assert "v0.46.9...v0.47.0" in body

    def test_audience_subheadings_only_when_both_are_present(self):
        mixed = self._render(_log(
            ("feat(map): a", "", "flutter_client/a.dart"),
            ("feat(db): b", "", "models/db.py"),
        ))
        assert "### In the app" in mixed and "### Server & deployment" in mixed

        app_only = self._render(_log(("feat(map): a", "", "flutter_client/a.dart")))
        assert "### In the app" not in app_only

    def test_internal_changes_are_collapsed_not_dropped(self):
        body = self._render(_log(
            ("chore: refresh the docs", "", "docs/x.md"),
            ("fix(ci): pin the runner", "", ".github/workflows/test.yml"),
        ))

        assert "<details><summary>Internal changes (2)</summary>" in body
        assert "## 🚀 New" not in body

    def test_upgrade_notes_get_their_own_section(self):
        body = self._render(_log(
            ("feat(metrics): endpoint", "Upgrade-Note: Set METRICS_TOKEN.",
             "api/metrics.py"),
        ))
        assert "## ⚠️ Upgrade notes" in body
        assert "Set METRICS_TOKEN." in body

    def test_unscoped_breaking_change_has_no_empty_area_prefix(self):
        body = self._render(_log(
            ("feat!: project files use a new extension", "", "api/x.py"),
        ))
        assert "- Project files use a new extension." in body
        assert "****" not in body

    def test_change_count_excludes_internal_noise(self):
        body = self._render(_log(
            ("fix(map): a", "", "flutter_client/a.dart"),
            ("chore: b", "", "docs/x.md"),
        ))
        assert "_1 change since v0.46.9_" in body

    def test_empty_range_says_so_rather_than_rendering_bare_headings(self):
        body = self._render("")
        assert "_No changes since v0.46.9._" in body

    def test_highlights_render_when_supplied(self):
        body = self._render(_log(("feat(map): a", "", "flutter_client/a.dart")),
                            highlights="Two fixes for the map.")
        assert "## ✨ Highlights" in body
        assert "Two fixes for the map." in body

    def test_no_highlights_heading_when_absent(self):
        assert "Highlights" not in self._render(
            _log(("feat(map): a", "", "flutter_client/a.dart")))


class TestPolish:
    """Layer 3 is a garnish — every failure path must return None so the
    deterministic notes still render and the release proceeds."""

    def _changes(self):
        return parse_commits(_log(("feat(map): a", "", "flutter_client/a.dart")))

    def test_returns_none_when_the_sdk_is_missing(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def _no_anthropic(name, *args, **kwargs):
            if name == "anthropic":
                raise ImportError("no module named anthropic")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_anthropic)
        assert polish(self._changes()) is None

    def test_returns_none_on_api_failure(self):
        fake = MagicMock()
        fake.Anthropic.side_effect = RuntimeError("no API key")
        with patch.dict("sys.modules", {"anthropic": fake}):
            assert polish(self._changes()) is None

    def test_returns_none_on_refusal(self):
        response = MagicMock(stop_reason="refusal", content=[])
        fake = MagicMock()
        fake.Anthropic.return_value.messages.create.return_value = response
        with patch.dict("sys.modules", {"anthropic": fake}):
            assert polish(self._changes()) is None

    def test_returns_the_text_on_success(self):
        block = MagicMock(type="text", text="  The map got faster.  ")
        response = MagicMock(stop_reason="end_turn", content=[block])
        fake = MagicMock()
        fake.Anthropic.return_value.messages.create.return_value = response

        with patch.dict("sys.modules", {"anthropic": fake}):
            assert polish(self._changes()) == "The map got faster."

    def test_skips_the_call_entirely_when_nothing_is_user_visible(self):
        """An internal-only release has nothing to write highlights about."""
        fake = MagicMock()
        with patch.dict("sys.modules", {"anthropic": fake}):
            assert polish(parse_commits(_log(("chore: x", "", "docs/x.md")))) is None
        fake.Anthropic.assert_not_called()

    def test_sends_the_visible_changes_to_the_model(self):
        block = MagicMock(type="text", text="ok")
        fake = MagicMock()
        create = fake.Anthropic.return_value.messages.create
        create.return_value = MagicMock(stop_reason="end_turn", content=[block])

        with patch.dict("sys.modules", {"anthropic": fake}):
            polish(parse_commits(_log(
                ("feat(map): faster rendering", "", "flutter_client/a.dart"),
                ("chore: tidy docs", "", "docs/x.md"),
            )))

        prompt = create.call_args.kwargs["messages"][0]["content"]
        assert "faster rendering" in prompt
        assert "tidy docs" not in prompt


class TestLanguageDetection:
    """v0.48.0 shipped with sixty Portuguese bullets between the English ones,
    because a `Release-Note:` trailer is written in whatever language the author
    was thinking in. The fixtures below are lines from that release."""

    @pytest.mark.parametrize("text", [
        "You can now hide activity icons on the trip map with a new toggle, "
        "next to the Memories and Encounters ones — handy when the map gets "
        "crowded.",
        "Fixed a bug where opening the map for a trip with many photos could "
        "overload the server and cause connection timeouts.",
        "Photos imported from Polarsteps now keep the same order they had on "
        "Polarsteps, instead of sometimes appearing scrambled.",
        "Poster generation now emails you a download link as soon as it's "
        "ready (or lets you know if it failed).",
    ])
    def test_english_is_not_flagged(self, text):
        assert not looks_non_english(text)

    @pytest.mark.parametrize("text", [
        "As fotos das memórias no mapa carregam agora instantaneamente ao "
        "reabrir a app, em vez de mostrarem círculos pretos.",
        "A app agora ocupa uma fração do espaço a descarregar e a instalar.",
        "Painel do Grafana \"Host Resources\" passou a mostrar CPU, carga e "
        "disco, e todos os dashboards permitem escolher entre produção e "
        "validação.",
        "Adiciona suporte para escolher fotos de substituição a partir de uma "
        "biblioteca Immich ligada.",
    ])
    def test_portuguese_is_flagged(self, text):
        assert looks_non_english(text)

    @pytest.mark.parametrize("text", [
        "add PR template",
        "raise marker thumbnail concurrency from 6 to 8",
        "regenerate pubspec.lock for sqflite",
        "Connect your Immich server from Settings.",
    ])
    def test_short_technical_english_is_not_flagged(self, text):
        """A bullet with no function words at all must stay quiet — a warning
        that fires on good English is worse than one that misses a line."""
        assert not looks_non_english(text)

    def test_a_url_is_not_a_romance_marker(self):
        """"com" and "de" look Portuguese and appear in English text and URLs,
        which is why they are left out of the marker set."""
        assert not looks_non_english(
            "See https://github.com/rui-nar/TraxJourney for the de-duplication "
            "rules that apply when a step is imported twice.")


class TestNonEnglishWarning:
    def _pt(self, subject="fix(map): x"):
        return parse_commits(_log((
            subject,
            "Release-Note: A app agora ocupa uma fração do espaço a instalar.",
            "flutter_client/a.dart",
        )))

    def test_reports_the_stray_entries(self, capsys):
        assert warn_non_english(self._pt()) == 1
        err = capsys.readouterr().err
        assert "1 entry" in err
        assert "docs/RELEASING.md" in err
        assert "ocupa uma fração" in err

    def test_silent_when_everything_is_english(self, capsys):
        changes = parse_commits(_log(
            ("fix(map): stop the day carousel from freezing", "",
             "flutter_client/a.dart")))
        assert warn_non_english(changes) == 0
        assert capsys.readouterr().err == ""

    def test_internal_changes_are_not_checked(self, capsys):
        """The internal block is raw commit subjects by design — nobody reads
        it for the prose, so a Portuguese one there is not worth a warning."""
        changes = parse_commits(_log(
            ("chore(ci): corrige a autenticação do scrape do Alloy", "",
             "docker-compose.yml")))
        assert warn_non_english(changes) == 0


class TestTranslate:
    """Same contract as :func:`polish`: every failure path leaves the
    deterministic notes intact rather than blocking the release."""

    def _pt(self):
        return parse_commits(_log((
            "fix(map): x",
            "Release-Note: A app agora ocupa uma fração do espaço a instalar.",
            "flutter_client/a.dart",
        )))

    def _reply(self, text):
        block = MagicMock(type="text", text=text)
        fake = MagicMock()
        fake.Anthropic.return_value.messages.create.return_value = MagicMock(
            stop_reason="end_turn", content=[block])
        return fake

    def test_replaces_the_flagged_entry(self):
        fake = self._reply("1. The app now takes a fraction of the space to install.")
        with patch.dict("sys.modules", {"anthropic": fake}):
            out = translate(self._pt())
        assert out[0].text == "The app now takes a fraction of the space to install."

    def test_translates_upgrade_notes_too(self):
        changes = parse_commits(_log((
            "feat(api): x",
            "Upgrade-Note: Define a variável LOG_LEVEL antes de reiniciar.",
            "api/x.py",
        )))
        fake = self._reply("1. Set the LOG_LEVEL variable before restarting.")
        with patch.dict("sys.modules", {"anthropic": fake}):
            out = translate(changes)
        assert out[0].upgrade_note == "Set the LOG_LEVEL variable before restarting."

    def test_skips_the_call_entirely_when_everything_is_english(self):
        fake = MagicMock()
        changes = parse_commits(_log(
            ("fix(map): stop the day carousel from freezing", "",
             "flutter_client/a.dart")))
        with patch.dict("sys.modules", {"anthropic": fake}):
            assert translate(changes) == changes
        fake.Anthropic.assert_not_called()

    def test_sends_only_the_flagged_lines(self):
        changes = parse_commits(_log(
            ("fix(map): stop the day carousel from freezing", "",
             "flutter_client/a.dart"),
            ("fix(map): y", "Release-Note: A app agora ocupa uma fração.",
             "flutter_client/b.dart"),
        ))
        fake = self._reply("1. The app now takes a fraction.")
        with patch.dict("sys.modules", {"anthropic": fake}):
            translate(changes)

        prompt = fake.Anthropic.return_value.messages.create.call_args \
            .kwargs["messages"][0]["content"]
        assert "ocupa uma fração" in prompt
        assert "day carousel" not in prompt

    def test_keeps_the_originals_on_a_count_mismatch(self):
        fake = self._reply("1. One line.\n2. An extra line nobody asked for.")
        with patch.dict("sys.modules", {"anthropic": fake}):
            changes = self._pt()
            assert translate(changes) == changes

    def test_keeps_the_originals_on_api_failure(self):
        fake = MagicMock()
        fake.Anthropic.side_effect = RuntimeError("no API key")
        with patch.dict("sys.modules", {"anthropic": fake}):
            changes = self._pt()
            assert translate(changes) == changes


@pytest.mark.parametrize("subject,expected_area", [
    ("fix(e2ee): x", "Encryption"),
    ("fix(track-edit): x", "Track editor"),
    ("fix(activity-split): x", "Track editor"),
    ("fix(view-mode): x", "Map"),
    ("fix(counters): x", "Trip stats"),
    ("feat(metrics): x", "Monitoring"),
])
def test_scope_maps_to_a_user_facing_area(subject, expected_area):
    """Internal scope names ("e2ee", "track-edit") mean nothing to a reader."""
    assert _one(subject).area == expected_area
