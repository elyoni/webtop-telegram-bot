"""Todoist via its API v1 (personal API token).

All homework goes into one project (share it with family in the Todoist app),
with a section per kid: the free plan allows only 5 projects. A kid's "list id"
is "<project id>/<section id>".
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

API = "https://api.todoist.com/api/v1"
PROJECT_NAME = "שיעורי בית"


class TodoistClient:
    def __init__(self, token: str):
        self._http = httpx.AsyncClient(
            base_url=API, timeout=20.0, headers={"Authorization": f"Bearer {token}"}
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        resp = await self._http.request(method, path, **kwargs)
        if resp.status_code >= 400 and resp.status_code != 404:
            raise RuntimeError(f"Todoist {method} {path} failed: HTTP {resp.status_code}")
        return resp

    async def _all(self, path: str, **params: Any) -> List[Dict[str, Any]]:
        """Every item of a paginated list endpoint."""
        items: List[Dict[str, Any]] = []
        while True:
            data = (await self._request("GET", path, params=params)).json()
            items += [i for i in data.get("results", []) if not i.get("is_deleted")]
            if not data.get("next_cursor"):
                return items
            params["cursor"] = data["next_cursor"]

    async def _ensure(self, path: str, name: str, **fields: Any) -> str:
        for item in await self._all(path, **fields):
            if item.get("name") == name:
                return item["id"]
        return (await self._request("POST", path, json={"name": name, **fields})).json()["id"]

    async def ensure_list(self, kid_name: str) -> str:
        project_id = await self._ensure("/projects", PROJECT_NAME)
        section_id = await self._ensure("/sections", kid_name, project_id=project_id)
        return f"{project_id}/{section_id}"

    async def _exists(self, path: str) -> bool:
        resp = await self._request("GET", path)
        return resp.status_code != 404 and not resp.json().get("is_deleted")

    async def list_exists(self, list_id: str) -> bool:
        project_id, _, section_id = list_id.partition("/")
        return bool(section_id) and await self._exists(f"/projects/{project_id}") and await self._exists(
            f"/sections/{section_id}"
        )

    async def create_task(self, list_id: str, title: str, body: str, due: dt.date, tz: str) -> str:
        project_id, _, section_id = list_id.partition("/")
        payload: Dict[str, Any] = {
            "content": title[:500],
            "description": body,
            "project_id": project_id,
            "section_id": section_id,
            "due_date": due.isoformat(),
        }
        return (await self._request("POST", "/tasks", json=payload)).json()["id"]

    async def open_tasks(self, list_id: str) -> List[Tuple[str, str, Optional[dt.date]]]:
        project_id, _, section_id = list_id.partition("/")
        tasks = await self._all("/tasks", project_id=project_id, section_id=section_id)
        return [
            (t["id"], t["content"], dt.date.fromisoformat(t["due"]["date"][:10]) if t.get("due") else None)
            for t in tasks
            if not t.get("checked")
        ]

    async def task_status(self, list_id: str, task_id: str) -> Optional[str]:
        """'completed', 'notStarted', or None if the task was deleted (To Do's vocabulary)."""
        resp = await self._request("GET", f"/tasks/{task_id}")
        if resp.status_code == 404 or resp.json().get("is_deleted"):
            return None
        return "completed" if resp.json().get("checked") else "notStarted"

    async def delete_task(self, list_id: str, task_id: str) -> None:
        await self._request("DELETE", f"/tasks/{task_id}")

    async def update_task(self, list_id: str, task_id: str, title: str, body: str) -> None:
        await self._request("POST", f"/tasks/{task_id}", json={"content": title[:500], "description": body})

    async def set_completed(self, list_id: str, task_id: str, completed: bool = True) -> None:
        await self._request("POST", f"/tasks/{task_id}/{'close' if completed else 'reopen'}")
