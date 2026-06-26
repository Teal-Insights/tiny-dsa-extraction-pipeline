from src import docstring_callback


def test_docstring_callback_is_always_requested(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        docstring_callback,
        "DOCSTRING_CACHE_PATH",
        tmp_path / "missing-docstring-cache.json",
    )

    assert (
        docstring_callback.available_docstring_callback()
        == docstring_callback.CALLBACK_NAME
    )
