"""logrotate uses COPYTRUNCATE semantics because launchd holds the logs open — so the
tests assert the inode is preserved and an fd opened before rotation keeps working."""
import os

import pytest

import logrotate


@pytest.fixture(autouse=True)
def small_thresholds(monkeypatch):
    """Same code path, KB instead of MB, so tests stay fast."""
    monkeypatch.setattr(logrotate, "MAX_BYTES", 4096)
    monkeypatch.setattr(logrotate, "KEEP_BYTES", 1024)


def write_lines(path, n, prefix="line"):
    path.write_text("".join(f"{prefix} {i:06d}\n" for i in range(n)))
    return path


def test_small_file_untouched(tmp_path):
    p = write_lines(tmp_path / "a.log", 5)
    before = p.read_text()
    assert logrotate.rotate(str(p)) is None
    assert p.read_text() == before


def test_missing_file_returns_none(tmp_path):
    assert logrotate.rotate(str(tmp_path / "gone.log")) is None


def test_rotate_trims_and_reports_sizes(tmp_path):
    p = write_lines(tmp_path / "big.log", 2000)
    old_size = p.stat().st_size
    res = logrotate.rotate(str(p))
    assert res is not None
    size_before, size_after = res
    assert size_before == old_size
    assert size_after == p.stat().st_size < old_size


def test_rotate_keeps_the_tail_and_adds_a_header(tmp_path):
    p = write_lines(tmp_path / "big.log", 2000)
    logrotate.rotate(str(p))
    lines = p.read_text().splitlines()
    assert lines[0].startswith("--- log rotated:")
    assert lines[-1] == "line 001999"                      # newest line survives
    assert "line 000000" not in p.read_text()              # oldest is gone


def test_kept_portion_starts_on_a_line_boundary(tmp_path):
    p = write_lines(tmp_path / "big.log", 2000)
    logrotate.rotate(str(p))
    for line in p.read_text().splitlines()[1:]:
        assert line.startswith("line ") and len(line) == len("line 000000")


def test_rotate_preserves_the_inode_for_the_live_writer(tmp_path):
    """launchd holds an append-mode fd; replacing the inode would silently orphan it."""
    p = write_lines(tmp_path / "big.log", 2000)
    inode = p.stat().st_ino
    with open(p, "a") as live_writer:
        logrotate.rotate(str(p))
        live_writer.write("still writing\n")
    assert p.stat().st_ino == inode
    assert p.read_text().endswith("still writing\n")


def test_rotate_is_idempotent_once_under_the_limit(tmp_path):
    p = write_lines(tmp_path / "big.log", 2000)
    assert logrotate.rotate(str(p)) is not None
    assert logrotate.rotate(str(p)) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_rotate_reports_failure_without_raising(tmp_path, capsys):
    """An unwritable log must not take the whole rotation run down with it."""
    p = write_lines(tmp_path / "big.log", 2000)
    before = p.read_text()
    p.chmod(0o444)
    try:
        assert logrotate.rotate(str(p)) is None
    finally:
        p.chmod(0o644)
    assert "rotate failed" in capsys.readouterr().out
    assert p.read_text() == before


def test_main_only_rotates_oversized_log_and_err_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(logrotate, "BASE", str(tmp_path))
    big_log = write_lines(tmp_path / "bot.log", 2000)
    big_err = write_lines(tmp_path / "bot.err", 2000)
    small = write_lines(tmp_path / "quiet.log", 3)
    other = write_lines(tmp_path / "keep.json", 2000)       # not a log: never touched
    other_before = other.read_text()

    logrotate.main()

    out = capsys.readouterr().out
    assert "checked 3 files, rotated 2" in out
    assert big_log.read_text().startswith("--- log rotated:")
    assert big_err.read_text().startswith("--- log rotated:")
    assert not small.read_text().startswith("--- log rotated:")
    assert other.read_text() == other_before


def test_main_on_empty_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(logrotate, "BASE", str(tmp_path))
    logrotate.main()
    assert "checked 0 files, rotated 0" in capsys.readouterr().out
