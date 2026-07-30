"""Carrot AI — FastAPI application.

Consolidated API server exposing chat (streaming + non-streaming), bootstrap,
search, computer-use, terminal, recap, goals, reminders, notes, config,
leaderboard, and speech endpoints.
"""
from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import os
import json

from carrot.database import get_db, init_db
from carrot import (
    config,
    search,
    conversation as conv_mod,
    ollama_client as ollama_mod,
    computer_use as cpu_mod,
    terminal as term_mod,
    recap as recap_mod,
    goals as goals_mod,
    reminders as rem_mod,
    notes as notes_mod,
    leaderboard as lb_mod,
    bootstrap as bootstrap_mod,
    deep_research as dr_mod,
    skills as skills_mod,
    mcp_client as mcp_mod,
    widgets as widgets_mod,
    health as health_mod,
    github_oauth as gh_mod,
    files_api,
)
from carrot.speech import whisper_stt, kokoro_tts
from carrot.recap import DUCKDUCKGO_QUERY

app = FastAPI(title="Carrot AI", version="0.2.0")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

app.mount("/css", StaticFiles(directory=os.path.join(WEB_DIR, "css")), name="css")
app.mount("/js", StaticFiles(directory=os.path.join(WEB_DIR, "js")), name="js")
ASSETS_DIR = os.path.join(WEB_DIR, "assets")
os.makedirs(ASSETS_DIR, exist_ok=True)
app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")
VENDOR_DIR = os.path.join(WEB_DIR, "vendor")
os.makedirs(VENDOR_DIR, exist_ok=True)
app.mount("/vendor", StaticFiles(directory=VENDOR_DIR), name="vendor")

app.include_router(files_api.router)


# ===== Pydantic request models =====

class SearchQuery(BaseModel):
    query: str
    limit: Optional[int] = 20
    hybrid_weight: Optional[float] = 0.5


class ClassifyQueryRequest(BaseModel):
    query: str


class RecapRequest(BaseModel):
    include_web_search: Optional[bool] = True
    model: Optional[str] = None


class VlmScanRequest(BaseModel):
    use_vlm: Optional[bool] = True
    scan_dirs: Optional[list] = None


class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None
    model: Optional[str] = None
    stream: Optional[bool] = False
    skill: Optional[str] = None


class AddMessageRequest(BaseModel):
    role: str
    content: str


class CreateConversationRequest(BaseModel):
    title: str = ""
    metadata: Dict[str, Any] = {}


class ConversationUpdateRequest(BaseModel):
    folder_id: Optional[str] = None
    starred: Optional[bool] = None
    title: Optional[str] = None


class FolderRequest(BaseModel):
    name: str


class CommandRequest(BaseModel):
    command: str
    cwd: Optional[str] = None
    timeout: Optional[int] = 30


class GoalRequest(BaseModel):
    title: str
    description: str = ""
    category: str = ""
    metadata: Dict[str, Any] = {}


class DataPointRequest(BaseModel):
    value: Any
    label: str = ""
    metadata: Dict[str, Any] = {}


class ReminderRequest(BaseModel):
    title: str
    description: str = ""
    due_at: Optional[str] = None
    metadata: Dict[str, Any] = {}


class ReminderCompleteRequest(BaseModel):
    completed: Optional[bool] = True


class NoteRequest(BaseModel):
    title: str
    content: str = ""
    folder: str = ""


class NoteUpdateRequest(BaseModel):
    content: str
    title: Optional[str] = None


class SpeechTranscribeRequest(BaseModel):
    audio_base64: str


class SpeechSpeakRequest(BaseModel):
    text: str
    voice: Optional[str] = None


class ModelPullRequest(BaseModel):
    model: str


class ModelSelectRequest(BaseModel):
    model: str


class SkillRequest(BaseModel):
    name: str
    description: str = ""
    instructions: str = ""
    slug: Optional[str] = None


class McpServerRequest(BaseModel):
    name: str
    command: str
    args: List[str] = []
    enabled: bool = True


