import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

import requests
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI()
GITEA_TOKEN = os.getenv("GITEA_TOKEN")
GITEA_API_URL = os.getenv("GITEA_API_URL", "").rstrip("/")
AI_REVIEWER_USERNAME = os.getenv("AI_REVIEWER_USERNAME", "ai-reviewer")
REVIEW_COMMANDS = {"@ai-reviewer review", "/review"}
_active_reviews: set[str] = set()
_active_reviews_lock = threading.Lock()


class GiteaWebhookPayload(BaseModel):
    action: str
    number: int
    pull_request: Dict[str, Any]
    repository: Dict[str, Any]
    sender: Dict[str, Any]


def command_matches(body: object) -> bool:
    """Accept only the two documented manual-review commands."""
    return isinstance(body, str) and body.strip().casefold() in REVIEW_COMMANDS


def is_pr_comment_payload(payload: Dict[str, Any]) -> bool:
    """Return true only for a comment attached to a pull request."""
    issue = payload.get("issue")
    if not isinstance(issue, dict):
        return False
    return bool(issue.get("pull_request"))


def _api_headers() -> Dict[str, str]:
    if not GITEA_API_URL or not GITEA_TOKEN:
        raise RuntimeError("GITEA_API_URL and GITEA_TOKEN must be configured")
    return {"Authorization": f"token {GITEA_TOKEN}", "Accept": "application/json"}


def api_get(path: str) -> requests.Response:
    return requests.get(f"{GITEA_API_URL}{path}", headers=_api_headers(), timeout=15)


def api_post(path: str, payload: Dict[str, Any]) -> requests.Response:
    return requests.post(f"{GITEA_API_URL}{path}", headers=_api_headers(), json=payload, timeout=15)


def _repo_identity(repo: Dict[str, Any]) -> Tuple[str, str]:
    return repo["owner"]["login"], repo["name"]


