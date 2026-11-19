# Repository Guidelines

## Project Structure & Module Organization

`stream_clipper/` contains the Python service. `live/` handles recording, chat backends, rolling buffers, and spike detection; `vod/` handles ingestion and highlight scoring; `media/` handles transcription, layouts, motion, and rendering. Top-level modules coordinate the SQLite queue, workers, pipeline, delivery, and notifications. Keep CLI wiring in `cli.py`.

`tests/` contains unit and integration tests. `config/config.example.yaml` is the configuration template, and `assets/fonts/` holds the bundled font and license. Consult `README.md` for operations and `PLAN.md` for design context. Runtime files belong in ignored `data/`, `buffer/`, `models/`, and `output/` directories.

## Build, Test, and Development Commands

Use Python 3.12 or newer in a virtual environment.

- `python -m pip install -e ".[dev]"`: install the editable package and pytest.
- `python -m pytest -q`: run the test suite.
- `python -m pytest -q -m "not ffmpeg and not slow"`: run tests without media binaries or long-running work.
- `stream-clipper -c config/config.yaml validate-config`: validate local configuration after copying the example.
- `stream-clipper -c config/config.yaml run`: start locally; adjust container paths in the configuration first.
- `docker compose up -d --build`: build and start the containerized service and Ollama.
- `docker compose logs -f clipper`: follow service logs.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` functions and modules, and `PascalCase` classes. Follow existing type annotations, import grouping, and local formatting. Keep processing logic in its domain module and reuse configuration models. No formatter or linter is configured in `pyproject.toml`; avoid unrelated formatting changes.

## Testing Guidelines

Name files `tests/test_<module>.py` and functions `test_<behavior>`. Use pytest fixtures, `tmp_path` for filesystem isolation, and `monkeypatch` for external services or transcription. Mark media integration tests with `pytest.mark.ffmpeg`; they automatically skip when FFmpeg or FFprobe is missing. Reserve `slow` for model downloads or lengthy execution. Add regression coverage for behavior changes; no numeric coverage threshold is configured.

## Commit & Pull Request Guidelines

History uses short, descriptive subjects, often prefixed by a component, such as `Recorder:` or `LLM:`. Follow that pattern and keep commits focused. Pull requests should explain the problem, resulting behavior, configuration changes, and validation performed. Link relevant issues and include rendered samples when changing visual output.

## Security & Configuration

Keep tokens, OAuth credentials, and local settings out of commits. Use ignored `config/config.yaml` and `rclone/` for private configuration; update the example and README when configuration behavior changes.
