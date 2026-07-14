from typing import Any

from fastapi.testclient import TestClient

import webhook_server as server


REPO = {
    "full_name": "HomeLab/llm-proxy",
    "name": "llm-proxy",
    "clone_url": "https://gitea.ext.ben.io/HomeLab/llm-proxy.git",
    "owner": {"login": "HomeLab"},
}
PR = {"head": {"sha": "abc123", "ref": "feature"}, "base": {"ref": "main"}}


def comment_payload(body="@ai-reviewer review", is_pr=True, comment_id=968):
    issue: dict[str, Any] = {"number": 3}
    if is_pr:
        issue["pull_request"] = {"url": "https://example/pr/3"}
    return {
        "action": "created",
        "repository": REPO,
        "issue": issue,
        "comment": {"id": comment_id, "body": body},
        "sender": {"login": "b3nw"},
    }


def pull_payload(action="opened"):
    return {
        "action": action,
        "number": 3,
        "repository": REPO,
        "pull_request": PR,
        "sender": {"login": "b3nw"},
    }


def test_command_matches_only_documented_forms():
    assert server.command_matches("@ai-reviewer review")
    assert server.command_matches(" /REVIEW ")
    assert not server.command_matches("@ai-reviewer please review")
    assert not server.command_matches("/review now")


def test_manual_command_queues_normalized_pr(monkeypatch):
    queued = []
    reactions = []
    monkeypatch.setattr(server, "add_ack_reaction", lambda repo, comment_id: reactions.append(comment_id))
    monkeypatch.setattr(server, "ensure_self_requested_as_reviewer", lambda payload: None)
    monkeypatch.setattr(
        server,
        "get_pull_request_payload",
        lambda payload: {**pull_payload(), "action": "manual_review"},
    )
    monkeypatch.setattr(server, "run_review_task", lambda payload, key: queued.append((payload, key)))
    with TestClient(server.app) as client:
        response = client.post(
            "/webhook",
            headers={"X-Gitea-Event": "issue_comment"},
            json=comment_payload("/review"),
        )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert queued[0][0]["number"] == 3
    assert reactions == [968]


def test_manual_command_rejects_issue_comments(monkeypatch):
    with TestClient(server.app) as client:
        response = client.post(
            "/webhook",
            headers={"X-Gitea-Event": "issue_comment"},
            json=comment_payload(is_pr=False),
        )
    assert response.json()["reason"] == "Command is not on a pull request"


def test_manual_command_rejects_unknown_command(monkeypatch):
    with TestClient(server.app) as client:
        response = client.post(
            "/webhook",
            headers={"X-Gitea-Event": "issue_comment"},
            json=comment_payload(body="@ai-reviewer please review"),
        )
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "Not a supported review command"


def test_deduplicates_same_pr_head(monkeypatch):
    server._active_reviews.clear()
    monkeypatch.setattr(server, "ensure_self_requested_as_reviewer", lambda payload: None)
    monkeypatch.setattr(server, "run_review_task", lambda payload, key: None)
    with TestClient(server.app) as client:
        first = client.post("/webhook", headers={"X-Gitea-Event": "pull_request"}, json=pull_payload())
        second = client.post("/webhook", headers={"X-Gitea-Event": "pull_request"}, json=pull_payload())
    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "ignored"
    assert second.json()["reason"] == "Review already queued for this PR head"


def test_is_clean_summary_matches_structured_format():
    body = """## Code Review Summary

**Status:** `No Issues Found` | **Recommendation:** `Approve`

### Overview

| Severity | Count |
| --- | ---: |
| CRITICAL | `0` |
| WARNING | `0` |
| SUGGESTION | `0` |
"""
    assert server._is_clean_summary(body)
    assert not server._is_clean_summary(body.replace("`0`", "`1`", 1))


def test_is_clean_summary_matches_kilocode_status_line():
    body = """Status: No Issues Found | Recommendation: Merge

## Code Review Summary

**Status:** `No Issues Found` | **Recommendation:** `Approve`

### Overview

| Severity | Count |
| --- | ---: |
| CRITICAL | `0` |
| WARNING | `0` |
| SUGGESTION | `0` |

#ai-review-summary
"""
    assert server._is_clean_summary(body)


def test_is_clean_summary_uses_latest_section_only():
    body = """Status: No Issues Found | Recommendation: Merge

## Code Review Summary

**Status:** `No Issues Found` | **Recommendation:** `Approve`

### Overview

| Severity | Count |
| --- | ---: |
| CRITICAL | `0` |
| WARNING | `0` |
| SUGGESTION | `0` |

<!-- ai-review-history-separator -->

### Previous review
Status: 4 Issues Found | Recommendation: Address before merge
| CRITICAL | `1` |
#ai-review-summary
"""
    assert server._is_clean_summary(body)


def test_is_clean_summary_rejects_conflicting_llm_body():
    body = """Status: No Issues Found | Recommendation: Merge

## Code Review Summary

**Status:** `2 Issues Found` | **Recommendation:** `Address before merge`

### Overview

| Severity | Count |
| --- | ---: |
| CRITICAL | `1` |
| WARNING | `1` |
| SUGGESTION | `0` |

#ai-review-summary
"""
    assert not server._is_clean_summary(body)
