"""Static offline gate: the project tree must contain no network references."""

from __future__ import annotations

from pathlib import Path

from offlinepdf.offline_check import scan_tree

ROOT = Path(__file__).resolve().parent.parent


def test_project_tree_has_no_violations():
    violations = scan_tree(ROOT)
    assert violations == [], "\n".join(str(v) for v in violations)


def test_scanner_flags_a_planted_violation(tmp_path):
    pkg = tmp_path / "offlinepdf"
    pkg.mkdir()
    (pkg / "bad.py").write_text('import requests\nurl = "https://cdn.example.com/x.js"\n')
    violations = scan_tree(tmp_path)
    rules = {v.rule for v in violations}
    assert "network-import" in rules
    assert "remote-url" in rules
