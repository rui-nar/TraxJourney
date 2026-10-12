"""Tag -> iOS build name and build number (issue #579, plan D11).

App Store Connect refuses a build number it has already seen under the same
version, and closes a version to new builds once it is released. If validation
builds reuse a released version, or their numbers collide or sort above the
real release's, TestFlight uploads start failing and only a new tag fixes it.
"""
import pytest

from scripts import ios_build_number as ibn
from scripts.android_version_code import version_code


def _apple_order(build: str) -> tuple[int, ...]:
    """How App Store Connect orders CFBundleVersion: integer by integer."""
    return tuple(int(part) for part in build.split("."))


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("v1.0.0", ("1.0.0", "10000")),
        ("v0.53.1", ("0.53.1", "5301")),
        ("v1.2.3", ("1.2.3", "10203")),
    ],
)
def test_a_real_tag_matches_the_android_version_code(tag, expected):
    assert ibn.build_number(tag) == expected
    assert expected[1] == str(version_code(tag))


def test_validation_after_a_release_files_under_the_next_patch():
    """Not 1.0.0: that version's train closes once it is released."""
    assert ibn.build_number("validation", 17, "v1.0.0") == ("1.0.1", "10000.17")


def test_validation_before_any_release():
    assert ibn.build_number("validation", 17, None) == ("0.0.1", "0.17")
    assert ibn.build_number("validation", 17, "") == ("0.0.1", "0.17")


@pytest.mark.parametrize("run", [1, 9, 10, 17, 999])
def test_validation_sorts_below_the_real_build_of_its_version(run):
    """10000.N < 10001, the code the real v1.0.1 gets, for any run number."""
    name, number = ibn.build_number("validation", run, "v1.0.0")
    real_name, real_number = ibn.build_number(f"v{name}")
    assert real_name == name
    assert _apple_order(number) < _apple_order(real_number)


@pytest.mark.parametrize("run", [1, 9, 99, 999])
def test_validation_sorts_above_the_previous_run(run):
    """Including 9 -> 10, where a string comparison would go backwards."""
    _, previous = ibn.build_number("validation", run, "v1.0.0")
    _, current = ibn.build_number("validation", run + 1, "v1.0.0")
    assert _apple_order(current) > _apple_order(previous)


@pytest.mark.parametrize(
    "tag", ["latest", "v1.2", "", "vX.Y.Z", "1.0.0", "v1.0.0-rc1", "v0.100.0"]
)
def test_bad_tags_fail_loudly(tag):
    """A suffix or a missing v must break the build, not reach App Store Connect."""
    with pytest.raises(ValueError):
        ibn.build_number(tag)


@pytest.mark.parametrize("latest", ["validation", "v1.0", "v1.0.0-rc1"])
def test_a_bad_latest_tag_fails_loudly(latest):
    with pytest.raises(ValueError):
        ibn.build_number("validation", 3, latest)


@pytest.mark.parametrize("run", [None, 0, -1])
def test_validation_needs_a_run_number(run):
    with pytest.raises(ValueError):
        ibn.build_number("validation", run, "v1.0.0")


def test_a_real_tag_refuses_validation_options():
    with pytest.raises(ValueError):
        ibn.build_number("v1.0.0", 3)
    with pytest.raises(ValueError):
        ibn.build_number("v1.0.1", None, "v1.0.0")


def test_cli_prints_name_and_number(capsys):
    argv = ["ios_build_number.py", "validation", "--validation-run", "17",
            "--latest-tag", "v1.0.0"]
    assert ibn.main(argv) == 0
    assert capsys.readouterr().out == "1.0.1 10000.17\n"

    assert ibn.main(["ios_build_number.py", "v1.0.0"]) == 0
    assert capsys.readouterr().out == "1.0.0 10000\n"


def test_cli_reports_a_bad_tag_on_stderr(capsys):
    assert ibn.main(["ios_build_number.py", "v1.2"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "v1.2" in captured.err


def test_cli_usage_error_without_a_tag():
    with pytest.raises(SystemExit) as exc:
        ibn.main(["ios_build_number.py"])
    assert exc.value.code == 2
