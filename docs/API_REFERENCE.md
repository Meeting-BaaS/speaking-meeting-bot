# Speaking Meeting Bot — API Reference

> **Version**: 0.0.1 | **Base URL**: `https://speaking.meetingbaas.com` (or `http://localhost:8000` for local dev)

## Authentication

All endpoints (except `/health`, `/docs`) require the header:

```
x-meeting-baas-api-key: <your-meetingbaas-api-key>
```

---

## Endpoints

### 🤖 Bot Management

#### `POST /bots`
Create and deploy a speaking bot in a video meeting.

**Request Body** (`BotRequest`):
| Field | Type | Required | Description |
|---|---|---|---|
| `meeting_url` | string | ✅ | Google Meet / Zoom / Teams URL |
| `bot_name` | string | | Display name for the bot |
| `personas` | string[] | | Ordered list of persona names to try |
| `bot_image` | string | | Avatar URL |
| `entry_message` | string | | First spoken message on join |
| `enable_tools` | boolean | | Enable weather/time tools (default: `true`) |
| `prompt` | string | | Custom system prompt (creates dynamic persona) |
| `extra` | object | | Arbitrary extra data passed to MeetingBaas |

**Response** (`201 Created`):
```json
{ "bot_id": "meetingbaas-bot-uuid" }
```

**Errors**: `400` missing URL · `500` MeetingBaas API failure

---

#### `DELETE /bots/{bot_id}`
Remove a bot from a meeting.

**Path Param**: `bot_id` — the MeetingBaas bot ID returned from `POST /bots`

**Response** (`200 OK`):
```json
{ "message": "Bot removal request processed", "status": "success", "bot_id": "..." }
```

**Errors**: `400` missing ID · `404` bot not found · `500` removal failure

---

#### `GET /bots/{bot_id}/summary`
Return a live session summary for an active bot.

**Response** (`200 OK`):
```json
{
  "bot_id": "...",
  "client_id": "...",
  "meeting_url": "https://meet.google.com/...",
  "mode": "passive | active | ended",
  "notes": ["[System] Bot joined...", "..."],
  "extracted_needs": [],
  "transcriptions": [
    { "speaker": "Alice", "text": "Hello", "timestamp": "2026-05-14T..." }
  ],
  "created_at": "2026-05-14T...",
  "engaged_at": null,
  "ended_at": null
}
```

**Errors**: `404` no active session for this bot ID

---

### 🧑‍🎨 Persona Management

#### `POST /personas/generate-image`
Generate a portrait image for a persona using Replicate.

**Request Body** (`PersonaImageRequest`):
| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | ✅ | Persona name |
| `description` | string | ✅ | Appearance description |
| `gender` | string | | `male` / `female` / `non-binary` |
| `characteristics` | string[] | | Visual traits (e.g. "blue eyes") |

**Response** (`201 Created`):
```json
{ "name": "Alex", "image_url": "https://...", "generated_at": "2026-05-14T..." }
```

---

### 🔔 Webhooks

#### `POST /webhook`
Receive event callbacks from MeetingBaas (bot_joined, bot_left, transcription, etc.).

**Response**: `200 OK` `{"status": "ok"}`

---

### 📊 Sales Agent

#### `POST /run-bot`
Launch a sales bot that joins a meeting in passive listening mode.

**Request Body** (`RunBotRequest`):
| Field | Type | Required | Description |
|---|---|---|---|
| `meeting_url` | string | ✅ | Meeting URL |
| `marketing_person_email` | string | ✅ | Sales rep email (for report delivery) |
| `client_name` | string | ✅ | Prospect display name |
| `bot_name` | string | | Bot display name (default: `Sales Assistant`) |
| `bot_image` | string | | Avatar URL |
| `entry_message` | string | | Greeting message |

**Response** (`201 Created`):
```json
{ "bot_id": "...", "client_id": "...", "status": "joined" }
```

---

#### `POST /bot/{client_id}/engage`
Switch a passive sales bot into active Q&A mode.

**Request Body**:
```json
{ "max_minutes": 3, "pain_point": "optional seed topic" }
```

**Response** (`200 OK`): Returns mode, `engaged_at`, and `max_minutes`.

---

#### `GET /bot/{client_id}/report`
Retrieve meeting notes and extracted client needs.

**Query Params**: `send=true` — also emails the report to the marketing person.

**Response**: Full session report as JSON object.

---

#### `POST /meeting-baas/webhook`
MeetingBaas event webhook for the sales-agent flow.

---

### 🔌 WebSocket Endpoints

| Endpoint | Direction | Description |
|---|---|---|
| `WS /ws/{client_id}` | MeetingBaas → Server | Receives audio from the meeting |
| `WS /pipecat/{client_id}` | Pipecat → Server | Receives processed audio from the pipeline |

---

### ⚙️ System

#### `GET /health`
Health check.

**Response** (`200 OK`):
```json
{ "status": "ok", "service": "speaking-meeting-bot", "version": "1.0.0", "endpoints": [...] }
```

---

## Key Architecture Nodes (from Graph)

| Node | Edges | Role |
|---|---|---|
| `PersonaManager` | 19 | Loads & resolves persona configs |
| `join_meeting()` | 12 | Cross-community bridge: persona → meeting |
| `SessionManager` | 12 | In-memory session store for sales bots |
| `MessageRouter` | 9 | Routes audio between MeetingBaas ↔ Pipecat |
| `ConnectionRegistry` | 7 | WebSocket connection pool |
| `websocket_endpoint()` | 7 | Entry point for MeetingBaas audio stream |

---

## Pipeline Flow

```
MeetingBaas → /ws/{id} → MessageRouter → /pipecat/{id} (Pipecat)
Pipecat → /pipecat/{id} → MessageRouter → /ws/{id} → MeetingBaas
                    ↑
     STT → LLM → TTS (via Deepgram + OpenAI/Gemini + Cartesia)
```
