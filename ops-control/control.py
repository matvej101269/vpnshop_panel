import hmac
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

TOKEN = os.environ.get("CONTROL_TOKEN", "")
PROJECT_DIR = os.environ.get("PROJECT_DIR", "/opt/vpnshop")
COMPOSE = ["docker", "compose", "--project-directory", PROJECT_DIR, "-f", os.path.join(PROJECT_DIR, "docker-compose.yml")]
STATE = {"operation": "", "status": "idle", "message": ""}
STATE_LOCK = threading.Lock()


def run_operation(operation):
    with STATE_LOCK:
        STATE.update(operation=operation, status="running", message="Выполняется…")
    try:
        time.sleep(1)
        if operation == "restart":
            output = subprocess.run(COMPOSE + ["restart", "vpnshop"], cwd=PROJECT_DIR,
                                    capture_output=True, text=True, timeout=90, check=False)
            print("restart: " + (output.stdout + output.stderr).strip(), flush=True)
            if output.returncode:
                raise RuntimeError((output.stdout + output.stderr)[-12000:])
            message = "VPN Shop перезапущен"
        else:
            current = subprocess.run(["git", "-C", PROJECT_DIR, "rev-parse", "HEAD"],
                                     capture_output=True, text=True, timeout=15, check=False)
            if current.returncode:
                raise RuntimeError("Cannot determine the currently installed revision")
            previous_revision = current.stdout.strip()
            dirty = subprocess.run(["git", "-C", PROJECT_DIR, "status", "--porcelain"],
                                   capture_output=True, text=True, timeout=15, check=False)
            if dirty.returncode or dirty.stdout.strip():
                raise RuntimeError("Update stopped because the installation directory contains local changes")
            image_result = subprocess.run(COMPOSE + ["images", "-q", "vpnshop"], cwd=PROJECT_DIR,
                                          capture_output=True, text=True, timeout=30, check=False)
            old_image = next((line.strip() for line in image_result.stdout.splitlines() if line.strip()), "")
            image_name = ""
            containers = subprocess.run(COMPOSE + ["ps", "-q", "vpnshop"], cwd=PROJECT_DIR,
                                        capture_output=True, text=True, timeout=30, check=False)
            if containers.stdout.strip():
                inspected = subprocess.run(["docker", "inspect", "-f", "{{.Config.Image}}", containers.stdout.splitlines()[0]],
                                           capture_output=True, text=True, timeout=15, check=False)
                image_name = inspected.stdout.strip()
            pulled = subprocess.run(["git", "-C", PROJECT_DIR, "pull", "--ff-only", "origin", "main"],
                                    capture_output=True, text=True, timeout=120, check=False)
            print("update git: " + (pulled.stdout + pulled.stderr).strip(), flush=True)
            if pulled.returncode:
                raise RuntimeError((pulled.stdout + pulled.stderr)[-12000:] or "git pull failed")
            result = subprocess.run(COMPOSE + ["build", "vpnshop"], cwd=PROJECT_DIR,
                                    capture_output=True, text=True, timeout=900, check=False)
            print("update build: " + (result.stdout + result.stderr).strip(), flush=True)
            if result.returncode:
                subprocess.run(["git", "-C", PROJECT_DIR, "reset", "--hard", previous_revision],
                               capture_output=True, text=True, timeout=30, check=False)
                raise RuntimeError((result.stdout + result.stderr)[-12000:] or "docker compose build failed")
            result = subprocess.run(COMPOSE + ["up", "-d", "--no-deps", "vpnshop"], cwd=PROJECT_DIR,
                                    capture_output=True, text=True, timeout=180, check=False)
            print("update deploy: " + (result.stdout + result.stderr).strip(), flush=True)
            if result.returncode:
                if old_image and image_name:
                    subprocess.run(["docker", "image", "tag", old_image, image_name], timeout=30, check=False)
                    subprocess.run(COMPOSE + ["up", "-d", "--no-deps", "--no-build", "vpnshop"],
                                   cwd=PROJECT_DIR, capture_output=True, text=True, timeout=180, check=False)
                subprocess.run(["git", "-C", PROJECT_DIR, "reset", "--hard", previous_revision],
                               capture_output=True, text=True, timeout=30, check=False)
                raise RuntimeError((result.stdout + result.stderr)[-12000:] or "vpnshop restart failed")
            healthy = False
            deadline = time.time() + 150
            while time.time() < deadline:
                check = subprocess.run(COMPOSE + ["ps", "-q", "vpnshop"], cwd=PROJECT_DIR,
                                       capture_output=True, text=True, timeout=15, check=False)
                container_id = check.stdout.strip().splitlines()[0] if check.stdout.strip() else ""
                if container_id:
                    health = subprocess.run(["docker", "inspect", "-f", "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}", container_id],
                                            capture_output=True, text=True, timeout=15, check=False)
                    if health.stdout.strip() == "healthy":
                        healthy = True
                        break
                    if health.stdout.strip() in {"unhealthy", "exited", "dead"}:
                        break
                time.sleep(5)
            if not healthy:
                # Retain and restore the exact previous image, then return the checkout to its known revision.
                if old_image and image_name:
                    subprocess.run(["docker", "image", "tag", old_image, image_name], timeout=30, check=False)
                    subprocess.run(COMPOSE + ["up", "-d", "--no-deps", "--no-build", "vpnshop"],
                                   cwd=PROJECT_DIR, capture_output=True, text=True, timeout=180, check=False)
                subprocess.run(["git", "-C", PROJECT_DIR, "reset", "--hard", previous_revision],
                               capture_output=True, text=True, timeout=30, check=False)
                raise RuntimeError("New version did not become healthy; previous image and source revision were restored")
            message = "Приложение обновлено до origin/main"
        with STATE_LOCK:
            STATE.update(status="success", message=message)
    except Exception as exc:
        with STATE_LOCK:
            STATE.update(status="failure", message=str(exc)[-12000:])


