from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path, PurePosixPath

import pytest

from untether.settings import TelegramFilesSettings
from untether.telegram import files as tg_files
from untether.telegram.files import ZipTooLargeError, zip_directory


def test_zip_directory_skips_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "dir"
    target.mkdir()
    (target / "safe.txt").write_text("ok", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link_path = target / "leak.txt"
    try:
        link_path.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")

    payload = zip_directory(root, Path("dir"), deny_globs=())

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = set(archive.namelist())

    assert "dir/safe.txt" in names
    assert "dir/leak.txt" not in names


def test_zip_directory_limits_size(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "dir"
    target.mkdir()
    (target / "data.bin").write_bytes(b"x" * 1024)

    with pytest.raises(ZipTooLargeError):
        zip_directory(root, Path("dir"), deny_globs=(), max_bytes=10)


def test_split_command_args_falls_back_on_bad_quotes() -> None:
    assert tg_files.split_command_args('bad "quote') == ("bad", '"quote')


def test_parse_file_command_unknown_command() -> None:
    command, rest, error = tg_files.parse_file_command("nope arg")
    assert command is None
    assert rest == "arg"
    assert error == tg_files.file_usage()


def test_parse_file_prompt_errors() -> None:
    path, force, error = tg_files.parse_file_prompt("--wat", allow_empty=False)
    assert path is None
    assert force is False
    assert error == "unknown flag: --wat"

    path, force, error = tg_files.parse_file_prompt("", allow_empty=False)
    assert path is None
    assert force is False
    assert error == "missing path"


def test_parse_file_prompt_force_flag() -> None:
    path, force, error = tg_files.parse_file_prompt(
        "--force note.txt", allow_empty=False
    )
    assert path == "note.txt"
    assert force is True
    assert error is None


def test_normalize_relative_path_rejects_invalid() -> None:
    for value in ("", "   ", "~/.ssh", "/etc/passwd", "../secret", ".git/config"):
        assert tg_files.normalize_relative_path(value) is None


def test_normalize_relative_path_rejects_dot_only() -> None:
    assert tg_files.normalize_relative_path("./") is None


def test_normalize_relative_path_strips_dots() -> None:
    assert tg_files.normalize_relative_path("docs/./guide.txt") == Path(
        "docs/guide.txt"
    )


def test_resolve_path_within_root_rejects_escape(tmp_path: Path) -> None:
    assert tg_files.resolve_path_within_root(tmp_path, Path("../escape")) is None


def test_deny_reason_matches_patterns() -> None:
    assert tg_files.deny_reason(Path(".git/config"), ["**/*.pem"]) == ".git/**"
    assert tg_files.deny_reason(Path("secrets/key.pem"), ["**/*.pem"]) == "**/*.pem"


def test_format_bytes_various_units() -> None:
    assert tg_files.format_bytes(0) == "0 b"
    assert tg_files.format_bytes(1536) == "1.5 kb"
    assert tg_files.format_bytes(20480) == "20 kb"


def test_default_upload_name_fallbacks() -> None:
    assert tg_files.default_upload_name("", "files/report.txt") == "report.txt"
    assert tg_files.default_upload_name(None, None) == "upload.bin"


def test_deduplicate_target_no_conflict(tmp_path: Path) -> None:
    target = tmp_path / "file.jpg"
    assert tg_files.deduplicate_target(target) == target


def test_deduplicate_target_single_conflict(tmp_path: Path) -> None:
    target = tmp_path / "file.jpg"
    target.write_bytes(b"existing")
    result = tg_files.deduplicate_target(target)
    assert result == tmp_path / "file_1.jpg"


def test_deduplicate_target_multiple_conflicts(tmp_path: Path) -> None:
    for name in ("file.jpg", "file_1.jpg", "file_2.jpg"):
        (tmp_path / name).write_bytes(b"x")
    result = tg_files.deduplicate_target(tmp_path / "file.jpg")
    assert result == tmp_path / "file_3.jpg"


def test_deduplicate_target_no_extension(tmp_path: Path) -> None:
    target = tmp_path / "Makefile"
    target.write_bytes(b"x")
    result = tg_files.deduplicate_target(target)
    assert result == tmp_path / "Makefile_1"


# ---------------------------------------------------------------------------
# #831 / #389 group A — recursive deny-glob matching
# ---------------------------------------------------------------------------

_DEFAULT_GLOBS = tuple(TelegramFilesSettings().deny_globs)


def _symlink(link: Path, target: Path | str) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")


@pytest.mark.parametrize(
    "rel",
    [
        ".env.local",
        "key.pem",
        "server.key",
        "id_rsa",
        "id_ed25519",
        ".npmrc",
        ".netrc",
        ".pypirc",
        ".envrc",
        ".ssh/config",
        ".ssh/id_rsa",
    ],
)
def test_deny_reason_root_level_defaults(rel: str) -> None:
    assert tg_files.deny_reason(Path(rel), _DEFAULT_GLOBS) is not None


def test_deny_reason_deep_trailing_globstar() -> None:
    assert tg_files.deny_reason(Path("a/b/.ssh/x/y"), _DEFAULT_GLOBS) == "**/.ssh/**"
    assert tg_files.deny_reason(Path(".git/a/b"), _DEFAULT_GLOBS) == ".git/**"
    # A trailing ``/**`` covers every depth below a matching ancestor.
    assert tg_files.deny_reason(Path("secrets/a/b/c"), ["secrets/**"]) == "secrets/**"
    assert tg_files.deny_reason(Path("x/secrets/a/b"), ["secrets/**"]) == "secrets/**"
    # ...but not the directory itself (same as ``full_match``).
    assert tg_files.deny_reason(Path("secrets"), ["secrets/**"]) is None


def test_deny_reason_middle_globstar() -> None:
    assert tg_files.deny_reason(Path("a/b"), ["a/**/b"]) == "a/**/b"
    assert tg_files.deny_reason(Path("a/x/y/b"), ["a/**/b"]) == "a/**/b"
    assert tg_files.deny_reason(Path("a/x/c"), ["a/**/b"]) is None


def test_deny_reason_keeps_right_anchored_names() -> None:
    assert tg_files.deny_reason(Path("a/b/.env"), [".env"]) == ".env"
    assert tg_files.deny_reason(Path("x/secrets/key.txt"), ["secrets/*"]) == (
        "secrets/*"
    )


def test_deny_reason_root_env_example_denied() -> None:
    # #389 Decision 6: a root-level .env.example now matches **/.env.*.
    assert tg_files.deny_reason(Path(".env.example"), _DEFAULT_GLOBS) == "**/.env.*"
    assert tg_files.deny_reason(Path("a/.env.example"), _DEFAULT_GLOBS) == ("**/.env.*")


@pytest.mark.parametrize(
    "rel",
    [
        "src/main.py",
        "docs/env.md",
        "environment.py",
        "keys/readme.md",
        "sshd.md",
        "ssh/config",
        "gitignore",
        ".github/workflows/ci.yml",
        "pem.txt",
    ],
)
def test_deny_reason_no_false_positives(rel: str) -> None:
    assert tg_files.deny_reason(Path(rel), _DEFAULT_GLOBS) is None


def test_deny_reason_git_casefold() -> None:
    # #390 D4: case-insensitive filesystems (macOS APFS).
    assert tg_files.deny_reason(Path(".GIT/hooks/x"), ()) == ".git/**"
    assert tg_files.deny_reason(Path("a/.Git/config"), ()) == ".git/**"


def test_deny_reason_git_glob_depth() -> None:
    # H12: pins the stdlib quirk and the fix together.
    posix = PurePosixPath(".git/hooks/pre-commit")
    assert posix.match(".git/**") is False
    assert tg_files.deny_reason(Path(".git/hooks/pre-commit"), [".git/**"]) == (
        ".git/**"
    )
    assert tg_files._glob_matches(posix, ".git/**") is True


_ORACLE_PATHS = [
    ".env",
    ".env.local",
    ".env.example",
    "a/.env",
    "a/b/.env.prod",
    ".envrc",
    "sub/.envrc",
    "key.pem",
    "a/key.pem",
    "a/b/c/cert.pem",
    "server.key",
    "a/server.key",
    "id_rsa",
    "home/id_rsa",
    "id_ed25519",
    ".ssh",
    ".ssh/config",
    "a/.ssh/id_rsa",
    "a/b/.ssh/x/y",
    ".netrc",
    "a/.npmrc",
    ".pypirc",
    "src/main.py",
    "docs/env.md",
    "keys/readme.md",
    "sshd.md",
    "notes/key.pem.txt",
]


@pytest.mark.skipif(sys.version_info < (3, 13), reason="needs PurePath.full_match")
def test_deny_reason_matches_full_match_oracle() -> None:
    for rel in _ORACLE_PATHS:
        posix = PurePosixPath(rel)
        expected = any(
            posix.full_match(glob) or posix.match(glob)  # type: ignore[attr-defined]
            for glob in _DEFAULT_GLOBS
        )
        assert (tg_files.deny_reason(Path(rel), _DEFAULT_GLOBS) is not None) is (
            expected
        ), rel


# ---------------------------------------------------------------------------
# #389 group B + #390 H1-H14 — check_path_access
# ---------------------------------------------------------------------------


def test_check_path_access_plain_file_ok(tmp_path: Path) -> None:
    # H1
    check = tg_files.check_path_access(tmp_path, Path("a/b.txt"), _DEFAULT_GLOBS)
    assert check.ok
    assert check.rel == Path("a/b.txt")
    assert check.root == tmp_path.resolve()
    assert check.target == tmp_path.resolve() / "a" / "b.txt"
    assert check.via_symlink is False


def test_check_path_access_dir_symlink_into_git_denied(tmp_path: Path) -> None:
    # H2
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    _symlink(tmp_path / "docs" / "x", Path("../.git/hooks"))
    check = tg_files.check_path_access(
        tmp_path, Path("docs/x/pre-commit"), _DEFAULT_GLOBS
    )
    assert check.reason == "denied"
    assert check.rule == ".git/**"
    assert check.via_symlink is True
    assert check.target is None
    assert check.rel is None
    assert not check.ok


def test_check_path_access_file_symlink_to_env_denied(tmp_path: Path) -> None:
    # H3
    (tmp_path / ".env").write_text("SECRET", encoding="utf-8")
    _symlink(tmp_path / "cfg.txt", Path(".env"))
    check = tg_files.check_path_access(tmp_path, Path("cfg.txt"), _DEFAULT_GLOBS)
    assert check.reason == "denied"
    assert check.rule == ".env"


def test_check_path_access_dangling_symlink_to_env_denied(tmp_path: Path) -> None:
    # H4: dangling links resolve to their destination.
    _symlink(tmp_path / "new.txt", Path(".env"))
    check = tg_files.check_path_access(tmp_path, Path("new.txt"), _DEFAULT_GLOBS)
    assert check.reason == "denied"
    assert check.rule == ".env"


def test_check_path_access_dir_symlink_into_ssh_denied(tmp_path: Path) -> None:
    # H5
    (tmp_path / "r" / ".ssh").mkdir(parents=True)
    _symlink(tmp_path / "keys", Path("r/.ssh"))
    check = tg_files.check_path_access(
        tmp_path, Path("keys/authorized_keys"), _DEFAULT_GLOBS
    )
    assert check.reason == "denied"
    assert check.rule == "**/.ssh/**"


def test_check_path_access_benign_symlink_allowed(tmp_path: Path) -> None:
    # H6
    (tmp_path / "benchmarks").mkdir()
    _symlink(tmp_path / "bench", Path("benchmarks"))
    check = tg_files.check_path_access(tmp_path, Path("bench/x.txt"), _DEFAULT_GLOBS)
    assert check.ok
    assert check.rel == Path("benchmarks/x.txt")
    assert check.via_symlink is True


def test_check_path_access_escape_rejected(tmp_path: Path) -> None:
    # H7
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    _symlink(root / "out", tmp_path / "outside")
    check = tg_files.check_path_access(root, Path("out/x"), _DEFAULT_GLOBS)
    assert check.reason == "outside"
    assert not check.ok
    assert tg_files.resolve_path_within_root(root, Path("out/x")) is None


def test_check_path_access_symlinked_root_ok(tmp_path: Path) -> None:
    # H8
    (tmp_path / "real").mkdir()
    _symlink(tmp_path / "link", Path("real"))
    root = tmp_path / "link"
    check = tg_files.check_path_access(root, Path("a.txt"), _DEFAULT_GLOBS)
    assert check.ok
    assert check.rel == Path("a.txt")
    assert check.root == (tmp_path / "real").resolve()
    assert check.target is not None
    assert check.target.relative_to(check.root) == Path("a.txt")


def test_check_path_access_symlink_loop_unresolvable(tmp_path: Path) -> None:
    # H9: 3.12 raises RuntimeError, 3.13+ returns the unresolved path.
    _symlink(tmp_path / "loop1", Path("loop2"))
    _symlink(tmp_path / "loop2", Path("loop1"))
    check = tg_files.check_path_access(tmp_path, Path("loop1/x.txt"), _DEFAULT_GLOBS)
    assert check.reason == "unresolvable"
    assert not check.ok


def test_check_path_access_requested_deny_wins(tmp_path: Path) -> None:
    # H10
    check = tg_files.check_path_access(tmp_path, Path(".env"), _DEFAULT_GLOBS)
    assert check.reason == "denied"
    assert check.rule == ".env"
    assert check.via_symlink is False


def test_check_path_access_hidden_via_symlink(tmp_path: Path) -> None:
    # H11
    (tmp_path / ".hidden").mkdir()
    _symlink(tmp_path / "vis", Path(".hidden"))
    check = tg_files.check_path_access(
        tmp_path, Path("vis/x"), _DEFAULT_GLOBS, deny_hidden=True
    )
    assert check.reason == "hidden"
    assert check.via_symlink is True
    check = tg_files.check_path_access(tmp_path, Path("vis/x"), _DEFAULT_GLOBS)
    assert check.ok
    assert check.rel == Path(".hidden/x")


def test_check_path_access_absolute_candidate(tmp_path: Path) -> None:
    # H13
    root_r = tmp_path.resolve()
    check = tg_files.check_path_access(tmp_path, root_r / "a" / "b.txt", ())
    assert check.ok
    assert check.rel == Path("a/b.txt")
    check = tg_files.check_path_access(tmp_path, Path("/etc/passwd"), ())
    assert check.reason == "outside"
    _symlink(tmp_path / "cfg.txt", Path(".env"))
    check = tg_files.check_path_access(tmp_path, root_r / "cfg.txt", _DEFAULT_GLOBS)
    assert check.reason == "denied"
    assert check.rule == ".env"


def test_check_path_access_dotdot_candidate(tmp_path: Path) -> None:
    # H14
    check = tg_files.check_path_access(tmp_path, Path("a/../b.txt"), _DEFAULT_GLOBS)
    assert check.ok
    assert check.rel == Path("b.txt")
    check = tg_files.check_path_access(tmp_path, Path("a/../../x"), _DEFAULT_GLOBS)
    assert check.reason == "outside"
    check = tg_files.check_path_access(tmp_path, Path("a/../.env"), _DEFAULT_GLOBS)
    assert check.reason == "denied"
    assert check.rule == ".env"


def test_check_path_access_denied_name_symlink_to_ok(tmp_path: Path) -> None:
    (tmp_path / "ok.txt").write_text("fine", encoding="utf-8")
    _symlink(tmp_path / ".env", Path("ok.txt"))
    check = tg_files.check_path_access(tmp_path, Path(".env"), _DEFAULT_GLOBS)
    assert check.reason == "denied"
    assert check.rule == ".env"


def test_check_path_access_hidden_component(tmp_path: Path) -> None:
    check = tg_files.check_path_access(
        tmp_path, Path(".untether/untether.toml"), _DEFAULT_GLOBS, deny_hidden=True
    )
    assert check.reason == "hidden"


def test_check_path_access_hidden_allowlisted(tmp_path: Path) -> None:
    check = tg_files.check_path_access(
        tmp_path,
        Path(".github/workflows/ci.yml"),
        _DEFAULT_GLOBS,
        deny_hidden=True,
        hidden_allow=frozenset({".github", ".gitignore"}),
    )
    assert check.ok


def test_check_path_access_hidden_off_by_default(tmp_path: Path) -> None:
    check = tg_files.check_path_access(
        tmp_path, Path(".untether/untether.toml"), _DEFAULT_GLOBS
    )
    assert check.ok


def test_check_path_access_deny_beats_allowlist(tmp_path: Path) -> None:
    check = tg_files.check_path_access(
        tmp_path,
        Path(".github/.env"),
        _DEFAULT_GLOBS,
        deny_hidden=True,
        hidden_allow=frozenset({".github", ".env"}),
    )
    assert check.reason == "denied"


def test_check_path_access_root_path_itself_hidden(tmp_path: Path) -> None:
    root = tmp_path / ".hidden" / "proj"
    root.mkdir(parents=True)
    check = tg_files.check_path_access(
        root, Path("src/a.py"), _DEFAULT_GLOBS, deny_hidden=True
    )
    assert check.ok
    check = tg_files.check_path_access(
        root, root.resolve() / "src" / "a.py", _DEFAULT_GLOBS, deny_hidden=True
    )
    assert check.ok
    assert check.rel == Path("src/a.py")


def test_check_path_access_root_itself_ok(tmp_path: Path) -> None:
    check = tg_files.check_path_access(
        tmp_path, Path("."), _DEFAULT_GLOBS, deny_hidden=True
    )
    assert check.ok
    assert check.target == tmp_path.resolve()


def test_check_path_access_missing_path_not_checked_for_existence(
    tmp_path: Path,
) -> None:
    check = tg_files.check_path_access(tmp_path, Path("nope/x.txt"), _DEFAULT_GLOBS)
    assert check.ok
    missing = tg_files.check_path_access(tmp_path, Path(".env"), _DEFAULT_GLOBS)
    (tmp_path / ".env").write_text("x", encoding="utf-8")
    present = tg_files.check_path_access(tmp_path, Path(".env"), _DEFAULT_GLOBS)
    assert missing == present
    assert missing.reason == "denied"


def test_check_path_access_raw_path_resolved_with_os_semantics(
    tmp_path: Path,
) -> None:
    (tmp_path / "sub" / "deep").mkdir(parents=True)
    _symlink(tmp_path / "link", Path("sub/deep"))
    check = tg_files.check_path_access(tmp_path, Path("link/../x.txt"), ())
    assert check.ok
    # The kernel follows ``link`` before ``..``: sub/x.txt, not the root x.txt.
    assert check.rel == Path("sub/x.txt")
