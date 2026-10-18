"""Recovery store, temp workspace and session autosave integration."""

from __future__ import annotations

import json
import os
import time

from offlinepdf.core.document import PdfSession
from offlinepdf.core.recovery import RecoveryStore, TempWorkspace, purge_stale_recovery


def test_temp_workspace_roundtrip(tmp_path):
    ws = TempWorkspace(base=tmp_path / "temp")
    f = ws.new_file(prefix="job", suffix=".pdf")
    assert f.parent == ws.base and not f.exists()
    f.write_bytes(b"data")
    assert ws.wipe() >= 1
    assert not f.exists()
    assert ws.wipe() == 0  # empty dir


def test_recovery_store_roundtrip(tmp_path):
    store = RecoveryStore(base=tmp_path / "rec")
    source = tmp_path / "doc.pdf"
    assert store.read(source) is None
    assert store.meta(source) is None

    store.write(source, b"snapshot-bytes")
    assert store.read(source) == b"snapshot-bytes"
    meta = store.meta(source)
    assert meta is not None and meta["size"] == len(b"snapshot-bytes")
    assert isinstance(meta["saved_at"], float)

    store.delete(source)
    assert store.read(source) is None and store.meta(source) is None


def test_recovery_keys_are_path_hashed(tmp_path):
    store = RecoveryStore(base=tmp_path / "rec")
    a = store.file_for(tmp_path / "a.pdf")
    b = store.file_for(tmp_path / "b.pdf")
    assert a != b and len(a.stem) == 24


def test_purge_stale_recovery(tmp_path):
    base = tmp_path / "rec"
    base.mkdir()
    old = base / "old.pdf"
    old.write_bytes(b"x")
    old_time = time.time() - 31 * 86400
    os.utime(old, (old_time, old_time))
    fresh = base / "fresh.pdf"
    fresh.write_bytes(b"y")

    removed = purge_stale_recovery(base)
    assert removed == 1
    assert not old.exists() and fresh.exists()


def test_purge_on_missing_dir(tmp_path):
    assert purge_stale_recovery(tmp_path / "nope") == 0


def test_session_autosave_roundtrip(tmp_path):
    import pymupdf

    doc = pymupdf.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()

    source = tmp_path / "doc.pdf"
    source.write_bytes(data)
    session = PdfSession.open_bytes(data, source_path=source)
    session.recovery = RecoveryStore(base=tmp_path / "rec")

    assert session.write_recovery() is True
    stored = session.recovery.read(source)
    assert stored is not None and stored.startswith(b"%PDF")

    # A real save clears the snapshot.
    dest = tmp_path / "saved.pdf"
    session.save_as(dest, overwrite=True)
    assert session.recovery.read(source) is None
    assert session.recovery.read(dest) is None
    session.close()


def test_session_recovery_disabled_without_path(corpus_dir, tmp_path):
    data = (corpus_dir / "basic.pdf").read_bytes()
    session = PdfSession.open_bytes(data)  # no source path
    session.recovery = RecoveryStore(base=tmp_path / "rec")
    assert session.write_recovery() is False
    session.close()


def test_recovery_meta_is_json(tmp_path):
    store = RecoveryStore(base=tmp_path / "rec")
    source = tmp_path / "doc.pdf"
    store.write(source, b"abc")
    raw = json.loads(store.meta_for(source).read_text(encoding="utf-8"))
    assert raw["source"] == str(source)
