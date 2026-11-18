# Safety Matrix

Generated: 2026-11-19

| Case | Result | Evidence |
|---|---|---|
| Front obstacle forward/reverse/turn gating | Pass | core/safety.py + test_phase3_safety.py |
| Rear obstacle backward gating | Pass | core/safety.py directional checks |
| Cliff trigger blocks forward and allows reverse/turn | Pass | test_phase3_safety.py |
| Sensor read failure/freshness | Pass | Missing/stale sensor snapshots now stop active motion; dedicated cases pass |
| 500 ms command watchdog | Partial | Safety watchdog exists; dedicated timing assertion is not present |
| E-stop latch/reset | Pass | Compatibility E-stop and reset checks passed |
| Linear/angular and wheel mapping | Pass | motion_controller.py |
| Runtime speed clamp | Pass | 39 safety checks passed after fixing manual-mode ceiling bypass |

Fix: [core/safety.py] now clamps both wheel duties through `max_speed_multiplier` in every mode. It also fails safe on missing sensor data or snapshots older than 1.5 seconds.

Result: `test_phase3_safety.py` — 39 passed, 0 failed.