class McpEnableRequest(BaseModel):
    enabled: bool = True


# ===== Startup =====

@app.on_event("startup")
def startup():
    init_db()
    os.makedirs(DB_DIR, exist_ok=True)
    dr_mod.start_scheduler()


# ===== Index / static =====

@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = os.path.join(WEB_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return HTMLResponse("<h1>Carrot</h1><p>Frontend not found. Run from project root.</p>")


# ===== Health / status =====

@app.get("/api/health")
async def health():
    return {"status": "healthy"}


@app.get("/api/status")
async def api_status():
    ollama = ollama_mod.OllamaClient()
    available = ollama.is_available()
    conn = get_db()
    conv_count = conn.execute("SELECT COUNT(*) as c FROM conversations").fetchone()["c"]
    msg_count = conn.execute("SELECT COUNT(*) as c FROM messages").fetchone()["c"]
    goal_count = conn.execute("SELECT COUNT(*) as c FROM goals").fetchone()["c"]
    rem_count = conn.execute("SELECT COUNT(*) as c FROM reminders").fetchone()["c"]
    conn.close()

    default_model = config.get_config().get("ollama_model", bootstrap_mod.DEFAULT_MODEL)
    model_loaded = False
    if available:
        model_loaded = bootstrap_mod.is_model_available(default_model)

    return {
        "status": "ok",
        "ollama_available": available,
        "default_model": default_model,
        "model_loaded": model_loaded,
        "bootstrap_complete": bootstrap_mod.bootstrap_status().get("bootstrap_complete", False),
        "conversations": conv_count,
        "messages": msg_count,
        "goals": goal_count,
        "reminders": rem_count,
    }


# ===== Bootstrap =====

@app.get("/api/bootstrap/status")
async def bootstrap_status():
    return bootstrap_mod.bootstrap_status()


@app.post("/api/bootstrap/run")
async def bootstrap_run():
    try:
        return bootstrap_mod.run_bootstrap()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Bootstrap failed: {e}")


# ===== Models =====

# Curated catalog shown in the model picker for one-click install.
SUGGESTED_MODELS = [
    {"name": "gemma4:e4b", "label": "Gemma 4 E4B", "size_hint": "~4 GB", "blurb": "Default all-rounder"},
    {"name": "llama3.2:3b", "label": "Llama 3.2 3B", "size_hint": "~2 GB", "blurb": "Fast, light"},
    {"name": "qwen2.5-coder:7b", "label": "Qwen 2.5 Coder 7B", "size_hint": "~4.7 GB", "blurb": "Code-focused"},
    {"name": "mistral:7b", "label": "Mistral 7B", "size_hint": "~4.1 GB", "blurb": "General purpose"},
    {"name": "phi4:14b", "label": "Phi 4 14B", "size_hint": "~9.1 GB", "blurb": "Strong reasoning"},
    {"name": "deepseek-r1:8b", "label": "DeepSeek R1 8B", "size_hint": "~4.9 GB", "blurb": "Reasoning"},
]


@app.get("/api/models")
async def list_models():
    client = ollama_mod.OllamaClient()
    installed = client.list_models() if client.is_available() else []
    installed_names = {m["name"] for m in installed}
    active = config.get_config().get("ollama_model", bootstrap_mod.DEFAULT_MODEL)
    suggested = [
        {**m, "installed": m["name"] in installed_names}
        for m in SUGGESTED_MODELS
    ]
    return {
        "installed": installed,
        "active_model": active,
        "default_model": bootstrap_mod.DEFAULT_MODEL,
        "suggested": suggested,
    }


@app.post("/api/models/select")
async def select_model(req: ModelSelectRequest):
    config.set_config("ollama_model", req.model)
    return {"active_model": req.model}


@app.post("/api/models/pull")
async def pull_model(req: ModelPullRequest):
    """Pull a model from the Ollama registry, streaming progress as SSE."""
    client = ollama_mod.OllamaClient()
    if not client.is_available():
        raise HTTPException(status_code=503, detail="Ollama is not available")

    def event_stream():
        try:
            for update in client.pull_model(req.model):
                payload = {
                    "status": update.get("status", ""),
                    "completed": update.get("completed"),
                    "total": update.get("total"),
                }
                if update.get("error"):
                    payload["error"] = update["error"]
                yield f"data: {json.dumps(payload)}\n\n"
            yield f"data: {json.dumps({'done': True, 'model': req.model})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ===== Chat =====

def _prepare_history(conv, message, skill_slug):
    """Build the model message list: optional skill system prompt + recent turns."""
    history = []
    skill = None
    if skill_slug:
        skill = skills_mod.get_skill(skill_slug)
        if skill and skill.get("instructions"):
            history.append({
                "role": "system",
                "content": (
                    f"The user invoked the '{skill['name']}' skill. "
                    f"Follow these instructions:\n\n{skill['instructions']}"
                ),
            })
    history += [
        {"role": m["role"], "content": m["content"]}
        for m in conv["messages"][-20:]
    ]
    history.append({"role": "user", "content": message})
    return history, skill


MAX_TOOL_ROUNDS = 5


def _agentic_chat_events(client, history, model, skill=None):
    """Yield SSE dicts for a chat turn, running the MCP tool-calling loop.

    When enabled MCP tools exist and the model emits tool_calls, each call is
    surfaced as a `tool` event, executed via MCP, and fed back to the model
    (up to MAX_TOOL_ROUNDS) before the final answer streams as `chunk` events.
    Returns the assembled assistant text via the final {'done': ...} dict.
    """
    if skill:
        yield {"skill": {"slug": skill["slug"], "name": skill["name"]}}
    try:
        tools = mcp_mod.ollama_tools()
    except Exception:
        tools = []
    working = list(history)
    final_text = []
    for _ in range(MAX_TOOL_ROUNDS):
        content_parts = []
        tool_calls = []
        for ev in client.chat_stream_events(working, model=model, tools=tools or None):
            if ev["type"] == "thinking":
                yield {"thinking": ev["text"]}
            elif ev["type"] == "tool_calls":
                tool_calls.extend(ev["calls"])
            else:
                content_parts.append(ev["text"])
                yield {"chunk": ev["text"]}
        content_str = "".join(content_parts)
        if content_str:
            final_text.append(content_str)
        if not tool_calls:
            break
        working.append({"role": "assistant", "content": content_str, "tool_calls": tool_calls})
        for call in tool_calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            yield {"tool": {"name": name, "args": args}}
            try:
                result = mcp_mod.call_namespaced_tool(name, args)
            except Exception as e:
                result = f"error: {e}"
            yield {"tool_result": {"name": name, "result": result[:2000]}}
            working.append({"role": "tool", "content": result, "name": name})
    yield {"_final_text": "".join(final_text)}


@app.post("/api/chat")
async def chat(req: ChatRequest):
    client = ollama_mod.OllamaClient()
    if not client.is_available():
        raise HTTPException(status_code=503, detail="Ollama is not available")

    if req.conversation_id is None:
        conv = conv_mod.create_conversation(title=req.message[:80])
        req.conversation_id = conv["id"]

    conv = conv_mod.get_conversation(req.conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    history, skill = _prepare_history(conv, req.message, req.skill)
    conv_mod.add_message(req.conversation_id, "user", req.message)

    model = req.model or config.get_config().get("ollama_model", bootstrap_mod.DEFAULT_MODEL)

    if req.stream:
        async def stream():
            final_text = ""
            for ev in _agentic_chat_events(client, history, model, skill):
                if "_final_text" in ev:
                    final_text = ev["_final_text"]
                    continue
                yield f"data: {json.dumps(ev)}\n\n"
            conv_mod.add_message(req.conversation_id, "assistant", final_text)
            yield f"data: {json.dumps({'done': True, 'conversation_id': req.conversation_id})}\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream")

    response = client.chat(history, model=model)
    conv_mod.add_message(req.conversation_id, "assistant", response)
    return {
        "conversation_id": req.conversation_id,
        "response": response,
    }


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    """Dedicated SSE streaming endpoint."""
    client = ollama_mod.OllamaClient()
    if not client.is_available():
        raise HTTPException(status_code=503, detail="Ollama is not available")

    if req.conversation_id is None:
        conv = conv_mod.create_conversation(title=req.message[:80])
        req.conversation_id = conv["id"]

    conv = conv_mod.get_conversation(req.conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    history, skill = _prepare_history(conv, req.message, req.skill)
    conv_mod.add_message(req.conversation_id, "user", req.message)

    model = req.model or config.get_config().get("ollama_model", bootstrap_mod.DEFAULT_MODEL)

    async def stream():
        final_text = ""
        for ev in _agentic_chat_events(client, history, model, skill):
            if "_final_text" in ev:
                final_text = ev["_final_text"]
                continue
            yield f"data: {json.dumps(ev)}\n\n"
        conv_mod.add_message(req.conversation_id, "assistant", final_text)
        yield f"data: {json.dumps({'done': True, 'conversation_id': req.conversation_id})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# ===== Conversations =====

@app.get("/api/conversations")
async def list_conversations(limit: int = 50):
    return conv_mod.list_conversations(limit=limit)


@app.get("/api/conversations/{conv_id}")
async def get_conversation(conv_id: str):
    conv = conv_mod.get_conversation(conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


@app.post("/api/conversations")
async def create_conversation(req: CreateConversationRequest = CreateConversationRequest()):
    return conv_mod.create_conversation(title=req.title, metadata=req.metadata)


@app.post("/api/conversations/{conv_id}/messages")
async def add_message(conv_id: str, req: AddMessageRequest):
    try:
        msg = conv_mod.add_message(conv_id, req.role, req.content)
        return msg
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.patch("/api/conversations/{conv_id}")
async def update_conversation(conv_id: str, req: ConversationUpdateRequest):
    result = conv_mod.update_conversation_meta(
        conv_id, folder_id=req.folder_id, starred=req.starred, title=req.title
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return result


@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    if not conv_mod.delete_conversation(conv_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"status": "deleted"}


# ===== Chat folders =====

@app.get("/api/chat-folders")
async def list_chat_folders():
    return conv_mod.list_folders()


@app.post("/api/chat-folders")
async def create_chat_folder(req: FolderRequest):
    return conv_mod.create_folder(req.name.strip() or "Untitled")


@app.put("/api/chat-folders/{folder_id}")
async def rename_chat_folder(folder_id: str, req: FolderRequest):
    if not conv_mod.rename_folder(folder_id, req.name.strip() or "Untitled"):
        raise HTTPException(status_code=404, detail="Folder not found")
    return {"id": folder_id, "name": req.name.strip()}


@app.delete("/api/chat-folders/{folder_id}")
async def delete_chat_folder(folder_id: str):
    conv_mod.delete_folder(folder_id)
    return {"status": "deleted"}


# ===== Search =====

@app.get("/api/search")
async def search_get(q: str, limit: int = 20, hybrid_weight: float = 0.5):
    return search.search_conversations(q, limit=limit, hybrid_weight=hybrid_weight)


@app.post("/api/search")
async def search_post(req: SearchQuery):
    return search.search_conversations(req.query, limit=req.limit, hybrid_weight=req.hybrid_weight)


@app.post("/api/search/classify")
async def classify_query(req: ClassifyQueryRequest):
    return search.classify_query(req.query)


# ===== Computer use =====

@app.get("/api/assignments")
async def get_assignments():
    results = cpu_mod.find_assignments()
    return {"count": len(results), "assignments": results}


@app.get("/api/computer_use/scan")
async def scan_computer():
    count = cpu_mod.index_computer_use()
    return {"indexed": count}


@app.post("/api/computer_use/vlm_scan")
async def vlm_scan(req: VlmScanRequest = VlmScanRequest()):
    count = cpu_mod.computer_use_scan(scan_dirs=req.scan_dirs, use_vlm=req.use_vlm)
    return {"indexed": count, "use_vlm": req.use_vlm}


@app.post("/api/computer_use/read")
async def read_file(req: dict):
    path = req.get("path", "")
    max_chars = req.get("max_chars", 5000)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found")
    return cpu_mod.read_file(path, max_chars=max_chars)


@app.post("/api/computer_use/analyze_html")
async def analyze_html(req: dict):
    path = req.get("path", "")
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found")
    return cpu_mod.analyze_html_with_vlm(path)


@app.post("/api/computer_use/screenshot")
async def take_screenshot(req: dict = {}):
    window_title = req.get("window_title")
    return cpu_mod.take_screenshot(window_title=window_title)


@app.post("/api/computer_use/analyze_screenshot")
async def analyze_screenshot(req: dict):
    image_path = req.get("path", "")
    if not image_path or not os.path.exists(image_path):
        raise HTTPException(status_code=404, detail="Image not found")
    return cpu_mod.analyze_screenshot_with_vlm(image_path)


# ===== Terminal =====

@app.post("/api/terminal/execute")
async def execute_command(req: CommandRequest):
    return term_mod.execute_command(req.command, cwd=req.cwd, timeout=req.timeout)


@app.get("/api/terminal/history")
async def terminal_history():
    conn = get_db()
    rows = conn.execute(
        "SELECT value FROM config WHERE key LIKE 'terminal_cmd:%' ORDER BY rowid DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return [json.loads(r["value"]) for r in rows]


# ===== Recap =====

@app.get("/api/recap")
async def get_recaps(limit: int = 7):
    return recap_mod.get_recaps(limit=limit)


@app.post("/api/recap/run")
async def run_recap(req: RecapRequest = RecapRequest()):
    return recap_mod.run_recap(include_web_search=req.include_web_search, model=req.model)


@app.post("/api/recap/run/stream")
async def run_recap_stream(req: RecapRequest = RecapRequest()):
    """Run the deep-research pipeline, streaming every step (analysis, searches,
    page reads, model thinking, summary tokens) as SSE."""
    def event_stream():
        try:
            for ev in dr_mod.run_deep_research_stream(
                model=req.model, include_web_search=req.include_web_search
            ):
                yield f"data: {json.dumps(ev)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/recap/briefing/today")
async def briefing_today():
    briefing = dr_mod.get_today_briefing()
    if not briefing:
        return {"available": False}
    return {"available": True, **briefing}


@app.get("/api/recap/web-search")
async def run_web_search(query: str = DUCKDUCKGO_QUERY, max_results: int = 5):
    articles = recap_mod.fetch_live_tech_articles(max_results=max_results)
    return {"count": len(articles), "articles": articles}


# ===== Goals =====

@app.get("/api/goals")
async def list_goals(category: str = None):
    return goals_mod.list_goals(category=category)


@app.post("/api/goals")
async def create_goal(req: GoalRequest):
    return goals_mod.create_goal(req.title, req.description, req.category, req.metadata)


@app.post("/api/goals/{goal_id}/data")
async def add_goal_data(goal_id: str, req: DataPointRequest):
    return goals_mod.add_data_point(goal_id, req.value, req.label, req.metadata)


@app.get("/api/goals/{goal_id}/history")
async def goal_history(goal_id: str, start: str = None, end: str = None):
    return goals_mod.get_goal_history(goal_id, start=start, end=end)


# ===== Reminders =====

@app.get("/api/reminders")
async def list_reminders(completed: str = None):
    c = None
    if completed == "true":
        c = True
    elif completed == "false":
        c = False
    return rem_mod.list_reminders(completed=c)


@app.get("/api/reminders/overdue")
async def overdue_reminders():
    return rem_mod.get_overdue_reminders()


@app.get("/api/reminders/today")
async def today_reminders():
    return rem_mod.get_reminders_today()


@app.post("/api/reminders")
async def create_reminder(req: ReminderRequest):
    return rem_mod.create_reminder(req.title, req.description, req.due_at, req.metadata)


@app.post("/api/reminders/{reminder_id}/complete")
async def complete_reminder(reminder_id: str, req: ReminderCompleteRequest = ReminderCompleteRequest()):
    rem_mod.complete_reminder(reminder_id, completed=req.completed)
    return {"status": "completed" if req.completed else "uncompleted"}


# ===== Notes =====

@app.get("/api/notes")
async def list_notes(folder: str = None):
    return notes_mod.list_notes(folder=folder)


@app.get("/api/notes/{note_id}")
async def get_note(note_id: str):
    note = notes_mod.get_note(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


@app.post("/api/notes")
async def create_note(req: NoteRequest):
    return notes_mod.create_note(req.title, req.content, req.folder or None)


@app.put("/api/notes/{note_id}")
async def update_note(note_id: str, req: NoteUpdateRequest):
    result = notes_mod.update_note(note_id, req.content, title=req.title)
    if result is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return result


@app.delete("/api/notes/{note_id}")
async def delete_note(note_id: str):
    ok = notes_mod.delete_note(note_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Note not found")
    return {"status": "deleted"}


# ===== Config =====

@app.get("/api/config")
async def get_app_config():
    return config.get_config()


@app.put("/api/config/{key}")
async def set_config_value(key: str, value: Any = Body(...)):
    config.set_config(key, value)
    return {"key": key, "value": value}


# ===== Widgets =====

@app.get("/api/widgets/catalog")
async def widget_catalog():
    return {"catalog": widgets_mod.list_catalog()}


@app.get("/api/widgets")
async def list_widgets(slot: str = None):
    return {"widgets": widgets_mod.list_widgets(slot=slot)}


@app.post("/api/widgets")
async def add_widget(req: dict):
    wtype = req.get("type", "")
    try:
        return widgets_mod.add_widget(wtype, slot=req.get("slot"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ----- GitHub contribution widget (device flow) -----

@app.post("/api/widgets/github/auth")
async def github_auth_start():
    try:
        return gh_mod.start_auth()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"GitHub auth start failed: {e}")


@app.get("/api/widgets/github/status")
async def github_auth_status():
    return gh_mod.auth_status()


@app.get("/api/widgets/github/grid")
async def github_grid(refresh: bool = False):
    if refresh:
        gh_mod.refresh_grid()
    grid = gh_mod.get_grid()
    if grid is None:
        return {"available": False}
    return {"available": True, **grid}


@app.delete("/api/widgets/github")
async def github_disconnect():
    gh_mod.disconnect()
    return {"status": "disconnected"}


# ----- Generic widget instance ops (defined after specific paths) -----

@app.put("/api/widgets/{widget_id}")
async def update_widget(widget_id: str, req: dict):
    cfg = req.get("config", req)
    widget = widgets_mod.update_widget(widget_id, cfg)
    if widget is None:
        raise HTTPException(status_code=404, detail="Widget not found")
    return widget


@app.delete("/api/widgets/{widget_id}")
async def remove_widget(widget_id: str):
    if not widgets_mod.remove_widget(widget_id):
        raise HTTPException(status_code=404, detail="Widget not found")
    return {"status": "deleted"}


# ===== Apple Health sync (inbound from iOS Shortcuts) =====

@app.post("/api/health/sync")
async def health_sync(req: dict):
    try:
        return health_mod.sync_metrics(req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/health/today")
async def health_today(date: str = None):
    return {"date": date, "metrics": health_mod.today_metrics(date)}


@app.get("/api/health/range")
async def health_range(days: int = 90):
    return {"days": days, "metrics": health_mod.range_metrics(days)}


# ===== Skills =====

@app.get("/api/skills")
async def get_skills():
    return skills_mod.list_skills()


@app.get("/api/skills/{slug}")
async def get_skill(slug: str):
    skill = skills_mod.get_skill(slug)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


@app.post("/api/skills")
async def save_skill(req: SkillRequest):
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Skill name is required")
    return skills_mod.save_skill(req.name, req.description, req.instructions, slug=req.slug)


@app.delete("/api/skills/{slug}")
async def delete_skill(slug: str):
    if not skills_mod.delete_skill(slug):
        raise HTTPException(status_code=404, detail="Skill not found")
    return {"status": "deleted"}


# ===== MCP servers =====

@app.get("/api/mcp/servers")
async def list_mcp_servers():
    cfg = mcp_mod.load_mcp_config()
    return {"servers": cfg.get("servers", {})}


@app.get("/api/mcp/tools")
async def list_mcp_tools():
    """Discover tools for every configured server (spawns each briefly)."""
    return mcp_mod.discover_all_tools()


@app.post("/api/mcp/servers")
async def add_mcp_server(req: McpServerRequest):
    if not req.name.strip() or not req.command.strip():
        raise HTTPException(status_code=400, detail="Server name and command are required")
    return mcp_mod.add_server(req.name, req.command, req.args, req.enabled)


@app.post("/api/mcp/servers/{name}/enable")
async def enable_mcp_server(name: str, req: McpEnableRequest = McpEnableRequest()):
    if not mcp_mod.set_server_enabled(name, req.enabled):
        raise HTTPException(status_code=404, detail="Server not found")
    return {"name": name, "enabled": req.enabled}


@app.delete("/api/mcp/servers/{name}")
async def delete_mcp_server(name: str):
    if not mcp_mod.remove_server(name):
        raise HTTPException(status_code=404, detail="Server not found")
    return {"status": "deleted"}


# ===== Leaderboard =====

@app.get("/api/leaderboard")
async def get_leaderboard(
    os_name: str = None,
    ram_gb_min: float = None,
    gpu: str = None,
    model: str = None,
    limit: int = 50,
):
    filters = {}
    if os_name:
        filters["os"] = os_name
    if ram_gb_min:
        filters["ram_gb_min"] = ram_gb_min
    if gpu:
        filters["gpu"] = gpu
    if model:
        filters["model"] = model
    return lb_mod.get_leaderboard(filters=filters, limit=limit)


@app.get("/api/leaderboard/stats")
async def get_leaderboard_stats():
    return lb_mod.get_leaderboard_stats()


@app.get("/api/leaderboard/recommendations")
async def get_model_recommendations(ram_gb: float = None, gpu: str = None):
    return lb_mod.get_model_recommendations(user_ram_gb=ram_gb, user_gpu=gpu)


@app.post("/api/leaderboard/submit")
async def submit_leaderboard(req: dict):
    anon_id = req.get("anonymous_id")
    return lb_mod.submit_leaderboard_entry(anon_id=anon_id)


@app.put("/api/leaderboard/model")
async def set_leaderboard_model(anon_id: str, model: str, task_type: str = "general"):
    lb_mod.update_leaderboard_model(anon_id, model, task_type)
    return {"status": "updated", "anonymous_id": anon_id}


@app.get("/api/leaderboard/my-profile")
async def get_my_profile(anon_id: str):
    entries = lb_mod.get_leaderboard(filters={"anon_id": anon_id}, limit=1)
    if entries:
        return entries[0]
    raise HTTPException(status_code=404, detail="Profile not found")


# ===== Speech =====

@app.post("/api/speech/transcribe")
async def speech_transcribe(req: SpeechTranscribeRequest):
    """Transcribe base64-encoded audio using whisper.cpp STT."""
    try:
        result = whisper_stt.transcribe_base64(req.audio_base64)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Transcription failed: {e}")


@app.post("/api/speech/speak")
async def speech_speak(req: SpeechSpeakRequest):
    """Synthesize speech via Kokoro TTS and return base64 audio."""
    try:
        result = kokoro_tts.synthesize_base64(req.text, voice_style=req.voice or "us_rabbit")
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Speech synthesis failed: {e}")


@app.get("/api/speech/voices")
async def speech_voices():
    """List available TTS voices."""
    return {"voices": kokoro_tts.list_voices()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8181)
