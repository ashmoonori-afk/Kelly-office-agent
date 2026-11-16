# macOS baseline before platform implementation

Measured on macOS, Python 3.13, after the package rename and before platform
implementation. The source is PR-01 commit `5f44242`; all 957 case statuses match
the unchanged `b1e0054` baseline.

Command: `uv run --locked python -m pytest -q`.

Result: 623 passed, 214 failed, 84 setup errors, 36 existing platform skips.
Windows CI at the same commit passed all 957 tests.

The first PR-P1 commit adds a macOS CI job without platform fixes so the runner's
gap is measured independently. Subsequent commits must address causes rather than
retrying a failing job.

## Failure boundaries

- 244 failure/error messages directly report the unsupported platform capability.
  Safe directory creation, directory guards and publication are Windows-only.
- Parser failures propagate through document and verification tests.
- The startup module also assumes Windows subprocess flags and native log handles.
- One repeated-midnight metering case uses system timezone data whose 2026 Havana
  offset differs from the fixture's expected offset on this Mac.
- The isolated install script explicitly rejects macOS before doing any work.

Services, doctor and the isolated install path are assigned to PR-P2. This baseline
does not claim those paths work before their implementation.

## File-level results

The table records passing cases and cases that fail or error. Existing skips are
separate; no test was added to a skip list to obtain these results.

| Test file | Passed | Failed or error | Existing skips |
|---|---:|---:|---:|
| `tests/test_aggregate_output.py` | 42 | 1 | 0 |
| `tests/test_api.py` | 0 | 59 | 0 |
| `tests/test_api_workflow.py` | 0 | 5 | 0 |
| `tests/test_approval.py` | 5 | 0 | 0 |
| `tests/test_artifacts.py` | 7 | 4 | 0 |
| `tests/test_automation.py` | 26 | 4 | 0 |
| `tests/test_cli.py` | 0 | 0 | 2 |
| `tests/test_config.py` | 15 | 12 | 11 |
| `tests/test_context.py` | 7 | 2 | 0 |
| `tests/test_contracts.py` | 47 | 0 | 0 |
| `tests/test_db.py` | 3 | 0 | 0 |
| `tests/test_engine.py` | 10 | 0 | 0 |
| `tests/test_engine_regressions.py` | 9 | 0 | 0 |
| `tests/test_eval_automation.py` | 17 | 7 | 0 |
| `tests/test_eval_comparison.py` | 16 | 1 | 0 |
| `tests/test_eval_execution.py` | 13 | 2 | 0 |
| `tests/test_eval_live.py` | 4 | 11 | 0 |
| `tests/test_eval_qualification.py` | 0 | 28 | 0 |
| `tests/test_evals.py` | 39 | 1 | 0 |
| `tests/test_input_scope.py` | 1 | 3 | 0 |
| `tests/test_install_check.py` | 1 | 0 | 0 |
| `tests/test_live_smoke.py` | 4 | 16 | 0 |
| `tests/test_memory.py` | 3 | 12 | 0 |
| `tests/test_metered_workflow.py` | 8 | 0 | 0 |
| `tests/test_office.py` | 1 | 26 | 0 |
| `tests/test_office_workflow.py` | 0 | 6 | 0 |
| `tests/test_operations.py` | 0 | 15 | 0 |
| `tests/test_parser_security.py` | 4 | 24 | 2 |
| `tests/test_plan_reuse.py` | 13 | 0 | 0 |
| `tests/test_planning.py` | 34 | 1 | 0 |
| `tests/test_prepare_run.py` | 11 | 2 | 0 |
| `tests/test_provider_contract.py` | 16 | 0 | 0 |
| `tests/test_quality_review.py` | 16 | 0 | 0 |
| `tests/test_render_binding.py` | 15 | 1 | 0 |
| `tests/test_replanning.py` | 20 | 0 | 0 |
| `tests/test_retention.py` | 13 | 6 | 0 |
| `tests/test_routing.py` | 83 | 0 | 0 |
| `tests/test_runtime.py` | 12 | 8 | 0 |
| `tests/test_runtime_cache.py` | 0 | 12 | 0 |
| `tests/test_safe_io.py` | 7 | 0 | 21 |
| `tests/test_startup.py` | 0 | 9 | 0 |
| `tests/test_telegram.py` | 25 | 2 | 0 |
| `tests/test_telegram_recovery.py` | 21 | 4 | 0 |
| `tests/test_ui.py` | 0 | 2 | 0 |
| `tests/test_usage.py` | 32 | 1 | 0 |
| `tests/test_verification.py` | 5 | 5 | 0 |
| `tests/test_workflows.py` | 0 | 6 | 0 |
| `tests/test_xai_provider.py` | 18 | 0 | 0 |
