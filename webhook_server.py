
import os
import shutil
import subprocess
import tempfile
import logging
from typing import Optional, Dict, Any
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from pydantic import BaseModel

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI()

# Configuration from environment variables
GITEA_TOKEN = os.getenv("GITEA_TOKEN")
# XAI Review configuration should also be in env vars (e.g. OPENAI_API_KEY, etc.)

class GiteaWebhookPayload(BaseModel):
    action: str
    number: int
    pull_request: Dict[str, Any]
    repository: Dict[str, Any]
    sender: Dict[str, Any]

def run_review_task(payload: Dict[str, Any]):
    repo_name = payload["repository"]["full_name"]
    pr_number = payload["number"]
    clone_url = payload["repository"]["clone_url"]
    head_sha = payload["pull_request"]["head"]["sha"]
    base_ref = payload["pull_request"]["base"]["ref"]
    head_ref = payload["pull_request"]["head"]["ref"] # Branch name
    
    logger.info(f"Starting review for PR #{pr_number} in {repo_name} (SHA: {head_sha})")

    # Create a temporary directory for the repo
    work_dir = tempfile.mkdtemp(prefix="gitea-review-")
    try:
        # Clone the repository
        # We might need authentication for private repos. 
        # For simplicity, assuming public or token usage in URL if needed.
        # Ideally, use the GITEA_TOKEN to authenticate.
        
        # Inject token into URL if provided and not already present
        clean_clone_url = clone_url
        if GITEA_TOKEN and "://" in clone_url:
            protocol, address = clone_url.split("://", 1)
            # Basic check to avoid double auth
            if "@" not in address:
                clean_clone_url = f"{protocol}://oauth2:{GITEA_TOKEN}@{address}"

        logger.info(f"Cloning {repo_name}...")
        subprocess.check_call(["git", "clone", clean_clone_url, "."], cwd=work_dir)
        
        # Checkout the head commit
        logger.info(f"Checking out {head_sha}...")
        subprocess.check_call(["git", "checkout", head_sha], cwd=work_dir)
        
        # Run XAI Review
        # Assuming 'ai-review' alias or 'xai-review' binary is available
        # The command might be 'ai-review run' or similar depending on the tool ver.
        # Based on research: 'ai-review run-summary' or just 'ai-review'
        logger.info("Running AI Review...")
        
        # We need to set environment variables for ai-review to know context if it wasn't implicit
        # But usually it reads from git.
        # We might need to pass specific flags.
        
        # NOTE: This command is a placeholder based on general usage. 
        # We might need to adjust arguments based on actual help output.
        cmd = ["ai-review", "run"] 
        
        # Prepare environment variables for ai-review
        # We need to explicitly pass the pipeline config as env vars because they are dynamic per PR
        env_vars = {
            **os.environ,
            "GitHub_Actions": "false",
            "VCS__PIPELINE__PULL_NUMBER": str(pr_number),
            "VCS__PIPELINE__OWNER": payload["repository"]["owner"]["login"],
            "VCS__PIPELINE__REPO": payload["repository"]["name"]
        }
        
        # Log the env vars for debugging (excluding secrets)
        debug_env = {k: v for k, v in env_vars.items() if "TOKEN" not in k and "KEY" not in k}
        logger.info(f"Running ai-review with env: {debug_env}")

        result = subprocess.run(
            cmd, 
            cwd=work_dir, 
            capture_output=True, 
            text=True,
            env=env_vars
        )
        
        if result.returncode == 0:
            logger.info("AI Review completed successfully.")
            logger.info(result.stdout)
        else:
            logger.error("AI Review failed.")
            logger.error(result.stderr)
            
    except subprocess.CalledProcessError as e:
        logger.error(f"Git operation failed: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
    finally:
        # Cleanup
        logger.info(f"Cleaning up {work_dir}...")
        shutil.rmtree(work_dir)

@app.post("/webhook")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        payload_json = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Gitea sends headers like X-Gitea-Event: pull_request
    event_type = request.headers.get("X-Gitea-Event")
    if event_type != "pull_request":
        return {"status": "ignored", "reason": "Not a pull_request event"}

    action = payload_json.get("action")
    if action not in ["opened", "synchronize", "reopened"]:
        return {"status": "ignored", "reason": f"Action '{action}' ignored"}

    # Run the review in the background to avoid timing out the webhook
    background_tasks.add_task(run_review_task, payload_json)

    return {"status": "accepted", "message": "Review queued"}

@app.get("/health")
def health_check():
    return {"status": "ok"}
