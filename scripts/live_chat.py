"""Quick live console against a running stack: python scripts/live_chat.py "question"

Reads OPS_API_KEY from the environment (the API refuses to boot without one)
and sends it like the web console does: X-API-Key header on HTTP, x-api-key
subprotocol token on the WebSocket, never the query string. If thread
creation ever returns a thread_token (per-thread ownership), it rides back as
an x-thread-token.<tok> subprotocol on the socket.
"""

import asyncio
import json
import os
import sys

import httpx
import websockets

API_KEY = os.environ.get("OPS_API_KEY")


async def main() -> None:
    question = sys.argv[1] if len(sys.argv) > 1 else "What was our total revenue from paid orders?"
    api = "http://localhost:8000"
    headers = {"X-API-Key": API_KEY} if API_KEY else {}
    if not API_KEY:
        print("note: OPS_API_KEY is unset; the API will reject this (fail-closed auth)")

    async with httpx.AsyncClient(timeout=10, headers=headers) as client:
        created = (await client.post(f"{api}/api/threads")).json()
    thread_id = created["thread_id"]
    thread_token = created.get("thread_token")  # absent until per-thread ownership lands
    print(f"thread: {thread_id}")

    subprotocols = ["copilot"]
    if API_KEY:
        subprotocols.append("x-api-key." + API_KEY)
    if thread_token:
        subprotocols.append("x-thread-token." + thread_token)

    async with websockets.connect(
        f"ws://localhost:8000/ws/{thread_id}", subprotocols=subprotocols
    ) as ws:
        await ws.recv()  # connected frame
        await ws.send(json.dumps({"type": "user_message", "content": question}))

        while True:
            event = json.loads(await asyncio.wait_for(ws.recv(), timeout=180))
            kind = event.get("type")
            if kind == "node":
                print(f"  [node] {', '.join(event['nodes'])}")
            elif kind == "token":
                print(f"    {event['content']}", end="", flush=True)
            elif kind == "interrupt":
                payload = event.get("payload") or {}
                print(f"  [INTERRUPT] {json.dumps(payload, indent=2)[:600]}")
                print(f"thread_id={thread_id}")
                return
            elif kind == "error":
                print(f"\n[ERROR] {event['message']}")
                return
            elif kind == "done":
                print("\n[done]")
                print(f"http://localhost:3000  (thread {thread_id})")
                return


if __name__ == "__main__":
    asyncio.run(main())
