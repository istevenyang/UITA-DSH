"""证据落盘：路径安全、事件回读容错、产物索引。"""
from __future__ import annotations

import json
import os

import pytest

from uita.tools.dsh.evidence import (EvidenceError, EvidenceStore,
                                     _safe_join, new_session_id)


def test_new_session_id_shape():
    session_id = new_session_id()
    assert session_id.startswith("dsh-")
    assert len(session_id.split("-")) == 4


def test_safe_join_rejects_traversal(tmp_path):
    for bad in ("../evil", "a/b", "", ".hidden", ".."):
        with pytest.raises(EvidenceError):
            _safe_join(str(tmp_path), bad)


def test_safe_join_accepts_plain_name(tmp_path):
    path = _safe_join(str(tmp_path), "shot-1.png")
    assert os.path.dirname(path) == os.path.abspath(str(tmp_path))


def test_store_writes_and_reads_events(tmp_path):
    store = EvidenceStore("dsh-test", root=str(tmp_path))
    store.append_event({"seq": 1, "kind": "a"})
    store.append_event({"seq": 2, "kind": "b"})
    events = store.read_events()
    assert [event["kind"] for event in events] == ["a", "b"]
    assert store.read_events(limit=1)[0]["kind"] == "b"


def test_store_tolerates_broken_line(tmp_path):
    store = EvidenceStore("dsh-test", root=str(tmp_path))
    store.append_event({"seq": 1, "kind": "a"})
    with open(store.events_path, "a", encoding="utf-8") as handle:
        handle.write('{"seq": 2, "kind": "trunc\n')
    events = store.read_events()
    assert len(events) == 1


def test_write_json_atomic_and_rejects_bad_name(tmp_path):
    store = EvidenceStore("dsh-test", root=str(tmp_path))
    path = store.write_json("session.json", {"a": 1})
    assert json.load(open(path, encoding="utf-8")) == {"a": 1}
    with pytest.raises(EvidenceError):
        store.write_json("../escape.json", {})


def test_artifacts_listing_and_resolution(tmp_path):
    store = EvidenceStore("dsh-test", root=str(tmp_path))
    target = store.artifact_path("shot-1.png")
    with open(target, "wb") as handle:
        handle.write(b"png")
    items = store.list_artifacts()
    assert items[0]["name"] == "shot-1.png"
    assert items[0]["url"].endswith("/artifacts/shot-1.png")
    assert store.resolve_artifact("shot-1.png") == target
    assert store.resolve_artifact("missing.png") is None
    assert store.resolve_artifact("../x") is None


def test_read_events_missing_file(tmp_path):
    store = EvidenceStore("dsh-missing", root=str(tmp_path))
    assert store.read_events() == []
    assert store.list_artifacts() == []
