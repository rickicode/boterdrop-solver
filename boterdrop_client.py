#!/usr/bin/env python3
"""Boterdrop captcha solver client — pool + load balancer otomatis.

Node pool (failover per-REQ, bukan sekali saat startup):
  1. Boterdrop laptop  (http://laptop-host.example.com:20011)  - offload CPU/RAM
  2. Boterdrop Docker  (http://127.0.0.1:20011)      - lokal, selalu ada

Endpoint upstream per node:
  GET /turnstile?url=<url>&sitekey=<key>      -> {"value": "<token>"}
  GET /clearance?url=<url>&timeout=<sec>      -> {"cf_clearance", "cookies", "user_agent"}
  GET /aws-token?url=<url>&timeout=<sec>      -> {"value": "<token>", ...}
  GET /recaptchaV3?url=<url>&sitekey=<key>    -> {"value": "<token>"}
  GET /result?id=<task_id>                    -> poll until status success/error

Aturan pool:
  - Sehat = /openapi.json respons <= HEALTH_TTL detik (cache, cek ulang saat expired).
  - Submit disebar round-robin antar node sehat; task_id lalu di-POLL di node yang sama
    (task bersifat lokal per node).
  - Node gagal di tengah -> failover otomatis ke node berikutnya.

Pemakaian (import):
    from boterdrop_client import BoterdropSolver
    s = BoterdropSolver()
    token = s.solve_turnstile("https://situs.com/", "0x4AAAAAAA...")
    clr   = s.solve_clearance("https://situs.com/")

Pemakaian (CLI):
    python3 boterdrop_client.py health
    python3 boterdrop_client.py turnstile --url https://x.com --sitekey 0x4AAA...
"""
import argparse
import itertools
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

NODE_CANDIDATES = [
    ("cloud", "http://node-cloud.example.com:11473"),  # utama: cloud node
    ("laptop", "http://laptop-host.example.com:20011"),                        # cadangan: laptop
    ("gateway", "http://127.0.0.1:20012"),                          # cadangan: local pool gateway
    ("local_docker", "http://127.0.0.1:20011"),                     # cadangan: docker lokal
]
HEALTH_TTL = 20.0        # detik sebelum health di-check ulang
HEALTH_TIMEOUT = 1.5


def _node_list() -> list[tuple[str, str]]:
    """Ambil daftar node: env BOTERDROP_URLS (comma) > BOTERDROP_URL > default pool."""
    env_multi = os.environ.get("BOTERDROP_URLS", "").strip()
    if env_multi:
        out = []
        for i, u in enumerate(x.strip().rstrip("/") for x in env_multi.split(",") if x.strip()):
            out.append((f"env{i}", u))
        return out
    env_one = os.environ.get("BOTERDROP_URL", "").strip()
    if env_one:
        return [("env", env_one.rstrip("/"))]
    return list(NODE_CANDIDATES)


def get_default_boterdrop_base() -> str:
    """Backward-compat: base node sehat pertama (atau lokal sebagai fallback)."""
    nodes = _node_list()
    for name, base in nodes:
        if _probe(base):
            return base
    return "http://node-cloud.example.com:11473"


def _probe(base: str) -> bool:
    try:
        req = urllib.request.Request(f"{base}/openapi.json")
        with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT) as resp:
            return resp.status == 200
    except Exception:
        return False


class BoterdropError(RuntimeError):
    """Raised ketika task gagal, timeout, atau semua node tidak terjangkau."""


