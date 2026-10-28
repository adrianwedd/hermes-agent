"""``hermes doctor`` names cache-root dirs that no pruner covers once they are big enough."""

from hermes_cli.doctor_state import unpruned_cache_hogs


def test_unpruned_cache_hogs_skips_pruned_dirs_and_small_entries(tmp_path):
    cache = tmp_path / "cache"
    for name in ("scratch", "terminal", "campaign-x", "web"):
        (cache / name).mkdir(parents=True)
        with open(cache / name / "blob", "wb") as fh:
            fh.truncate(2048)
            fh.write(b"x" * 2048)
    hogs = unpruned_cache_hogs(tmp_path, min_bytes=1024)
    assert {name for name, _ in hogs} == {"campaign-x", "web"}
    assert not unpruned_cache_hogs(tmp_path, min_bytes=1 << 20)
    assert all(size >= 2048 for _, size in hogs)


def test_scratch_diagnostics_preserve_project_containment(tmp_path, monkeypatch):
    from hermes_cli import doctor_state
    import hermes_constants_scratch_stuck

    messages = []
    for name in ("check_info", "check_warn", "check_ok"):
        monkeypatch.setattr(doctor_state, name, lambda text: messages.append(text))
    monkeypatch.setattr(doctor_state, "unpruned_cache_hogs", lambda home: [("campaign", 2 << 30)])
    monkeypatch.setattr(hermes_constants_scratch_stuck, "stuck_entries", lambda root: [])
    doctor_state._check_scratch_dir(tmp_path, "HERMES_HOME")

    assert any("canonical project folder" in message for message in messages)
    assert any("explicit authorisation" in message for message in messages)
    assert not any("move task files under" in message or "entries pruned after" in message for message in messages)
    assert any("never in prunable runtime scratch" in message for message in messages)
