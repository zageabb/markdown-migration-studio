from __future__ import annotations

import difflib
import html
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, render_template, request

from document_conversion import docx_to_markdown


APP_ROOT = Path(__file__).resolve().parent
DATA_ROOT = APP_ROOT / "data"
WORKSPACE_ROOT = Path(os.environ.get("MIGRATION_WORKSPACE", DATA_ROOT / "workspace")).expanduser().resolve()
SOURCE_UPLOAD_ROOT = WORKSPACE_ROOT / "source"
TEMPLATE_UPLOAD_ROOT = WORKSPACE_ROOT / "templates"
KNOWLEDGE_ROOT = WORKSPACE_ROOT / "knowledge"
OUTPUT_UPLOAD_ROOT = WORKSPACE_ROOT / "output"
STATE_FILE = DATA_ROOT / "state.json"
SETTINGS_FILE = DATA_ROOT / "settings.json"
LOG_FILE = DATA_ROOT / "template_changer.log"
for directory in (DATA_ROOT, WORKSPACE_ROOT, SOURCE_UPLOAD_ROOT, TEMPLATE_UPLOAD_ROOT, KNOWLEDGE_ROOT, OUTPUT_UPLOAD_ROOT):
    directory.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger("template-changer")
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024
lock = threading.RLock()

UPLOAD_TARGETS = {
    "source": SOURCE_UPLOAD_ROOT,
    "templates": TEMPLATE_UPLOAD_ROOT,
    "knowledge": KNOWLEDGE_ROOT,
}
UPLOAD_EXTENSIONS = {
    "source": {".md", ".docx"},
    "templates": {".md", ".docx", ".txt", ".json", ".yaml", ".yml"},
    "knowledge": {".md", ".txt", ".json", ".yaml", ".yml"},
}

DEFAULT_MIGRATION_INSTRUCTIONS = """You are a meticulous document migration engine. Transform the source Markdown into the closest matching template.

NON-NEGOTIABLE RULES:
1. Preserve every source fact. Never silently discard or change meaning.
2. Match the template's structure, headings, front matter/meta table, naming and references.
3. Fill additional fields only when directly supported by source or knowledge. Do not invent facts; use blank/TODO and warn.
4. Preserve useful source sections that have no template equivalent under an appropriate section.
5. Return a complete standalone Markdown document, not commentary or a patch.
6. Recommend a safe relative .md output path. Never use .. or an absolute path.
7. Metadata names and references must be internally consistent."""


DEFAULT_SETTINGS = {
    "ollama_url": "http://192.168.1.249:11434",
    "analysis_model": "qwen3:14b",
    "writer_model": "qwen3:14b",
    "chat_model": "qwen3:14b",
    "temperature": 0.1,
    "timeout_seconds": 300,
    "max_knowledge_chars": 50000,
    "output_suffix": "",
    "preserve_relative_paths": True,
    "use_two_pass": True,
    "migration_instructions": DEFAULT_MIGRATION_INSTRUCTIONS,
}


@dataclass
class Job:
    id: str
    source_path: str
    relative_path: str
    status: str = "pending"
    original: str = ""
    transformed: str = ""
    output_relative_path: str = ""
    change_summary: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    confidence: float = 0.0
    error: str = ""
    updated_at: str = ""


