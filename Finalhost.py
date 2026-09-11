import asyncio
import html
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Preserved from the supplied sample.
BOT_TOKEN = "8717606762:AAGXYkSVws9XADKtaU5XaQ5outD5hot5xwI"
ADMIN_IDS = {6858000955}
ADMIN_IDS.update(int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit())

BASE_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
PROJECT_DIR = BASE_DIR / "projects"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = BASE_DIR / "runner.sqlite3"
for directory in (BASE_DIR, PROJECT_DIR, LOG_DIR):
    directory.mkdir(parents=True, exist_ok=True)

MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "50"))
MAX_PROJECTS = int(os.getenv("MAX_PROJECTS", "10"))
DEFAULT_QUOTA_MB = int(os.getenv("DEFAULT_QUOTA_MB", "500"))
MAX_RUNTIME_SECONDS = int(os.getenv("MAX_RUNTIME_SECONDS", "900"))
MAX_OUTPUT_BYTES = int(os.getenv("MAX_OUTPUT_BYTES", "200000"))
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "2"))
RUNNER_NETWORK = os.getenv("RUNNER_NETWORK", "none")
DOCKER_IMAGE_PYTHON = os.getenv("DOCKER_IMAGE_PYTHON", "python:3.12-slim")
DOCKER_IMAGE_NODE = os.getenv("DOCKER_IMAGE_NODE", "node:22-slim")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("telegram-vps-runner")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def esc(value: object) -> str:
    return html.escape(str(value))


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '', username TEXT NOT NULL DEFAULT '',
                approved INTEGER NOT NULL DEFAULT 0,
                quota_mb INTEGER NOT NULL DEFAULT 500,
                used_mb REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL,
                path TEXT NOT NULL, kind TEXT NOT NULL, entrypoint TEXT,
                status TEXT NOT NULL DEFAULT 'stopped', container TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(user_id, name)
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, user_id INTEGER NOT NULL,
                mode TEXT NOT NULL, status TEXT NOT NULL, command TEXT NOT NULL,
                container TEXT, exit_code INTEGER, started_at TEXT, finished_at TEXT,
                log_path TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, user_id INTEGER NOT NULL,
                interval_seconds INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                next_run REAL NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS env_vars (
                project_id TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL,
                PRIMARY KEY(project_id, name)
            );
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT,
                detail TEXT, created_at TEXT NOT NULL
            );
            """
        )


def audit(user_id: int, action: str, detail: str = "") -> None:
    with db() as conn:
        conn.execute("INSERT INTO audit_logs(user_id, action, detail, created_at) VALUES(?,?,?,?)", (user_id, action, detail[:500], now()))


def user_row(user_id: int, update: Optional[Update] = None) -> sqlite3.Row:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            name = update.effective_user.full_name if update and update.effective_user else ""
            username = update.effective_user.username if update and update.effective_user else ""
            conn.execute("INSERT INTO users(user_id,name,username,created_at) VALUES(?,?,?,?)", (user_id, name, username or "", now()))
            row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def approved(user_id: int) -> bool:
    return is_admin(user_id) or bool(user_row(user_id)["approved"])


def guard(update: Update) -> bool:
    return bool(update.effective_user and approved(update.effective_user.id))


def project_for(user_id: int, name: str) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM projects WHERE user_id=? AND name=?", (user_id, name)).fetchone()


def safe_name(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", name.strip())
    return name[:40].strip("._-") or "project"


def safe_zip_extract(source: Path, target: Path) -> None:
    max_unpacked = MAX_FILE_MB * 10 * 1024 * 1024
    total = 0
    with zipfile.ZipFile(source) as archive:
        for item in archive.infolist():
            if item.is_dir():
                continue
            total += item.file_size
            if total > max_unpacked:
                raise ValueError("ZIP unpacked size is too large")
            destination = (target / item.filename).resolve()
            if not str(destination).startswith(str(target.resolve()) + os.sep):
                raise ValueError("Unsafe ZIP path detected")
        archive.extractall(target)


def detect_project(root: Path, original_name: str) -> tuple[str, str]:
    manifest = root / "runner.json"
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        kind = data.get("type", "python")
        entry = data.get("entrypoint", "main.py" if kind == "python" else "index.js")
        if kind not in {"python", "node"}:
            raise ValueError("runner.json type must be python or node")
        if not (root / entry).is_file():
            raise ValueError("runner.json entrypoint does not exist")
        return kind, entry
    if original_name.endswith(".py"):
        return "python", original_name
    if original_name.endswith(".js"):
        return "node", original_name
    priority = ["main.py", "bot.py", "app.py", "index.py", "index.js", "main.js", "bot.js", "app.js"]
    files = {p.name: p for p in root.rglob("*") if p.is_file()}
    for item in priority:
        if item in files:
            return ("python" if item.endswith(".py") else "node"), str(files[item].relative_to(root))
    for p in sorted(files.values()):
        if p.suffix == ".py":
            return "python", str(p.relative_to(root))
        if p.suffix == ".js":
            return "node", str(p.relative_to(root))
    raise ValueError("No Python or JavaScript entrypoint found")


def docker_available() -> bool:
    return shutil.which("docker") is not None


def container_command(kind: str, entry: str) -> list[str]:
    if kind == "python":
        return ["python", "-u", entry]
    return ["node", entry]


class Runner:
    def __init__(self, application: Application):
        self.application = application
        self.tasks: dict[str, asyncio.Task] = {}
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
        self.processes: dict[str, asyncio.subprocess.Process] = {}

    async def start(self, project: sqlite3.Row, user_id: int, chat_id: int, mode: str = "once") -> str:
        job_id = uuid.uuid4().hex[:12]
        log_path = LOG_DIR / f"{job_id}.log"
        command = " ".join(container_command(project["kind"], project["entrypoint"]))
        with db() as conn:
            conn.execute("INSERT INTO jobs(id,project_id,user_id,mode,status,command,log_path,created_at) VALUES(?,?,?,?,?,?,?,?)", (job_id, project["id"], user_id, mode, "queued", command, str(log_path), now()))
            conn.execute("UPDATE projects SET status=? WHERE id=?", ("queued", project["id"]))
        task = asyncio.create_task(self._run(job_id, project, user_id, chat_id, mode, log_path))
        self.tasks[job_id] = task
        return job_id

    async def _run(self, job_id: str, project: sqlite3.Row, user_id: int, chat_id: int, mode: str, log_path: Path) -> None:
        async with self.semaphore:
            container = f"tg-runner-{job_id}"
            command = container_command(project["kind"], project["entrypoint"])
            image = DOCKER_IMAGE_PYTHON if project["kind"] == "python" else DOCKER_IMAGE_NODE
            project_path = Path(project["path"]).resolve()
            env = {"PYTHONUNBUFFERED": "1", "NODE_ENV": "production"}
            with db() as conn:
                rows = conn.execute("SELECT name,value FROM env_vars WHERE project_id=?", (project["id"],)).fetchall()
                env.update({row["name"]: row["value"] for row in rows})
                conn.execute("UPDATE jobs SET status='running',container=?,started_at=? WHERE id=?", (container, container, job_id))
                conn.execute("UPDATE projects SET status='running',container=?,updated_at=? WHERE id=?", (container, now(), project["id"]))
            docker = ["docker", "run", "--rm", "--name", container, "--init", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit", "128", "--memory", "512m", "--cpus", "1.0", "--network", RUNNER_NETWORK, "-v", f"{project_path}:/workspace:rw", "-w", "/workspace"]
            for key, value in env.items():
                docker.extend(["-e", f"{key}={value}"])
            docker.extend([image] + command)
            output = bytearray()
            result_code = 1
            try:
                log_path.write_text(f"$ {' '.join(docker)}\n", encoding="utf-8")
                process = await asyncio.create_subprocess_exec(*docker, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                self.processes[job_id] = process
                async def collect() -> None:
                    nonlocal output
                    assert process.stdout
                    async for line in process.stdout:
                        if len(output) < MAX_OUTPUT_BYTES:
                            output.extend(line[: MAX_OUTPUT_BYTES - len(output)])
                            with log_path.open("ab") as stream:
                                stream.write(line)
                await asyncio.wait_for(collect(), timeout=MAX_RUNTIME_SECONDS)
                result_code = await process.wait()
            except asyncio.TimeoutError:
                log.warning("job %s timed out", job_id)
                await self.stop(job_id)
                output.extend(b"\n[Runner] Maximum runtime exceeded.\n")
                result_code = 124
            except Exception as exc:
                output.extend(f"\n[Runner] {exc}\n".encode())
            finally:
                self.processes.pop(job_id, None)
                status = "completed" if result_code == 0 else "failed"
                with db() as conn:
                    conn.execute("UPDATE jobs SET status=?,exit_code=?,finished_at=? WHERE id=?", (status, result_code, now(), job_id))
                    conn.execute("UPDATE projects SET status=?,container=NULL,updated_at=? WHERE id=?", ("running" if mode == "service" and result_code == 0 else "stopped", now(), project["id"]))
                message = f"Job <code>{esc(job_id)}</code> {status}. Exit code: <code>{result_code}</code>\n\n<pre>{esc(output.decode(errors='replace')[-3500:])}</pre>"
                try:
                    await self.application.bot.send_message(chat_id, message, parse_mode=ParseMode.HTML)
                except Exception:
                    log.exception("failed to send job result")

    async def stop(self, job_id: str) -> bool:
        process = self.processes.get(job_id)
        if not process:
            with db() as conn:
                row = conn.execute("SELECT container FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or not row["container"]:
                return False
            subprocess.run(["docker", "rm", "-f", row["container"]], capture_output=True, timeout=20)
            return True
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
        return True


runner: Runner


def menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Projects", callback_data="projects"), InlineKeyboardButton("System", callback_data="system")], [InlineKeyboardButton("Help", callback_data="help")]])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    assert user
    user_row(user.id, update)
    if not approved(user.id):
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Request access", callback_data="request_access")]])
        await update.message.reply_text(f"Access is restricted.\n\nUser ID: <code>{user.id}</code>", parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return
    await update.message.reply_text("Telegram VPS Runner\n\nSend a .py, .js, or .zip file to create a project.\nUse /help for commands.", reply_markup=menu())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not guard(update):
        return
    text = ("Commands\n\n"
            "/projects - list projects\n/run NAME - run once\n/service NAME - run as background service\n/stop JOB_ID - stop a job\n/logs JOB_ID - view logs\n/delete NAME - delete a project\n/env NAME KEY VALUE - set an environment variable\n/schedule NAME MINUTES - schedule repeated runs\n/unschedule NAME - remove schedule\n/myinfo - account and quota\n/monitor - VPS status\n\nAdmin: /addus ID, /removeus ID, /listus, /approve ID")
    await update.message.reply_text(text)


async def projects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not guard(update):
        return
    with db() as conn:
        rows = conn.execute("SELECT name,kind,status,created_at FROM projects WHERE user_id=? ORDER BY created_at DESC", (update.effective_user.id,)).fetchall()
    if not rows:
        target = update.message or update.callback_query.message
        await target.reply_text("No projects found.")
        return
    lines = ["Projects", ""]
    for row in rows:
        lines.append(f"{row['name']} | {row['kind']} | {row['status']} | {row['created_at'][:10]}")
    target = update.message or update.callback_query.message
    await target.reply_text("\n".join(lines))


async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str = "once") -> None:
    if not guard(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /run PROJECT_NAME")
        return
    project = project_for(update.effective_user.id, context.args[0])
    if not project:
        await update.message.reply_text("Project not found.")
        return
    job_id = await runner.start(project, update.effective_user.id, update.effective_chat.id, mode)
    await update.message.reply_text(f"Job queued: {job_id}")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not guard(update) or not context.args:
        return
    job_id = context.args[0]
    with db() as conn:
        row = conn.execute("SELECT id FROM jobs WHERE id=? AND user_id=?", (job_id, update.effective_user.id)).fetchone()
    if not row and not is_admin(update.effective_user.id):
        await update.message.reply_text("Job not found.")
        return
    await update.message.reply_text("Stop requested." if await runner.stop(job_id) else "Job is not running.")


async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not guard(update) or not context.args:
        return
    with db() as conn:
        row = conn.execute("SELECT log_path FROM jobs WHERE id=? AND user_id=?", (context.args[0], update.effective_user.id)).fetchone()
    if not row:
        await update.message.reply_text("Job not found.")
        return
    path = Path(row["log_path"])
    text = path.read_text(encoding="utf-8", errors="replace")[-3800:] if path.exists() else "No logs yet."
    await update.message.reply_text(f"<pre>{esc(text)}</pre>", parse_mode=ParseMode.HTML)


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not guard(update) or not context.args:
        return
    project = project_for(update.effective_user.id, context.args[0])
    if not project:
        await update.message.reply_text("Project not found.")
        return
    with db() as conn:
        conn.execute("DELETE FROM env_vars WHERE project_id=?", (project["id"],))
        conn.execute("DELETE FROM schedules WHERE project_id=?", (project["id"],))
        conn.execute("DELETE FROM jobs WHERE project_id=?", (project["id"],))
        conn.execute("DELETE FROM projects WHERE id=?", (project["id"],))
    shutil.rmtree(project["path"], ignore_errors=True)
    await update.message.reply_text("Project deleted.")


async def env_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not guard(update) or len(context.args) < 3:
        await update.message.reply_text("Usage: /env PROJECT KEY VALUE")
        return
    project = project_for(update.effective_user.id, context.args[0])
    key = context.args[1]
    if not project or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", key):
        await update.message.reply_text("Invalid project or environment key.")
        return
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO env_vars(project_id,name,value) VALUES(?,?,?)", (project["id"], key, " ".join(context.args[2:])))
    await update.message.reply_text("Environment variable saved.")


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not guard(update) or len(context.args) < 2:
        await update.message.reply_text("Usage: /schedule PROJECT_NAME MINUTES")
        return
    project = project_for(update.effective_user.id, context.args[0])
    try:
        minutes = int(context.args[1])
    except ValueError:
        minutes = 0
    if not project or minutes < 1 or minutes > 10080:
        await update.message.reply_text("Project not found or interval must be 1–10080 minutes.")
        return
    with db() as conn:
        conn.execute("DELETE FROM schedules WHERE project_id=?", (project["id"],))
        conn.execute("INSERT INTO schedules(id,project_id,user_id,interval_seconds,next_run,created_at) VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex[:12], project["id"], update.effective_user.id, minutes * 60, time.time() + minutes * 60, now()))
    await update.message.reply_text(f"Schedule enabled for {project['name']} every {minutes} minutes.")


async def unschedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not guard(update) or not context.args:
        await update.message.reply_text("Usage: /unschedule PROJECT_NAME")
        return
    project = project_for(update.effective_user.id, context.args[0])
    if not project:
        await update.message.reply_text("Project not found.")
        return
    with db() as conn:
        conn.execute("DELETE FROM schedules WHERE project_id=?", (project["id"],))
    await update.message.reply_text("Schedule removed.")


async def monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not guard(update):
        return
    disk = shutil.disk_usage(BASE_DIR)
    running = len(runner.processes)
    await update.message.reply_text(f"Runner status\n\nRunning jobs: {running}\nDisk used: {disk.used / 1024**3:.2f} GB / {disk.total / 1024**3:.2f} GB\nDocker: {'available' if docker_available() else 'missing'}")


async def myinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not guard(update):
        return
    row = user_row(update.effective_user.id)
    await update.message.reply_text(f"User ID: {row['user_id']}\nApproved: yes\nStorage: {row['used_mb']:.2f} MB / {row['quota_mb']} MB")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not guard(update):
        return
    document = update.message.document
    name = document.file_name or "upload"
    lower = name.lower()
    if not lower.endswith((".py", ".js", ".zip")):
        await update.message.reply_text("Only .py, .js, and .zip files are supported.")
        return
    if not document.file_size or document.file_size > MAX_FILE_MB * 1024 * 1024:
        await update.message.reply_text(f"File is too large. Maximum: {MAX_FILE_MB} MB.")
        return
    context.user_data["pending_upload"] = {"file_id": document.file_id, "name": name, "size": document.file_size}
    await update.message.reply_text("Send a project name using /name PROJECT_NAME")


async def name_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not guard(update):
        return
    pending = context.user_data.pop("pending_upload", None)
    if not pending or not context.args:
        await update.message.reply_text("Upload a file first, then use /name PROJECT_NAME")
        return
    project_name = safe_name(context.args[0])
    if project_for(update.effective_user.id, project_name):
        await update.message.reply_text("That project name already exists.")
        return
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM projects WHERE user_id=?", (update.effective_user.id,)).fetchone()["n"]
        user = user_row(update.effective_user.id)
        if count >= MAX_PROJECTS:
            await update.message.reply_text(f"Project limit reached: {MAX_PROJECTS}")
            return
        if user["used_mb"] + pending["size"] / 1024**2 > user["quota_mb"]:
            await update.message.reply_text("Storage quota exceeded.")
            return
    project_id = uuid.uuid4().hex[:16]
    root = PROJECT_DIR / str(update.effective_user.id) / project_id
    root.mkdir(parents=True, exist_ok=True)
    temp = root / "upload.bin"
    try:
        tg_file = await context.bot.get_file(pending["file_id"])
        await tg_file.download_to_drive(temp)
        if pending["name"].lower().endswith(".zip"):
            safe_zip_extract(temp, root)
            temp.unlink(missing_ok=True)
        else:
            destination = root / Path(pending["name"]).name
            temp.rename(destination)
        kind, entry = detect_project(root, pending["name"].lower())
        with db() as conn:
            conn.execute("INSERT INTO projects(id,user_id,name,path,kind,entrypoint,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (project_id, update.effective_user.id, project_name, str(root), kind, entry, now(), now()))
            conn.execute("UPDATE users SET used_mb=used_mb+? WHERE user_id=?", (pending["size"] / 1024**2, update.effective_user.id))
        audit(update.effective_user.id, "project_created", project_name)
        await update.message.reply_text(f"Project created: {project_name}\nType: {kind}\nEntrypoint: {entry}\nRun: /run {project_name}")
    except Exception as exc:
        shutil.rmtree(root, ignore_errors=True)
        await update.message.reply_text(f"Upload rejected: {esc(exc)}")


async def request_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = query.from_user
    user_row(user.id)
    with db() as conn:
        conn.execute("UPDATE users SET name=?,username=? WHERE user_id=?", (user.full_name, user.username or "", user.id))
    for admin_id in ADMIN_IDS:
        await context.bot.send_message(admin_id, f"Access request\nUser: {user.full_name}\nID: {user.id}\n\nUse /approve {user.id} or /removeus {user.id}")
    await query.edit_message_text("Access request sent to the administrator.")


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id) or not context.args:
        return
    target = int(context.args[0])
    user_row(target)
    with db() as conn:
        conn.execute("UPDATE users SET approved=1 WHERE user_id=?", (target,))
    await update.message.reply_text(f"Approved: {target}")


async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await approve(update, context)


async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id) or not context.args:
        return
    target = int(context.args[0])
    if target in ADMIN_IDS:
        await update.message.reply_text("Administrators cannot be removed.")
        return
    with db() as conn:
        conn.execute("UPDATE users SET approved=0 WHERE user_id=?", (target,))
    await update.message.reply_text(f"Access removed: {target}")


async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    with db() as conn:
        rows = conn.execute("SELECT user_id,name,approved,used_mb,quota_mb FROM users ORDER BY created_at DESC").fetchall()
    text = "Users\n\n" + "\n".join(f"{r['user_id']} | {r['name']} | {'approved' if r['approved'] else 'pending'} | {r['used_mb']:.1f}/{r['quota_mb']} MB" for r in rows)
    await update.message.reply_text(text[:4000] or "No users.")


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data == "request_access":
        await request_access(update, context)
    elif query.data == "projects":
        await projects(update, context)
    elif query.data == "help":
        await help_command(update, context)
    elif query.data == "system":
        await monitor(update, context)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("telegram error", exc_info=context.error)


async def scheduler_loop(application: Application) -> None:
    while True:
        await asyncio.sleep(30)
        with db() as conn:
            rows = conn.execute("SELECT s.id AS schedule_id,s.interval_seconds,s.next_run,p.* FROM schedules s JOIN projects p ON p.id=s.project_id WHERE s.enabled=1 AND s.next_run<=?", (time.time(),)).fetchall()
            for row in rows:
                await runner.start(row, row["user_id"], row["user_id"], "scheduled")
                conn.execute("UPDATE schedules SET next_run=? WHERE id=?", (time.time() + row["interval_seconds"], row["schedule_id"]))


async def post_init(application: Application) -> None:
    global runner
    runner = Runner(application)
    application.create_task(scheduler_loop(application))


def main() -> None:
    init_db()
    if not docker_available():
        log.warning("Docker is not installed or not in PATH")
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("projects", projects))
    application.add_handler(CommandHandler("run", run_command))
    application.add_handler(CommandHandler("service", lambda u, c: run_command(u, c, "service")))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("logs", logs_command))
    application.add_handler(CommandHandler("delete", delete_command))
    application.add_handler(CommandHandler("env", env_command))
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CommandHandler("unschedule", unschedule_command))
    application.add_handler(CommandHandler("name", name_command))
    application.add_handler(CommandHandler("monitor", monitor))
    application.add_handler(CommandHandler("myinfo", myinfo))
    application.add_handler(CommandHandler("approve", approve))
    application.add_handler(CommandHandler("addus", add_user))
    application.add_handler(CommandHandler("removeus", remove_user))
    application.add_handler(CommandHandler("listus", list_users))
    application.add_handler(CallbackQueryHandler(button))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_error_handler(error_handler)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
