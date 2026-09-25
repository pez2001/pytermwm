# M5 - Docs and hardening

| id | title | plan.md feature | acceptance | status |
|----|-------|-----------------|------------|--------|
| PTW-059 | Documentation | full documentation | README, user guide, config, plugin, API docs | done |
| PTW-060 | Test suite + smoke test | (quality) | unittest suite, `scripts/smoke.py` | done |
| PTW-061 | Platform layer | portability | `compat.py`: dirs, shell, signals, threaded I/O pumps, TCP+key endpoints; Linux/macOS/Windows | done |
| PTW-062 | Windows backend | portability | ConPTY via ctypes (`winpty.py`), console client, Windows stats (`winsys.py`) | done (real ConPTY verified only by CI/`doctor`) |
| PTW-063 | Wrappers, doctor, CI | portability | `ptw`/`ptw.cmd`/`ptw.py`, `run_tests`/`run_tests.cmd`, `pytermwm doctor`, `scripts/smoke.py`, GitHub Actions matrix | done |