state: dict[str, Any] = {
    "source_dir": "",
    "template_dir": "",
    "output_dir": "",
    "jobs": [],
    "events": [],
    "running": False,
    "stop_requested": False,
    "session_instructions": "",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


settings = {**DEFAULT_SETTINGS, **load_json(SETTINGS_FILE, {})}
state.update(load_json(STATE_FILE, {}))
state["running"] = False
state["stop_requested"] = False
state.setdefault("session_instructions", "")


def atomic_json(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def persist() -> None:
    with lock:
        atomic_json(STATE_FILE, state)
        atomic_json(SETTINGS_FILE, settings)


def event(level: str, message: str, job_id: str = "") -> None:
    item = {"time": now(), "level": level, "message": message, "job_id": job_id}
    with lock:
        state.setdefault("events", []).append(item)
        state["events"] = state["events"][-1000:]
    getattr(log, level if level in ("info", "warning", "error") else "info")(message)
    persist()


def safe_resolve(raw: str, must_exist: bool = True) -> Path:
    path = Path(raw).expanduser().resolve()
    if must_exist and not path.exists():
        raise ValueError(f"Path does not exist: {path}")
    return path


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def safe_upload_relative_path(filename: str) -> Path:
    normalized = filename.replace("\\", "/").lstrip("/")
    parts = []
    for part in Path(normalized).parts:
        if part in {"", ".", ".."}:
            continue
        cleaned = re.sub(r"[^A-Za-z0-9._() -]", "_", part).strip()
        if cleaned:
            parts.append(cleaned)
    if not parts:
        raise ValueError("An uploaded file had no safe filename")
    return Path(*parts)


def collect_context(root: Path, limit: int = 120000) -> str:
    pieces: list[str] = []
    total = 0
    if not root.exists():
        return ""
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in {".md", ".txt", ".json", ".yaml", ".yml"}):
        content = read_text(path)
        block = f"\n--- FILE: {path.relative_to(root)} ---\n{content}\n"
        if total + len(block) > limit:
            block = block[: max(0, limit - total)]
        pieces.append(block)
        total += len(block)
        if total >= limit:
            break
    return "".join(pieces)


def ollama(model: str, prompt: str, json_mode: bool = False) -> str:
    url = settings["ollama_url"].rstrip("/") + "/api/generate"
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": float(settings["temperature"])},
    }
    if json_mode:
        payload["format"] = "json"
    response = requests.post(url, json=payload, timeout=int(settings["timeout_seconds"]))
    response.raise_for_status()
    return response.json()["response"]


