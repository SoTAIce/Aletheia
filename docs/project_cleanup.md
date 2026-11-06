# Project cleanup ? 2026-11-04

## Scope
At the user's request, removed tests/, validation and benchmark scripts/, init.ps1/init.sh test launchers, .pytest_cache/, application __pycache__/ and accessible temporary experiment artifacts. All pre-existing docs/ contents, runtime sources, configuration, dependencies, IDE settings, models, business data and output/ are preserved. No commit or push.

## Recoverability
Verified external backup: `C:\Users\48174\AppData\Local\Temp\Aletheia-cleanup-jjfezytd\removed.zip`.
The adjacent manifest.json contains original paths, sizes and SHA-256 hashes. The archive contains 68,384 files (129,027,524 bytes before compression), including tests and original temporary experiment evidence. Backup contents were read back and every SHA-256 verified before deletion. Treat the backup as private: experimental logs may contain model transcripts. OS temporary storage is not permanent; move this backup to durable storage if needed.
To restore, extract selected archive paths into the repository root; avoid overwriting newer work. Historical commands and artifact paths throughout docs/ describe the pre-cleanup checkout. Test code and scripts must be restored before those commands can run.

## Verification
Before cleanup: full regression 1093 passed, 2 skipped (34.90 seconds). Startup scoped checks: 59 passed, 1 skipped; existing pytest-cache permission warning.
After cleanup: all 91 app Python source files passed in-memory syntax compilation; SHA-256 comparison confirmed all existing runtime sources and docs, .env/.env.example and pyproject.toml unchanged. README and AGENTS.md were updated to reflect cleanup.
No model/API task or database operation was executed. `python -B -m app.agent --help` exited successfully and exposed both single/team execution modes; this verifies imports and argument parsing, not a live Agent task.

## Remaining limitation
Ten old tmp/pytest-harness-* fixture directories could not be fully removed due to access-denied errors, including after an elevated deletion attempt. Their readable files were included in the verified backup. No ownership/ACL modification was made. This is incomplete temporary-directory cleanup, not an application failure.

## Follow-up 2026-11-05

The ten tmp/pytest-harness-* directories were removed with `rd /s /q "\\?\<path>"`. The earlier failure came from paths longer than the Windows limit (a deep-root fixture), not from permissions; no ACL was changed. tmp/ is now empty. The TEMP items `aletheia-cleanup-check-*` (access denied) and `aletheia-cleanup-current.txt` (points to the backup folder) and the backup folder itself are kept.
