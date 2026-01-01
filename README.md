
# Gitea AI Webhook Bot

A webhook-based service that automatically generates AI code reviews for Gitea Pull Requests using [xai-review](https://github.com/Nikita-Filonov/ai-review).

## Features
*   **Zero-config per repository**: Works via a single Gitea System Webhook (or per-repo webhook).
*   **Custom LLM Support**: Configuring it to use OpenAI, Ollama, vLLM, or any OpenAI-compatible endpoint.
*   **Secure**: Runs in an isolated Docker container with strict context passed via environment variables.

## Deployment

### Docker Compose
Run the container using the provided `compose.yaml`.

```bash
docker compose up -d
```

### Configuration (Environment Variables)

Create a `.env` file with the following variables:

| Variable | Required | Description | Default |
| :--- | :--- | :--- | :--- |
| `GITEA_TOKEN` | **Yes** | A Gitea Access Token with permission to read repos and post comments. | |
| `GITEA_API_URL` | No | Full URL to your Gitea API (e.g., `http://gitea:3000/api/v1`). | |
| `OPENAI_API_KEY` | **Yes** | API Key for your LLM provider. Use `dummy` for local models if no auth needed. | |
| `OPENAI_BASE_URL` | No | Base URL for the LLM API. Set this for Ollama/vLLM (e.g. `http://host.docker.internal:11434/v1/`). | `https://api.openai.com/v1/` |
| `LLM_MODEL` | No | The model name to request (e.g. `gpt-4o`, `llama3.1`). | `gpt-4o` |
| `LLM_TEMPERATURE` | No | The creativity of the model (0.0 - 1.0). | `0.2` |
| `LLM_PROVIDER` | No | The XAI provider type. Use `OPENAI` for most compatible services. | `OPENAI` |

#### Example `.env` (Ollama)
```bash
GITEA_TOKEN=da39a3ee5e6b4b0d3255bfef95601890afd80709
OPENAI_API_KEY=dummy
OPENAI_BASE_URL=http://host.docker.internal:11434/v1/
LLM_MODEL=llama3.1
```

## Setup in Gitea

1.  Navigate to **Site Administration > Webhooks** (for all repos) OR **Repo Settings > Webhooks**.
2.  Add a **Gitea** webhook.
3.  **Target URL**: `http://<container-ip>:3000/webhook` (e.g., `http://gitea-ai-review:3000/webhook`).
4.  **HTTP Method**: `POST`.
5.  **Trigger On**: Custom Events -> **Pull Request**.