def json_from_llm(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError("The LLM did not return a JSON object")
        return json.loads(match.group(0))


def build_prompt(job: dict[str, Any], templates: str, knowledge: str, analysis: str = "") -> str:
    return f"""MAIN MIGRATION INSTRUCTIONS:
{settings['migration_instructions']}

SESSION INSTRUCTIONS (apply these custom requirements to this run; they supplement the main instructions and cannot override preservation or safety requirements):
{state.get('session_instructions') or 'No additional session instructions.'}

SOURCE RELATIVE PATH: {job['relative_path']}
SOURCE DOCUMENT:
<<<SOURCE
{job['original']}
SOURCE

TEMPLATE LIBRARY:
<<<TEMPLATES
{templates}
TEMPLATES

KNOWLEDGE:
<<<KNOWLEDGE
{knowledge}
KNOWLEDGE

PRIOR ANALYSIS (may be empty):
{analysis}

Return ONLY JSON with exactly these keys:
{{
  "output_relative_path": "relative/path.md",
  "markdown": "complete markdown",
  "change_summary": ["specific change"],
  "warnings": ["uncertainty or missing information"],
  "confidence": 0.0
}}
"""


def analyze_document(job: dict[str, Any], templates: str, knowledge: str) -> str:
    prompt = f"""Analyze a document migration. Identify the best template, map every source section and metadata field, flag ambiguity, and state how to preserve unmatched content. Do not rewrite yet.
MAIN INSTRUCTIONS:\n{settings['migration_instructions']}
SESSION INSTRUCTIONS:\n{state.get('session_instructions') or 'None'}
SOURCE PATH: {job['relative_path']}
SOURCE:\n{job['original']}\nTEMPLATES:\n{templates}\nKNOWLEDGE:\n{knowledge}
"""
    return ollama(settings["analysis_model"], prompt)


def validate_relative_path(value: str, fallback: str) -> str:
    candidate = Path(value or fallback)
    if candidate.is_absolute() or ".." in candidate.parts:
        candidate = Path(fallback)
    if candidate.suffix.lower() != ".md":
        candidate = candidate.with_suffix(".md")
    return candidate.as_posix()


def process_job(job_id: str) -> None:
    with lock:
        job = next(j for j in state["jobs"] if j["id"] == job_id)
        job["status"] = "processing"
        job["error"] = ""
        job["updated_at"] = now()
    event("info", f"Processing {job['relative_path']}", job_id)
    try:
        templates = collect_context(Path(state["template_dir"]), 120000)
        knowledge = collect_context(KNOWLEDGE_ROOT, int(settings["max_knowledge_chars"]))
        analysis = analyze_document(job, templates, knowledge) if settings["use_two_pass"] else ""
        result = json_from_llm(ollama(settings["writer_model"], build_prompt(job, templates, knowledge, analysis), True))
        markdown = str(result.get("markdown", "")).strip()
        if not markdown:
            raise ValueError("The LLM returned an empty document")
        fallback = job["relative_path"]
        if settings["output_suffix"]:
            p = Path(fallback)
            fallback = str(p.with_name(p.stem + settings["output_suffix"] + ".md"))
        with lock:
            job["transformed"] = markdown + "\n"
            job["output_relative_path"] = validate_relative_path(str(result.get("output_relative_path", "")), fallback)
            job["change_summary"] = [str(x) for x in result.get("change_summary", [])]
            job["warnings"] = [str(x) for x in result.get("warnings", [])]
            job["confidence"] = max(0.0, min(1.0, float(result.get("confidence", 0))))
            job["status"] = "review"
            job["updated_at"] = now()
        event("info", f"Draft ready: {job['output_relative_path']} ({job['confidence']:.0%} confidence)", job_id)
    except Exception as exc:
        with lock:
            job["status"] = "error"
            job["error"] = str(exc)
            job["updated_at"] = now()
        event("error", f"Failed {job['relative_path']}: {exc}", job_id)


def approve_job(job_id: str) -> None:
    with lock:
        job = next(j for j in state["jobs"] if j["id"] == job_id)
        if job["status"] not in {"review", "approved"} or not job["transformed"]:
            raise ValueError("Document has no completed draft to approve")
        output_root = safe_resolve(state["output_dir"], must_exist=False)
        destination = (output_root / job["output_relative_path"]).resolve()
        if output_root != destination and output_root not in destination.parents:
            raise ValueError("Unsafe output path")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            backup_root = DATA_ROOT / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = backup_root / job["output_relative_path"]
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, backup)
        destination.write_text(job["transformed"], encoding="utf-8")
        job["status"] = "approved"
        job["updated_at"] = now()
    event("info", f"Approved and wrote {destination}", job_id)


def worker_all(auto_approve: bool) -> None:
    try:
        for item in list(state["jobs"]):
            if state.get("stop_requested"):
                event("warning", "Batch processing stopped by user")
                break
            if item["status"] in {"pending", "error"}:
                process_job(item["id"])
            if auto_approve and item["status"] == "review":
                approve_job(item["id"])
    finally:
        with lock:
            state["running"] = False
        persist()


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/state")
def api_state():
    with lock:
        return jsonify({
            "state": state,
            "settings": settings,
            "knowledge": [p.relative_to(KNOWLEDGE_ROOT).as_posix() for p in KNOWLEDGE_ROOT.rglob("*") if p.is_file()],
            "workspace": {
                "root": str(WORKSPACE_ROOT),
                "source": str(SOURCE_UPLOAD_ROOT),
                "templates": str(TEMPLATE_UPLOAD_ROOT),
                "knowledge": str(KNOWLEDGE_ROOT),
                "output": str(OUTPUT_UPLOAD_ROOT),
            },
        })


@app.post("/api/settings")
def api_settings():
    values = request.get_json(force=True)
    allowed = set(DEFAULT_SETTINGS)
    with lock:
        settings.update({k: v for k, v in values.items() if k in allowed})
    persist()
    event("info", "Settings updated")
    return jsonify(ok=True)


