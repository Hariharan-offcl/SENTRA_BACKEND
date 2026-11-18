# Contract Map

Generated: 2026-11-19

| Feature | Flutter evidence | HTTP/WS contract | Backend evidence | Match | Notes |
|---|---|---|---|---|---|
| Auth login | [api_endpoints.dart] | POST /api/auth/login | compat.py | Yes | Login 200 and role mapping verified |
| Rover pair | [api_endpoints.dart] | POST /api/auth/rover/pair | compat.py | Yes | Node token and role verified |
| Robot mode | [robot_service.dart] | PATCH /api/robots/{id}/mode | compat.py | Yes | POST/PATCH/PUT aliases accepted |
| Manual control | [control_service.dart] | POST /api/robots/{id}/control/move | compat.py + app_ws.py | Yes | Left/right contract uses -1..1 REST and -100..100 WS |
| Alerts | [alert_service.dart] | PATCH /api/alerts/{id}/dismiss | compat.py | Yes | POST/PATCH aliases verified |
| Mark unknown person known | [alert_detail_screen.dart] | POST /api/alerts/{id}/mark-known | compat.py | Added | Registers snapshot face, persists person, dismisses alert |
| People multipart | [people_service.dart] | POST /api/people | compat.py | Conditional | Passes or 400 without a detectable face on dev hardware |
| Call lifecycle | [call_service.dart] | POST /api/calls/{initiate,accept,reject,end} | compat.py | Yes | 65-check compatibility suite passed |
| Final multiplexed WS | [ws_client.dart] | /ws and /ws/user, /ws/rover | compat_ws.py + app_ws.py | Partial | /ws ping/pong/control/telemetry pass; role sockets need live phone test |

Missing contract gap: mark-as-known is now implemented and Flutter is wired.
Remaining: full signed auth/role matrix and real-phone camera/WebRTC signaling require hardware/manual verification.

