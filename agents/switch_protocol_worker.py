"""Bridge a Switch agent's legacy SSE stream into the PreFlight HTTP webhook."""

import asyncio
import json
import os
from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import quote
from uuid import uuid4

import httpx


class StopConnection(RuntimeError):
    pass


def switch_room(event: dict) -> str | None:
    payload = event.get("payload")
    for envelope in (event, payload):
        if isinstance(envelope, dict):
            for key in ("room_id", "matrix_room_id", "transport_room_id"):
                if isinstance(envelope.get(key), str):
                    return envelope[key]
    return None


@dataclass
class Connection:
    identifier: str
    room_id: str
    cursor: int | None = None


async def forward_events(upstream: httpx.AsyncClient, downstream: httpx.AsyncClient,
                         agent_path: str, connection: Connection) -> None:
    params = {
        "connection_id": connection.identifier, "scope": "single", "filter": "all",
        "start_from": "head" if connection.cursor is None else str(connection.cursor),
        "rooms": connection.room_id,
    }
    headers = {"Accept": "text/event-stream"}
    if connection.cursor is not None:
        headers["Last-Event-ID"] = str(connection.cursor)
    async with upstream.stream("GET", f"{agent_path}/events", headers=headers, params=params) as response:
        if response.status_code in {401, 403, 404, 410}:
            raise StopConnection(f"Switch SSE is unavailable (HTTP {response.status_code}).")
        if response.status_code == 409:
            await response.aread()
            if "taken_over" in response.text:
                raise StopConnection("Switch connection was taken over.")
        response.raise_for_status()
        if not response.headers.get("content-type", "").startswith("text/event-stream"):
            raise StopConnection("Switch did not return an SSE stream.")
        data: list[str] = []
        event_type = None
        event_id = None
        async for line in response.aiter_lines():
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "data":
                data.append(value)
            elif field == "event":
                event_type = value
            elif field == "id":
                event_id = value
            elif not line and data:
                event = json.loads("\n".join(data))
                if not isinstance(event, dict):
                    raise ValueError("Switch SSE event is not a JSON object.")
                kind = event_type or event.get("type")
                if kind == "evicted" and event.get("code") in {"taken_over", "closed"}:
                    raise StopConnection(f"Switch closed the connection: {event['code']}.")
                room = switch_room(event)
                if room == connection.room_id:
                    if event_id:
                        event.setdefault("event_id", f"{agent_path}:{event_id}")
                    event.setdefault("sse_event_type", kind)
                    receipt = await downstream.post("/api/switch/webhook", json=event)
                    if receipt.status_code in {401, 403, 503}:
                        raise StopConnection(f"PreFlight webhook refused the event (HTTP {receipt.status_code}).")
                    receipt.raise_for_status()
                cursor = event.get("resumed_at") if kind == "gap" else event.get("cursor") if kind == "connection_state" else event_id
                if isinstance(cursor, (str, int)) and str(cursor).isdigit():
                    connection.cursor = int(cursor)
                data, event_type, event_id = [], None, None
            elif not line:
                event_type, event_id = None, None


async def heartbeat(upstream: httpx.AsyncClient, agent_path: str, connection: Connection) -> None:
    while True:
        await asyncio.sleep(2)
        body = {"connection_id": connection.identifier}
        if connection.cursor is not None:
            body["cursor"] = connection.cursor
        response = await upstream.post(f"{agent_path}/connection/beat", json=body, timeout=4)
        if response.status_code in {401, 403, 422} or (
            response.status_code == 409 and "taken_over" in response.text
        ):
            raise StopConnection(f"Switch rejected the heartbeat (HTTP {response.status_code}).")
        response.raise_for_status()


async def worker() -> None:
    server = os.environ.get("SWITCH_SERVER_URL") or os.environ.get("SWITCH_BASE_URL")
    token = os.environ.get("SWITCH_AGENT_TOKEN") or os.environ.get("SWITCH_AGENT_API_KEY")
    settings = {"SWITCH_SERVER_URL": server, "SWITCH_AGENT_TOKEN": token,
                **{name: os.environ.get(name) for name in
                   ("SWITCH_AGENT_ID", "SWITCH_ROOM_ID", "PREFLIGHT_SWITCH_WEBHOOK_TOKEN")}}
    if missing := [setting for setting, value in settings.items() if not value]:
        raise StopConnection(f"Missing Switch bridge settings: {', '.join(missing)}")
    agent_path = f"/agents/{quote(os.environ['SWITCH_AGENT_ID'], safe='')}"
    connection = Connection(str(uuid4()), os.environ["SWITCH_ROOM_ID"])
    async with (
        httpx.AsyncClient(base_url=server, timeout=httpx.Timeout(8, read=25),
                          headers={"Authorization": f"Bearer {token}"}) as upstream,
        httpx.AsyncClient(base_url=os.environ.get("PREFLIGHT_API_URL", "http://localhost:8000"), timeout=8,
                          headers={"Authorization": f"Bearer {os.environ['PREFLIGHT_SWITCH_WEBHOOK_TOKEN']}"},
                          trust_env=False) as downstream,
    ):
        while True:
            tasks = {asyncio.create_task(forward_events(upstream, downstream, agent_path, connection)),
                     asyncio.create_task(heartbeat(upstream, agent_path, connection))}
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            except (httpx.HTTPError, ValueError) as exc:
                print(f"Switch bridge reconnecting after {type(exc).__name__}.", flush=True)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(2)


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        try:
            asyncio.run(worker())
        except StopConnection as exc:
            raise SystemExit(str(exc)) from None