class Handler(BaseHTTPRequestHandler):
    server_version = "VPNShopControl/1.0"

    def log_message(self, fmt, *args):
        # BaseHTTPRequestHandler includes the container source IP; keep it out of logs.
        return

    def _authorized(self):
        supplied = self.headers.get("Authorization", "")
        return bool(TOKEN) and hmac.compare_digest(supplied, "Bearer " + TOKEN)

    def _reply(self, status, body, content_type="application/json"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _run(self, args, timeout=60):
        result = subprocess.run(COMPOSE + args, cwd=PROJECT_DIR, capture_output=True, text=True,
                                timeout=timeout, check=False)
        output = (result.stdout + result.stderr).strip()
        if result.returncode:
            raise RuntimeError(output[-12000:] or "Command failed with exit code " + str(result.returncode))
        return output

    def do_GET(self):
        if not self._authorized():
            return self._reply(401, '{"error":"unauthorized"}')
        parsed = urlsplit(self.path)
        if parsed.path == "/status":
            with STATE_LOCK:
                self._reply(200, json.dumps(STATE, ensure_ascii=False))
            return
        if parsed.path != "/logs":
            return self._reply(404, '{"error":"not found"}')
        try:
            lines = int(parse_qs(parsed.query).get("lines", ["500"])[0])
            lines = max(50, min(lines, 1000))
            output = self._run(["logs", "--no-color", "--tail=" + str(lines), "vpnshop", "vpnshop-control"], timeout=30)
            self._reply(200, output, "text/plain")
        except Exception as exc:
            self._reply(502, json.dumps({"error": str(exc)}))

    def do_POST(self):
        if not self._authorized():
            return self._reply(401, '{"error":"unauthorized"}')
        path = urlsplit(self.path).path
        if path not in {"/restart", "/update"}:
            return self._reply(404, '{"error":"not found"}')
        operation = path.removeprefix("/")
        with STATE_LOCK:
            if STATE["status"] in {"queued", "running"}:
                return self._reply(409, '{"error":"another operation is running"}')
            STATE.update(operation=operation, status="queued", message="Задача поставлена в очередь")
        threading.Thread(target=run_operation, args=(operation,), daemon=True).start()
        self._reply(202, json.dumps({"ok": True, "status": "queued"}))


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("CONTROL_TOKEN is required")
    ThreadingHTTPServer(("0.0.0.0", 8765), Handler).serve_forever()
