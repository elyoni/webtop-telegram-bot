"""Microsoft To Do via Microsoft Graph (personal account, delegated Tasks.ReadWrite)."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import msal

logger = logging.getLogger(__name__)

AUTHORITY = "https://login.microsoftonline.com/consumers"
SCOPES = ["Tasks.ReadWrite"]  # msal adds offline_access itself
GRAPH = "https://graph.microsoft.com/v1.0"


class TodoAuthError(RuntimeError):
    pass


class _TokenCache:
    """msal token cache persisted to a file readable only by the owner."""

    def __init__(self, path: Path):
        self.path = path
        self.cache = msal.SerializableTokenCache()
        if path.exists():
            self.cache.deserialize(path.read_text())

    def save(self) -> None:
        if self.cache.has_state_changed:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(self.cache.serialize())


def _app(client_id: str, data_dir: Path) -> tuple[msal.PublicClientApplication, _TokenCache]:
    cache = _TokenCache(data_dir / "msal_cache.json")
    return msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=cache.cache), cache


def login_device_flow(client_id: str, data_dir: Path) -> str:
    """Interactive one-time login; prints a code to enter at microsoft.com/devicelogin."""
    app, cache = _app(client_id, data_dir)
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise TodoAuthError(f"Could not start device flow: {flow.get('error_description', flow)}")
    print(flow["message"], flush=True)
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise TodoAuthError(f"Login failed: {result.get('error_description', result.get('error'))}")
    cache.save()
    return result.get("id_token_claims", {}).get("preferred_username", "?")


class TodoClient:
    def __init__(self, client_id: str, data_dir: Path):
        self._app, self._cache = _app(client_id, data_dir)
        self._http = httpx.AsyncClient(base_url=GRAPH, timeout=20.0)

    async def close(self) -> None:
        await self._http.aclose()

    def _token(self) -> str:
        accounts = self._app.get_accounts()
        result = self._app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
        self._cache.save()
        if not result or "access_token" not in result:
            raise TodoAuthError("Not logged in to Microsoft; run: webtop-bot login-microsoft")
        return result["access_token"]

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        token = await asyncio.to_thread(self._token)
        resp = await self._http.request(method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs)
        if resp.status_code >= 400 and resp.status_code != 404:
            raise RuntimeError(f"Graph {method} {path.split('?')[0]} failed: HTTP {resp.status_code}")
        return resp

    async def ensure_list(self, name: str) -> str:
        """Id of the To Do list named `name`, creating it if missing."""
        resp = await self._request("GET", "/me/todo/lists")
        for item in resp.json().get("value", []):
            if item.get("displayName") == name:
                return item["id"]
        resp = await self._request("POST", "/me/todo/lists", json={"displayName": name})
        return resp.json()["id"]

    async def list_exists(self, list_id: str) -> bool:
        return (await self._request("GET", f"/me/todo/lists/{list_id}")).status_code != 404

    async def create_task(self, list_id: str, title: str, body: str, due: dt.date, tz: str) -> str:
        payload: Dict[str, Any] = {
            "title": title[:255],
            "body": {"content": body, "contentType": "text"},
            "dueDateTime": {"dateTime": f"{due.isoformat()}T00:00:00", "timeZone": tz},
        }
        resp = await self._request("POST", f"/me/todo/lists/{list_id}/tasks", json=payload)
        return resp.json()["id"]

    async def task_status(self, list_id: str, task_id: str) -> Optional[str]:
        """'completed', 'notStarted', ... or None if the task was deleted."""
        resp = await self._request("GET", f"/me/todo/lists/{list_id}/tasks/{task_id}")
        return None if resp.status_code == 404 else resp.json().get("status")

    async def set_completed(self, list_id: str, task_id: str, completed: bool = True) -> None:
        status = "completed" if completed else "notStarted"
        await self._request("PATCH", f"/me/todo/lists/{list_id}/tasks/{task_id}", json={"status": status})