class BoterdropPool:
    """Load balancer sehat-failover antar node Boterdrop."""

    def __init__(self, nodes: list[tuple[str, str]] | None = None):
        self.nodes = nodes or _node_list()
        self._healthy: dict[str, bool] = {b: False for _, b in self.nodes}
        self._checked: dict[str, float] = {b: 0.0 for _, b in self.nodes}
        self._rr = itertools.cycle([b for _, b in self.nodes])
        self._lock_note = ""

    def _refresh(self, base: str) -> bool:
        now = time.time()
        if now - self._checked.get(base, 0.0) < HEALTH_TTL:
            return self._healthy.get(base, False)
        ok = _probe(base)
        self._healthy[base] = ok
        self._checked[base] = now
        return ok

    def healthy_nodes(self) -> list[str]:
        return [b for _, b in self.nodes if self._refresh(b)]

    def next_node(self) -> str:
        """Round-robin antar node sehat; kalau semua tidak sehat pakai lokal."""
        healthy = self.healthy_nodes()
        if not healthy:
            return "http://node-cloud.example.com:11473"
        for _ in range(len(self.nodes)):
            base = next(self._rr)
            if base in healthy:
                return base
        return healthy[0]

    def all_nodes_down(self) -> bool:
        return not self.healthy_nodes()


DEFAULT_BASE = get_default_boterdrop_base()


class BoterdropSolver:
    def __init__(self, base_url: str = None, poll_interval: float = 1.0, timeout: float = 300.0,
                 pool: BoterdropPool = None):
        # base_url eksplisit -> pin satu node (mode lama); tanpa itu -> pakai pool LB
        self._pinned = base_url.rstrip("/") if base_url else None
        self.pool = pool or BoterdropPool()
        self.poll_interval = poll_interval
        self.timeout = timeout

    @property
    def base(self) -> str:
        return self._pinned if self._pinned else self.pool.next_node()

    # ---------- internal ----------
    def _get(self, path: str, params: dict, timeout: float = 30.0,
              max_retries: int = 5, base: str = None):
        """GET ke node `base` (default: pilihan pool), failover ke node lain kalau connection error."""
        nodes_order = [base] if base else []
        # susun urutan: base utama dulu, lalu sisanya dari pool
        seen = set(nodes_order)
        for _, b in self.pool.nodes:
            if b not in seen:
                nodes_order.append(b)
                seen.add(b)

        last_exc = None
        for node in nodes_order:
            url = f"{node}{path}?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            for attempt in range(max_retries):
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        return json.loads(resp.read().decode("utf-8", "replace") or "{}")
                except urllib.error.HTTPError as e:
                    body = e.read().decode("utf-8", "replace")[:300]
                    if e.code in (429, 502, 503) and attempt < max_retries - 1:
                        time.sleep(2 + attempt * 2)
                        last_exc = BoterdropError(f"HTTP {e.code} dari {path}: {body}")
                        continue
                    raise BoterdropError(f"HTTP {e.code} dari {path}: {body}") from e
                except urllib.error.URLError as e:
                    # node mati -> failover langsung ke node berikutnya
                    last_exc = BoterdropError(f"Node {node} down ({e.reason})")
                    break
                except TimeoutError as e:
                    last_exc = BoterdropError(f"Node {node} timeout: {e}")
                    break
            continue
        if last_exc:
            raise last_exc
        raise BoterdropError(f"Gagal request ke {path} di semua node pool")

    def _submit(self, path: str, params: dict) -> dict:
        # pilih node sehat untuk submit (task_id lokal per node)
        if self._pinned:
            node = self._pinned
        else:
            node = self.pool.next_node()
        accepted = self._get(path, params, base=node)
        task_id = accepted.get("task_id")
        if not task_id:
            if accepted.get("status") == "success":
                return accepted
            raise BoterdropError(f"Tidak ada task_id dari {path}: {accepted}")
        time.sleep(1.0)
        return self._poll(task_id, base=node)

    def _poll(self, task_id: str, base: str = None) -> dict:
        deadline = time.time() + self.timeout
        last = {}
        consecutive_404 = 0
        while time.time() < deadline:
            try:
                last = self._get("/result", {"id": task_id}, timeout=30.0, base=base)
                consecutive_404 = 0
            except BoterdropError as e:
                # 404 awal = task baru belum ter-propagasi; connection error = node down -> failover (handled di _get)
                if "HTTP 404" in str(e) and consecutive_404 < 8:
                    consecutive_404 += 1
                    time.sleep(self.poll_interval + 1.0)
                    continue
                raise
            status = (last.get("status") or "").lower()
            if status == "success":
                return last
            if status in ("error", "failed"):
                raise BoterdropError(f"Solve gagal: {json.dumps(last)[:300]}")
            time.sleep(self.poll_interval)
        raise BoterdropError(f"Timeout {self.timeout}s menanti task {task_id}: {json.dumps(last)[:200]}")

    # ---------- public ----------
    def health(self) -> bool:
        """True kalau minimal satu node pool hidup."""
        return bool(self.pool.healthy_nodes())

    def health_detail(self) -> dict:
        return {
            "pooled": [b for _, b in self.pool.nodes],
            "healthy": self.pool.healthy_nodes(),
            "active": self.base,
        }

    def solve_turnstile(self, url: str, sitekey: str) -> str:
        res = self._submit("/turnstile", {"url": url, "sitekey": sitekey})
        token = res.get("value") or res.get("token")
        if not token:
            raise BoterdropError(f"Turnstile tanpa token: {json.dumps(res)[:300]}")
        return token

    def solve_clearance(self, url: str, timeout: int = 90) -> dict:
        return self._submit("/clearance", {"url": url, "timeout": timeout})

    def solve_aws_token(self, url: str, timeout: int = 90) -> str:
        res = self._submit("/aws-token", {"url": url, "timeout": timeout})
        token = res.get("value") or res.get("token")
        if not token:
            raise BoterdropError(f"AWS WAF tanpa token: {json.dumps(res)[:300]}")
        return token

    def solve_recaptcha_v3(self, url: str, sitekey: str, action: str = "submit") -> str:
        res = self._submit("/recaptchaV3", {"url": url, "sitekey": sitekey, "action": action})
        token = res.get("value") or res.get("token")
        if not token:
            raise BoterdropError(f"reCAPTCHA v3 tanpa token: {json.dumps(res)[:300]}")
        return token


