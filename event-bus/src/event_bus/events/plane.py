from __future__ import annotations
from typing import Any
from pydantic import BaseModel, model_validator


class PlaneStateDetail(BaseModel):
    id: str = ""
    name: str = ""
    group: str = ""
    color: str = ""


class PlaneEvent(BaseModel):
    """
    Normalised Plane webhook payload.

    Plane CE sends:
      { "event": "issue", "action": "updated",
        "payload": { "id": "...", "state_detail": {...}, ... },
        "workspace_id": "...", "project_id": "..." }

    Some versions use "data" instead of "payload"; we accept both.
    """

    event: str
    action: str
    workspace_id: str = ""
    project_id: str = ""
    # Normalised issue dict (populated by validator from payload or data)
    issue: dict[str, Any] = {}

    @model_validator(mode="before")
    @classmethod
    def _normalise(cls, raw: dict[str, Any]) -> dict[str, Any]:
        body = raw.get("payload") or raw.get("data") or {}
        raw = dict(raw)
        raw["issue"] = body
        return raw

    @property
    def issue_id(self) -> str | None:
        return self.issue.get("id")

    @property
    def state_detail(self) -> PlaneStateDetail | None:
        detail = self.issue.get("state_detail")
        if isinstance(detail, dict):
            return PlaneStateDetail(**detail)
        return None

    @property
    def state_name(self) -> str | None:
        detail = self.state_detail
        return detail.name if detail else None

    def is_issue_event(self) -> bool:
        return self.event in ("issue", "issue_activity")
