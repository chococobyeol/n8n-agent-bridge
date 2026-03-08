import json
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

import requests


CONFIG_PATH = Path("config.json")
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

RAW_LOG_FILE = LOG_DIR / "acp_raw_multisession.log"
JSON_LOG_FILE = LOG_DIR / "acp_json_multisession.log"


def log_raw(line: str) -> None:
    with open(RAW_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


def log_json(obj) -> None:
    with open(JSON_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

N8N_BASE_URL = CONFIG["n8n_base_url"].rstrip("/")
POLL_ENDPOINT = CONFIG["poll_endpoint"]
RESULT_ENDPOINT = CONFIG["result_endpoint"]
SHARED_SECRET = CONFIG["shared_secret"]
POLL_INTERVAL = int(CONFIG.get("poll_interval_sec", 5))
CURSOR_COMMAND = CONFIG.get("cursor_command", ["cursor", "agent", "acp"])

workspace_raw = str(CONFIG.get("workspace", "")).strip()
if not workspace_raw:
    raise SystemExit("config.json missing required key: workspace")

WORKSPACE = os.path.abspath(os.path.expanduser(workspace_raw))
os.makedirs(WORKSPACE, exist_ok=True)

HEADERS = {
    "X-Webhook-Secret": SHARED_SECRET,
    "Content-Type": "application/json",
}


@dataclass
class SessionState:
    name: str
    session_id: Optional[str] = None
    busy: bool = False
    output_buffer: list[str] = field(default_factory=list)
    current_prompt_rpc_id: Optional[int] = None
    current_job: Optional[dict] = None
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)

    # cancel / kill support
    cancel_requested: bool = False
    closed: bool = False


class CursorAcpBridgeMultiSession:
    def __init__(self):
        self.proc = None
        self.reader_thread = None
        self.stderr_thread = None
        self.writer_thread = None

        self.running = True
        self.outgoing = queue.Queue()
        self.next_id = 1

        self.init_done = False
        self.auth_done = False

        # 세션 이름 -> 세션 상태
        self.sessions_by_name: Dict[str, SessionState] = {}

        # ACP sessionId -> 세션 이름
        self.session_name_by_id: Dict[str, str] = {}

        # session/new rpc_id -> 생성할 세션 이름
        self.pending_session_create_rpc: Dict[int, str] = {}

        # 세션 생성이 끝나면 즉시 넣을 job
        self.pending_job_for_session_name: Dict[str, dict] = {}

        # session/request_permission 대응용
        self.permission_request_ids_handled: set[int] = set()

    # --------------------------------------------------
    # Process / RPC basics
    # --------------------------------------------------
    def start_cursor(self) -> None:
        self.proc = subprocess.Popen(
            CURSOR_COMMAND,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=WORKSPACE,
        )
        print(f"[multi] started Cursor ACP: pid={self.proc.pid}")
        print(f"[multi] workspace = {WORKSPACE}")
        print(f"[multi] process cwd = {WORKSPACE}")

        self.reader_thread = threading.Thread(target=self.stdout_loop, daemon=True)
        self.stderr_thread = threading.Thread(target=self.stderr_loop, daemon=True)
        self.writer_thread = threading.Thread(target=self.writer_loop, daemon=True)

        self.reader_thread.start()
        self.stderr_thread.start()
        self.writer_thread.start()

    def stdout_loop(self) -> None:
        while self.running and self.proc and self.proc.stdout:
            line = self.proc.stdout.readline()
            if not line:
                break

            log_raw(line)
            stripped = line.strip()
            if not stripped:
                continue

            try:
                msg = json.loads(stripped)
                log_json(msg)
                self.handle_rpc_message(msg)
            except Exception as e:
                print(f"[multi] failed to parse stdout JSON: {e}")

    def stderr_loop(self) -> None:
        while self.running and self.proc and self.proc.stderr:
            line = self.proc.stderr.readline()
            if not line:
                break
            log_raw("[stderr] " + line)
            print("[cursor stderr]", line.rstrip())

    def writer_loop(self) -> None:
        while self.running and self.proc and self.proc.stdin:
            msg = self.outgoing.get()
            if msg is None:
                return

            payload = json.dumps(msg, ensure_ascii=False)
            try:
                self.proc.stdin.write(payload + "\n")
                self.proc.stdin.flush()
                print("[multi] ->", payload)
            except Exception as e:
                print(f"[multi] failed to write to Cursor ACP: {e}")

    def send_rpc(self, method: str, params=None, rpc_id=None) -> int:
        if rpc_id is None:
            rpc_id = self.next_id
            self.next_id += 1

        msg = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": method,
            "params": params or {},
        }
        self.outgoing.put(msg)
        return rpc_id

    def send_rpc_notification(self, method: str, params=None) -> None:
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        self.outgoing.put(msg)

    def bootstrap(self) -> None:
        self.send_rpc(
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {
                    "name": "discord-n8n-bridge-multisession",
                    "version": "0.2.0",
                },
                "capabilities": {},
            },
        )

    # --------------------------------------------------
    # Session helpers
    # --------------------------------------------------
    def ensure_main_session(self) -> None:
        if "main" not in self.sessions_by_name:
            self.create_session("main")

    def create_session(self, name: str) -> None:
        name = name.strip()
        if not name:
            return

        if name in self.sessions_by_name:
            return

        state = SessionState(name=name)
        self.sessions_by_name[name] = state

        rpc_id = self.send_rpc(
            "session/new",
            {
                "cwd": WORKSPACE,
                "mcpServers": [],
            },
        )
        self.pending_session_create_rpc[rpc_id] = name
        print(f"[multi] creating session name={name!r} rpc_id={rpc_id} cwd={WORKSPACE}")

    def get_or_create_session(self, name: str) -> SessionState:
        name = name.strip() or "main"
        if name not in self.sessions_by_name:
            self.create_session(name)
        return self.sessions_by_name[name]

    def format_sessions_list(self) -> str:
        if not self.sessions_by_name:
            return "세션이 없습니다."

        lines = ["세션 목록"]
        for name in sorted(self.sessions_by_name.keys()):
            s = self.sessions_by_name[name]
            sid = s.session_id or "-"
            sid_short = sid[:8] if sid != "-" else "-"
            if s.closed:
                status = "closed"
            elif s.busy and s.cancel_requested:
                status = "cancelling"
            elif s.busy:
                status = "busy"
            else:
                status = "idle"
            lines.append(f"- {name} | {status} | {sid_short}")

        return "\n".join(lines)

    def close_session(self, name: str) -> tuple[bool, str]:
        name = name.strip()

        if not name:
            return False, "세션 이름이 비어 있습니다."

        if name == "main":
            return False, "main 세션은 종료할 수 없습니다."

        s = self.sessions_by_name.get(name)
        if not s:
            return False, f"{name} 세션이 없습니다."

        if s.busy:
            return False, f"{name} 세션은 현재 작업 중이라 종료할 수 없습니다."

        self._final_remove_session(name)
        return True, f"{name} 세션을 종료했습니다."

    def cancel_session(self, name: str) -> tuple[bool, str]:
        name = name.strip()
        if not name:
            return False, "세션 이름이 비어 있습니다."

        s = self.sessions_by_name.get(name)
        if not s:
            return False, f"{name} 세션이 없습니다."

        if not s.busy:
            return False, f"{name} 세션은 현재 작업 중이 아닙니다."

        s.cancel_requested = True
        return True, f"{name} 세션 작업 취소를 요청했습니다."

    def kill_session(self, name: str) -> tuple[bool, str]:
        name = name.strip()
        if not name:
            return False, "세션 이름이 비어 있습니다."

        if name == "main":
            return False, "main 세션은 강제 종료할 수 없습니다."

        s = self.sessions_by_name.get(name)
        if not s:
            return False, f"{name} 세션이 없습니다."

        # busy 여부와 관계없이 즉시 close 상태로 전환
        s.cancel_requested = True
        s.closed = True

        # 실행 중인 job이 있으면 즉시 취소 결과 전송
        if s.current_job:
            self.send_local_result(
                s.current_job.get("jobId"),
                False,
                "",
                f"{name} 세션이 강제 종료되었습니다.",
                -1,
            )

        # 로컬 참조 정리
        self._final_remove_session(name)
        return True, f"{name} 세션을 강제 종료했습니다."

    def close_all_sessions(self) -> str:
        closed = []
        skipped_busy = []

        for name in list(self.sessions_by_name.keys()):
            if name == "main":
                continue

            s = self.sessions_by_name[name]
            if s.busy:
                skipped_busy.append(name)
                continue

            self._final_remove_session(name)
            closed.append(name)

        parts = []
        parts.append("종료됨: " + (", ".join(closed) if closed else "없음"))
        if skipped_busy:
            parts.append("작업 중이라 유지됨: " + ", ".join(skipped_busy))
        return " | ".join(parts)

    def _final_remove_session(self, name: str) -> None:
        s = self.sessions_by_name.get(name)
        if not s:
            return

        if s.session_id:
            self.session_name_by_id.pop(s.session_id, None)

        self.sessions_by_name.pop(name, None)
        self.pending_job_for_session_name.pop(name, None)

    # --------------------------------------------------
    # Command / prompt parsing
    # --------------------------------------------------
    def parse_special_command(self, text: str) -> tuple[str, Optional[str]]:
        raw = (text or "").strip()

        if raw == "/sessions":
            return "sessions", None

        if raw == "/closeall":
            return "closeall", None

        m = re.match(r"^/new\s+([A-Za-z0-9_\-]+)\s*$", raw)
        if m:
            return "new", m.group(1)

        m = re.match(r"^/close\s+([A-Za-z0-9_\-]+)\s*$", raw)
        if m:
            return "close", m.group(1)

        m = re.match(r"^/cancel\s+([A-Za-z0-9_\-]+)\s*$", raw)
        if m:
            return "cancel", m.group(1)

        m = re.match(r"^/kill\s+([A-Za-z0-9_\-]+)\s*$", raw)
        if m:
            return "kill", m.group(1)

        return "none", None

    def parse_session_and_prompt(self, text: str) -> tuple[str, str]:
        raw = (text or "").strip()
        m = re.match(r"^([A-Za-z0-9_\-]+)\s*:\s*(.+)$", raw, flags=re.DOTALL)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        return "main", raw

    # --------------------------------------------------
    # n8n I/O
    # --------------------------------------------------
    def poll_job(self):
        url = N8N_BASE_URL + POLL_ENDPOINT
        payload = {"agent": "macbook-1-multisession"}

        try:
            r = requests.post(url, json=payload, headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()
            return data.get("job")
        except Exception as e:
            print("[multi] poll_job error:", e)
            return None

    def notify_result(self, result_payload: dict) -> None:
        url = N8N_BASE_URL + RESULT_ENDPOINT
        try:
            r = requests.post(url, json=result_payload, headers=HEADERS, timeout=30)
            r.raise_for_status()
            print("[multi] result sent:", result_payload.get("jobId"))
        except Exception as e:
            print("[multi] notify_result error:", e)

    def send_local_result(
        self,
        job_id: Optional[str],
        ok: bool,
        stdout: str,
        stderr: str = "",
        exit_code: int = 0,
    ) -> None:
        self.notify_result(
            {
                "jobId": job_id,
                "ok": ok,
                "stdout": stdout,
                "stderr": stderr,
                "exitCode": exit_code,
            }
        )

    # --------------------------------------------------
    # Job dispatch
    # --------------------------------------------------
    def handle_job(self, job: dict) -> None:
        raw_prompt = (job.get("prompt") or "").strip()
        job_id = job.get("jobId")

        if not raw_prompt:
            self.send_local_result(job_id, False, "", "Empty prompt", -1)
            return

        cmd, arg = self.parse_special_command(raw_prompt)
        if cmd != "none":
            if cmd == "sessions":
                self.send_local_result(job_id, True, self.format_sessions_list())
                return

            if cmd == "new":
                name = arg or ""
                if name in self.sessions_by_name:
                    self.send_local_result(job_id, True, f"{name} 세션은 이미 존재합니다.")
                    return
                self.create_session(name)
                self.send_local_result(job_id, True, f"{name} 세션 생성 요청을 보냈습니다.")
                return

            if cmd == "close":
                ok, msg = self.close_session(arg or "")
                self.send_local_result(job_id, ok, msg, "" if ok else msg, 0 if ok else -1)
                return

            if cmd == "closeall":
                self.send_local_result(job_id, True, self.close_all_sessions())
                return

            if cmd == "cancel":
                ok, msg = self.cancel_session(arg or "")
                self.send_local_result(job_id, ok, msg, "" if ok else msg, 0 if ok else -1)
                return

            if cmd == "kill":
                ok, msg = self.kill_session(arg or "")
                self.send_local_result(job_id, ok, msg, "" if ok else msg, 0 if ok else -1)
                return

        session_name, prompt = self.parse_session_and_prompt(raw_prompt)
        if not prompt:
            self.send_local_result(job_id, False, "", "Prompt empty after session parse", -1)
            return

        session = self.get_or_create_session(session_name)

        if session.closed:
            self.send_local_result(job_id, False, "", f"{session_name} 세션은 종료된 상태입니다.", -1)
            return

        if session.busy:
            self.send_local_result(job_id, False, "", f"{session_name} 세션은 현재 작업 중입니다.", -1)
            return

        if not session.session_id:
            self.pending_job_for_session_name[session_name] = {
                "jobId": job_id,
                "prompt": prompt,
                "session_name": session_name,
            }
            print(f"[multi] session {session_name!r} not ready yet; pending job saved")
            return

        self.submit_prompt_to_session(
            session_name=session_name,
            job={
                "jobId": job_id,
                "prompt": prompt,
            },
        )

    def submit_prompt_to_session(self, session_name: str, job: dict) -> None:
        session = self.sessions_by_name[session_name]
        prompt = (job.get("prompt") or "").strip()

        if not prompt:
            self.send_local_result(job.get("jobId"), False, "", "Empty prompt", -1)
            return

        if session.closed:
            self.send_local_result(job.get("jobId"), False, "", f"{session_name} 세션은 종료된 상태입니다.", -1)
            return

        if session.busy:
            self.send_local_result(job.get("jobId"), False, "", f"{session_name} 세션은 현재 작업 중입니다.", -1)
            return

        if not session.session_id:
            self.send_local_result(job.get("jobId"), False, "", f"{session_name} 세션이 아직 준비되지 않았습니다.", -1)
            return

        session.busy = True
        session.cancel_requested = False
        session.current_job = job
        session.output_buffer = []
        session.last_used_at = time.time()

        session.current_prompt_rpc_id = self.send_rpc(
            "session/prompt",
            {
                "sessionId": session.session_id,
                "prompt": [
                    {
                        "type": "text",
                        "text": prompt,
                    }
                ],
            },
        )

        print(f"[multi] got job for session={session_name!r}: {job}")
        print(f"[multi] submitted job {job.get('jobId')} as rpc_id={session.current_prompt_rpc_id}")

    # --------------------------------------------------
    # Permission handling
    # --------------------------------------------------
    def reject_permission_request(self, msg: dict) -> None:
        req_id = msg.get("id")
        if req_id is None:
            return
        if req_id in self.permission_request_ids_handled:
            return

        self.permission_request_ids_handled.add(req_id)

        # request_permission은 서버가 요청한 id에 대해 result로 응답
        response = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "optionId": "reject-once"
            },
        }
        self.outgoing.put(response)
        print(f"[multi] auto-rejected permission request id={req_id}")

    # --------------------------------------------------
    # RPC handling
    # --------------------------------------------------
    def handle_rpc_message(self, msg: dict) -> None:
        print("[multi] <-", json.dumps(msg, ensure_ascii=False))

        # initialize result
        if msg.get("id") == 1 and "result" in msg:
            self.init_done = True
            self.send_rpc(
                "authenticate",
                {
                    "methodId": "cursor_login",
                },
            )
            return

        # authenticate result
        if msg.get("id") == 2 and "result" in msg:
            self.auth_done = True
            self.ensure_main_session()
            return

        # session/new result
        if msg.get("id") in self.pending_session_create_rpc and "result" in msg:
            session_name = self.pending_session_create_rpc.pop(msg["id"])
            result = msg["result"]

            session_id = result.get("sessionId")
            if not session_id:
                print(f"[multi] session/new for {session_name!r} returned no sessionId")
                return

            state = self.sessions_by_name[session_name]
            state.session_id = session_id
            state.last_used_at = time.time()

            self.session_name_by_id[session_id] = session_name

            modes = result.get("modes", {})
            current_mode = modes.get("currentModeId")
            print(f"[multi] session ready name={session_name!r} session_id={session_id} mode={current_mode}")

            pending = self.pending_job_for_session_name.pop(session_name, None)
            if pending and not state.closed:
                print(f"[multi] dispatching pending job for session={session_name!r}")
                self.submit_prompt_to_session(session_name, pending)
            return

        # permission request
        if msg.get("method") == "session/request_permission":
            self.reject_permission_request(msg)
            return

        # prompt finished
        if "result" in msg and isinstance(msg["result"], dict) and msg["result"].get("stopReason") == "end_turn":
            finished_rpc_id = msg.get("id")

            for session_name, s in list(self.sessions_by_name.items()):
                if s.current_prompt_rpc_id == finished_rpc_id:
                    # close/kill 후 늦게 온 결과는 무시
                    if s.closed:
                        print(f"[multi] ignoring end_turn for closed session={session_name!r}")
                        return

                    full = "".join(s.output_buffer).strip()

                    if s.cancel_requested:
                        self.send_local_result(
                            s.current_job.get("jobId") if s.current_job else None,
                            False,
                            "",
                            f"{session_name} 세션 작업이 취소되었습니다.",
                            -1,
                        )
                    else:
                        # 에러 텍스트면 실패 처리
                        error_markers = [
                            "Error:",
                            "Connection stalled",
                            "timed out",
                            "failed",
                        ]
                        if any(marker in full for marker in error_markers):
                            self.send_local_result(
                                s.current_job.get("jobId") if s.current_job else None,
                                False,
                                "",
                                full,
                                -1,
                            )
                        else:
                            final_text = f"[session: {session_name}]\n{full}" if full else f"[session: {session_name}]"
                            self.send_local_result(
                                s.current_job.get("jobId") if s.current_job else None,
                                True,
                                final_text,
                                "",
                                0,
                            )

                    print(f"[multi] completed job for session={session_name!r}: {s.current_job.get('jobId') if s.current_job else None}")
                    print("[multi] final output:", full)

                    s.busy = False
                    s.current_job = None
                    s.output_buffer = []
                    s.current_prompt_rpc_id = None
                    s.cancel_requested = False
                    s.last_used_at = time.time()
                    return

        # rpc error
        if "error" in msg:
            err_text = json.dumps(msg["error"], ensure_ascii=False)
            failed_rpc_id = msg.get("id")

            for session_name, s in list(self.sessions_by_name.items()):
                if s.current_prompt_rpc_id == failed_rpc_id:
                    if s.closed:
                        print(f"[multi] ignoring rpc error for closed session={session_name!r}")
                        return

                    self.send_local_result(
                        s.current_job.get("jobId") if s.current_job else None,
                        False,
                        "",
                        err_text,
                        -1,
                    )
                    s.busy = False
                    s.current_job = None
                    s.output_buffer = []
                    s.current_prompt_rpc_id = None
                    s.cancel_requested = False
                    print(f"[multi] rpc error for session={session_name!r}: {err_text}")
                    return

            print("[multi] rpc error:", err_text)
            return

        # streaming update
        if msg.get("method") == "session/update":
            params = msg.get("params", {})
            session_id = params.get("sessionId")
            update = params.get("update", {})
            update_type = update.get("sessionUpdate")

            print("[multi] session/update =", json.dumps(update, ensure_ascii=False))

            session_name = self.session_name_by_id.get(session_id)
            if not session_name:
                print(f"[multi] unknown sessionId in update: {session_id}")
                return

            s = self.sessions_by_name.get(session_name)
            if not s:
                print(f"[multi] missing local session state for {session_name!r}")
                return

            # close/kill된 세션이면 이벤트 무시
            if s.closed:
                print(f"[multi] ignoring update for closed session={session_name!r}")
                return

            if update_type == "agent_message_chunk":
                content = update.get("content", {})
                if content.get("type") == "text":
                    text = content.get("text", "")
                    s.output_buffer.append(text)
                    print(f"[multi][{session_name}][message_chunk]", repr(text))

            elif update_type == "tool_call":
                print(f"[multi][{session_name}][tool_call]", json.dumps(update, ensure_ascii=False))

            elif update_type == "tool_call_update":
                print(f"[multi][{session_name}][tool_call_update]", json.dumps(update, ensure_ascii=False))

            return

    # --------------------------------------------------
    # Main loop
    # --------------------------------------------------
    def run(self) -> None:
        self.start_cursor()
        self.bootstrap()

        while self.running:
            if self.init_done and self.auth_done and "main" not in self.sessions_by_name:
                self.ensure_main_session()

            job = self.poll_job()
            if job:
                self.handle_job(job)

            time.sleep(POLL_INTERVAL)

    def stop(self) -> None:
        self.running = False
        self.outgoing.put(None)

        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    bridge = CursorAcpBridgeMultiSession()
    try:
        bridge.run()
    except KeyboardInterrupt:
        print("[multi] stopped by user")
        bridge.stop()