def apply_clearance_to_nodriver(tab, clearance: dict):
    """Set cookie cf_clearance ke tab nodriver + opsional override UA.

    Catatan: UA tidak bisa diubah setelah browser start; pakai UA hasil solver
    saat membuat browser baru bila situs memeriksa konsistensi UA<->cookie.
    """
    cookie_str = clearance.get("cookies") or ""
    if not cookie_str and clearance.get("cf_clearance"):
        cookie_str = f"cf_clearance={clearance['cf_clearance']}"
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        if name.strip().lower() != "cf_clearance":
            continue
        try:
            tab.send(__import__("nodriver").cdp.network.set_cookie(
                name=name.strip(), value=value.strip(), domain=None, path="/",
                secure=True, http_only=False,
            ))
        except Exception as e:  # pragma: no cover
            sys.stderr.write(f"[boterdrop] gagal set cookie via CDP: {e}\n")


def main():
    ap = argparse.ArgumentParser(description="Boterdrop captcha solver client (pool LB)")
    ap.add_argument("mode", choices=["turnstile", "clearance", "aws-token", "recaptchaV3", "health"])
    ap.add_argument("--url")
    ap.add_argument("--sitekey")
    ap.add_argument("--action", default="submit")
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()

    s = BoterdropSolver(timeout=args.timeout)
    try:
        if args.mode == "health":
            print(json.dumps({"alive": s.health(), **s.health_detail()}))
        elif args.mode == "turnstile":
            print(json.dumps({"value": s.solve_turnstile(args.url, args.sitekey)}))
        elif args.mode == "clearance":
            print(json.dumps(s.solve_clearance(args.url)))
        elif args.mode == "aws-token":
            print(json.dumps({"value": s.solve_aws_token(args.url)}))
        else:
            print(json.dumps({"value": s.solve_recaptcha_v3(args.url, args.sitekey, args.action)}))
    except BoterdropError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
