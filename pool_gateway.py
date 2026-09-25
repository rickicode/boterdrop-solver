#!/usr/bin/env python3
"""Boterdrop Pool Gateway & Load Balancer.

Combines multiple Boterdrop instances (Laptop, VPS, Remote) into a single
high-availability pool endpoint with health tracking, failover, and round-robin.
Exposes standard Boterdrop HTTP REST API (/turnstile, /clearance, /aws-token, /result).

Failover policy (macet -> pindah node otomatis):
  - Submit: coba node sehat berikutnya kalau node pertama error / non-202.
  - Result: task gagal (captcha_fail) -> RESUBMIT transparan ke node lain
    (client tetap poll task_id lama; gateway mer redirect).
  - Node gagal >= POOL_FAIL_COOLDOWN_AFTER task -> cooldown POOL_FAIL_COOLDOWN_SECS
    detik, routing otomatis lompat ke node lain (mis. solver mesin lokal).
  - Poll miss (node ngehang di /result) >= POOL_MAX_POLL_MISSES -> koordinat
    failover juga.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import os
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
    {"name": "laptop", "url": "http://laptop-host.example.com:20011", "weight": 2},
    {"name": "vps_local", "url": "http://127.0.0.1:20011", "weight": 1},
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

# ---- Failover policy (env-tunable) ----
FAIL_COOLDOWN_AFTER = int(os.environ.get("POOL_FAIL_COOLDOWN_AFTER", "2"))
FAIL_COOLDOWN_SECS = float(os.environ.get("POOL_FAIL_COOLDOWN_SECS", "60"))
MAX_TASK_FAILOVERS = int(os.environ.get("POOL_MAX_FAILOVERS", "2"))
MAX_POLL_MISSES = int(os.environ.get("POOL_MAX_POLL_MISSES", "3"))
SUBMIT_PATHS = ("/turnstile", "/clearance", "/aws-token")


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
        self.consecutive_failures = 0
        self.cooldown_until = 0.0

    @property
    def in_cooldown(self) -> bool:
        return time.time() < self.cooldown_until

    async def check_health(self, client: httpx.AsyncClient) -> bool:
        try:
            r = await client.get(f"{self.url}/openapi.json", timeout=3.0)
            self.is_healthy = (r.status_code == 200)
        except Exception:
            self.is_healthy = False
        self.last_check = time.time()
        return self.is_healthy

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= FAIL_COOLDOWN_AFTER and not self.in_cooldown:
            self.cooldown_until = time.time() + FAIL_COOLDOWN_SECS
            logger.warning(
                f"Node '{self.name}' COOLDOWN {FAIL_COOLDOWN_SECS:.0f}s "
                f"after {self.consecutive_failures} consecutive failures")


class PoolManager:
    def __init__(self, nodes: List[Dict[str, Any]]):
        self.nodes = [NodeState(n["name"], n["url"], n.get("weight", 1)) for n in nodes]
        self.task_node_map: Dict[str, NodeState] = {}   # upstream tid -> NodeState
        self.task_params: Dict[str, dict] = {}          # client tid -> submit params
        self.task_redirects: Dict[str, str] = {}        # client tid -> latest upstream tid
        self.task_failovers: Dict[str, int] = {}        # client tid -> failover count
        self.task_poll_misses: Dict[str, int] = {}      # client tid -> consecutive poll misses
        self._lock = asyncio.Lock()

    async def get_healthy_node(self, exclude: Optional[NodeState] = None,
                               skip: frozenset | set = frozenset()) -> Optional[NodeState]:
        # Prefer sehat + tidak cooldown; kalau semua cooldown, pakai sehat apa adanya
        # (cooldown itu peringatan routing, bukan alasan pool mati total).
        healthy = [n for n in self.nodes
                   if n.is_healthy and n is not exclude and n.name not in skip
                   and not n.in_cooldown]
        if not healthy:
            healthy = [n for n in self.nodes
                       if n.is_healthy and n is not exclude and n.name not in skip]
        if not healthy:
            return None
        # Sort primarily by active tasks, secondary by weight inverse
        healthy.sort(key=lambda n: (n.active_tasks, -n.weight))
        return healthy[0]

    def register_task(self, task_id: str, node: NodeState,
                      client_id: Optional[str] = None, params: Optional[dict] = None):
        self.task_node_map[task_id] = node
        node.active_tasks += 1
        if client_id is not None:
            self.task_redirects[client_id] = task_id
            self.task_failovers.setdefault(client_id, 0)
            self.task_poll_misses.setdefault(client_id, 0)
            if params is not None:
                self.task_params[client_id] = params

    def finish_task(self, task_id: str, success: bool = True) -> None:
        node = self.task_node_map.pop(task_id, None)
        if node:
            node.active_tasks = max(0, node.active_tasks - 1)
            if success:
                node.total_solved += 1
                node.record_success()
            else:
                node.total_failed += 1
                node.record_failure()

    def get_node_for_task(self, task_id: str) -> Optional[NodeState]:
        return self.task_node_map.get(task_id)

    def cleanup_task(self, client_id: str) -> None:
        self.task_params.pop(client_id, None)
        self.task_redirects.pop(client_id, None)
        self.task_failovers.pop(client_id, None)
        self.task_poll_misses.pop(client_id, None)


pool = PoolManager(NODES_CONFIG)
app = FastAPI(title="Boterdrop Pool Gateway", version="2.0.0")
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
                now = await node.check_health(_client())
                if was != now:
                    status = "ONLINE" if now else "OFFLINE"
                    logger.info(f"Node '{node.name}' ({node.url}) is now {status}")
        except Exception as e:
            logger.error(f"Health monitor error: {e}")
        await asyncio.sleep(10.0)


def _client() -> httpx.AsyncClient:
    if http_client is None:
        raise HTTPException(status_code=503, detail="Gateway not started yet")
    return http_client


async def submit_to_pool(path: str, params: dict) -> tuple[dict, NodeState]:
    """Submit ke node sehat; kalau node pertama error / non-202, coba node lain."""
    if path not in SUBMIT_PATHS:
        raise HTTPException(status_code=500, detail=f"Unknown submit path {path}")
    tried: set[str] = set()
    last_err = ""
    while True:
        node = await pool.get_healthy_node(skip=tried)
        if not node:
            if last_err:
                raise HTTPException(status_code=502,
                                    detail=f"All solvers failed: {last_err}")
            raise HTTPException(status_code=503,
                                detail="No healthy Boterdrop solvers available in pool")
        tried.add(node.name)
        try:
            r = await _client().get(f"{node.url}{path}", params=params, timeout=15.0)
        except Exception as e:
            node.is_healthy = False
            last_err = f"{node.name}: {e}"
            logger.warning(f"Node {node.name} failed during {path}: {e}")
            continue
        if r.status_code == 202:
            data = r.json()
            if data.get("task_id"):
                return data, node
        last_err = f"{node.name}: HTTP {r.status_code} {r.text[:120]}"
        logger.warning(f"Node {node.name} refused {path}: {last_err}")


async def maybe_failover(client_id: str, reason: str) -> Optional[dict]:
    """Task terminal-gagal / node macet -> resubmit transparan ke node lain.

    Client tetap poll task_id lama; redirect dipecah di /result.
    """
    params = pool.task_params.get(client_id)
    tries = pool.task_failovers.get(client_id, 0)
    if not params or tries >= MAX_TASK_FAILOVERS:
        return None
    old_tid = pool.task_redirects.get(client_id, client_id)
    old_node = pool.task_node_map.pop(old_tid, None)
    if old_node:
        old_node.active_tasks = max(0, old_node.active_tasks - 1)

    ep = params.get("_endpoint", "/turnstile")
    q = {k: v for k, v in params.items() if not k.startswith("_")}
    skip: set[str] = set()
    if old_node:
        skip.add(old_node.name)
    last_err = ""
    for _ in range(len(pool.nodes)):
        alt = await pool.get_healthy_node(skip=skip)
        if not alt:
            break
        skip.add(alt.name)
        try:
            r = await _client().get(f"{alt.url}{ep}", params=q, timeout=15.0)
        except Exception as e:
            alt.is_healthy = False
            last_err = f"{alt.name}: {e}"
            logger.warning(f"Failover submit to {alt.name} failed: {e}")
            continue
        if r.status_code != 202:
            last_err = f"{alt.name}: HTTP {r.status_code} {r.text[:120]}"
            continue
        tid = r.json().get("task_id")
        if not tid:
            last_err = f"{alt.name}: no task_id"
            continue

        pool.task_node_map[tid] = alt
        alt.active_tasks += 1
        pool.task_redirects[client_id] = tid
        pool.task_failovers[client_id] = tries + 1
        pool.task_poll_misses[client_id] = 0
        logger.info(f"FAILOVER task {client_id[:8]}.. -> {alt.name} "
                    f"(reason: {reason}, attempt {tries + 1}/{MAX_TASK_FAILOVERS})")
        return {"status": "process", "task_id": client_id,
                "gateway_node": alt.name, "failover": tries + 1}
    logger.warning(f"FAILOVER exhausted for {client_id[:8]}.. ({reason}) {last_err}")
    return None


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
                "in_cooldown": n.in_cooldown,
                "consecutive_failures": n.consecutive_failures,
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
    params: dict = {"url": url, "sitekey": sitekey}
    if action:
        params["action"] = action
    if cdata:
        params["cdata"] = cdata

    data, node = await submit_to_pool("/turnstile", params)
    tid = data["task_id"]
    pool.register_task(tid, node, client_id=tid,
                       params={**params, "_endpoint": "/turnstile"})
    data["gateway_node"] = node.name
    return JSONResponse(content=data, status_code=202)


@app.get("/clearance")
async def submit_clearance(
    url: str = Query(...),
    timeout: int = Query(30),
):
    params = {"url": url, "timeout": timeout}
    data, node = await submit_to_pool("/clearance", params)
    tid = data["task_id"]
    pool.register_task(tid, node, client_id=tid,
                       params={**params, "_endpoint": "/clearance"})
    data["gateway_node"] = node.name
    return JSONResponse(content=data, status_code=202)


@app.get("/aws-token")
async def submit_aws_token(
    url: str = Query(...),
    timeout: int = Query(30),
):
    params = {"url": url, "timeout": timeout}
    data, node = await submit_to_pool("/aws-token", params)
    tid = data["task_id"]
    pool.register_task(tid, node, client_id=tid,
                       params={**params, "_endpoint": "/aws-token"})
    data["gateway_node"] = node.name
    return JSONResponse(content=data, status_code=202)


@app.get("/result")
async def get_result(id: str = Query(...)):
    client_id = id
    current = pool.task_redirects.get(client_id, client_id)
    node = pool.get_node_for_task(current)
    nodes_to_query = [node] if node else [n for n in pool.nodes if n.is_healthy]

    data: Optional[dict] = None
    resp_status = 200
    miss_node: Optional[NodeState] = None
    for n in nodes_to_query:
        if not n:
            continue
        try:
            r = await _client().get(f"{n.url}/result", params={"id": current},
                                    timeout=10.0)
        except Exception as e:
            miss_node = n
            logger.warning(f"Poll miss on {n.name} for {current[:8]}..: {e}")
            continue
        if r.status_code == 404:
            miss_node = n
            continue
        try:
            data = r.json()
        except Exception:
            miss_node = n
            continue
        resp_status = r.status_code if r.status_code in (200, 202) else 200
        break

    if data is None:
        # Task tidak ditemukan / node ngehang total -> failover kalau masih ada params
        misses = pool.task_poll_misses.get(client_id, 0) + 1
        pool.task_poll_misses[client_id] = misses
        if miss_node and misses >= MAX_POLL_MISSES:
            miss_node.record_failure()
        fo = await maybe_failover(client_id,
                                  f"poll miss x{misses} ({miss_node.name if miss_node else '?'})")
        if fo:
            return JSONResponse(content=fo, status_code=200)
        raise HTTPException(status_code=404, detail="Task ID not found on active pool solvers")

    status = data.get("status")
    if status == "success":
        pool.finish_task(current, success=True)
        pool.cleanup_task(client_id)
        return JSONResponse(content=data, status_code=resp_status)

    if status == "error":
        pool.finish_task(current, success=False)
        fo = await maybe_failover(client_id,
                                  f"task error: {data.get('value', '?')}")
        if fo:
            return JSONResponse(content=fo, status_code=200)
        pool.cleanup_task(client_id)
        return JSONResponse(content=data, status_code=resp_status)

    # status "process" / lainnya -> relay apa adanya
    if client_id in pool.task_poll_misses:
        pool.task_poll_misses[client_id] = 0
    return JSONResponse(content=data, status_code=resp_status)


def main():
    port = int(os.environ.get("GATEWAY_PORT", "8000"))
    uvicorn.run("pool_gateway:app", host="0.0.0.0", port=port, log_level="warning",
                access_log=False)


if __name__ == "__main__":
    main()
