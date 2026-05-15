# Code Review Report — Speaking Meeting Bot

**Reviewer**: Code Reviewer Agent | **Date**: 2026-05-14 | **Files audited**: 14

---

## ✅ Strengths

- **Clean community separation**: Graph confirms strong cohesion in `Message Routing System` (0.14), `Connection Registry` (0.15), `FastAPI App Lifecycle` (0.17), `ProtobufConverter` (0.18), and `Audio Processing & TTS` (0.27).
- **Graceful process cleanup**: `terminate_process_gracefully()` correctly sends SIGTERM before SIGKILL with configurable timeout.
- **Protobuf abstraction**: `ProtobufConverter` neatly isolates serialization from the routing logic.
- **Pydantic validation**: `BotRequest` uses Pydantic v2 with `Field` descriptors and inline `Config.json_schema_extra` examples.

---

## 🔴 Critical Issues

### 1. `internal_client_id` NameError in `websockets.py` `finally` block
**File**: `app/websockets.py` — `websocket_endpoint()`  
**Status**: ✅ **Fixed by Bug Fixer Agent**  
`internal_client_id` was assigned inside the `try` block but referenced in `finally`. If the WebSocket accept raised before assignment, a `NameError` would crash cleanup silently.

### 2. Duplicate `import os` in `app/bots/openai_bot.py`
**Status**: ✅ **Fixed by Clean-up Agent**

### 3. No session persistence
**Risk**: All `MEETING_DETAILS` and `SessionManager` state lives in-memory. A server restart drops all active sessions with no recovery path.  
**Recommendation**: Add Redis or SQLite-backed persistence for production.

---

## 🟡 Warnings

### 4. `prompt_derived_details` may be referenced before assignment
**File**: `app/routes.py`, line ~234  
```python
elif request.prompt and prompt_derived_details:  # NameError if prompt is None branch
```
`prompt_derived_details` is only defined inside the `if request.prompt:` branch. The `elif` on the same variable outside that scope is guarded by `request.prompt` so it won't error, but it's fragile. Refactor to define `prompt_derived_details = None` at the top of `join_meeting()`.

### 5. Hardcoded `gpt-4-turbo-preview` model string
**File**: `app/bots/openai_bot.py`, line 222  
Move to `.env` as `OPENAI_MODEL` to allow easy swapping without code changes.

### 6. `PORT` env var read inconsistently
`app/main.py` defaults to `8000`, `app/routes_sales.py` defaults to `8000`, but `app/bots/openai_bot.py` uses `7014`. Document the two-port design (HTTP=8000, internal WS=7014) in `.env.example`.

### 7. Broad `except Exception` in pipeline
**File**: `app/bots/openai_bot.py` — `main()` catch-all re-raises but swallows the structured error context for monitoring. Add structured logging before re-raise.

---

## 🟢 Recommendations

| # | Recommendation | Priority |
|---|---|---|
| A | Add `prompt_derived_details = None` before `join_meeting` try block | High |
| B | Externalize `OPENAI_MODEL` to `.env` | Medium |
| C | Add Redis/SQLite session persistence for production | Medium |
| D | Document dual-port design in `.env.example` | Low |
| E | Add `pytest-asyncio` to `pyproject.toml` dev deps | Low |
| F | Serve `frontend/index.html` as a static route in `app/main.py` | Low |

---

## Test Coverage (Testing Agent)
- `tests/test_routes.py`: health check, `POST /bots` validation, SessionManager CRUD, ConnectionRegistry async connect/disconnect
- `tests/test_router.py`: MessageRouter send_binary, send_text, broadcast, send_to_pipecat, send_from_pipecat, closing-client skips, protobuf fallback
