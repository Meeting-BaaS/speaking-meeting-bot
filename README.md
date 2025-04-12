# Speaking Bot

<p align="center">
  <img src="./images/SpeakingBot.png" alt="Speaking Bot Banner" width="100%">
</p>

This repository expands upon [Pipecat](https://github.com/pipecat-ai/pipecat)'s Python framework for building voice and multimodal conversational agents. Our implementation creates AI meeting agents that can join and participate in Google Meet and Microsoft Teams meetings with distinct personalities and capabilities defined in Markdown files.

## Overview

This project extends [Pipecat's WebSocket server implementation](https://github.com/pipecat-ai/pipecat/tree/main/examples/websocket-server) to create:

- Meeting agents that can join Google Meet or Microsoft Teams through the [MeetingBaas API](https://meetingbaas.com)
- Customizable personas with unique context
- Support for running multiple instances via a simple API
- WebSocket-based communication for real-time interaction

## Architecture

### Core Framework: Pipecat Integration

[Pipecat](https://github.com/pipecat-ai/pipecat) provides the foundational framework with:

- Real-time audio processing pipeline
- WebSocket communication
- Voice activity detection
- Message context management

In this implementation, Pipecat is integrated with [Cartesia](https://www.cartesia.ai/) for speech generation (text-to-speech), [Gladia](https://www.gladia.io/) or [Deepgram](https://deepgram.com/) for speech-to-text conversion, and [OpenAI](https://platform.openai.com/)'s GPT-4 as the underlying LLM.

### API-First Architecture

The project now follows an API-first approach with:

- A lightweight FastAPI server that handles bot management
- WebSocket server for real-time communication
- Designed to be used as a backend service behind a more comprehensive API
- No authentication (meant to be handled by the parent API)

#### API Endpoints

1. Root endpoint (`GET /`):

   - Health check endpoint
   - Returns: `{"message": "MeetingBaas Bot API is running"}`

2. Run Bots (`POST /run-bots`):

   ```json
   {
     "count": 1,
     "meeting_url": "https://meet.google.com/xxx-yyyy-zzz",
     "personas": ["interviewer", "pair_programmer"],
     "recorder_only": false,
     "websocket_url": "ws://your-websocket-server:8000"
   }
   ```

   - All fields are optional except `count` and `meeting_url`
   - Returns WebSocket URL for real-time communication

3. WebSocket endpoint (`/ws/{client_id}`):
   - Real-time communication channel for bot control
   - Supports message broadcasting between connected clients

### Project Extensions

Building upon Pipecat, we've added:

- Persona system with Markdown-based configuration for:
  - Core personality traits and behaviors
  - Knowledge base and domain expertise
  - Additional contextual information (websites formatted to MD, technical documentation, etc.)
- AI image generation via [Replicate](https://replicate.com/docs)
- Image hosting through [UploadThing](https://uploadthing.com/) (UTFS)
- [MeetingBaas](https://meetingbaas.com) integration for video meeting platform support
- Multi-agent orchestration via API

## Required API Keys

### For Pipecat-related Services

- [OpenAI](https://platform.openai.com/) (LLM)
- [Cartesia](https://www.cartesia.ai/) (text-to-speech)
- [Gladia](https://www.gladia.io/) or [Deepgram](https://deepgram.com/) (speech-to-text)
- [MeetingBaas](https://meetingbaas.com) (video meeting platform integration)

### For Project-specific Add-ons

- [OpenAI](https://platform.openai.com/) (LLM to complete the user prompt and match to a Cartesia Voice ID)
- [Replicate](https://replicate.com/docs) (AI image generation)
- [UploadThing](https://uploadthing.com/) (UTFS) (image hosting)

For speech-related services (TTS/STT) and LLM choice (like Claude, GPT-4, etc), you can freely choose and swap between any of the integrations available in [Pipecat's supported services](https://docs.pipecat.ai/api-reference/services/supported-services).

### Important Note

[OpenAI](https://platform.openai.com/)'s GPT-4, [UploadThing](https://uploadthing.com/) (UTFS), and [Replicate](https://replicate.com/docs) are currently hard-coded specifically for the CLI-based persona generation features: matching personas to available voices from Cartesia, generating AI avatars, and creating initial personality descriptions and knowledge bases.
You do not need a Replicat or UTFS API key to run the project if you're not using the CLI-based persona creation feature and edit Markdowns manually.

## Persona System

### Bot Service

- Real-time audio processing pipeline
- WebSocket-based communication
- Tool integration (weather, time)
- Voice activity detection
- Message context management

- Dynamic persona loading from markdown files
- Customizable personality traits and behaviors
- Support for multiple languages
- Voice characteristic customization
- Image generation for persona avatars
- Metadata management for each persona

### Persona Structure

Each persona is defined in the `@personas` directory with:

- A README.md defining their personality
- Space for additional markdown files to expand knowledge and behaviour

### Example Persona Structure

```
@personas/
└── quantum_physicist/
    ├── README.md
    └── (additional beVhavior files)
```

## Prerequisites

- Python 3.x
- `grpc_tools` for protocol buffer compilation
- Ngrok (for local deployment)
- Poetry for dependency management

## Installation

### 1. Set Up Poetry Environment

```bash
# Install Poetry (Unix/macOS)
curl -sSL https://install.python-poetry.org | python3 -

# Install Poetry (Windows)
(Invoke-WebRequest -Uri https://install.python-poetry.org -UseBasicParsing).Content | py -

# Install dependencies
poetry install

# Activate virtual environment
poetry shell
```

### 2. Compile Protocol Buffers

```bash
poetry run python -m grpc_tools.protoc --proto_path=./protobufs --python_out=./protobufs frames.proto
```

### 3. Configure Environment

```bash
cp env.example .env
```

Edit `.env` with your MeetingBaas credentials.

## Running Meeting Agents

### Single Agent Deployment

To launch one agent into a meeting:

```bash
poetry run python scripts/batch.py -c 1 --meeting-url <your-meeting-url>
```

### Multiple Agent Deployment

To launch two agents simultaneously:

```bash
poetry run python scripts/batch.py -c 2 --meeting-url <your-meeting-url>
```

### Local Deployment with Ngrok

For 1-2 agents, use Ngrok to expose your local server:

```bash
ngrok start --all --config ~/.config/ngrok/ngrok.yml,./config/ngrok/config.yml
```

### Web Deployment

For more than 2 agents, deploy to a web server to avoid Ngrok limitations.

## Future Extensibility

The persona architecture is designed to support:

- Scrapping the websites given by the user to MD for the bot knowledge base
- Containerizing this nicely

## Troubleshooting

- Verify Poetry environment is activated
- Check Ngrok connection status
- Validate environment variables
- Ensure unique Ngrok URLs for multiple agents

For more detailed information about specific personas or deployment options, check the respective documentation in the `@personas` directory.

## Troubleshooting WebSocket Connections

### Handling Timing Issues with ngrok and Meeting Baas Bots

Sometimes, due to WebSocket connection delays through ngrok, the Meeting Baas bots may join the meeting before your local bot connects. If this happens:

- Simply press `Enter` to respawn your bot
- This will reinitiate the connection and allow your bot to join the meeting

This is a normal occurrence and can be easily resolved with a quick bot respawn.

## Running the API Server

### Local Development

```bash
# Install dependencies
poetry install

# Compile Protocol Buffers
poetry run python -m grpc_tools.protoc --proto_path=./protobufs --python_out=./protobufs frames.proto

# Run the API server with hot reload
poetry run uvicorn api:app --reload --host 0.0.0.0 --port 8000
```

### Local Testing with Multiple Bots

For local development and testing with multiple bots, you'll need two terminals:

```bash
# Terminal 1: Start the API server
poetry run uvicorn api:app --reload --host 0.0.0.0 --port 8000

# Terminal 2: Start ngrok to expose your local server
ngrok http 8000
```

Once ngrok is running, it will provide you with a public URL (e.g., `https://abc123.ngrok.io`). Use this URL for your WebSocket connections:

```bash
# Test the API with curl
curl -X POST https://your-ngrok-url/run-bots \
  -H "Content-Type: application/json" \
  -d '{
    "count": 2,
    "meeting_url": "https://your-meeting-url",
    "personas": ["interviewer"],
    "recorder_only": false,
    "websocket_url": "wss://your-ngrok-url"
  }'
```

Note:

- Use `wss://` instead of `ws://` when connecting through ngrok
- Each bot will create its own WebSocket connection to the server
- You can monitor the connections in the uvicorn logs

### API Improvements

The API has been improved to use direct parameter passing instead of modifying sys.argv, which makes it:

- More maintainable and less error-prone
- Compatible with Python best practices
- Easier to integrate with other systems
- Ready for containerization

The BotProxyManager now accepts parameters directly:

```python
manager = BotProxyManager()
asyncio.create_task(
    manager.async_main(
        count=request.count,
        meeting_url=request.meeting_url,
        websocket_url=websocket_url,
        personas=request.personas,
        recorder_only=request.recorder_only,
    )
)
```

This approach maintains backward compatibility with command-line usage while providing a cleaner API for programmatic use.

### Production Deployment

```bash
# Run the API server in production mode
poetry run uvicorn api:app --host 0.0.0.0 --port 8000
```

### API Documentation

Once the server is running, you can access:

- Interactive API docs: `http://localhost:8000/docs`
- OpenAPI specification: `http://localhost:8000/openapi.json`

## Future Development

The API-first approach enables several planned features:

1. Parent API Integration:

   - Authentication and authorization
   - Rate limiting
   - User management
   - Billing integration

2. Enhanced Bot Management:

   - Real-time bot status monitoring
   - Dynamic persona loading
   - Bot lifecycle management
   - Meeting recording and transcription

3. WebSocket Features:

   - Real-time bot control
   - Live transcription streaming
   - Meeting analytics
   - Multi-bot coordination

4. Persona Management:
   - Dynamic persona creation via API
   - Persona validation and testing
   - Knowledge base expansion
   - Voice characteristic customization
