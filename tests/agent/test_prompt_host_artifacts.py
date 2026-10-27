"""Building host hints must not trigger creation/pruning of global scratch."""


def test_host_hints_are_read_only_for_artifact_storage(tmp_path, monkeypatch):
    from agent import prompt_builder

    def refuse(*args, **kwargs):
        raise AssertionError('Prompt rendering must not create or prune scratch')

    monkeypatch.setattr(prompt_builder, 'get_scratch_dir', refuse, raising=False)
    monkeypatch.setattr(prompt_builder, 'resolve_agent_cwd', lambda: str(tmp_path))
    before = list(tmp_path.iterdir())
    hints = prompt_builder._local_host_hints()
    assert str(tmp_path) in '\n'.join(hints)
    assert list(tmp_path.iterdir()) == before
