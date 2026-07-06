from src.pipeline_config import load_pipeline_config
from src import docstring_callback


def test_docstring_callback_name_comes_from_workbook_config(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = load_pipeline_config()
    assert config.docstring_callback_name == "series_docs"
    assert config.guide_path.is_file()

    monkeypatch.setattr(
        docstring_callback,
        "docstring_cache_path",
        lambda _repo_root: tmp_path / "missing-docstring-cache.json",
    )
    assert docstring_callback.configure_docstring_callback(config) == "series_docs"
