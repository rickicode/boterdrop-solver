#!/usr/bin/env python3
"""Boterdrop Pool Gateway & Load Balancer.

Combines multiple Boterdrop instances (Laptop, VPS, Remote) into a single
high-availability pool endpoint with health tracking, failover, and round-robin.
Exposes standard Boterdrop HTTP REST API (/turnstile, /clearance, /aws-token, /result).
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional
import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("boterdrop_pool")

# Default upstream workers (can be overridden via BOTERDROP_NODES env: "url1,url2")
DEFAULT_NODES = [
    {"name": "laptop", "url": "http://laptop-host.example.com:8001", "weight": 2},
    {"name": "vps_local", "url": "http://127.0.0.1:8002", "weight": 1},
]

env_nodes = os.environ.get("BOTERDROP_NODES", "").strip()
if env_nodes:
    NODES_CONFIG = []
    for idx, u in enumerate(env_nodes.split(",")):
        u = u.strip().rstrip("/")
        if u:
            NODES_CONFIG.append({"name": f"node_{idx+1}", "url": u, "weight": 1})
else:
    NODES_CONFIG = DEFAULT_NODES


class NodeState:
    def __init__(self, name: str, url: str, weight: int = 1):
        self.name = name
        self.url = url.rstrip("/")
        self.weight = weight
        self.is_healthy = True
        self.last_check = 0.0
        self.active_tasks = 0
        self.total_solved = 0
        self.total_failed = 0

    async def check_health(self, client: httpx.AsyncClient) -> bool:
        try:
            r = await client.get(f"{self.url}/openapi.json", timeout=3.0)
            self.is_healthy = (r.status_code == 200)
        except Exception:
            self.is_healthy = False
        self.last_check = time.time()
        return self.is_healthy


class PoolManager:
    def __init__(self, nodes: List[Dict[str, Any]]):
        self.nodes = [NodeState(n["name"], n["url"], n.get("weight", 1)) for n in nodes]
        self.task_node_map: Dict[str, NodeState] = {}  # task_id -> NodeState
        self._round_robin = itertools.cycle(range(len(self.nodes)))
        self._lock = asyncio.Lock()

    async def get_healthy_node(self) -> Optional[NodeState]:
        # Filter healthy nodes, prefer nodes with lowest active_tasks
        healthy = [n for n in self.nodes if n.is_healthy]
        if not healthy:
            return None
        # Sort primarily by active tasks, secondary by weight inverse
        healthy.sort(key=lambda n: (n.active_tasks, -n.weight))
        return healthy[0]

    def register_task(self, task_id: str, node: NodeState):
        self.task_node_map[task_id] = node
        node.active_tasks += 1

    def finish_task(self, task_id: str, success: bool = True):
        node = self.task_node_map.pop(task_id, None)
        if node:
            node.active_tasks = max(0, node.active_tasks - 1)
            if success:
                node.total_solved += 1
            else:
                node.total_failed += 1

    def get_node_for_task(self, task_id: str) -> Optional[NodeState]:
        return self.task_node_map.get(task_id)


pool = PoolManager(NODES_CONFIG)
app = FastAPI(title="Boterdrop Pool Gateway", version="1.0.0")
http_client: Optional[httpx.AsyncClient] = None


@app.on_event("startup")
async def startup():
    global http_client
    http_client = httpx.AsyncClient(timeout=45.0)
    asyncio.create_task(health_monitor_loop())


@app.on_event("shutdown")
async def shutdown():
    global http_client
    if http_client:
        await http_client.aclose()


async def health_monitor_loop():
    while True:
        try:
            for node in pool.nodes:
                was = node.is_healthy
                now = await node.check_health(http_client)
                if was != now:
                    status = "ONLINE" if now else "OFFLINE"
                    logger.info(f"Node '{node.name}' ({node.url}) is now {status}")
        except Exception as e:
            logger.error(f"Health monitor error: {e}")
        await asyncio.sleep(10.0)


# ==================== ENDPOINTS ====================

@app.get("/health")
async def get_health():
    return {
        "status": "ok",
        "nodes": [
            {
                "name": n.name,
                "url": n.url,
                "healthy": n.is_healthy,
                "active_tasks": n.active_tasks,
                "total_solved": n.total_solved,
                "total_failed": n.total_failed,
            }
            for n in pool.nodes
        ],
    }


@app.get("/turnstile")
async def submit_turnstile(
    url: str = Query(...),
    sitekey: str = Query(...),
    action: Optional[str] = Query(None),
    cdata: Optional[str] = Query(None),
):
    node = await pool.get_healthy_node()
    if not node:
        raise HTTPException(status_code=503, detail="No healthy Boterdrop solvers available in pool")

    params: dict = {"url": url, "sitekey": sitekey}
    if action:
        params["action"] = action
    if cdata:
        params["cdata"] = cdata

    try:
        r = await http_client.get(f"{node.url}/turnstile", params=params, timeout=15.0)
        if r.status_code == 202:
            data = r.json()
            tid = data.get("task_id")
            if tid:
                pool.register_task(tid, node)
                data["gateway_node"] = node.name
                return JSONResponse(content=data, status_code=202)
        return JSONResponse(content=r.json(), status_code=r.status_code)
    except Exception as e:
        node.is_healthy = False
        logger.warning(f"Node {node.name} failed during /turnstile: {e}")
        # Retry with another healthy node
        fallback_node = await pool.get_healthy_node()
        if not fallback_node or fallback_node == node:
            raise HTTPException(status_code=502, detail=f"Solver request failed: {e}")
        try:
            r2 = await http_client.get(f"{fallback_node.url}/turnstile", params=params, timeout=15.0)
            if r2.status_code == 202:
                data2 = r2.json()
                tid2 = data2.get("task_id")
                if tid2:
                    pool.register_task(tid2, fallback_node)
                    data2["gateway_node"] = fallback_node.name
                    return JSONResponse(content=data2, status_code=202)
            return JSONResponse(content=r2.json(), status_code=r2.status_code)
        except Exception as e2:
            raise HTTPException(status_code=502, detail=f"Fallback solver failed: {e2}")


@app.get("/clearance")
async def submit_clearance(
    url: str = Query(...),
    timeout: int = Query(30),
):
    node = await pool.get_healthy_node()
    if not node:
        raise HTTPException(status_code=503, detail="No healthy Boterdrop solvers available")

    params = {"url": url, "timeout": timeout}
    try:
        r = await http_client.get(f"{node.url}/clearance", params=params, timeout=15.0)
        if r.status_code == 202:
            data = r.json()
            tid = data.get("task_id")
            if tid:
                pool.register_task(tid, node)
                data["gateway_node"] = node.name
                return JSONResponse(content=data, status_code=202)
        return JSONResponse(content=r.json(), status_code=r.status_code)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Clearance submit failed: {e}")


@app.get("/aws-token")
async def submit_aws_token(
    url: str = Query(...),
    timeout: int = Query(30),
):
    node = await pool.get_healthy_node()
    if not node:
        raise HTTPException(status_code=503, detail="No healthy Boterdrop solvers available")

    params = {"url": url, "timeout": timeout}
    try:
        r = await http_client.get(f"{node.url}/aws-token", params=params, timeout=15.0)
        if r.status_code == 202:
            data = r.json()
            tid = data.get("task_id")
            if tid:
                pool.register_task(tid, node)
                data["gateway_node"] = node.name
                return JSONResponse(content=data, status_code=202)
        return JSONResponse(content=r.json(), status_code=r.status_code)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AWS token submit failed: {e}")


@app.get("/result")
async def get_result(id: str = Query(...)):
    node = pool.get_node_for_task(id)
    # If not in local tracking, query all healthy nodes to find the task
    nodes_to_query = [node] if node else [n for n in pool.nodes if n.is_healthy]

    for n in nodes_to_query:
        if not n:
            continue
        try:
            r = await http_client.get(f"{n.url}/result", params={"id": id}, timeout=10.0)
            if r.status_code == 200:
                data = r.json()
                status = data.get("status")
                if status in ("success", "error"):
                    pool.finish_task(id, success=(status == "success"))
                return JSONResponse(content=data, status_code=200)
            elif r.status_code == 404:
                continue
            return JSONResponse(content=r.json(), status_code=r.status_code)
        except Exception:
            continue

    raise HTTPException(status_code=404, detail="Task ID not found on active pool solvers")


def main():
    port = int(os.environ.get("GATEWAY_PORT", "8000"))
    uvicorn.run("pool_gateway:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
