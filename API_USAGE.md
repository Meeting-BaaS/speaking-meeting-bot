# Speaking Meeting Bot — API usage

AI bots that JOIN a Google Meet / Zoom / Teams call and TALK (STT → LLM → TTS).
This file covers driving the API **directly on the host** (loopback), plus the
public prod URL for off-box callers.

- **On this server (localhost):** `http://127.0.0.1:7014` — no TLS, straight to the port.
- **Prod (off-box):** `https://speaking.gmeetrecorder.com`
- **Auth:** header `x-meeting-baas-api-key: <MEETING_BAAS_API_KEY>` on every call
  except `/docs`, `/openapi.json`, `/redoc`, `/health`, `/ready`, `/`, `/webhook`.
- **Live schema / try-it:** `GET /docs` (Swagger UI), raw at `GET /openapi.json`.

## Get the key (on this box)

```bash
KEY=$(grep '^MEETING_BAAS_API_KEY=' /data/lazrossi/code/speaking-meeting-bot/.env_clean | cut -d= -f2-)
# fallback if that file moved:
# KEY=$(systemctl --user show speaking-meeting-bot -p Environment | tr ' ' '\n' | grep MEETING_BAAS_API_KEY | cut -d= -f2-)
```

## Send a bot — `POST /bots`

Creates a MeetingBaas bot + spawns the voice pipeline. Returns immediately; the
bot then joins and waits in the lobby until a host admits it.

```bash
curl -s -X POST http://127.0.0.1:7014/bots \
  -H "Content-Type: application/json" \
  -H "x-meeting-baas-api-key: $KEY" \
  -d '{
    "meeting_url": "https://meet.google.com/abc-defg-hij",
    "bot_name": "Vibe",
    "prompt": "You are Vibe, an upbeat engineer. Keep turns to 2-3 sentences.",
    "entry_message": "Hey everyone, Vibe here — let us get into it.",
    "enable_tools": false
  }'
# → 201 {"bot_id":"c804aa7c-cfe3-474c-ac87-a1907fce5db1"}
```

### Body fields (strict schema — unknown fields are REJECTED)

| field           | type      | notes |
|-----------------|-----------|-------|
| `meeting_url`   | string    | **required.** http(s) Meet/Zoom/Teams URL. |
| `bot_name`      | string    | Display name in the call. Also used as persona if it matches one. Keep DISTINCT per bot in the same call. |
| `prompt`        | string    | Ad-hoc system persona. Overrides persona lookup — best for one-off bots. |
| `personas`      | [string]  | Named persona(s) from `config/personas`; first available wins. |
| `entry_message` | string    | Spoken **verbatim** once admitted. Omit → bot stays reactive (no greeting). |
| `bot_image`     | string    | Optional avatar URL. |
| `enable_tools`  | bool      | Weather/time function-calling. Default `true`; set `false` for pure chat. |
| `extra`         | object    | Free-form context handed to the persona. |
| `turn_config`   | object    | VAD tuning: `{confidence, start_secs, stop_secs, min_volume}`. Optional. |
| `websocket_url` | string    | Override public WS base. Leave unset in prod. |

## Remove a bot — `DELETE /bots/{bot_id}`

Leaves the call, closes the socket, kills the pipeline process.

```bash
curl -s -X DELETE http://127.0.0.1:7014/bots/<bot_id> \
  -H "Content-Type: application/json" \
  -H "x-meeting-baas-api-key: $KEY" -d '{}'
# → 200 {"ok":true}
```

## Check status (MeetingBaas directly — needs internet)

```bash
curl -s https://api.meetingbaas.com/v2/bots/<bot_id>/status \
  -H "x-meeting-baas-api-key: $KEY"
# data.status: queued → joining_call → in_waiting_room → in_call_recording → …
```

The bot speaks its `entry_message` the moment status reaches `in_call_recording`
(i.e. when the host admits it from the lobby). Never admitted → it stays silent.

## Multiple bots in one call (debate / panel)

`POST /bots` once per bot, same `meeting_url`. Rules that matter:

- **Distinct `bot_name` each.** Floor control (bots not talking over each other)
  keys off the display name MeetingBaas reports back — same name = no gating.
- **Only ONE bot gets an `entry_message`** (the opener). The rest stay reactive
  so they respond instead of all greeting at once.
- **Keep turns short** in the `prompt` ("2-3 sentences"). Floor is held while a
  sibling speaks, with a 20s max-hold backstop — long monologues get cut off.

Example: two-bot debate — `Vibe` (opener, `entry_message` set) vs `Skeptic`
(reactive, no `entry_message`), each `prompt` pins its stance + "2-3 sentences".

## Ops (on this box)

```bash
systemctl --user status  speaking-meeting-bot
systemctl --user restart speaking-meeting-bot          # reload after a redeploy
journalctl --user -u speaking-meeting-bot -f           # live: join, floor, TTS
```

- **Port:** `127.0.0.1:7014`
- **State dir** (`SPEAKING_BOT_STATE_DIR`): `/data/lazrossi/speaking-meeting-bot`
  - `transcripts/<bot_id>.json` — rolling transcript (saved every 10s)
  - `floor/<key>.json` — current floor holder for bot-vs-bot turn-taking
  - `ready_signals/<client_id>.ready` — admission signal that releases the greeting
  - `call_summaries/` — end-of-call summaries
