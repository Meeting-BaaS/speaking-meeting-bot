# Agent Work Division: Meeting Bot Project

This document defines roles and responsibilities for the AI agent team.

## Project Goal
Develop a conversational Google Meet bot that:
- Joins meetings and interacts naturally (context-aware, handles interruptions).
- Provides summaries and insights.
- Supports multiple user-defined personas via API-driven system prompts.

## Role Definitions

### 1. Orchestrator Agent (Master)
- **Role**: Workflow management and project oversight.
- **Deliverable**: Maintain `orchestrator_log.md` as the project source of truth.
- **Constraint**: Minimize total token consumption across all agents.

### 2. Backend Engineer
- **Tech**: FastAPI, Pydantic.
- **Tasks**: Develop scalable API endpoints and robust data validation models.

### 3. Frontend Engineer
- **Tech**: HTML, Vanilla CSS (other frameworks optional).
- **Features**: UI for Persona/Meeting management, real-time transcription, summaries, and bot interaction.

### 4. DevOps Agent
- **Tech**: Docker, Kubernetes.
- **Tasks**: Set up secure, stable infrastructure and deployment pipelines.

### 5. Integration Agent
- **Tasks**: Connect frontend to backend; ensure seamless end-to-end data flow and functionality.

### 6. Testing Agent
- **Role**: Continuous monitoring and session logging.
- **Tasks**: Flag unexpected behaviors/errors to the Bug Fixer Agent in real-time.

### 7. Bug Fixer Agent
- **Tasks**: Resolve issues identified by Testing and Code Reviewer agents to stabilize the codebase.

### 8. Code Reviewer Agent
- **Tasks**: Audit code for optimization, standards compliance, documentation, and test coverage.

### 9. Documentation Agent
- **Tasks**: Create and maintain comprehensive, accessible project documentation.

### 10. Clean-up Agent
- **Tasks**: Prune redundant code/files, optimize folder structure, and refactor for clarity.
