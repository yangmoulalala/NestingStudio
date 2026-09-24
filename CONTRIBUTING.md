# Contributing

## Development setup

```powershell
python -m pip install -r requirements-dev.txt
```

## Checks

```powershell
ruff check nesting nesting_studio tests auto_nest.py --output-format concise
$env:QT_QPA_PLATFORM="offscreen"
python -m unittest discover -s tests -v
```

## Pull requests

- Keep geometry and GUI responsibilities separated.
- Add a regression test for every bug fix.
- Do not infer thickness from filenames.
- Preserve `face_up_locked` semantics for STEP parts.
- Keep project and global-summary import compatibility.
- Avoid blocking the Qt main thread with CAD parsing or optimization.

## Code style

- Python 3.10-compatible syntax.
- Type annotations are preferred.
- Use `QThread` for file parsing and nesting.
- Run `ruff check` before submitting.