def ensure_self_requested_as_reviewer(payload: Dict[str, Any]) -> None:
    repo = payload["repository"]
    owner, name = _repo_identity(repo)
    pr_number = payload["number"]
    try:
        pr = api_get(f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/pulls/{pr_number}")
        pr.raise_for_status()
        requested = pr.json().get("requested_reviewers") or []
        if any(item.get("login") == AI_REVIEWER_USERNAME for item in requested if isinstance(item, dict)):
            logger.info("Reviewer request already present: reviewer=%s pr=%s/%s#%s", AI_REVIEWER_USERNAME, owner, name, pr_number)
            return
        response = api_post(f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/pulls/{pr_number}/requested_reviewers", {"reviewers": [AI_REVIEWER_USERNAME]})
        if response.ok:
            logger.info("Reviewer request created: reviewer=%s pr=%s/%s#%s", AI_REVIEWER_USERNAME, owner, name, pr_number)
        else:
            logger.warning("Reviewer request failed: reviewer=%s pr=%s/%s#%s status=%s", AI_REVIEWER_USERNAME, owner, name, pr_number, response.status_code)
    except requests.RequestException:
        logger.exception("Reviewer request failed unexpectedly: reviewer=%s pr=%s/%s#%s", AI_REVIEWER_USERNAME, owner, name, pr_number)


def _is_ai_summary_comment(item: Dict[str, Any]) -> bool:
    return (
        isinstance(item, dict)
        and isinstance(item.get("id"), int)
        and item.get("user", {}).get("login") == AI_REVIEWER_USERNAME
        and "#ai-review-summary" in item.get("body", "")
    )


def _summary_comment_snapshot(repo: Dict[str, Any], pr_number: int) -> Dict[int, str]:
    """Map AI summary comment id -> body, used to detect create or in-place update."""
    owner, name = _repo_identity(repo)
    response = api_get(f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/issues/{pr_number}/comments?limit=100")
    response.raise_for_status()
    return {
        item["id"]: item.get("body", "")
        for item in response.json()
        if _is_ai_summary_comment(item)
    }


def _is_clean_summary(body: object) -> bool:
    """Match either structured LLM summary or kilocode status-line clean results."""
    if not isinstance(body, str):
        return False
    # Only evaluate the authoritative (latest) section when history is stacked.
    latest = body.split("<!-- ai-review-history-separator -->", 1)[0]
    clean_status = bool(
        re.search(
            r"(?:\*\*Status:\*\*|Status:)\s*`?No Issues Found`?\s*\|\s*"
            r"(?:\*\*Recommendation:\*\*|Recommendation:)\s*`?(?:Approve|Merge)`?",
            latest,
            re.IGNORECASE,
        )
    )
    zero_counts = (
        re.search(r"\|\s*CRITICAL\s*\|\s*`?0`?\s*\|", latest)
        and re.search(r"\|\s*WARNING\s*\|\s*`?0`?\s*\|", latest)
        and re.search(r"\|\s*SUGGESTION\s*\|\s*`?0`?\s*\|", latest)
    )
    # Reject if the latest section still reports positive issue counts.
    has_findings = bool(
        re.search(r"(?:\*\*Status:\*\*|Status:)\s*`?\d+\s+Issues?\s+Found`?", latest, re.IGNORECASE)
        or re.search(r"\|\s*(?:CRITICAL|WARNING|SUGGESTION)\s*\|\s*`?[1-9]\d*`?\s*\|", latest)
    )
    return bool(clean_status and zero_counts and not has_findings)


def approve_if_clean_summary(payload: Dict[str, Any], prior_summaries: Dict[int, str]) -> None:
    repo = payload["repository"]
    owner, name = _repo_identity(repo)
    pr_number = payload["number"]
    head_sha = payload["pull_request"]["head"]["sha"]
    try:
        current = _summary_comment_snapshot(repo, pr_number)
        # New id, or same id with a rewritten body (history-stacking updates).
        changed = [
            (cid, body)
            for cid, body in current.items()
            if cid not in prior_summaries or prior_summaries[cid] != body
        ]
        if not changed:
            logger.warning("Approval skipped: no new or updated AI summary found for pr=%s/%s#%s", owner, name, pr_number)
            return
        summary_id, summary_body = max(changed, key=lambda item: item[0])
        if not _is_clean_summary(summary_body):
            logger.info(
                "Approval skipped: AI summary has findings or is not a valid clean result pr=%s/%s#%s summary_id=%s",
                owner,
                name,
                pr_number,
                summary_id,
            )
            return
        reviews = api_get(f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/pulls/{pr_number}/reviews?limit=100")
        reviews.raise_for_status()
        if any(
            item.get("user", {}).get("login") == AI_REVIEWER_USERNAME
            and item.get("state") == "APPROVED"
            and item.get("commit_id") == head_sha
            for item in reviews.json()
            if isinstance(item, dict)
        ):
            logger.info("Approval already present: pr=%s/%s#%s sha=%s", owner, name, pr_number, head_sha)
            return
        approval = api_post(
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/pulls/{pr_number}/reviews",
            {
                "event": "APPROVED",
                "commit_id": head_sha,
                "body": f"✅ Automated review completed with no findings. See summary comment #{summary_id}.",
            },
        )
        if approval.ok:
            logger.info(
                "AI review approved: pr=%s/%s#%s sha=%s summary_id=%s",
                owner,
                name,
                pr_number,
                head_sha,
                summary_id,
            )
        else:
            logger.warning("AI approval failed: pr=%s/%s#%s status=%s", owner, name, pr_number, approval.status_code)
    except requests.RequestException:
        logger.exception("Approval check failed: pr=%s/%s#%s", owner, name, pr_number)


def add_ack_reaction(repo: Dict[str, Any], comment_id: object) -> None:
    if not isinstance(comment_id, int):
        logger.warning("Manual review accepted but no valid comment ID was supplied for acknowledgment")
        return
    owner = repo["owner"]["login"]
    name = repo["name"]
    try:
        response = requests.post(
            f"{GITEA_API_URL}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/issues/comments/{comment_id}/reactions",
            headers=_api_headers(),
            json={"content": "eyes"},
            timeout=15,
        )
        if response.ok:
            logger.info("Manual review acknowledged with eyes reaction: comment_id=%s", comment_id)
        else:
            logger.warning("Manual review accepted but acknowledgment reaction failed: comment_id=%s status=%s", comment_id, response.status_code)
    except requests.RequestException:
        logger.exception("Manual review accepted but acknowledgment reaction request failed: comment_id=%s", comment_id)


def get_pull_request_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a PR-comment webhook into the normal pull_request payload."""
    repo = payload["repository"]
    owner = repo["owner"]["login"]
    name = repo["name"]
    issue = payload["issue"]
    pr_number = issue["number"]
    response = api_get(f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/pulls/{pr_number}")
    response.raise_for_status()
    return {
        "action": "manual_review",
        "number": pr_number,
        "pull_request": response.json(),
        "repository": repo,
        "sender": payload["sender"],
    }


def review_key(payload: Dict[str, Any]) -> str:
    repo = payload["repository"]["full_name"]
    pr_number = payload["number"]
    head_sha = payload["pull_request"]["head"]["sha"]
    return f"{repo}#{pr_number}@{head_sha}"


def queue_review(background_tasks: BackgroundTasks, payload: Dict[str, Any]) -> Tuple[bool, str]:
    key = review_key(payload)
    with _active_reviews_lock:
        if key in _active_reviews:
            return False, key
        _active_reviews.add(key)
    background_tasks.add_task(run_review_task, payload, key)
    return True, key


def run_review_task(payload: Dict[str, Any], key: Optional[str] = None) -> None:
    repo_name = payload["repository"]["full_name"]
    pr_number = payload["number"]
    clone_url = payload["repository"]["clone_url"]
    head_sha = payload["pull_request"]["head"]["sha"]

    logger.info("decision=started review=%s pr=#%s repo=%s sha=%s", key, pr_number, repo_name, head_sha)
    work_dir = tempfile.mkdtemp(prefix="gitea-review-")
    try:
        prior_summaries = _summary_comment_snapshot(payload["repository"], pr_number)
        clean_clone_url = clone_url
        if GITEA_TOKEN and "://" in clone_url:
            protocol, address = clone_url.split("://", 1)
            if "@" not in address:
                clean_clone_url = f"{protocol}://oauth2:{GITEA_TOKEN}@{address}"

        logger.info("Cloning %s...", repo_name)
        subprocess.check_call(["git", "clone", clean_clone_url, "."], cwd=work_dir)
        logger.info("Checking out %s...", head_sha)
        subprocess.check_call(["git", "checkout", head_sha], cwd=work_dir)

        env_vars = {
            **os.environ,
            "GitHub_Actions": "false",
            "VCS__PIPELINE__PULL_NUMBER": str(pr_number),
            "VCS__PIPELINE__OWNER": payload["repository"]["owner"]["login"],
            "VCS__PIPELINE__REPO": payload["repository"]["name"],
        }
        result = subprocess.run(
            ["ai-review", "run"], cwd=work_dir, capture_output=True, text=True, env=env_vars
        )
        if result.returncode == 0:
            logger.info("decision=completed review=%s", key)
            logger.info(result.stdout)
            approve_if_clean_summary(payload, prior_summaries)
        else:
            logger.error("decision=failed review=%s error=%s", key, result.stderr)
    except subprocess.CalledProcessError as exc:
        logger.error("decision=failed review=%s error=git_operation: %s", key, exc)
    except Exception:
        logger.exception("decision=failed review=%s unexpected error pr=#%s repo=%s", key, pr_number, repo_name)
    finally:
        logger.info("Cleaning up %s...", work_dir)
        shutil.rmtree(work_dir, ignore_errors=True)
        if key:
            with _active_reviews_lock:
                _active_reviews.discard(key)


@app.post("/webhook")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        payload_json = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    event_type = request.headers.get("X-Gitea-Event")
    action = payload_json.get("action")
    logger.info("Webhook received: event=%s action=%s repo=%s", event_type, action, payload_json.get("repository", {}).get("full_name"))

    if event_type == "pull_request":
        if action not in {"opened", "synchronize", "reopened"}:
            logger.info("decision=ignored reason=unsupported_pr_action action=%s", action)
            return {"status": "ignored", "reason": f"Action '{action}' ignored"}
        review_payload = payload_json
    elif event_type in {"issue_comment", "pull_request_comment"}:
        comment = payload_json.get("comment", {})
        comment_id = comment.get("id") if isinstance(comment, dict) else None
        comment_body = comment.get("body") if isinstance(comment, dict) else None
        if action not in {"created", "commented"}:
            logger.info(
                "decision=ignored reason=unsupported_comment_action action=%s comment_id=%s",
                action,
                comment_id,
            )
            return {"status": "ignored", "reason": "Not a supported review command"}
        if not command_matches(comment_body):
            logger.info("decision=ignored reason=unsupported_command comment_id=%s", comment_id)
            return {"status": "ignored", "reason": "Not a supported review command"}
        if not is_pr_comment_payload(payload_json):
            logger.info("decision=ignored reason=not_pr_comment comment_id=%s", comment_id)
            return {"status": "ignored", "reason": "Command is not on a pull request"}
        try:
            requester = payload_json["sender"]["login"]
            logger.info(
                "decision=accepted reason=manual_command requester=%s comment_id=%s",
                requester,
                comment_id,
            )
            add_ack_reaction(payload_json["repository"], comment_id)
            review_payload = get_pull_request_payload(payload_json)
        except requests.RequestException:
            logger.exception("Could not fetch pull request for accepted manual review request")
            raise HTTPException(status_code=502, detail="Gitea API validation failed")
    else:
        logger.info("decision=ignored reason=unsupported_event event=%s", event_type)
        return {"status": "ignored", "reason": "Unsupported event"}

    ensure_self_requested_as_reviewer(review_payload)
    queued, key = queue_review(background_tasks, review_payload)
    if not queued:
        logger.info("decision=duplicate-suppressed review=%s", key)
        return {"status": "ignored", "reason": "Review already queued for this PR head", "review": key}
    logger.info("decision=queued review=%s", key)
    return {"status": "accepted", "message": "Review queued", "review": key}


@app.get("/health")
def health_check():
    return {"status": "ok"}
