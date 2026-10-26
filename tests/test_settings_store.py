import json

import pytest

import settings_store as store


def test_migrates_legacy_without_modifying_it_and_separates_sections(tmp_path):
    legacy, target = tmp_path / "old.json", tmp_path / "new.json"
    original = '{"lang":"zh","quality":79,"fmt":"PNG","check_updates":false}'
    legacy.write_text(original, encoding="utf-8")
    data = store.load(target, legacy)
    data["video"] = {"quality": "high", "preset": "webm", "mute": True}
    store.save(data, target)
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    assert document["general"]["lang"] == "zh"
    assert document["image"]["quality"] == 79
    assert document["video"]["quality"] == "high"
    assert store.load(target, legacy) == data
    assert legacy.read_text(encoding="utf-8") == original


def test_forgetting_settings_removes_both_modes_but_keeps_general(tmp_path):
    path = tmp_path / "settings.json"
    values = {"remember_settings": False, "quality": 90, "quality_mode": "size", "target_size_val": 300,
              "out_dir": "private", "video": {"out_dir": "video", "mute": True}, "engine_dir": "tools"}
    store.save(values, path)
    document = json.loads(path.read_text())
    assert document["image"] == document["video"] == {}
    assert store.load(path)["engine_dir"] == "tools"


@pytest.mark.parametrize("content", ["bad json", "[]", '{"schema_version":2}', '{"schema_version":1,"general":[]}'])
def test_invalid_or_newer_settings_are_preserved(tmp_path, content):
    path = tmp_path / "settings.json"
    path.write_text(content)
    with pytest.raises(store.SettingsError):
        store.load(path)
    with pytest.raises(store.SettingsError):
        store.save({"lang": "en"}, path)
    assert path.read_text() == content


def test_failed_atomic_replace_preserves_old_settings(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    store.save({"lang": "en"}, path)
    original = path.read_bytes()
    def denied(*args):
        raise PermissionError("locked")
    monkeypatch.setattr(store.os, "replace", denied)
    with pytest.raises(PermissionError):
        store.save({"lang": "zh"}, path)
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_invalid_types_are_not_passed_to_tk(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"schema_version": 1, "general": {"lang": [], "check_updates": "false"},
                               "image": {"quality": "bad", "filename_tokens": 3},
                               "video": {"mute": [], "max_height": "bad"}}))
    assert store.load(path) == {"video": {}}
