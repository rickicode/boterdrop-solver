#!/usr/bin/env python3
"""Boterdrop FastMCP Server for Hermes Agent.

Provides Turnstile, Cloudflare Clearance, and AWS WAF token solving capabilities
directly to Hermes through MCP protocol over stdio.
Endpoint di-resolve dari env BOTERDROP_URL / BOTERDROP_GATEWAY_URL / BOTERDROP_NODES.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any, Dict, Optional
import httpx
from mcp.server.fastmcp import FastMCP

def get_boterdrop_gateway() -> str:
    """Auto-detect active Boterdrop solver endpoint.
    
    Priority: BOTERDROP_GATEWAY_URL > BOTERDROP_URL > first healthy in BOTERDROP_NODES.
    """
    env = os.environ.get("BOTERDROP_GATEWAY_URL") or os.environ.get("BOTERDROP_URL")
    if env:
        return env.rstrip("/")

    candidates = [
        u.strip().rstrip("/")
        for u in os.environ.get("BOTERDROP_NODES", "").split(",")
        if u.strip()
    ]
    for url in candidates:
        try:
            r = httpx.get(f"{url}/openapi.json", timeout=1.0)
            if r.status_code == 200:
                return url
        except Exception:
            continue
    return candidates[0] if candidates else ""


GATEWAY_URL = get_boterdrop_gateway()

mcp = FastMCP(
    "boterdrop",
    instructions="Boterdrop CAPTCHA & Turnstile solver pool. Use to solve Cloudflare Turnstile, cf_clearance, and AWS WAF tokens without browser automation.",
)


async def _poll_result(client: httpx.AsyncClient, task_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(2.0)
        try:
            r = await client.get(f"{get_boterdrop_gateway()}/result", params={"id": task_id}, timeout=10.0)
            if r.status_code == 200:
                data = r.json()
                if data.get("status") in ("success", "error"):
                    return data
        except Exception:
            pass
    raise TimeoutError(f"Task {task_id} did not complete within {timeout}s")


@mcp.tool()
async def get_solver_pool_status() -> Dict[str, Any]:
    """Check the health status and current load of the Boterdrop solver pool nodes."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        gw = get_boterdrop_gateway()
        try:
            r = await client.get(f"{gw}/openapi.json")
            if r.status_code == 200:
                return {"status": "ok", "active_endpoint": gw}
            return {"status": "degraded", "active_endpoint": gw, "code": r.status_code}
        except Exception as e:
            return {"status": "error", "message": f"Failed to contact endpoint {gw}: {e}"}


@mcp.tool()
async def solve_turnstile(url: str, sitekey: str, action: Optional[str] = None) -> Dict[str, Any]:
    """Solve a Cloudflare Turnstile challenge.

    Args:
        url: The full target URL displaying the Turnstile challenge (e.g. 'https://accounts.x.ai/sign-up?redirect=grok-com').
        sitekey: The Turnstile sitekey (e.g. '0x4AAAAAAAhr9JGVDZbrZOo0').
        action: Optional action string. Leave empty/null for widgets without an action (like xAI).

    Returns:
        Dict with status, token value (to place into cf-turnstile-response), and elapsed time.
    """
    params: dict = {"url": url, "sitekey": sitekey}
    if action:
        params["action"] = action

    gw = get_boterdrop_gateway()
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Submit task with retry
        task_id = None
        for attempt in range(5):
            try:
                r = await client.get(f"{gw}/turnstile", params=params)
                if r.status_code == 202:
                    data = r.json()
                    task_id = data.get("task_id")
                    break
                if r.status_code in (429, 502, 503):
                    await asyncio.sleep(2.0 + attempt * 2)
                    continue
            except Exception:
                await asyncio.sleep(2.0)

        if not task_id:
            return {"status": "error", "message": f"Failed to submit Turnstile task to {gw}"}

        try:
            res = await _poll_result(client, task_id, timeout=90.0)
            if res.get("status") == "success":
                return {
                    "status": "success",
                    "token": res.get("value"),
                    "elapsed_time": res.get("elapsed_time"),
                    "task_id": task_id,
                    "solver_endpoint": gw,
                }
            return {
                "status": "error",
                "message": res.get("value", "Solve failed"),
                "task_id": task_id,
                "solver_endpoint": gw,
            }
        except TimeoutError as e:
            return {"status": "timeout", "message": str(e), "task_id": task_id, "solver_endpoint": gw}


@mcp.tool()
async def solve_cloudflare_clearance(url: str, timeout: int = 30) -> Dict[str, Any]:
    """Solve Cloudflare Under-Attack / Challenge clearance page to obtain cf_clearance cookie.

    Args:
        url: The challenge-protected target URL.
        timeout: Maximum seconds to wait for clearance (default 30).

    Returns:
        Dict with cf_clearance cookie, cookies header string, and matching user_agent.
    """
    gw = get_boterdrop_gateway()
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.get(f"{gw}/clearance", params={"url": url, "timeout": timeout})
            if r.status_code != 202:
                return {"status": "error", "message": f"Clearance submit returned HTTP {r.status_code}: {r.text[:200]}"}
            task_id = r.json().get("task_id")
            res = await _poll_result(client, task_id, timeout=float(timeout + 15))
            if res.get("status") == "success":
                val = res.get("value") or {}
                return {
                    "status": "success",
                    "cf_clearance": val.get("cf_clearance"),
                    "user_agent": val.get("user_agent"),
                    "cookies": val.get("cookies"),
                    "elapsed_time": res.get("elapsed_time"),
                    "solver_endpoint": gw,
                }
            return {"status": "error", "message": res.get("value", "Clearance failed"), "solver_endpoint": gw}
        except Exception as e:
            return {"status": "error", "message": str(e)}


@mcp.tool()
async def solve_aws_waf_token(url: str, timeout: int = 30) -> Dict[str, Any]:
    """Solve AWS WAF challenge page and obtain aws-waf-token cookie.

    Args:
        url: The target protected URL.
        timeout: Maximum seconds to wait (default 30).

    Returns:
        Dict with token and matching user_agent.
    """
    gw = get_boterdrop_gateway()
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.get(f"{gw}/aws-token", params={"url": url, "timeout": timeout})
            if r.status_code != 202:
                return {"status": "error", "message": f"AWS token submit returned HTTP {r.status_code}: {r.text[:200]}"}
            task_id = r.json().get("task_id")
            res = await _poll_result(client, task_id, timeout=float(timeout + 15))
            if res.get("status") == "success":
                return {
                    "status": "success",
                    "token": res.get("value"),
                    "elapsed_time": res.get("elapsed_time"),
                    "solver_endpoint": gw,
                }
            return {"status": "error", "message": res.get("value", "AWS token solve failed"), "solver_endpoint": gw}
        except Exception as e:
            return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    mcp.run()
