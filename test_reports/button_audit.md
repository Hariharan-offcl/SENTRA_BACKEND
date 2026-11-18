# Flutter Button & Widget Audit

Generated: 2026-11-19

## Automated result

| Screen | Widget | Evidence | Finding | Status |
|---|---|---|---|---|
| Splash | Auth/session restore | test/widget_test.dart | WebSocket session bridge was active during isolated splash test and left a timer pending | Fixed by test override; app bridge remains runtime behavior |
| Alert detail | REGISTER PERSON / mark known | alert_detail_screen.dart | Unknown-person alert had no direct snapshot registration flow | Replaced with MARK AS KNOWN dialog + backend call |
| Alert detail | Dismiss | alerts_provider.dart | Catch block swallowed API errors | Now stores UI error state |
| Patrol | Pause/Resume/Stop | services/api/patrol_service.dart | Existing compatibility guard prevents dead methods | Existing suite coverage |
| Manual control | Direction, brake, E-stop | manual_control_screen.dart | Handler present; WS and REST fallback wired | Verified by compatibility and safety suites |

No exhaustive tap-by-tap mock test suite exists; only the splash widget test was executable as a pre-existing test.
The targeted Flutter suite now passes: `flutter test` — 1 test, 0 failures.