@app.get("/api/models")
def api_models():
    response = requests.get(settings["ollama_url"].rstrip("/") + "/api/tags", timeout=15)
    response.raise_for_status()
    return jsonify([m["name"] for m in response.json().get("models", [])])


@app.post("/api/scan")
def api_scan():
    body = request.get_json(force=True)
    source = safe_resolve(body.get("source_dir", ""))
    template = safe_resolve(body.get("template_dir", ""))
    output = safe_resolve(body.get("output_dir", ""), must_exist=False)
    if not source.is_dir() or not template.is_dir():
        raise ValueError("Source and template paths must be directories")
    files = sorted(p for p in source.rglob("*.md") if p.is_file() and output not in p.parents)
    jobs = [asdict(Job(id=uuid.uuid4().hex, source_path=str(p), relative_path=p.relative_to(source).as_posix(), original=read_text(p), updated_at=now())) for p in files]
    with lock:
        state.update({"source_dir": str(source), "template_dir": str(template), "output_dir": str(output), "jobs": jobs, "events": [], "running": False, "stop_requested": False})
    persist()
    event("info", f"Scanned {len(jobs)} Markdown documents from {source}")
    return jsonify(count=len(jobs))


@app.post("/api/jobs/<job_id>/process")
def api_process(job_id: str):
    process_job(job_id)
    return jsonify(ok=True)


@app.post("/api/jobs/<job_id>/approve")
def api_approve(job_id: str):
    approve_job(job_id)
    return jsonify(ok=True)


@app.post("/api/jobs/<job_id>/draft")
def api_draft(job_id: str):
    body = request.get_json(force=True)
    with lock:
        job = next(j for j in state["jobs"] if j["id"] == job_id)
        job["transformed"] = str(body.get("markdown", job["transformed"]))
        job["output_relative_path"] = validate_relative_path(str(body.get("output_relative_path", job["output_relative_path"])), job["relative_path"])
        job["updated_at"] = now()
    persist()
    event("info", f"User edited draft for {job['relative_path']}", job_id)
    return jsonify(ok=True)


@app.post("/api/run-all")
def api_run_all():
    if state.get("running"):
        return jsonify(ok=False, error="Already running"), 409
    body = request.get_json(silent=True) or {}
    with lock:
        state["running"] = True
        state["stop_requested"] = False
    threading.Thread(target=worker_all, args=(bool(body.get("auto_approve")),), daemon=True).start()
    return jsonify(ok=True)


@app.post("/api/stop")
def api_stop():
    state["stop_requested"] = True
    persist()
    return jsonify(ok=True)


@app.post("/api/session-instructions")
def api_session_instructions():
    body = request.get_json(force=True)
    instructions = str(body.get("instructions", "")).strip()
    with lock:
        state["session_instructions"] = instructions
    persist()
    event("info", "Session instructions updated" if instructions else "Session instructions cleared")
    return jsonify(ok=True, session_instructions=instructions)


