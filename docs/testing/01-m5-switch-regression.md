# M5 Switch Regression Coverage Matrix

Concise index of Cold Switch invariants → existing tests. Does not replace
`docs/state-machines/01-cold-switch.md`.

| Scenario / invariant | Primary test file(s) |
|---|---|
| Successful Cold Switch (14 steps, probe, route/traffic/desired) | `worker/tests/test_cold_switch.py` (`test_cold_switch_happy_path`) |
| Resume without duplicate start/stop / routing-version bump | `worker/tests/test_cold_switch.py` (resume + `*_without_version_does_not_double_bump`) |
| Automatic rollback once; no duplicate `ROLLBACK_*`; final Source restored | `worker/tests/test_cold_switch.py` (rollback suite) |
| Unsafe/ambiguous rollback → MIR | `worker/tests/test_cold_switch.py` (health/probe/timeout/third-party) |
| Safe Cancel boundary (queued / pre / STOP race / post / during rollback) | `worker/tests/test_cold_switch_cancel.py`, `backend/tests/test_operations_cancel_api.py` |
| Explicit Retry lineage, eligibility, Idempotency-Key, concurrency | `backend/tests/test_operations_retry_api.py` |
| MIR reconciliation outcomes + probe/NA/lock/cooldown guards | `worker/tests/test_cold_switch_reconcile.py` |
| Cross-feature: reconciled `ROLLED_BACK` retryable | `backend/tests/test_operations_retry_api.py` |
| Cross-feature: reconciled terminal Job not reopened by stale recovery | `worker/tests/test_m5_switch_regression.py` |
| Cross-feature: cancel intent preserved across MIR reconcile | `worker/tests/test_cold_switch_reconcile.py`, `worker/tests/test_m5_switch_regression.py` |
| Cross-feature: retry child uses same Worker Cold Switch path | `worker/tests/test_m5_switch_regression.py` |
| Terminal Operation / non-terminal Job mismatch without side effects | `worker/tests/test_cold_switch.py`, `worker/tests/test_cold_switch_cancel.py` |

Out of scope for this matrix: HOT / AUTO / ALTERNATE_NODE / Admin UI.
