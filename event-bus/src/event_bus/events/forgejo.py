from __future__ import annotations
from typing import Any
from pydantic import BaseModel


class ForgejoRef(BaseModel):
    ref: str = ""
    sha: str = ""
    label: str = ""


class ForgejoPullRequest(BaseModel):
    id: int = 0
    number: int = 0
    title: str = ""
    state: str = ""
    head: ForgejoRef = ForgejoRef()
    base: ForgejoRef = ForgejoRef()
    html_url: str = ""
    merged: bool = False


class ForgejoRepository(BaseModel):
    id: int = 0
    name: str = ""
    full_name: str = ""
    clone_url: str = ""
    ssh_url: str = ""


class ForgejoPREvent(BaseModel):
    """
    Normalised Forgejo pull_request webhook payload.
    X-Gitea-Event: pull_request
    """

    action: str
    number: int
    pull_request: ForgejoPullRequest
    repository: ForgejoRepository
    sender: dict[str, Any] = {}

    @property
    def repo_full_name(self) -> str:
        return self.repository.full_name

    @property
    def pr_number(self) -> int:
        return self.pull_request.number

    @property
    def head_sha(self) -> str:
        return self.pull_request.head.sha

    @property
    def head_ref(self) -> str:
        return self.pull_request.head.ref

    @property
    def base_ref(self) -> str:
        return self.pull_request.base.ref

    def is_review_trigger(self) -> bool:
        """Return True when the PR should trigger review fan-out (Phase 4).

        Forgejo/Gitea emit "synchronized" for a new push to the PR branch;
        GitHub uses "synchronize". Accept both so updates re-trigger review.
        """
        return self.action in ("opened", "synchronize", "synchronized", "reopened")


class ForgejoReview(BaseModel):
    id: int = 0
    type: str = ""   # Forgejo sends: "approve", "reject", "comment"
    body: str = ""
    html_url: str = ""


class ForgejoReviewEvent(BaseModel):
    """
    Forgejo pull_request_review webhook payload.
    X-Gitea-Event: pull_request_review  (or pull_request_review_rejected)
    """

    action: str = ""
    review: ForgejoReview = ForgejoReview()
    pull_request: ForgejoPullRequest = ForgejoPullRequest()
    repository: ForgejoRepository
    sender: dict[str, Any] = {}

    def is_changes_requested(self) -> bool:
        """True when the reviewer is requesting changes (not approve/comment)."""
        return self.review.type.lower() in ("reject", "request_changes", "request changes")

    @property
    def pr_html_url(self) -> str:
        return self.pull_request.html_url

    @property
    def pr_number(self) -> int:
        return self.pull_request.number

    @property
    def head_ref(self) -> str:
        return self.pull_request.head.ref

    @property
    def repo_full_name(self) -> str:
        return self.repository.full_name