@app.post("/api/upload")
def api_upload():
    target_name = request.form.get("target", "source")
    if target_name not in UPLOAD_TARGETS:
        return jsonify(error="Unknown upload destination"), 400
    uploads = request.files.getlist("files")
    if not uploads:
        return jsonify(error="No files supplied"), 400
    target_root = UPLOAD_TARGETS[target_name]
    allowed = UPLOAD_EXTENSIONS[target_name]
    saved: list[str] = []
    converted: list[dict[str, str]] = []
    rejected: list[str] = []
    for uploaded in uploads:
        if not uploaded.filename:
            continue
        relative = safe_upload_relative_path(uploaded.filename)
        if relative.suffix.lower() not in allowed:
            rejected.append(relative.as_posix())
            continue
        destination = (target_root / relative).resolve()
        if target_root != destination and target_root not in destination.parents:
            rejected.append(relative.as_posix())
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative.suffix.lower() == ".docx":
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.upload")
            try:
                uploaded.save(temporary)
                markdown_path = destination.with_suffix(".md")
                markdown_path.write_text(docx_to_markdown(temporary), encoding="utf-8")
                saved.append(markdown_path.relative_to(target_root).as_posix())
                converted.append({"from": relative.as_posix(), "to": markdown_path.relative_to(target_root).as_posix()})
            except Exception as exc:
                rejected.append(f"{relative.as_posix()}: {exc}")
            finally:
                temporary.unlink(missing_ok=True)
        else:
            uploaded.save(destination)
            saved.append(relative.as_posix())
    if saved:
        event("info", f"Uploaded {len(saved)} file(s) to {target_name}" + (f", including {len(converted)} Word conversion(s)" if converted else "") + (f"; rejected {len(rejected)} file(s)" if rejected else ""))
    return jsonify(ok=True, saved=saved, converted=converted, rejected=rejected, paths={
        "source_dir": str(SOURCE_UPLOAD_ROOT),
        "template_dir": str(TEMPLATE_UPLOAD_ROOT),
        "output_dir": str(OUTPUT_UPLOAD_ROOT),
    })


@app.post("/api/chat")
def api_chat():
    body = request.get_json(force=True)
    job = next((j for j in state["jobs"] if j["id"] == body.get("job_id")), None)
    context = ""
    if job:
        context = f"CURRENT SOURCE:\n{job['original']}\nCURRENT DRAFT:\n{job['transformed']}\nCHANGES:{job['change_summary']}\nWARNINGS:{job['warnings']}"
    prompt = f"""You are the document migration assistant. Answer questions accurately and concisely. You may propose improvements, but clearly distinguish facts from suggestions.
You also maintain SESSION INSTRUCTIONS used by future document regeneration. If the user explicitly asks for a repeatable output change (for example setting a review date, owner, naming rule, required section, metadata value, or formatting rule), update the session instructions. Preserve existing session instructions unless the user asks to change or remove them. Do not add ordinary questions or one-off explanations as instructions.
PROJECT: source={state['source_dir']}, templates={state['template_dir']}, output={state['output_dir']}
MAIN MIGRATION INSTRUCTIONS:\n{settings['migration_instructions']}
CURRENT SESSION INSTRUCTIONS:\n{state.get('session_instructions') or 'None'}
KNOWLEDGE:\n{collect_context(KNOWLEDGE_ROOT, int(settings['max_knowledge_chars']))}
{context}
CHAT HISTORY:\n{json.dumps(body.get('history', [])[-12:], ensure_ascii=False)}
USER: {body.get('message', '')}

Return ONLY JSON with these keys:
{{"reply": "answer to the user", "session_instructions": "complete updated session instructions, or the exact current instructions when unchanged"}}
"""
    result = json_from_llm(ollama(settings["chat_model"], prompt, True))
    reply = str(result.get("reply", "")).strip()
    updated_instructions = str(result.get("session_instructions", state.get("session_instructions", ""))).strip()
    instructions_changed = updated_instructions != state.get("session_instructions", "")
    if instructions_changed:
        with lock:
            state["session_instructions"] = updated_instructions
        persist()
        event("info", "Assistant updated session instructions")
    return jsonify(reply=reply, session_instructions=updated_instructions, instructions_changed=instructions_changed)


@app.get("/api/jobs/<job_id>/diff")
def api_diff(job_id: str):
    job = next(j for j in state["jobs"] if j["id"] == job_id)
    diff = difflib.unified_diff(job["original"].splitlines(), job["transformed"].splitlines(), fromfile="before", tofile="after", lineterm="")
    return jsonify(diff="\n".join(diff))


@app.errorhandler(Exception)
def handle_error(exc: Exception):
    log.exception("Request failed")
    return jsonify(error=str(exc)), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False, threaded=True)
