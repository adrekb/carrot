"""Carrot AI — FastAPI application.

Consolidated API server exposing chat (streaming + non-streaming), bootstrap,
search, computer-use, terminal, recap, goals, reminders, notes, config,
leaderboard, and speech endpoints.
"""
from fastapi import FastAPI, HTTPException, Body, Request
from fastapi.responses import JSONResponse
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import os
import re
import json
import uuid
import contextlib
from datetime import datetime
from urllib.parse import urlparse
import queue
import logging
import threading

LOG = logging.getLogger("carrot.app")

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
    links as links_mod,
    leaderboard as lb_mod,
    bootstrap as bootstrap_mod,
    hub as hub_mod,
    calfeed as calfeed_mod,
    attachments as attach_mod,
    artifacts as artifacts_mod,
    interop as interop_mod,
    deep_research as dr_mod,
    skills as skills_mod,
    mcp_client as mcp_mod,
    widgets as widgets_mod,
    health as health_mod,
    github_oauth as gh_mod,
    files_api,
    vectors as vectors_mod,
    memory as memory_mod,
    summarize as summarize_mod,
    indexer as indexer_mod,
    agent_tools as agent_mod,
    extensions as extensions_mod,
    doc_agent,
    router as router_mod,
    providers as providers_mod,
    context_windows as ctxwin_mod,
    pruning as pruning_mod,
    components as components_mod,
    commitments as commitments_mod,
    systemdocs as systemdocs_mod,
    interests as interests_mod,
    sysmon as sysmon_mod,
    markets as markets_mod,
    security as security_mod,
    proactive as proactive_mod,
    scheduled as scheduled_mod,
    backup as backup_mod,
    policy as policy_mod,
    research as research_mod,
    agent as carrot_agent,
    workspaces as workspaces_mod,
    coder as coder_mod,
    coder_api,
    media as media_mod,
    media_api,
    planner as planner_mod,
    planner_api,
    webhooks as webhooks_mod,
    webhooks_api,
    consensus as consensus_mod,
    consensus_api,
    ambient as ambient_mod,
    ambient_api,
    dualauth,
    gitops as gitops_mod,
    help as help_mod,
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


@app.middleware("http")
async def _revalidate_app_assets(request, call_next):
    """Make the app's own JS/CSS revalidate on every load.

    StaticFiles serves far-future-cacheable responses, and the Electron
    renderer honours that — so after an update the shell kept running the
    previous build's JavaScript against the new backend. Fonts and vendor
    bundles are content-addressed and huge, so they stay cacheable.
    """
    response = await call_next(request)
    path = request.url.path
    if path.startswith(("/js/", "/css/")) or path == "/":
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    elif path.startswith(("/assets/fonts/", "/vendor/")):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response

app.include_router(files_api.router)
app.include_router(coder_api.router)
app.include_router(media_api.router)
app.include_router(media_api.auth_router)
app.include_router(planner_api.router)
app.include_router(webhooks_api.router)
app.include_router(webhooks_api.public_router)
app.include_router(consensus_api.router)
app.include_router(ambient_api.router)


# ===== Pydantic request models =====

class SearchQuery(BaseModel):
    # None means "the active workspace"; "all" means every workspace.
    workspace: Optional[str] = None
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


class ChatAttachment(BaseModel):
    name: Optional[str] = ""
    mime: Optional[str] = ""
    data: str            # base64, with or without a data: URL prefix


class ChatRequest(BaseModel):
    message: str
    attachments: Optional[List[ChatAttachment]] = None
    conversation_id: Optional[str] = None
    model: Optional[str] = None
    stream: Optional[bool] = False
    skill: Optional[str] = None
    task: Optional[str] = None
    provider: Optional[str] = None
    cloud: Optional[bool] = False
    # off | single | multi. Omitted means the saved default.
    search_mode: Optional[str] = None
    # File a newly created conversation into this workspace. The quick-ask
    # panel uses it to ask a question "in" a project without opening the app.
    workspace_id: Optional[str] = None
    # Which surface asked. "code" marks the Code tab's agent so its sessions
    # stay out of chat history; None is the ordinary chat box.
    surface: Optional[str] = None
    # A chat that is answered but not remembered, and deleted afterwards.
    temporary: Optional[bool] = False
    # Whether what Carrot remembers about the user may be used this turn.
    # `None` means the saved default. It is per-turn because the global setting
    # is the wrong shape for the actual complaint: asking for the news and
    # being told about a dog you mentioned once does not mean you want memory
    # off, it means you want it off *now*.
    memory: Optional[bool] = None
    # Sent by the Code tab's agent panel, and only by it. The plan/act preamble
    # and the workspace's rules ride on this: `coder_mode` is a single global
    # setting, so without a per-turn signal every ordinary chat was told it was
    # a coding agent in ACT mode with file tools. Asked for "recent china
    # political news" it went and read pong.py, because that is what it had
    # just been told it was for.
    coder: Optional[bool] = False
    # Let the message pick the task, and the task pick the model. `None` means
    # the saved picker setting; the flag exists so a caller can opt one turn in
    # or out without changing it.
    auto: Optional[bool] = None
    # Running a turn again over a transcript that already contains the
    # question. Without it, rerun stored the user's message a second time and
    # the conversation grew a duplicate every time you asked for another go.
    replay: Optional[bool] = False


class AddMessageRequest(BaseModel):
    role: str
    content: str


class BranchRequest(BaseModel):
    message_id: int
    title: Optional[str] = ""


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
    confirm: Optional[bool] = False


class GoalRequest(BaseModel):
    title: str
    description: str = ""
    category: str = ""
    metadata: Dict[str, Any] = {}


class GoalDecisionRequest(BaseModel):
    accepted: bool


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
    # Markdown or LaTeX, decided when the document is made and kept on it.
    format: str = "markdown"


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


class AutoModelRequest(BaseModel):
    enabled: bool = True


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


class MemoryRequest(BaseModel):
    kind: str = "fact"
    subject: str
    content: str
    confidence: Optional[float] = 1.0
    pinned: Optional[bool] = False


class MemoryUpdateRequest(BaseModel):
    kind: Optional[str] = None
    subject: Optional[str] = None
    content: Optional[str] = None
    confidence: Optional[float] = None
    pinned: Optional[bool] = None
    status: Optional[str] = None


class MemoryExtractRequest(BaseModel):
    user_text: str
    assistant_text: Optional[str] = ""
    conversation_id: Optional[str] = None


class IndexDirRequest(BaseModel):
    path: str


class IndexScanRequest(BaseModel):
    force: Optional[bool] = False


class ApprovalRequest(BaseModel):
    decision: str
    remember: Optional[bool] = False
    # Typed back by the user for the handful of actions that move money or
    # destroy something. An allow without the right phrase is treated as a deny.
    confirmation: Optional[str] = ""


class ResearchRequest(BaseModel):
    question: str
    depth: Optional[str] = None
    conversation_id: Optional[str] = None


class AgentRunRequest(BaseModel):
    task: str
    surface: Optional[str] = "browser"
    conversation_id: Optional[str] = None
    max_steps: Optional[int] = None
    max_seconds: Optional[int] = None
    require_plan_approval: Optional[bool] = None


class DomainRequest(BaseModel):
    domain: str


class WorkspaceRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    folder_id: Optional[str] = None
    color: Optional[str] = None
    archived: Optional[bool] = None


class FolderNodeRequest(BaseModel):
    name: Optional[str] = None
    parent_id: Optional[str] = None


class WorkspaceItem(BaseModel):
    kind: str
    item_id: str


class WorkspaceItemsRequest(BaseModel):
    items: List[WorkspaceItem] = []


class ActiveWorkspaceRequest(BaseModel):
    workspace_id: Optional[str] = ""


class SecretRequest(BaseModel):
    name: str
    value: str


class RouteRequest(BaseModel):
    task: str
    model: str
    provider: Optional[str] = None
    effort: Optional[str] = None


class DocSendRequest(BaseModel):
    """A note (or a selection from one) being sent to the agent."""

    text: str
    note_id: Optional[str] = None
    title: Optional[str] = None
    conversation_id: Optional[str] = None
    task: Optional[str] = None
    skill: Optional[str] = None
    # An explicit choice from the picker, overriding the note's own @/to.
    destination: Optional[str] = None
    option: Optional[str] = None
    search_mode: Optional[str] = None


class LatexRequest(BaseModel):
    source: str
    out_path: Optional[str] = None


class BibliographyRequest(BaseModel):
    bib: str
    tex: str = ""


class ProviderRequest(BaseModel):
    id: str
    label: str = ""
    kind: str = "openai"
    base_url: str = ""
    models: Optional[List[str]] = None
    env_var: str = ""
    api_key: Optional[str] = None


class ProviderKeyRequest(BaseModel):
    # Required, with no default. It used to default to "", which meant a body
    # naming the field anything else — a client typo — validated fine and
    # quietly cleared a working key while returning 200. Clearing a key is a
    # real operation and should have to be asked for explicitly.
    api_key: str


class ProviderEnabledRequest(BaseModel):
    enabled: bool = True


class TaskRequest(BaseModel):
    id: str
    label: str = ""
    description: str = ""
    local_only: bool = False


class BackupExportRequest(BaseModel):
    path: Optional[str] = None
    include_vectors: Optional[bool] = True


class BackupImportRequest(BaseModel):
    path: str
    safety_copy: Optional[bool] = True


class McpEnableRequest(BaseModel):
    enabled: bool = True


# ===== Startup and shutdown =====
#
# A lifespan context manager rather than `@app.on_event`, which is deprecated
# and warned on every boot. The real gain is the other half: there was no
# shutdown path at all, so the research scheduler and the proactive watcher
# were only ever stopped by the process dying. They are daemon threads, so
# that worked — but "it works because nothing outlives it" is not the same as
# stopping, and a reload in dev left the old watcher running beside the new.

def _startup():
    init_db()
    os.makedirs(DB_DIR, exist_ok=True)
    # "Temporary" that survives a crash is not temporary, it is
    # usually-temporary. Sweeping at startup makes the promise unconditional.
    try:
        conv_mod.purge_temporary()
    except Exception:
        pass
    # Every install made before the default learned to read hardware has
    # `gemma4:e4b` sitting in its config, put there by bootstrap rather than
    # chosen. Fixing new installs and leaving those on the wrong model would
    # fix nothing for anyone who already has Carrot. Runs once — see
    # hub.resize_stale_default.
    try:
        hub_mod.resize_stale_default()
    except Exception:
        pass
    vectors_mod.migrate_legacy_embeddings()
    dr_mod.start_scheduler()
    scheduled_mod.start_scheduler()
    proactive_mod.start_watcher()
    if config.get_config().get("index_on_startup", False) and indexer_mod.index_dirs():
        indexer_mod.start_scan_async()


def _shutdown():
    # Best effort and individually guarded: one background thread refusing to
    # stop must not prevent the next one from being asked.
    for stop in (getattr(proactive_mod, "stop_watcher", None),
                 getattr(dr_mod, "stop_scheduler", None)):
        if not callable(stop):
            continue
        try:
            stop()
        except Exception:
            LOG.debug("a background worker did not stop cleanly", exc_info=True)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    _startup()
    yield
    _shutdown()


app.router.lifespan_context = lifespan


@app.middleware("http")
async def require_session_token(request: Request, call_next):
    """Gate the API behind the session token.

    The bind to loopback stops the network, not the machine — any page in the
    browser can reach 127.0.0.1. The token lives in the app's own HTML, which
    the same-origin policy keeps out of reach of other origins.
    """
    if not security_mod.auth_enabled() or security_mod.is_public_path(request.url.path):
        return await call_next(request)

    # `EventSource` cannot set a header, so the two SSE endpoints present a
    # ticket instead: minted by an authenticated POST, spent here, and gone.
    # The session token used to ride in the query string for these, which put
    # it in the server log and the browser's history on every launch. Scoped
    # to those paths, because a ticket is not a session and must not open one.
    if request.url.path in security_mod.TICKET_PATHS:
        if security_mod.spend_ticket(
                request.query_params.get(security_mod.TICKET_QUERY_PARAM)):
            return await call_next(request)

    presented = request.headers.get(security_mod.TOKEN_HEADER) or request.query_params.get(
        security_mod.TOKEN_QUERY_PARAM
    )
    if not security_mod.token_valid(presented):
        return JSONResponse(
            status_code=401,
            content={"detail": "missing or invalid session token"},
        )
    return await call_next(request)


# ===== Index / static =====

def _asset_fingerprint() -> str:
    """A short hash of every JS/CSS file's size and mtime.

    Appended to asset URLs so an update changes the URL itself. Setting
    cache headers alone cannot fix an *already* cached response — the
    browser will not revalidate until the old entry expires — but a new
    URL is a new cache key, so the new build always wins.
    """
    import hashlib
    digest = hashlib.sha1()
    for folder in ("js", "css"):
        directory = os.path.join(WEB_DIR, folder)
        for name in sorted(os.listdir(directory)) if os.path.isdir(directory) else []:
            try:
                stat = os.stat(os.path.join(directory, name))
            except OSError:
                continue
            digest.update(f"{name}:{stat.st_size}:{int(stat.st_mtime)}".encode())
    return digest.hexdigest()[:10]


_ASSET_RE = re.compile(r'(src|href)="(/(?:js|css)/[^"?]+)"')


@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = os.path.join(WEB_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as handle:
            html = handle.read()
        version = _asset_fingerprint()
        html = _ASSET_RE.sub(lambda m: f'{m.group(1)}="{m.group(2)}?v={version}"', html)
        return HTMLResponse(security_mod.inject_token(html))
    return HTMLResponse("<h1>Carrot</h1><p>Frontend not found. Run from project root.</p>")


# ===== Health / status =====

def app_version() -> str:
    """The installed version, for the UI and for bug reports.

    The build stamp written by scripts/build_installer.py wins: package
    metadata goes stale in an editable checkout, and a frozen app has no
    pyproject.toml to read.
    """
    try:
        from carrot._build import VERSION, COMMIT
        return f"{VERSION}+{COMMIT}" if COMMIT else VERSION
    except Exception:
        pass
    try:
        import tomllib
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "pyproject.toml"), "rb") as handle:
            return tomllib.load(handle)["project"]["version"]
    except Exception:
        pass
    try:
        from importlib.metadata import version
        return version("carrot")
    except Exception:
        return "unknown"


@app.post("/api/auth/sse-ticket")
async def sse_ticket():
    """A one-shot credential for an EventSource connection.

    Reaching this endpoint already required the session token in a header, so
    the ticket proves nothing new — it just carries that proof somewhere a
    header cannot go. Short lived and single use, so the copy left behind in
    the server log is dead by the time anyone reads it.
    """
    return {"ticket": security_mod.mint_ticket(),
            "expires_in": security_mod.TICKET_TTL_SECONDS}


@app.get("/api/health")
async def health():
    # The build id makes "am I running the update?" answerable at a glance,
    # which guesswork repeatedly got wrong.
    return {"status": "healthy", "version": app_version(),
            "assets": _asset_fingerprint()}


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

    default_model = hub_mod.configured_or_default_model()
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


class BootstrapRunRequest(BaseModel):
    # Model chosen on the setup splash; omitted = configured/default model.
    model: str | None = None


@app.post("/api/bootstrap/run")
async def bootstrap_run(req: BootstrapRunRequest | None = None):
    try:
        return bootstrap_mod.run_bootstrap(model=req.model if req else None)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Bootstrap failed: {e}")


@app.get("/api/bootstrap/stream")
async def bootstrap_stream(model: str = ""):
    """Run bootstrap, streaming install and download progress as SSE.

    The setup splash needs a real progress bar: pulling a model is a
    multi-gigabyte download and a bar that jumps 30% -> 100% tells the
    user nothing. Bootstrap runs on a worker thread and pushes events
    through a queue so the response can stream while it works.
    """
    import queue as _queue
    import threading as _threading

    events: _queue.Queue = _queue.Queue()
    DONE = object()

    def worker():
        try:
            result = bootstrap_mod.run_bootstrap(
                progress_cb=events.put, model=model or None)
            events.put({"type": "done", **result})
        except Exception as e:
            events.put({"type": "done", "error": f"Bootstrap failed: {e}"})
        finally:
            events.put(DONE)

    _threading.Thread(target=worker, daemon=True).start()

    def event_stream():
        while True:
            event = events.get()
            if event is DONE:
                break
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ===== Carrot Hub (hardware-aware model recommendations) =====

@app.get("/api/hub")
async def hub_overview(refresh: bool = False):
    """Detected specs, the fit-annotated catalog, and per-role picks."""
    try:
        return hub_mod.hub_overview(refresh=refresh)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Hub overview failed: {e}")


@app.get("/api/hub/search")
async def hub_search(workload: str = "", sort: str = "trending",
                     image: bool = False, audio: bool = False, video: bool = False,
                     limit: int = 20):
    """Thin-client live search: local specs + workload text, live HF fetch,
    local quant planning and fit filtering, ranked results."""
    modalities = [m for m, on in (("image", image), ("audio", audio), ("video", video)) if on]
    try:
        return hub_mod.live_search(workload=workload, sort=sort,
                                   modalities=modalities, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Hub search failed: {e}")


@app.post("/api/hub/refresh")
async def hub_refresh():
    """Force a re-fetch of the daily catalog and the HF trending feed."""
    try:
        os.remove(hub_mod.HF_CACHE_PATH)
    except OSError:
        pass
    hub_mod.fetch_hf_trending(force=True)
    models = hub_mod.refresh_catalog(force=True)
    if models is None:
        return {"refreshed": False, "detail": "Carrot Hub unreachable — using cached/bundled catalog."}
    return {"refreshed": True, "count": len(models)}


@app.post("/api/hub/choose")
async def hub_choose(req: ModelSelectRequest):
    """Make a Hub pick the active model (pull it via /api/models/pull)."""
    config.set_config("ollama_model", req.model)
    installed = bootstrap_mod.is_model_available(req.model)
    return {"active_model": req.model, "installed": installed}


# ===== Models =====
#
# There used to be a `SUGGESTED_MODELS` list here: six tags with hand-written
# size hints, the same six for a Raspberry Pi and a threadripper, `gemma4:e4b`
# at the top labelled "Default all-rounder". It was the last place in the app
# that recommended hardware-blind, and it was the place users actually looked —
# the picker is the screen you open to decide what answers the next question.
#
# What replaces it is the Hub's own catalog, annotated against this machine:
# `hub.find_more()`. Suggestions are things that fit, best first; things that
# do not fit are still listed, last, saying what they would need. That is the
# Hub's whole job, and it belongs here rather than behind a tab, because
# nobody opens a catalog to browse — they open the picker and want more.


def prompt_overhead() -> Dict[str, Any]:
    """How much of the window is gone before the user types anything.

    Measured rather than written down. The tool schemas are serialised exactly
    as they go to the provider and the directives are the real strings, so the
    figure moves when a tool is added — which is the whole point. A number
    hardcoded into the settings copy would be wrong by the next release and
    nobody would find out, and this one exists specifically to stop somebody
    setting an 8k window without knowing that a third of it is already spent.
    """
    out: Dict[str, Any] = {}
    for mode in (SEARCH_OFF, SEARCH_SINGLE, SEARCH_MULTI):
        try:
            tools = _available_tools(mode)
            directive = search_directive(mode)
            out[mode] = {
                "tools": len(tools),
                "tokens": (ctxwin_mod.estimate_tokens(json.dumps(tools))
                           + ctxwin_mod.estimate_tokens(directive)),
            }
        except Exception:
            LOG.exception("could not measure prompt overhead for %s", mode)
    # What the settings copy quotes: the worst case, because that is the one
    # that bites. Quoting the average would understate it exactly for the user
    # who has multi-turn search on and the smallest window set.
    out["worst"] = max((v.get("tokens", 0) for v in out.values()
                        if isinstance(v, dict)), default=0)
    return out


def _model_windows(installed, context_info, remote) -> Dict[str, Any]:
    """Context window per model, keyed ``provider/model``.

    Local models carry a probed value; hosted and custom ones fall through to
    the table and then to unknown. Reported per entry with *how* it was
    arrived at, because "200,000 because we recognised the family" and
    "200,000 because the model said so" deserve different confidence and the
    UI should be able to say which it has.
    """
    windows: Dict[str, Any] = {}
    client = None
    try:
        client = ollama_mod.OllamaClient()
    except Exception:
        client = None
    for entry in installed or []:
        name = entry.get("name", "")
        if not name:
            continue
        # The model's own ceiling, not the clamped value in `context_info`.
        # That one is what the model will *run* with, which is capped by the
        # setting — so using it here would show every local model as holding
        # exactly the configured amount, and the chip would tell you nothing
        # you did not already set yourself.
        probed = 0
        if client is not None:
            try:
                probed = int(client.context_limit(name) or 0)
            except Exception:
                probed = 0
        windows[ctxwin_mod.key_for("ollama", name)] = ctxwin_mod.window_for(
            "ollama", name, probed=probed)
    for group in remote or []:
        provider = group.get("provider", "")
        for name in group.get("models", []) or []:
            windows[ctxwin_mod.key_for(provider, name)] = ctxwin_mod.window_for(
                provider, name)
    return windows


class ContextWindowRequest(BaseModel):
    provider: str
    model: str
    # None clears the override and lets the table (or the probe) answer again.
    tokens: Optional[int] = None


@app.put("/api/models/context-window")
async def set_model_context_window(req: ContextWindowRequest):
    """Tell Carrot what a model can hold.

    The escape hatch the table needs in order to be allowed to be incomplete.
    A provider ships a new family, or someone points Carrot at their own
    endpoint serving something it has never heard of, and rather than guessing
    they can say. Their number outranks everything except a local model's own
    probe — and it outranks that too, because a person reading a model card
    beats a regular expression.
    """
    try:
        ctxwin_mod.set_override(req.provider, req.model, req.tokens)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "provider": req.provider,
        "model": req.model,
        "window": ctxwin_mod.window_for(req.provider, req.model),
    }


@app.get("/api/models")
async def list_models(live: bool = False):
    """`live=true` asks Hugging Face for the "find more" section instead of
    reading the list compiled into this build. Off by default because the
    picker popup should not wait on a network round-trip to draw the models
    you already have."""
    client = ollama_mod.OllamaClient()
    installed = client.list_models() if client.is_available() else []
    installed_names = {m["name"] for m in installed}
    cfg = config.get_config()
    active = hub_mod.configured_or_default_model()
    # Already-installed tags are dropped by `find_more` rather than marked,
    # because "Find more" is a list of what you could add — a row you already
    # have is noise on it, and it is one line above in the same popup.
    suggested = hub_mod.find_more(installed=installed_names, live=live)

    # Models from configured cloud providers belong in the picker too —
    # a key you already pasted is useless if the UI only offers Ollama.
    #
    # A provider whose model list cannot be fetched (expired key, rate
    # limit, proxy) is still listed, carrying its error: dropping it
    # silently made cloud models look unsupported rather than unreachable,
    # and left no way to pick one by name.
    remote = []
    try:
        status = router_mod.status()
        for provider in status.get("providers", []):
            if not (provider.get("enabled") and provider.get("configured")):
                continue
            if provider.get("id") == "ollama":
                continue
            try:
                listed = providers_mod.list_models(provider["id"])
                names, error = listed.get("models", []), listed.get("error", "")
            except Exception as exc:
                names, error = [], str(exc)
            remote.append({
                "provider": provider["id"],
                "label": provider.get("label", provider["id"]),
                "models": names,
                "error": error,
            })
    except Exception:
        pass

    # Which provider/model currently serves chat (a pinned route wins).
    chat_provider, chat_model, chat_local = "ollama", active, True
    try:
        chat_route = router_mod.route("chat")
        chat_provider, chat_model = chat_route.provider, chat_route.model
        chat_local = chat_route.local
    except Exception:
        pass

    # The context window each installed model will actually get. Cheap: the
    # client caches per model, and a model it cannot reach is simply omitted
    # rather than blocking the picker.
    context_info: Dict[str, Any] = {}
    try:
        client = ollama_mod.OllamaClient()
        for entry in installed:
            name = entry.get("name", "")
            if name:
                context_info[name] = client.context_length(name)
        context_info["_default"] = client.DEFAULT_NUM_CTX
        context_info["_configured"] = cfg.get("ollama_num_ctx", client.DEFAULT_NUM_CTX)
    except Exception:
        context_info = {}

    # Whether the model now answering can read an image at all.
    #
    # The server has always refused images a model cannot see, but only on
    # send, as a 400 — after you had found the file, attached it and written
    # the question. The composer needs to know beforehand so it can stop
    # offering images rather than take one and then reject it.
    try:
        chat_vision = (ollama_mod.OllamaClient().supports_vision(chat_model)
                       if chat_local else
                       attach_mod.model_supports_vision(chat_model))
    except Exception:
        # Claiming vision it may not have is the safe direction: the send-time
        # check still refuses, so the cost is the old behaviour rather than a
        # vision model that silently will not take pictures.
        chat_vision = True

    return {
        "installed": installed,
        "active_model": active,
        "chat_vision": chat_vision,
        # What each local model is actually being run with. Ollama's default is
        # 4096 whatever the model can hold, and the difference is not subtle —
        # a model in 4k loses the directive and the pages it just read — so the
        # number belongs where the model is chosen rather than buried.
        "context": context_info,
        # The same question for models that will not answer it. Ollama reports
        # a context length; no hosted provider does, so a Claude or a GPT in
        # the picker had no window shown at all and a custom endpoint had no
        # way to be told one. See carrot/context_windows.py.
        "windows": _model_windows(installed, context_info, remote),
        "overhead": prompt_overhead(),
        "default_model": hub_mod.configured_or_default_model(),
        "suggested": suggested,
        "remote": remote,
        "chat_provider": chat_provider,
        "chat_model": chat_model,
        "chat_local": chat_local,
        # Auto is a picker entry, not a model, so it rides alongside rather
        # than pretending to be one of the names above. `auto_local` is what
        # the empty state's privacy claim is allowed to depend on.
        "auto": router_mod.auto_enabled(),
        "auto_local": router_mod.auto_is_local(),
    }


@app.post("/api/models/select")
async def select_model(req: ModelSelectRequest):
    config.set_config("ollama_model", req.model)
    # Naming a model is the opposite of asking Carrot to name one.
    router_mod.set_auto(False)
    return {"active_model": req.model}


@app.post("/api/models/auto")
async def set_auto_model(req: AutoModelRequest):
    """Let each message pick its own task, and the task pick the model."""
    router_mod.set_auto(req.enabled)
    return {"auto": router_mod.auto_enabled(), "auto_local": router_mod.auto_is_local()}


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


# ===== Storage & cleanup (installed models eat disk fast) =====

@app.get("/api/hub/storage")
async def hub_storage():
    """Disk usage per installed model plus overall disk headroom."""
    client = ollama_mod.OllamaClient()
    models = client.list_models() if client.is_available() else []
    models.sort(key=lambda m: m.get("size", 0), reverse=True)
    import shutil as _shutil
    usage = _shutil.disk_usage(os.path.expanduser("~"))
    active = config.get_config().get("ollama_model", "")
    return {
        "models": [{**m, "active": m["name"] == active} for m in models],
        "models_total_bytes": sum(m.get("size", 0) for m in models),
        "disk_total_bytes": usage.total,
        "disk_free_bytes": usage.free,
        "active_model": active,
    }


@app.post("/api/models/delete")
async def delete_model(req: ModelSelectRequest):
    """One-click purge of an installed model."""
    active = config.get_config().get("ollama_model", "")
    if req.model == active:
        raise HTTPException(status_code=400,
                            detail="That's the active model — switch to another model first.")
    client = ollama_mod.OllamaClient()
    if not client.is_available():
        raise HTTPException(status_code=503, detail="Ollama is not available")
    if not client.delete_model(req.model):
        raise HTTPException(status_code=500, detail=f"Could not delete {req.model}")
    return {"deleted": req.model}


# ===== Interop: Obsidian and VS Code / Cursor =====

class VaultRequest(BaseModel):
    vault_path: str


class NoteIdRequest(BaseModel):
    note_id: str


@app.get("/api/interop/status")
async def interop_status():
    return interop_mod.status()


@app.put("/api/interop/vault")
async def interop_set_vault(req: VaultRequest):
    path = os.path.abspath(os.path.expanduser(req.vault_path.strip())) if req.vault_path.strip() else ""
    if path and not os.path.isdir(path):
        raise HTTPException(status_code=400, detail="That folder doesn't exist — paste your vault's full path.")
    config.set_config("obsidian_vault_path", path)
    return interop_mod.status()


@app.post("/api/interop/obsidian/send")
async def interop_send_note(req: NoteIdRequest):
    try:
        return interop_mod.send_note_to_obsidian(req.note_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/interop/obsidian/import")
async def interop_import_vault():
    try:
        return interop_mod.import_from_obsidian()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ===== Calendar (secret iCal URL — no OAuth, no keys) =====

class CalendarConfigRequest(BaseModel):
    ics_url: str | None = None      # empty string disconnects
    enabled: bool | None = None
    agent_aware: bool | None = None


@app.get("/api/calendar/status")
async def calendar_status():
    return calfeed_mod.status()


@app.put("/api/calendar/config")
async def calendar_config(req: CalendarConfigRequest):
    if req.ics_url is not None:
        url = req.ics_url.strip()
        if url and not url.lower().startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="The calendar address must start with https://")
        config.set_config("calendar_ics_url", url)
        if url:
            config.set_config("calendar_enabled", True)
    if req.enabled is not None:
        config.set_config("calendar_enabled", req.enabled)
    if req.agent_aware is not None:
        config.set_config("calendar_agent_aware", req.agent_aware)
    return calfeed_mod.status()


@app.get("/api/calendar/events")
async def calendar_events(days: int = 14):
    if not config.get_config().get("calendar_enabled", False):
        return {"configured": False, "events": []}
    events = calfeed_mod.upcoming_events(days=max(1, min(days, 90)))
    if events is None:
        return {"configured": bool(config.get_config().get("calendar_ics_url")), "events": [],
                "detail": "Calendar not configured or unreachable."}
    return {"configured": True, "events": events}


@app.post("/api/calendar/refresh")
async def calendar_refresh():
    events = calfeed_mod.upcoming_events(days=14, force=True)
    if events is None:
        return {"ok": False, "detail": "Could not fetch the calendar — check the secret address."}
    return {"ok": True, "count": len(events)}


# ===== Chat =====

def _coder_context(conversation_id: str = ""):
    """Mode guidance, the compacted plan, and the workspace's own rules.

    Only emitted when there is something to say: a chat about dinner should not
    carry a plan/act preamble, and an empty rules block is noise in every turn.
    """
    blocks = []
    cfg = config.get_config()
    mode = coder_mod.normalize_mode(cfg.get("coder_mode"))
    # Both modes get their preamble. Act's used to be written and then never
    # sent — the branch only emitted the plan brief, and with no brief it
    # emitted nothing at all. So the mode whose entire job is "use the tools"
    # was the one mode that never said so, and a small local model did the
    # thing models do without instruction: printed the file into the chat and
    # called it done. That is the whole "why can't the agent edit files".
    blocks.append({"role": "system", "content": coder_mod.MODE_PREAMBLE[mode]})
    if mode == coder_mod.MODE_ACT:
        # The brief replaces the planning transcript rather than joining it:
        # by Act time the reading and grepping has served its purpose, and
        # carrying it forward spends the window on transcript.
        brief = coder_mod.snapshot_for(conversation_id)
        if brief:
            blocks.append({
                "role": "system",
                "content": f"{coder_mod.SNAPSHOT_HEADER}\n\n{brief}",
            })
    if cfg.get("agent_tools_enabled", True):
        rules = coder_mod.load_rules(agent_mod.workspace_root())
        if rules:
            blocks.append({"role": "system", "content": rules})
    return blocks


def _prepare_history(conv, message, skill_slug, extra_system=None, mode=None,
                     images=None, coder=False, memory=None, replay=False):
    """Build the model message list for a turn.

    Order matters: the search directive, then skill instructions, then any
    caller-supplied context (a document's cited files, say), then what Carrot
    remembers about the user, then the rolling summary of everything older than
    the recent window, then the recent turns verbatim. Long conversations
    therefore keep their early context instead of falling off a fixed-size slice.
    """
    history = []
    skill = None
    # How the user wants an answer to read. First, so anything more specific
    # later in the prompt — a skill's instructions, a document's format — can
    # still override it.
    style = answer_style_directive()
    if style:
        history.append({"role": "system", "content": style})
    if mode:
        history.append({"role": "system", "content": search_directive(mode)})
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

    if extra_system:
        history.append({"role": "system", "content": extra_system})

    # The coding agent's mode, and whatever instruction files the workspace
    # carries. A repo already set up for Cline, Continue, Goose or Cursor is
    # already set up for Carrot — its rules are read, not re-authored.
    #
    # Only for turns that came from the Code tab. `coder_mode` is one global
    # setting with no idea which panel is asking, so this used to ride on every
    # chat: "recent china political news" arrived with an ACT-mode preamble and
    # a workspace's rules attached, and the model dutifully went off to read
    # pong.py. The mode belongs to the coding panel, not to the app.
    if coder:
        try:
            history.extend(_coder_context(conv.get("id", "") if conv else ""))
        except Exception:
            pass

    # Calendar awareness is an explicit opt-in; when on, the assistant sees
    # the next few days so "what does my week look like" just works.
    try:
        cal_block = calfeed_mod.agent_context()
        if cal_block:
            history.append({"role": "system", "content": cal_block})
    except Exception:
        pass

    # The turn's own choice wins; without one, the saved default stands.
    use_memory = (config.get_config().get("memory_enabled", True)
                  if memory is None else bool(memory))
    if use_memory:
        try:
            # Recall is scoped to the workspace the conversation lives in, not
            # the one that happens to be active — re-opening an old chat should
            # bring back its own context, not today's.
            block = memory_mod.as_prompt_block(memory_mod.recall(
                message,
                workspace_id=workspaces_mod.workspace_of(
                    workspaces_mod.KIND_CONVERSATION, conv.get("id", "")
                ) or "",
            ))
            if block:
                history.append({"role": "system", "content": block})
        except Exception:
            pass

    history += summarize_mod.build_history(conv)
    # On a rerun the question is already the last thing in the transcript —
    # only the answer was rewound — so appending it again would hand the model
    # the same question twice in a row and make it read as insistence.
    if replay and history and history[-1].get("role") == "user":
        return history, skill
    user_turn = {"role": "user", "content": message}
    if images:
        # Ollama takes base64 images on the message itself.
        user_turn["images"] = images
    history.append(user_turn)
    return history, skill


MAX_TOOL_ROUNDS = 8
# Multi-turn search needs room to search, read, notice a gap, and search again.
# Eight rounds is one pass; a real follow-up loop runs out halfway through.
MAX_TOOL_ROUNDS_MULTI = 16

# ===== How long a turn may go on =====
#
# A round count was the wrong unit and it was the visible one. A turn that had
# read six pages and was two calls from the answer stopped at eight and wrote
# up whatever it had, which is how "F-35 status" came back as four bullets
# after fourteen tool calls — the ceiling was reached, not the answer.
#
# Rounds are not what runs out. What runs out is the context window, and that
# is measurable: the transcript, the tool schemas and the directive are all
# strings we are about to send. So the loop continues while the next request
# still fits, and the ceilings above become a backstop for the pathological
# case rather than the thing that normally stops a turn.
#
# The backstop stays because a full window is not the only way a loop is
# wrong: a model calling list_dir on the same directory returns almost nothing
# each time, so it could spin for hundreds of rounds without the window
# noticing. That is a bug, not deep work, and it should not cost a hosted
# provider fifty calls before anybody sees it.
MAX_TOOL_ROUNDS_CEILING = 60

# Stop before the window is actually full. The estimate is four-characters-
# per-token, the provider counts differently, and a turn that discovers it has
# overrun gets a hard error from the provider instead of a written answer —
# the one outcome worse than stopping early. The headroom is what the answer
# itself is written into.
CONTEXT_STOP_FRACTION = 0.85

# Where a pruned turn aims to land. Far enough below the ceiling to buy several
# more rounds — trimming back to 0.84 would hit the same wall on the next tool
# result and trim again, which is a turn spending its remaining rounds on
# bookkeeping.
CONTEXT_RESUME_FRACTION = 0.6

# Below this much of the window recoverable, pruning is not worth doing and the
# turn should say it is out of room instead.
#
# A transcript that is 95% the user's own long prompt has nothing to give, and
# a turn told "I made room" that got a rounding error back buys one more round
# and hits the same wall — with the difference that it has now also deleted
# what it had. Giving up honestly is better than that.
MIN_WORTHWHILE_PRUNE = 0.1

# What "multi-turn" has to have actually done before an answer is accepted.
# The directive alone does not achieve this: a small on-device model reads
# "do not stop at the first set of results", searches once, and answers from
# the snippets anyway. These gates are checked in code, so the mode means the
# same thing whatever model is behind it.
MULTI_MIN_SEARCHES = 2
MULTI_MIN_READS = 1
# How many times we push back before taking what we are given. Without a cap a
# model that simply cannot use read_url would loop until the round budget ran
# out and the user would get nothing at all.
MAX_GATE_NUDGES = 3
# How many searches may be refused as off-topic before the check gets out of
# the way. A refusal only helps if the model changes course; past a couple it
# is just spending the round budget on nothing, which is how a turn ends with
# four rejected searches and no answer.
MAX_QUERY_REJECTIONS = 2

GATE_NUDGE_NO_READ = (
    "You have search results but have not opened any of them. Snippets are not "
    "an answer — they are a list of places an answer might be. Call read_url on "
    "the results most likely to contain the specifics, then answer from what the "
    "pages actually say."
)
GATE_NUDGE_ONE_SEARCH = (
    "That was one search. Before answering, name what you still cannot answer "
    "from what you have, and run another search aimed at exactly that gap — "
    "using the words a source would use, not the words of the question."
)


# An answer that describes the state of the reading rather than answering the
# question. Reported three times in a row, in three different shapes:
#
#   "Specs available include: 0-60 time, quarter mile times, top speed, price"
#   "The following resources cover technical specifications for the C8 ZR1X"
#   "The provided notes do not contain specific performance specifications...
#    I would need technical data sheets or a full article"
#
# The last one is the clearest: it had rounds left, it knew exactly what was
# missing, it said so — and stopped. Every one of these reads as diligence,
# which is why they kept getting shipped. The phrasings below are the seams
# where that shape shows: talking about "the notes", "the sources" or "the
# provided information" instead of about the subject, or stating what it would
# need instead of going to get it.
COVERAGE_REPORT_SIGNALS = [
    r"\b(the )?(provided |available |given |above )?(notes?|sources?|results?|search results?|information|data|documents?|articles?|snippets?)\b"
    r"[^.]{0,60}\b(do(es)? not|don'?t|didn'?t|fail(ed)? to|lack|are (missing|silent)|contain no)\b",
    r"\bi (would|will) need\b",
    r"\b(specs?|specifications?|details?|figures?|information)\s+(available|listed|covered|provided)\s+(include|are)\b",
    r"\bthe following (resources?|sources?|links?|pages?)\b[^.]{0,40}\bcover\b",
    r"\b(for|to get) (the )?(latest|full|complete|specific|exact)\b[^.]{0,40}\b(check|visit|refer to|consult|see)\b",
    r"\bthese (sources?|resources?|pages?) (cover|contain|discuss|provide)\b",
    r"\bno (specific|concrete|detailed) (figures?|numbers?|specifications?|data)\b[^.]{0,40}\b(were|was|are|is) (found|available|provided)\b",
]

_COVERAGE_RE = [re.compile(p, re.IGNORECASE) for p in COVERAGE_REPORT_SIGNALS]


def _reads_like_a_coverage_report(answer: str) -> bool:
    """Is this an answer, or a status report on the reading?

    Deliberately narrow. "I could not find the 0-60 time" inside an answer that
    also gives the horsepower is a legitimate, useful admission — the shape
    being caught is one where describing the material has *replaced* answering,
    which is why every pattern is about the notes rather than about the subject.
    """
    text = (answer or "").strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _COVERAGE_RE)


GATE_NUDGE_COVERAGE = (
    "That is a report on your own reading, not an answer. You named what is "
    "missing instead of going to get it, and you still have rounds left.\n"
    "Do that now: take the single most important thing the question asked for "
    "and that you do not yet have, search for it directly — the words a source "
    "would use, a specification sheet or a manufacturer page rather than a "
    "review — and open the result. Then write the answer with the figures in "
    "it.\n"
    "A consumer product's specifications are published somewhere. If two more "
    "attempts genuinely fail, say which single fact you could not find, in one "
    "sentence, inside an answer that gives everything you did find."
)


# ===== The plan a multi-turn turn works against =====
#
# Multi-turn searched, read, and stopped whenever it felt done — and "done" for
# a small model means "I have said something". It had no statement of what
# finishing would require, so there was nothing to be finished *against*, and
# nothing anybody could check.
#
# So it writes one first: a handful of concrete questions that together answer
# the request. They are shown to the user, and they are what the finish gate
# tests the answer against. A turn cannot stop while a goal is untouched and
# rounds remain.

MAX_GOALS = 5
MAX_GOAL_NUDGES = 2

PLAN_PROMPT = """You are about to research this request. Before searching, list what you will need to have found in order to answer it properly.

REQUEST: {question}

Rules:
- Write 2 to {limit} short questions, one per line, each a specific fact you must obtain.
- If the request names something you do not recognise — a model number, a product code, an abbreviation — your FIRST question must be what that thing actually is. Do not assume it means something similar that you do know.
- Each question must be checkable: "what engine does it use" is checkable, "understand the vehicle" is not.
- No numbering, no bullets, no preamble. Just the questions, one per line."""


def _research_plan(resolved, question: str) -> List[str]:
    """The concrete questions this turn must answer before it may stop.

    Best-effort: a model that will not produce a usable plan gets the old
    behaviour rather than a broken turn, because a planning step that can fail
    closed would make the whole mode fragile for the models that need it most.
    """
    try:
        raw = router_mod.complete(resolved, [{
            "role": "user",
            "content": PLAN_PROMPT.format(question=question[:400], limit=MAX_GOALS),
        }])
    except Exception:
        LOG.debug("could not draft a research plan", exc_info=True)
        return []

    goals = []
    for line in (raw or "").splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()
        # A plan is questions. A line that is not one is the model narrating.
        if len(line) < 8 or len(line) > 200 or not line.endswith("?"):
            continue
        goals.append(line)
        if len(goals) >= MAX_GOALS:
            break
    return goals


# The coding agent gets the same treatment, for the same reason and against a
# different kind of plan. Multi-turn search stops early because it has said
# something; ACT stops early because it has *done* something — one of the four
# files, and a paragraph describing the rest as though it had been written. The
# steps are what the finish is checked against, and unlike search they are
# checked against the work, not the prose: a step is done when a tool touched
# it, because in ACT mode saying you changed a file is precisely the failure.

CODER_PLAN_PROMPT = """You are about to carry out this coding task. Before touching anything, list the steps you will take.

TASK: {task}

Rules:
- Write 2 to {limit} short steps, one per line, in the order you will do them.
- Each step must name what it changes — a file, a function, a command to run. "update cli.py to accept --json" is a step; "improve the code" is not.
- If you need to read something before you can change it, that is a step.
- No numbering, no bullets, no preamble. Just the steps, one per line."""

# A plan of questions could be told from a model's narration by the question
# mark; a plan of steps has no such marker, and "Let me know if this works."
# parses as a step exactly as well as "Update the parser in args.py" does.
# It matters more than tidiness: a line like that shares no words with anything
# the tools will ever do, so it can never tick, and it would spend both nudges
# sending the turn back for a step that was never work in the first place.
# Steps are imperative; talking to the user is not, and it opens in one of a
# small number of ways.
CODER_NARRATION = re.compile(
    r"^(?:let me|let's|i'?ll|i have|i will|i'?ve|note that|please|"
    r"feel free|would you|do you|here'?s|here is|this (?:plan|will)|"
    r"that'?s|hope|if you)\b", re.I)


def _coder_plan(resolved, task: str) -> List[str]:
    """The steps this coding turn must carry out before it may stop.

    Best-effort in the same way and for the same reason as `_research_plan`:
    the models most likely to stop after one file are also the ones most likely
    to fluff the planning call, and failing closed there would take ACT mode
    away from exactly them.
    """
    try:
        raw = router_mod.complete(resolved, [{
            "role": "user",
            "content": CODER_PLAN_PROMPT.format(task=task[:400], limit=MAX_GOALS),
        }])
    except Exception:
        LOG.debug("could not draft a coding plan", exc_info=True)
        return []

    steps = []
    for line in (raw or "").splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()
        if len(line) < 8 or len(line) > 200:
            continue
        # A heading ("Steps:", "Implementation plan:") is the model framing the
        # list rather than an item in it. A question is it asking rather than
        # planning, which is PLAN mode's job and not this one's.
        if line.endswith(":") or line.endswith("?"):
            continue
        if CODER_NARRATION.match(line):
            continue
        steps.append(line)
        if len(steps) >= MAX_GOALS:
            break
    return steps


CODER_GATE_NUDGE = (
    "Your own plan is not finished. Nothing you have run has touched these "
    "steps:\n{listed}\n"
    "You have about {rounds} rounds left. Do the first one now, with the "
    "tools — write_file or edit_file for a change, run_command for something "
    "to run. Describing it is not doing it.\n"
    "If one of them turns out to be unnecessary or impossible, say which and "
    "why in one sentence, rather than leaving it silently undone."
)


def _coder_gate_nudge(unmet: List[str], rounds_left: int) -> str:
    return CODER_GATE_NUDGE.format(
        listed="\n".join(f"- {step}" for step in unmet), rounds=rounds_left)


def _work_terms(name: str, args: Dict[str, Any], result: str) -> str:
    """What a tool call contributes to the record of what was actually done.

    Paths and command lines carry the words a step names — the file, the flag,
    the function — so the arguments matter more here than the output does. The
    result is included but clipped, because a `run_command` that dumps a test
    suite would otherwise swamp every step's terms and mark the whole plan done
    on the strength of one command's noise.
    """
    parts = [name]
    for value in (args or {}).values():
        if isinstance(value, str):
            parts.append(value[:400])
    parts.append((result or "")[:400])
    return " ".join(parts)


# ===== Revising the plan while it runs =====
#
# The plan was written before anything had been looked at, and then it could
# only tick. That is the wrong shape for the thing it models: you find out what
# a task actually involves by starting it. A plan drafted from the question
# alone routinely contains a step that the first file read makes pointless, and
# routinely misses the step that same file makes necessary — and neither could
# be expressed, so the run either ground through a dead step or quietly did
# work that appeared nowhere on the list.
#
# The reason it stayed fixed is a real one, and it is the whole difficulty
# here: **a plan the model can shorten is a plan the model will shorten.** The
# search gate and the goal nudges exist because models stop early; hand the
# same model a way to delete the step it has not done and every one of those
# guarantees becomes advisory. So the revision is deliberately lopsided:
#
# * **Adding is cheap.** A new step is more work, and nothing about the model's
#   incentives makes it add busywork. Added steps are accepted on their face.
# * **Dropping is expensive.** A step may be removed only for a reason about
#   the *world* — it was already true, the file does not exist, the API was
#   removed — never for a reason about the run ("not needed", "covered by the
#   answer", "out of scope"). The reason is shown to the user, so a bad one is
#   visible rather than silent.
# * **A step cannot be dropped for being undone.** That is the loophole the
#   whole gate exists to close, and it is named explicitly in the prompt
#   because it is the one a model reaches for first.
# * **Revisions are capped.** A plan that can be rewritten every round is not
#   a plan, it is a running commentary.

MAX_REPLANS = 2

# Reasons that are about the run rather than about the world. A drop citing
# one of these is the model excusing itself, and is refused. Matched on the
# reason text because that is the only thing the model gives us — a model that
# words its way around this list has at least had to write something that
# sounds like a fact, which is the point.
_EXCUSE = re.compile(
    r"\b(not needed|unnecessary|unneeded|no longer needed|out of scope|"
    r"redundant|already covered|covered by|not required|skip|optional|"
    r"time|budget|too (?:long|hard|complex|difficult)|sufficient|"
    r"enough (?:information|detail|context)|can be omitted)\b", re.I)

REPLAN_PROMPT = """You are partway through this work and you now know things you did not when the plan was written.

REQUEST: {question}

THE PLAN, and what has been done:
{plan}

WHAT YOU HAVE ACTUALLY FOUND OR DONE SO FAR:
{evidence}

Revise the plan against what you found. Return JSON only:
{{"add": ["a new step, in the same style as the ones above"],
  "drop": [{{"step": "the exact text of a step above", "reason": "why it cannot or need not be done"}}]}}

Rules for adding:
- Add a step only if what you found makes it necessary and it is not already on the list.
- At most {add_limit} new steps.
- **Check the dates before you finish.** Look at when the pages you read were published. If anything you are relying on comes from a page noticeably older than the others — and especially if it says something is upcoming, imminent, expected shortly or planned for a date that has since passed — add a step to check whether a more recent figure exists. A two-year-old page describing a contract as "imminent" reads exactly like today's news and is the single most confident way to be wrong.
- **If you have just learned what kind of thing the subject is, add the facts an answer about that kind of thing is expected to contain.** This is the most valuable thing you can do here. The opening plan was written before you knew what you were looking at, so it could only ask what the thing *is*; now that you know, you know what a good answer looks like. Having established that something is a performance car, a reader expects acceleration, top speed, power and price — and an answer that gives only the model year and the production date has answered the question asked and left the reader to ask four more. The same applies to any subject: an aircraft has range and payload, a drug has efficacy and side effects, a law has who it applies to and when it takes effect. Add the two or three that are most conspicuous by their absence.

Rules for dropping — these are strict, and a drop that breaks them will be refused:
- You may drop a step ONLY because of something you have learned about the subject: the thing does not exist, the question was already answered by a source you read, the file or function is not there, the approach is impossible.
- You may NOT drop a step because it is unfinished, difficult, slow, redundant, out of scope, or because you think you have said enough. Being undone is not a reason to remove it — it is the reason it is still there.
- Quote the step exactly as written above, and give the specific fact that makes it moot.
- If nothing genuinely needs to change, return {{"add": [], "drop": []}}. That is the normal answer."""


def _replan(resolved, question: str, goals: List[str], open_goals: List[str],
            evidence_text: str) -> Dict[str, Any]:
    """What the plan should become, given what the run has found.

    Returns ``{"add": [...], "drop": [{"step", "reason"}]}``, already filtered.
    Best-effort in the same way as the initial plan: a model that will not
    produce usable JSON leaves the plan exactly as it was, because a revision
    step that could fail the turn would make every long run more fragile in
    exchange for a refinement.
    """
    empty: Dict[str, Any] = {"add": [], "drop": []}
    if not goals:
        return empty

    lines = "\n".join(
        f"- {goal}" + ("" if goal in open_goals else "   [done]")
        for goal in goals)
    prompt = REPLAN_PROMPT.format(
        question=question[:400], plan=lines,
        evidence=(evidence_text or "(nothing yet)")[:4000],
        add_limit=max(1, MAX_GOALS - len(goals)),
    )
    try:
        raw = router_mod.complete(resolved, [{"role": "user", "content": prompt}])
    except Exception:
        LOG.debug("could not revise the plan", exc_info=True)
        return empty

    parsed = research_mod.extract_json(raw)
    if not isinstance(parsed, dict):
        return empty

    room = max(0, MAX_GOALS - len(goals))
    add = []
    for step in parsed.get("add", []) or []:
        text = " ".join(str(step or "").split())
        if not 8 <= len(text) <= 200:
            continue
        # A "new" step that is already on the list is the model restating the
        # plan back at us, which would double an entry and un-tick it.
        if any(text.lower() == existing.lower() for existing in goals):
            continue
        add.append(text)
        if len(add) >= room:
            break

    drop = []
    for item in parsed.get("drop", []) or []:
        if not isinstance(item, dict):
            continue
        step = " ".join(str(item.get("step", "")).split())
        reason = " ".join(str(item.get("reason", "")).split())
        # Match against the real list rather than trusting the quote: a
        # paraphrase would delete nothing and report that it had.
        actual = next((g for g in goals if g.lower() == step.lower()), None)
        if actual is None:
            continue
        if not reason or _EXCUSE.search(reason):
            LOG.info("refused a plan drop with an excuse rather than a reason: %r", reason)
            continue
        # Never let the plan empty itself. A run with no steps left has no
        # gate, and "drop everything" is the shortest path to finishing.
        if len(drop) + 1 >= len(goals):
            break
        drop.append({"step": actual, "reason": reason[:200]})

    return {"add": add, "drop": drop}


def _unmet_goals(goals: List[str], question: str, answer: str) -> List[str]:
    """Goals the answer does not appear to touch at all.

    Compared on the words that are distinctive to each goal — the ones not
    already in the question, since those turn up in any answer. Deliberately
    generous: one matching term counts as touched. The job is to catch a goal
    that was silently dropped, not to grade how well it was covered, and a
    check that argued about quality would nudge forever.
    """
    asked = _content_terms(question)
    answered = _content_terms(answer or "")
    unmet = []
    for goal in goals:
        distinctive = _content_terms(goal) - asked
        # A goal made entirely of the question's own words cannot be checked
        # against the answer — every answer contains them. Treated as met,
        # because the alternative is nudging forever over a goal no evidence
        # could ever satisfy.
        if distinctive and not (distinctive & answered):
            unmet.append(goal)
    return unmet


def _content_terms(text: str) -> set:
    """Search terms with sentence punctuation taken off the ends.

    `websearch._terms` keeps `.`, `-` and `+` inside a token on purpose, so
    "f-15ex", "3.5" and "c++" survive as single terms. The side effect is that
    a word ending a sentence keeps its full stop — "engine." never matches
    "engine", which silently made every goal look unanswered. Stripped here
    rather than in the shared helper, because that one also decides search
    relevance and query drift, and this is not the moment to move those.
    """
    from . import websearch

    terms = set()
    for term in websearch._terms(text):
        term = term.rstrip(".+-")
        # The lightest possible stemming, and it earns its place: the goal says
        # "what does it cost", the answer says "costs $187,495", and without
        # this the goal reads as untouched and the turn is sent back to find
        # something it had already found.
        if len(term) > 3 and term.endswith("s") and not term.endswith("ss"):
            term = term[:-1]
        if term:
            terms.add(term)
    return terms


def _goal_nudge(unmet: List[str], rounds_left: int) -> str:
    listed = "\n".join(f"- {goal}" for goal in unmet)
    return (
        "Your own plan is not finished. These are still unanswered:\n"
        f"{listed}\n"
        f"You have about {rounds_left} rounds left. Search for the first one "
        "directly, using the words a source would use, open the best result, "
        "and answer from the page.\n"
        "If one of them turns out to be genuinely unobtainable, say so in one "
        "sentence inside the answer — but say it about that specific fact, "
        "after trying, not about your notes in general."
    )


# A line that announces a tool call rather than answering. These are the whole
# of what a round's prose usually is, and gluing them onto the answer would be
# a different bug from the one being fixed.
NARRATION = re.compile(
    r"^\s*(?:okay|ok|alright|sure|got it|let me|let's|i'?ll|i am going to|"
    r"i'?m going to|i will|first,? i|now i|next,? i|searching|looking|"
    r"let me search|let me look|one moment|hold on)\b", re.I)


# A citation written straight onto the end of a word.
#
# Reported as "architectural interestsAl Jazeera" and "at 15%Pew Research
# Center" — the model wrote `interests[Al Jazeera](url)` with nothing between,
# so the link renders flush against the last letter and the sentence appears
# to end in a proper noun. Asking it not to is a request; this is the fix.
#
# Only when a link *follows* text with no space. A link that already has one,
# and markdown like `**bold**[link]`, are left exactly as they are.
GLUED_CITATION = re.compile(r"(?<=[\w%),.;:])(\[[^\]]{1,60}\]\((?:https?:)?//)")


# The model's own gap analysis, written into the top of the answer.
#
# Reported twice, most legibly on an F-35 turn that opened:
#
#     From the current results, I still cannot answer the following:
#     1. The exact status of the F-35's Full Operational Capability …
#     2. Any recent updates on the engine modernization program …
#     I will now search for F-35 Full Operational Capability 2026 …
#     The F-35 Lightning II remains in active production …
#
# Everything before that last line is the multi-turn loop reflecting on what it
# still has to find — genuinely useful, and addressed to itself. It is not the
# tag-marker problem: there is no marker, and `ThinkTagStreamFilter` correctly
# refuses to guess at unmarked prose because doing so in general would
# eventually eat a real answer.
#
# This is narrower than the general case and that is what makes it safe. It
# fires only when all three hold: the block is at the very *start*, it ends
# with an explicit statement of intent to go and search, and substantial
# answer text follows it. An answer that merely mentions searching in passing
# has no such opening, and one that is nothing *but* narration keeps every
# word — there is nothing after the transition to keep instead.

# The hand-off sentence: the model announcing the search it is about to run.
_SEARCH_INTENT = re.compile(
    r"^[ \t]*(?:so |now |next,? |okay,? |ok,? )?"
    r"(?:i(?:'| a|’a)?m going to|i will|i'?ll|let me|let's|i need to|i should)\s+"
    # A small adverb slot. "Let me *also* look up the second thing" is the
    # second announcement in a list of them, and a slot that admitted only
    # "now" left it behind — so the first announcement was cut and the second
    # became the opening line of the answer, which is worse than cutting
    # neither.
    r"(?:(?:now|also|then|next|first|quickly|briefly)\s+){0,2}"
    r"(?:search|look up|look for|check|find out|dig into|research)"
    r"\b[^\n]*\n",
    re.I | re.M)

# The shapes a gap list opens with. One of these has to be present too, so a
# reply that simply begins "Let me check that for you." followed by an answer
# is left alone — that is a greeting, not a leaked reflection.
_GAP_PREAMBLE = re.compile(
    r"(?:still (?:cannot|can'?t|could not|do not|don'?t)\s+(?:answer|find|confirm|"
    r"determine|establish)|cannot yet answer|from the current results|"
    r"based on the (?:current|available) results|remain(?:s)? unanswered|"
    r"to fill (?:these|the) gaps|still (?:missing|unresolved|open)|"
    r"the following (?:gaps|questions|remain))",
    re.I)

# How much answer has to survive for the cut to be worth making. Below this
# the "preamble" is most of what there is, and removing it would leave the
# user with less than the model actually produced.
_MIN_ANSWER_AFTER = 400
# How far into the reply to look. A transition sentence in the middle of a
# long answer is the model narrating mid-flow, which is a different thing and
# not something to cut a thousand words on.
_PREAMBLE_WINDOW = 2500


def strip_process_preamble(text: str) -> str:
    """Remove a leading gap-analysis block the model addressed to itself."""
    text = text or ""
    head = text[:_PREAMBLE_WINDOW]
    if not _GAP_PREAMBLE.search(head):
        return text
    # The *last* intent sentence inside the window: a model that lists three
    # gaps and then announces two searches should lose both announcements.
    cut = None
    for match in _SEARCH_INTENT.finditer(head):
        cut = match.end()
    if cut is None:
        return text
    remainder = text[cut:].lstrip()
    if len(remainder) < _MIN_ANSWER_AFTER:
        return text
    return remainder


def _tidy_answer(text: str) -> str:
    """Small, safe repairs to the finished answer.

    Whitespace, and one bounded excision: a gap-analysis preamble the model
    wrote to itself and left at the top of the reply. Anything that rewrites
    the model's *words* still belongs in the directive where it can be argued
    with, rather than in a regex that silently edits what the user is told.
    """
    return GLUED_CITATION.sub(" " + chr(92) + "1", strip_process_preamble(text))


def _restore_carried(carried: List[str], final: str) -> str:
    """Put back answer prose written in rounds that also called a tool.

    Those rounds' text was dropped entirely, so a model that opened its answer,
    fetched one more fact and then continued lost its opening — the reported
    symptom was an answer that begins at a bullet with the heading missing.

    Two things are not restored. Narration ("Let me search for that") is not
    answer text and belongs nowhere near it. And anything the model wrote again
    in its final message is already there, so putting it back would duplicate
    rather than repair.
    """
    kept = []
    for piece in carried:
        text = piece.strip()
        if not text or NARRATION.match(text):
            continue
        # Compared on a prefix rather than the whole, because a model that
        # continues from its own opening usually re-states it with small edits.
        if text[:60] in final:
            continue
        kept.append(text)
    if not kept:
        return final
    return "\n\n".join(kept + ([final.strip()] if final.strip() else []))


# ===== Checking the answer against the pages it was written from =====
#
# Reported: asked for the C8 ZR1X, the answer said the car has "two electric
# motors, one on each front wheel". It has one, and the page it had just read
# said so — 186 hp on the front axle. Everything around the invention was
# right, sourced and specific, which is what makes this the worst failure
# shape in the app: a confident answer with a fabricated detail inside it.
#
# The gates already force it to search and to read. Nothing checked that what
# it wrote is what the pages said. This is Research's verification pass,
# scoped to one call: the model sees the answer and the page text and nothing
# else — no question, no narrative of its own to defend — which is the whole
# reason it can mark its own work here at all.

MAX_SUPPORT_NUDGES = 1
# The checker has to see at least what the model saw. It was 2500 against a
# 6000-character page, so it was grading an answer on 40% of the source — and
# in the reported case the sentence that *disproves* the claim sits at
# character 3085, outside the window entirely. A checker that cannot see the
# contradiction is not lenient, it is guessing, and it will strike out true
# statements as readily as false ones.
SUPPORT_EVIDENCE_CHARS = 8000

SUPPORT_PROMPT = """Below is an answer, and the source text it was written from.

Find statements in the answer that the sources do not support. A statement is unsupported if the sources do not say it — not if it is merely phrased differently, and not if it is general knowledge that the sources happen not to mention.

A number the answer worked out is not a number the sources state. If a figure appears in the answer but in no source — a combined total, a sum, a converted unit — it is unsupported however reasonable the arithmetic looks.

Be strict about specifics: counts, quantities, names, dates and configurations are exactly where an answer invents detail, and "two motors" where the source says one is the failure to catch.

Check who each fact is ABOUT, not just whether the words appear. A page about one product routinely describes its rivals, its predecessor and the rest of its range in the same paragraphs — a sentence saying competitors "have two motors up front" is not a statement about the subject, and an answer that reports it as one is wrong even though every word of it is on the page. This is the most common way a sourced answer is false, because nothing about it looks invented.

ANSWER:
{answer}

SOURCES:
{evidence}

Return JSON only: {{"unsupported": ["the exact statement, quoted from the answer", ...]}}
An empty list means everything checks out. Do not invent problems."""


def _unsupported_claims(resolved, answer: str, evidence: List[Dict[str, Any]]) -> List[str]:
    """Statements in the answer that the pages do not support.

    Best-effort in both directions. A check that cannot run returns nothing,
    because a verification step that fails closed would block answers over its
    own unavailability. And anything it reports that is not actually in the
    answer is dropped: a checker that hallucinates a quotation is the same
    problem one layer up.
    """
    pages = "\n\n".join(
        f"--- {item['source']} ---\n{item['text'][:SUPPORT_EVIDENCE_CHARS]}"
        for item in evidence if item.get("text")
    )
    if not pages.strip():
        return []
    try:
        raw = router_mod.complete(resolved, [{
            "role": "user",
            "content": SUPPORT_PROMPT.format(answer=answer[:6000], evidence=pages[:24000]),
        }])
    except Exception:
        LOG.debug("could not check the answer against its sources", exc_info=True)
        return []

    # Research already owns the forgiving JSON reader — models fence it, prefix
    # it, and trail commentary after it, and there is one tested copy of that.
    from .research import extract_json

    parsed = extract_json(raw or "")
    if not isinstance(parsed, dict):
        return []
    found = []
    for claim in parsed.get("unsupported", [])[:5]:
        text = str(claim).strip()
        # It has to be a quotation from the answer, or it is the checker
        # writing rather than reading.
        if len(text) > 12 and text[:40] in answer:
            found.append(text)
    return found


SUPPORT_NUDGE = (
    "These statements are not in any page you read:\n{listed}\n"
    "Each one is either something you inferred or something you invented. "
    "Rewrite the answer without them, or search for a source that actually "
    "states them and cite it. Everything else in the answer was fine — keep "
    "it, and keep the same structure."
)


def _support_nudge(claims: List[str]) -> str:
    return SUPPORT_NUDGE.format(listed="\n".join(f"- {c}" for c in claims))


def _search_gate_gap(searches: int, reads: int) -> Optional[str]:
    """What multi-turn still owes the user, if anything.

    Returned as the nudge to send back to the model; None once the mode has
    done what its name claims.
    """
    if reads < MULTI_MIN_READS:
        return GATE_NUDGE_NO_READ
    if searches < MULTI_MIN_SEARCHES:
        return GATE_NUDGE_ONE_SEARCH
    return None


# Any inline markdown link. Used as the signal that a turn actually attributed
# something, rather than as a citation check — one link is weak evidence, but
# zero links after a search is strong evidence that nothing was attributed.
_CITED = re.compile(r"\[[^\]]+\]\(https?://")


def _single_pass_gap(searches: int, reads: int, answer: str) -> Optional[str]:
    """Whether a single-pass turn searched and then answered from the list.

    Single is the *default* mode, so it is where most turns happen, and it had
    no floor at all: the model could search, get a page of titles, and write a
    summary of the titles. Reported as "it seems to be searching and doing a
    good job of searching, yet doesn't answer my question", which is exactly
    what it looks like from outside.

    Deliberately much weaker than the multi-turn gate — one nudge, and only
    when the turn read nothing *and* attributed nothing. A snippet that already
    states the figure is a fact the model may use, and an answer that cites one
    has used it; pushing that turn back would just make the fast mode slow.

    NOT WIRED IN YET, and the reason is worth writing down. Multi-turn can push
    an answer back because it buffers the model's prose until the gates pass —
    nothing has reached the screen, so there is nothing to take back. Single
    streams as it goes, which is the whole feel of the mode, so by the time we
    could judge the answer the user is already reading it. Nudging there means
    either printing a second answer under the first, or holding the first token
    back and making the fast mode feel slow. That is a product decision about
    what single-pass *is*, not a bug fix, so the prompt change lands first and
    this waits for a call on it.
    """
    if searches > 0 and reads == 0 and not _CITED.search(answer or ""):
        return GATE_NUDGE_NO_READ
    return None


def _act_mode_now() -> bool:
    """Is the coding agent allowed to change things right now?"""
    try:
        return coder_mod.normalize_mode(
            config.get_config().get("coder_mode")) == coder_mod.MODE_ACT
    except Exception:
        return False


def _query_drifted(question: str, query: str, context: Optional[set] = None) -> bool:
    """Did the model search for something other than what was asked?

    The failure this catches is not subtle — a question about the F-15EX
    program coming back with a search for "current American political news".
    A generated query that shares no content word with the question is not a
    rephrasing, it is a different question.

    But a *deepening* query shares no word with the question either, and that
    is the point of multi-turn search. "What is happening in American politics"
    leads to "August 4 2026 primary winners Kansas Missouri", which is the
    correct next move and which the first version of this check rejected — four
    times in a row, burning the round budget on refusals the user could not
    even see. So the comparison is against everything the turn has learned:
    the question, plus every query already run, plus the titles that came back.
    A follow-up is always related to its predecessor; only a genuine change of
    subject relates to none of them.
    """
    from . import websearch
    asked = websearch._terms(question) | (context or set())
    if not asked or not query.strip():
        return False
    return not (asked & websearch._terms(query))


QUERY_DRIFT_CORRECTION = (
    "That search was not run: the query {query!r} shares no word with what the "
    "user actually asked ({question!r}). Search for the user's question, using "
    "its specific names, models and numbers."
)


# Tokens that mix letters and digits: c8, zr1x, f-15ex, rtx4090, sm-t870. These
# are model numbers, part numbers and version designators — the most specific
# thing in a question and the least guessable. A model that does not recognise
# one is exactly the model that will replace it with something it does know.
_IDENTIFIER = re.compile(r"\b(?=[a-z0-9-]*[a-z])(?=[a-z0-9-]*\d)[a-z0-9-]{2,20}\b")


def _identifiers(text: str) -> set:
    return set(_IDENTIFIER.findall((text or "").lower()))


def _identifier_key(token: str) -> str:
    """An identifier reduced to what makes it that identifier.

    Hyphens, spaces and dots inside a model number are typography, not
    content: `f35`, `f-35` and `F 35` are one thing, and so are `gpt4` and
    `gpt-4`. Comparing them literally is what made this check fire on its own
    subject — asked for "f35 status" the model searched "F-35 delivery status
    2025 2026 Lockheed Martin production deliveries TR-3 Block 4", which is a
    *better* query than the question, and it was refused for "dropping f35".
    The model then complied literally and searched "f35", which is the worst
    query it could have run. The guard produced the exact failure it exists to
    prevent, on the turn it was watching.
    """
    return re.sub(r"[-_.\s]", "", token or "")


# ===== One site is not the web =====
#
# Asked for "recent us politics news", a turn opened whitehouse.gov five times
# out of six reads and answered entirely from administration press releases:
# the mining announcement, two executive orders, a tariff, a prices statement.
# Every fact was true and correctly cited, and the answer was still wrong —
# for that question, reading one government press office is not research, it
# is a press summary, and the reader cannot tell from the citations that no
# second view was ever consulted.
#
# The search results held Reuters, AP, PBS and Wikipedia. Nothing steered
# towards them and nothing objected when they were skipped.
#
# So a host gets a budget. This is not corroboration — requiring two sources
# to agree before a fact may be stated would suppress whatever only the
# primary source says. It is diversity of *reading*, which is a different
# thing: go and look elsewhere before you write, not agree with the crowd
# before you speak.
MAX_READS_PER_HOST = 2

# How many steps one turn may hand to another model. Two is enough for the
# case this exists for — a turn with one or two genuinely hard sub-problems —
# and low enough that a model which has decided delegation is easier than
# thinking runs out quickly and audibly.
MAX_DELEGATIONS = 2
DELEGATION_EXHAUSTED = (
    "You have already consulted another model {count} times this turn, which is "
    "the limit. Answer the rest yourself, and if a part is genuinely beyond you, "
    "say so in your reply rather than leaving it out."
)

HOST_CONCENTRATION_CORRECTION = (
    "That page was not opened. You have already read {count} pages from {host} "
    "and this would be another. One site's account of a story is one account, "
    "however authoritative it is — for anything contested or political it is a "
    "party to the subject, not a neutral record of it.\n"
    "Open one of the other results instead. If the remaining results are all "
    "from the same place, search again for the story as a different kind of "
    "outlet would report it."
)


def _host_of(url: str) -> str:
    """The registrable-ish host, so `www.x.com` and `x.com` are one place."""
    try:
        host = (urlparse(url).netloc or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _over_read_host(url: str, read_urls) -> str:
    """The host this read would exceed the budget for, if any."""
    host = _host_of(url)
    if not host:
        return ""
    already = sum(1 for seen in read_urls if _host_of(seen) == host)
    return host if already >= MAX_READS_PER_HOST else ""


def _dropped_identifiers(question: str, query: str) -> set:
    """Identifiers in the question that the first search threw away.

    From a reported turn. Asked for "c8 zr1X specs", the model's first search
    was "Toyota C-HR ZR1X 2026 specifications" — it kept `zr1x`, dropped `c8`,
    and substituted a car it had heard of. Then it read eleven pages about a
    Toyota and delivered a confident, well-formatted answer about the wrong
    vehicle.

    The existing drift check cannot see this: the query shares a word with the
    question, so by its definition it is on topic. The failure is not that the
    query went somewhere unrelated, it is that the *most specific term in the
    question was silently replaced by a guess* — and the answer never mentioned
    the substitution, so from the outside it looked like a good search.

    Only applied to the first search of a turn. A later query narrowing to
    "1250 hp coupe" has legitimately moved past the model number; the opening
    move has not earned that.

    Compared on the normalised form, so writing the identifier *better* than
    the user did counts as keeping it. The point is catching a substitution,
    never punishing a spelling.
    """
    kept = {_identifier_key(token) for token in _identifiers(query)}
    return {token for token in _identifiers(question)
            if _identifier_key(token) not in kept}


QUERY_IDENTIFIER_CORRECTION = (
    "That search was not run. The question contains {dropped}, which you left "
    "out of {query!r} — and you added terms the user did not use. {dropped} is "
    "the most specific thing they gave you: search for it literally, exactly as "
    "written, before assuming it means something you recognise. If it turns out "
    "to mean something else, say so in your answer rather than quietly "
    "answering about the other thing."
)

# ===== Chat search modes =====
#
# Whether a chat turn may reach the web at all, and how hard it should try.
# Off is a real setting, not a formality: a question about your own notes gets
# worse, not better, when the model decides to search the web first.

SEARCH_OFF = "off"
SEARCH_SINGLE = "single"
SEARCH_MULTI = "multi"

SEARCH_MODES = {
    SEARCH_OFF: {
        "label": "No search",
        "help": "Carrot answers from the conversation, your files and its memory. It never reaches the web.",
        "tools": set(),
    },
    SEARCH_SINGLE: {
        "label": "Search",
        "help": "Carrot may search the web and read a page when the question needs something current.",
        "tools": {"web_search", "read_url"},
    },
    SEARCH_MULTI: {
        "label": "Multi-turn search",
        "help": "Carrot searches, reads, works out what is still missing, and searches again — "
                "and can hand the whole question to Carrot Research.",
        "tools": {"web_search", "read_url", "start_research"},
    },
}

ALL_SEARCH_TOOLS = set().union(*(mode["tools"] for mode in SEARCH_MODES.values()))

# Prepended to both search directives. The date one is not optional advice:
# a model's "now" is its training cutoff, so asked for recent news it will
# search without a year, accept a 2020 satire page as current, and never
# notice. The source note exists because a free search backend returns content
# farms — sites that reword scraped text and match the query wording closely,
# which is exactly what relevance filtering cannot catch.
SEARCH_PREAMBLE = (
    "Before searching for anything time-sensitive — recent, current, latest, "
    "upcoming, 'now', or anything with a year in it — call current_datetime "
    "first. You do not know today's date; your sense of it is your training "
    "cutoff. Once you know it, put the year in the query and reject results "
    "that turn out to be older.\n"
    "Prefer sources with a name behind them: wire services, established "
    "outlets, official and academic sites, project documentation. Results are "
    "already ordered with those first. If the only results are sites you do "
    "not recognise, say that rather than reporting their contents as fact.\n"
    # Asked for recent US politics, it read nytimes.com/section/politics and
    # summarised the site's navigation — "The New York Times (covering US,
    # World News)" — because that is what is on a front page. Results now say
    # which are index pages; this says what to do about it.
    "Results marked 'index page' are a section front or a homepage. They list "
    "headlines, they are not the story: read a dated article instead. If only "
    "index pages came back, search again with the month and year in the query "
    "before falling back to reading one.\n"
    # "Cite the URL" produced answers with no links in them at all. The
    # research assistant has always said this the concrete way; chat now does.
    "Cite inline as markdown links — [The Guardian](https://...) — on the "
    "sentence the fact came from, not gathered at the end. Every claim you "
    "took from a page gets one. Name the outlet and the date when it matters.\n"
    # Asked for "c8 zr1X specs" it searched well, and answered: "Specs
    # available include: 0-60 time, quarter mile times, top speed, price,
    # engine specifications". Every one of those is the *name* of a number, and
    # one of the snippets it had already read said 1,250 hp and 1.89s. It
    # described the shape of an answer instead of giving one, which is the same
    # failure as summarising a section front — and it reads as competence,
    # which is why it kept happening.
    "Answer with the facts themselves, not with the names of the facts. If the "
    "question asks for specifications, give the numbers; for a price, the "
    "price; for a date, the date. 'Specs available include 0-60 and top speed' "
    "is a table of contents, not an answer — and listing what each source "
    "covers is a description of your search, not an answer either. If you have "
    "a figure, state it and cite it. If you do not have it, go and get it, and "
    "only then say plainly which part you could not find.\n"
    # It had the numbers in a snippet and did not use them, because it had been
    # told to answer from what it read and it had read nothing.
    "A snippet is a fact you have, not a place a fact might be. If a search "
    "result already states the figure, you may use it and cite that result — "
    "but open the page when the question needs more than one line of it.\n"
    # Asked for "c8 zr1X specs", it searched "Toyota C-HR ZR1X 2026
    # specifications" — it did not know what a C8 was, replaced it with
    # something it did know, read eleven pages about a Toyota and answered
    # confidently about the wrong car without ever mentioning the swap.
    "Search the user's words before your interpretation of them. A model "
    "number, part number or version you do not recognise is the most specific "
    "thing you were given: search it literally first. Never quietly replace it "
    "with something you have heard of — if the results show it means something "
    "other than you assumed, say so in the answer, and if you genuinely cannot "
    "identify it, ask rather than answering about a different thing.\n"
)

MULTI_SEARCH_DIRECTIVE = (
    SEARCH_PREAMBLE +
    "Search mode: multi-turn. Do not stop at the first set of results.\n"
    "- Search, read the pages that look most likely to answer the question, then ask "
    "yourself what you still cannot answer *about the question that was asked*, and "
    "search again for exactly that.\n"
    # Asked for "recent us political news" it read an NBC section front, took
    # that page's list of headlines as an agenda, and ran eight searches on
    # Max Miller, a Wisconsin convention and a "wonk reference manual" — none
    # of which anyone had asked about. A front page is a list of everything
    # that exists, not a list of what the user wants.
    "- A gap is something the question needs and you do not have. It is not every "
    "headline you happened to see: an index page lists everything a site has, and "
    "chasing each one answers a question nobody asked.\n"
    # Persistence was never the problem — the run that went wrong searched
    # eight times and the good one searched more. Keep going; the fix is where
    # it is pointed and what it hands over, not how hard it works.
    "- Keep going until you can actually answer, and do not stop at a round "
    "count. If a page will not load or a lead is thin, find the same fact "
    "somewhere else rather than reporting that you could not. Each round should "
    "be narrower than the last: use the words a source would use, not the words "
    "of the question.\n"
    # The preamble already asks for markdown links; this line used to say only
    # "cite the URL", which is weaker and read as permission for the bare
    # "[Gmauthority]" labels that link to nothing and cannot be checked.
    "- Every claim carries its source as a link on the same line — "
    "[GM Authority](https://...) — never a bare label. Say plainly when the "
    "sources disagree or when you could not find something.\n"
    # The shape of the answers that came back rated good: they open by saying
    # what the subject *is*, in the words someone who had to ask would need,
    # before any heading or list.
    "- Open with one sentence naming what the subject is and why it matters, "
    "before any heading — a page of figures with no idea what they belong to "
    "is a table, not an answer.\n"
    # The shape of an answer people actually want back: grouped, specific,
    # sourced. Not a topic-by-topic status report of the searching.
    # Headings are topics, not the questions the planner asked itself. A
    # model handed a plan will otherwise reply to it item by item and the
    # answer arrives as a worksheet with its own scaffolding still on.
    # A reported answer gave "approximately 953 lb-ft combined torque" for a
    # car whose two figures are 828 and 145. The sum is 973, no source said
    # either number, and combined hybrid torque is not additive anyway —
    # the two peaks arrive at different speeds. A figure nobody published,
    # arrived at by arithmetic nobody checked, reads exactly like a fact.
    "- Do not calculate figures. Quote the numbers your sources state and "
    "leave them separate: if no source gives a combined or derived total, "
    "there is no such number to report, and adding two of them yourself "
    "produces something that looks sourced and is not.\n"
    "- Headings name the subject, not the question: \"Powertrain\", "
    "\"Performance\", \"Price\" — never \"What are the engine "
    "specifications?\". Write to the person who asked, in prose, the way you "
    "would explain it out loud; the plan is how you worked, not the shape of "
    "the reply.\n"
    "- Group what you found under a few plain headings, and make every line a "
    "concrete fact — who, what, when, how much — with its source. A heading with "
    "one vague sentence under it means you stopped too early; go and get the "
    "detail.\n"
    "- Offering to go deeper is welcome, and is not the same as leaving the job "
    "unfinished: 'I can go further on Congress or the midterms' is an offer, "
    "while 'this remains unanswered, searching X might find it' is work handed "
    "back. Do the search instead, then offer.\n"
    # It shipped its own working notes: "Here's what the second search
    # uncovered, filling in the gaps from earlier", a status table, and a
    # closing "What Remains Unanswered" listing the searches it might run next.
    # In a brand-new conversation that reads as a half-finished job pushed out
    # early, which is exactly how it was reported.
    "- The rounds are how you work, not what you deliver. Write one answer to the "
    "question as though you had known it all along. Never refer to 'the first "
    "search' or 'the second search', never hand over a status table of topics, and "
    "never end with a list of what is still unanswered or what you would search "
    "next. If something genuinely could not be found and it matters, say that in a "
    "sentence, in place.\n"
    "- If the question is big enough to deserve a written report with checked "
    "citations, call start_research instead of doing it by hand."
)

SINGLE_SEARCH_DIRECTIVE = (
    SEARCH_PREAMBLE +
    "Search mode: single-pass. You may search the web and read a page when the "
    "question needs current information or a source you do not already have. "
    "Cite the URL for anything you take from a page. Do not search for things you "
    "already know or that are in the conversation.\n"
    # "May read a page" was the whole problem. This is the default mode, so it
    # is where most turns happen, and it said nothing about what to do once the
    # results came back — so a turn that searched well ended by summarising the
    # result list. Single-pass means one round of searching, not permission to
    # answer from titles.
    "Single-pass means one round of searching, not an answer built from the "
    "result list. If the question asks for something specific and the snippets "
    "do not already state it, open the best result and answer from the page."
)

NO_SEARCH_DIRECTIVE = (
    "Search mode: off. You have no web access this turn. Answer from the "
    "conversation, the user's indexed files, and what you remember about them. "
    "If the answer genuinely needs something from the web, say so rather than "
    "guessing — the user can switch search on."
)


def search_mode(requested: Optional[str] = None) -> str:
    """The mode for this turn: the request's choice, else the saved default."""
    mode = (requested or config.get_config().get("chat_search_mode", SEARCH_SINGLE) or "").lower()
    return mode if mode in SEARCH_MODES else SEARCH_SINGLE


# ===== How the answer should read =====
#
# Two answers to the same question, one rated "good" and one "hard to read",
# differed in shape rather than content. The readable one led every point with
# the claim in bold and explained underneath it; ours ran four dense
# paragraphs with the facts buried mid-sentence. Same research, same sources.
#
# And how much shape someone wants is a preference, not a fact about writing.
# Some people want the bullets; some find them a wall of fragments and would
# rather have prose. So it is a setting, with a default rather than a rule.

STYLE_BALANCED = "balanced"
STYLE_BRIEF = "brief"
STYLE_FULL = "full"
STYLE_DEFAULT = STYLE_BALANCED

ANSWER_STYLES = {
    STYLE_BRIEF: (
        "Answer in as few words as carry the facts. Lead with the answer "
        "itself, skip the preamble, and leave out anything the question did "
        "not ask for."
    ),
    STYLE_BALANCED: (
        "Open with one sentence saying what the answer is. Then, when there "
        "is more than one finding, give them as a short list where each point "
        "starts with its claim in bold and explains underneath — "
        "\"**The court blocked construction.** A three-judge panel ruled "
        "that...\" — because that is what makes an answer skimmable without "
        "losing the detail. Close with a line on what it adds up to, if it "
        "adds up to something."
    ),
    STYLE_FULL: (
        "Explain as well as report. Give the finding, then why it matters and "
        "what it follows from, in prose rather than fragments. Assume the "
        "reader wants to understand the subject, not just be told the facts "
        "about it."
    ),
}

# Structure is the axis people most often want turned down: the same content
# as prose rather than as headings and bullets.
STRUCTURE_LESS = (
    "Prefer flowing prose. Use a heading or a list only when the content is "
    "genuinely a list; do not impose structure on an answer that is really a "
    "paragraph."
)


def answer_style_directive() -> str:
    """The user's own preference for how an answer should read."""
    try:
        cfg = config.get_config()
    except Exception:
        return ANSWER_STYLES[STYLE_DEFAULT]

    parts = [ANSWER_STYLES.get(cfg.get("answer_style", STYLE_DEFAULT),
                               ANSWER_STYLES[STYLE_DEFAULT])]
    if cfg.get("answer_structure") == "less":
        parts.append(STRUCTURE_LESS)
    # Free text, last, so it can override anything above it — it is the most
    # specific thing the user has said about what they want.
    custom = str(cfg.get("answer_custom", "") or "").strip()
    if custom:
        parts.append("The user has asked for this specifically, and it takes "
                     "precedence over the guidance above:\n" + custom[:600])
    return "\n".join(parts)


def search_directive(mode: str) -> str:
    return {
        SEARCH_OFF: NO_SEARCH_DIRECTIVE,
        SEARCH_SINGLE: SINGLE_SEARCH_DIRECTIVE,
        SEARCH_MULTI: MULTI_SEARCH_DIRECTIVE,
    }[mode]


# What Ollama said a model can hold, kept between turns.
#
# `OllamaClient` caches this per instance, and a new instance was being built
# for every turn — so the cache was always empty and every local turn paid an
# HTTP round trip to /api/show before it could send its first token. A model's
# ceiling does not change while it is installed; the setting layered on top of
# it does, and that is read fresh below.
_PROBED_WINDOWS: Dict[str, int] = {}


def _window_tokens(resolved) -> int:
    """How much this route can hold, or 0 when nobody knows.

    Zero is not a failure to handle — it is the honest answer for a custom
    endpoint nobody has told us about, and it turns the context check off
    rather than inventing a ceiling and stopping turns at it.
    """
    try:
        probed = 0
        if getattr(resolved, "local", False):
            if resolved.model in _PROBED_WINDOWS:
                probed = _PROBED_WINDOWS[resolved.model]
            else:
                try:
                    from .ollama_client import OllamaClient

                    probed = int(OllamaClient().context_limit(resolved.model) or 0)
                except Exception:
                    probed = 0
                _PROBED_WINDOWS[resolved.model] = probed
        found = ctxwin_mod.window_for(
            getattr(resolved, "provider", "") or "ollama", resolved.model, probed=probed)
        return int(found.get("tokens") or 0)
    except Exception:
        return 0


def _available_tools(mode: str = SEARCH_SINGLE):
    """Built-in tools, enabled extension packs, and every enabled MCP server.

    The search mode subtracts rather than adds: every non-web tool is always
    offered, and the web ones are filtered to what this mode allows. Removing
    the tool is what makes "off" mean off — an instruction not to search is a
    request, but a tool that is not in the list cannot be called.
    """
    allowed = set(agent_mod.TOOLS) - ALL_SEARCH_TOOLS | SEARCH_MODES[mode]["tools"]
    tools = list(agent_mod.ollama_tools(enabled=sorted(allowed)))
    try:
        tools += extensions_mod.ollama_tools()
    except Exception:
        pass
    try:
        tools += mcp_mod.ollama_tools()
    except Exception:
        pass
    return _apply_coder_mode(tools)


def _apply_coder_mode(tools):
    """In plan mode, take the write tools away rather than asking nicely.

    Cline's plan/act split only means anything if it is enforced by the tool
    list: a model that *can* write eventually will, however the prompt is
    worded. This subtracts, so it also covers pack and MCP tools whose names
    happen to be write tools.
    """
    try:
        mode = coder_mod.normalize_mode(config.get_config().get("coder_mode"))
    except Exception:
        return tools
    if mode == coder_mod.MODE_ACT:
        return tools
    names = [t.get("function", {}).get("name", "") for t in tools]
    keep = set(coder_mod.tools_for_mode(names, mode))
    return [t for t in tools if t.get("function", {}).get("name", "") in keep]


def _run_tool(name, args, conversation_id):
    """Run one tool, forwarding any approval prompt it raises to the stream.

    Built-in mutating tools block on user approval, and the prompt has to reach
    the browser *while* they are blocked. The tool runs on a worker thread and
    pushes events onto a queue this generator drains, so the SSE stream stays
    live for the whole wait.
    """
    # Removing a tool's declaration is the first line of defence, but a small
    # model will still emit a call for a name it saw in training and was never
    # offered. Reject it here, structured, so it reads as a protocol error and
    # gets corrected rather than apologised for.
    try:
        refusal = coder_mod.reject_tool(
            name, config.get_config().get("coder_mode")
        )
    except Exception:
        refusal = None
    if refusal:
        yield {"_tool_result": json.dumps(refusal)}
        return

    events = queue.Queue()
    outcome = {}

    def work():
        try:
            if agent_mod.is_builtin(name):
                outcome["result"] = agent_mod.call(name, args, conversation_id, events.put)
            elif extensions_mod.is_extension_tool(name):
                outcome["result"] = extensions_mod.call(name, args, conversation_id, events.put)
            else:
                outcome["result"] = mcp_mod.call_namespaced_tool(name, args)
        except Exception as exc:
            outcome["result"] = f"error: {exc}"
        finally:
            events.put(None)

    threading.Thread(target=work, daemon=True, name="carrot-tool").start()
    while True:
        event = events.get()
        if event is None:
            break
        yield event
    yield {"_tool_result": outcome.get("result", "")}


# How much of each source is kept for the forced-answer digest. Six sources at
# 1200 characters is ~2k tokens, which fits in a 4k-context local model with
# room to write — the full transcript emphatically does not.
# How much of each page is kept for the checker and the fallback digest.
#
# 1200 of a 6000-character page: a fifth of what the model read, so anything
# it wrote from the rest could not be verified either way. It was sized when
# every local model ran in a 4k window; with the context fix that constraint
# is gone, and the one path that genuinely needs a small digest clips for
# itself where it builds one.
EVIDENCE_CHARS = 6000
# What the last-resort digest may use per source, so it fits any window.
DIGEST_CHARS = 1200
MAX_EVIDENCE_SOURCES = 6

# The last clause of this prompt used to read "if the notes do not answer it,
# say exactly what is missing and what they did cover" — and that is precisely
# the answer that got reported: "Specs available include: 0-60 time, quarter
# mile times, lap times, top speed, price". The model did what it was told. It
# listed what the notes covered, in a turn where the notes also contained
# "1,250 combined hp" and "1.89s 0-60", because describing coverage was an
# option and giving the numbers was never made the requirement.
FORCED_ANSWER_PROMPT = (
    "Answer the question below from the notes that follow. Write the answer in "
    "plain text now — do not think silently, do not ask to search again, and do "
    "not apologise.\n"
    "Give the facts themselves. If a note contains a number, a date, a price or "
    "a name that answers the question, put it in the answer and cite the source "
    "it came from. Never list what the notes are *about*: 'the sources cover "
    "0-60 time and top speed' is a table of contents, not an answer, and it is "
    "worthless to the person who asked.\n"
    "Answer with whatever the notes do support, even if it is partial — a real "
    "figure with a gap beside it beats a summary of the reading. Only if the "
    "notes contain no fact bearing on the question at all, say that in one "
    "sentence and name what you would need.\n\n"
    "QUESTION: {question}\n\nNOTES:\n{notes}"
)

# How many pages the server will open on the model's behalf once it is clear it
# is not going to do it itself. Two is enough to answer most questions and
# cheap enough not to matter when it was unnecessary.
AUTO_READ_LIMIT = 2

_RESULT_URL = re.compile(r"https?://[^\s\]<>\"']+")

AUTO_READ_NOTE = (
    "You did not open any of the search results after being asked to, so the "
    "pages below were opened for you. They are the sources you found. Answer "
    "the question from what they say, with the figures in them, and cite them."
)


def _unread_result_urls(evidence, already_read):
    """Search-result URLs the turn has seen and not opened, best first.

    Recovered from the text of the search results rather than tracked
    separately: the tool hands the model a formatted list, and that list is
    already in rank order, so re-reading the URLs out of it costs nothing and
    cannot drift out of sync with what the model was actually shown.
    """
    seen, urls = set(already_read), []
    for item in evidence:
        if item["tool"] != "web_search":
            continue
        for url in _RESULT_URL.findall(item["text"]):
            url = url.rstrip(".,);")
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def _auto_read(evidence, already_read, conversation_id, limit=AUTO_READ_LIMIT):
    """Open the top unread results ourselves. Yields stream events, then a list.

    The gate exists to stop a turn answering from a list of titles, and it
    works by telling the model to open a page. That assumes the model *can* —
    and a small local one often cannot, or will not, however many times it is
    asked. The reported turn nudged three times, got nothing, and fell back to
    writing from snippets, which is the failure the gate was built to prevent.

    Telling it a fourth time was never going to work. Reading the page is a
    thing the server can simply do, so it does it, and the model is handed page
    text instead of an argument it keeps losing.
    """
    fetched = []
    for url in _unread_result_urls(evidence, already_read)[:limit]:
        yield {"tool": {"name": "carrot__read_url", "args": {"url": url},
                        "auto": True}}
        result = ""
        for event in _run_tool("carrot__read_url", {"url": url}, conversation_id):
            if "_tool_result" in event:
                result = event["_tool_result"]
            else:
                yield event
        yield {"tool_result": {"name": "carrot__read_url", "result": result[:2000]}}
        if not result.startswith("error:"):
            fetched.append({"url": url, "text": result})
    yield {"_auto_read": fetched}


def _forced_answer(resolved, question, evidence):
    """One last, small ask: answer from a digest rather than the transcript.

    Yields the usual stream events, plus a final ``{"_answer": text}``. The
    prompt is rebuilt from scratch instead of appended to the working history,
    because the history is exactly what made the model run out of room.
    """
    # Clipped here rather than where the evidence is stored. This retry exists
    # because the transcript overran the model's window, so it is the one path
    # that has to stay small — the checker and everything else want the page.
    notes = "\n\n".join(
        f"[{item['tool']}] {item['source']}\n{item['text'][:DIGEST_CHARS]}"
        for item in evidence[-MAX_EVIDENCE_SOURCES:]
    ) or "(nothing was successfully gathered)"
    messages = [{
        "role": "user",
        "content": FORCED_ANSWER_PROMPT.format(question=question[:500], notes=notes),
    }]
    parts = []
    try:
        for event in router_mod.stream_events(resolved, messages, tools=None):
            if event["type"] == "thinking":
                yield {"thinking": event["text"]}
            elif event["type"] != "tool_calls":
                parts.append(event["text"])
    except Exception as exc:
        # A failed retry must not become an exception the user sees instead of
        # an answer; the deterministic fallback below still runs.
        yield {"thinking": f"(could not re-ask the model: {exc})"}
    yield {"_answer": "".join(parts)}


# A Python exception that escaped into the turn, rather than anything the
# provider did. These are the phrasings the interpreter uses, and none of them
# can come out of an HTTP error or a model refusal.
_OUR_FAULT = (
    "can only concatenate", "object has no attribute", "unsupported operand",
    "is not subscriptable", "not callable", "takes no arguments",
    "unexpected keyword argument", "positional argument", "NoneType",
    "list indices must be", "string indices must be",
)


def _blame_for(failure: str) -> str:
    """Say who actually broke, because the answer sends the user somewhere.

    A turn died with "can only concatenate str (not "list") to str" and was
    reported as *provider stopped the turn* — so the user went and checked
    Mistral's rate limits for a TypeError two frames down in Carrot. "The
    provider stopped the turn" is the right sentence for an HTTP 429 and the
    wrong one for our own bug, and the two need different things from whoever
    reads it.
    """
    if any(marker in failure for marker in _OUR_FAULT):
        return (f"this is a fault in Carrot, not in the provider or in what you "
                f"asked: `{failure}`. It is worth reporting.")
    return f"the provider stopped the turn (`{failure}`)."


def _evidence_answer(question, evidence, failure=""):
    """Write something true and useful without the model, as a last resort.

    This is not a good answer, and it does not pretend to be. It is the
    difference between "(no response)" — which tells the user nothing and
    wastes everything the turn did — and a list of what was actually found,
    which they can act on.

    ``failure`` is the provider's own words if it stopped mid-turn. Naming it
    matters: "the model ran out of room" and "your key hit its rate limit" call
    for completely different things from the user, and guessing between them is
    what made the last few of these unfixable.
    """
    blame = f"\n\n{_blame_for(failure)}" if failure else ""
    if not evidence:
        return (
            "I could not gather anything usable for that. Every source I tried "
            "either refused the request or returned nothing readable. Trying a "
            "narrower question, or sending this to Research, usually gets past it."
            + blame
        )
    queries = [e["source"] for e in evidence if e["tool"] == "web_search" and e["source"]]
    pages = [e["source"] for e in evidence if e["tool"] == "read_url" and e["source"]]
    lines = [
        "I gathered the material for this but could not write it up — "
        + (_blame_for(failure) if failure
           else "the model ran out of room to answer.")
        + " Here is what the turn actually collected, so it is not wasted:",
        "",
    ]
    if queries:
        lines.append("**Searched:** " + ", ".join(dict.fromkeys(queries))[:400])
    if pages:
        lines.append("**Read:**")
        lines += [f"- {url}" for url in list(dict.fromkeys(pages))[:8]]
    excerpt = next((e["text"] for e in evidence if e["tool"] == "read_url"), "")
    if excerpt:
        lines += ["", "**From the first page read:**", "", excerpt[:700].strip()]
    lines += [
        "",
        "A model with a larger context window, or sending this to Research, will "
        "get you the written answer.",
    ]
    return "\n".join(lines)


def _agentic_chat_events(history, resolved, skill=None, conversation_id=None,
                         mode=SEARCH_SINGLE, coder=False, turn_id=None):
    """Yield SSE dicts for one chat turn, running the tool-calling loop.

    Tool calls are dispatched to built-in tools or MCP by name prefix, surfaced
    to the UI as they happen, and fed back to the model for a bounded number of
    rounds before the final answer streams as `chunk` events. Multi-turn search
    gets a larger round budget because searching, reading and re-searching is
    several rounds on its own.

    ``turn_id`` registers the turn with the policy kernel so it can be stopped.
    Research and Agent have had a kill switch since they were written; chat had
    none, and a multi-turn search that has decided to read six more pages is
    the single longest thing the app does with no way out of it. Closing the
    browser tab was the only stop, and that leaves the provider call running
    and throws away everything the turn had already written.
    """
    if skill:
        yield {"skill": {"slug": skill["slug"], "name": skill["name"]}}
    yield {"route": resolved.as_dict()}
    yield {"search_mode": mode}

    tools = _available_tools(mode)
    # The old ceiling, kept only as the shape of "how much work is normal" for
    # the nudges that tell the model how many rounds it has left. What
    # actually stops the loop is the window filling up.
    rounds = MAX_TOOL_ROUNDS_MULTI if mode == SEARCH_MULTI else MAX_TOOL_ROUNDS
    window = _window_tokens(resolved)
    tools_tokens = ctxwin_mod.estimate_tokens(json.dumps(tools)) if tools else 0
    # What the turn cost before it had done anything, so later rounds can be
    # compared against it to estimate how many more will fit.
    first_used = 0
    rounds_left = rounds
    working = list(history)
    question = next((m["content"] for m in reversed(history) if m.get("role") == "user"), "")

    # The stop button, and only the stop button.
    #
    # The budget is deliberately enormous rather than taken from config. Chat
    # has never had a step or time ceiling and adding one here would be a
    # behaviour change smuggled in under a feature: a long multi-turn search
    # that used to finish would start dying at the agent's 40-step limit for
    # reasons no one asked for. The kernel's other limits belong to the agent,
    # which navigates and clicks. What chat needs from the kernel is the one
    # thing it has and chat did not: something the user can press.
    stop_context = None
    if turn_id:
        stop_context = policy_mod.register_run(policy_mod.RunContext(
            turn_id,
            budget=policy_mod.Budget(max_steps=10 ** 9, max_seconds=10 ** 9,
                                     max_navigations=10 ** 9, max_domains=10 ** 9),
        ))

    def stopped() -> bool:
        return bool(stop_context and stop_context.cancelled)

    gated = mode == SEARCH_MULTI
    # A coding turn that may actually change things. PLAN mode is excluded on
    # purpose: its whole output is a plan the user reads and approves, so a
    # second machine-made checklist above it is noise, and with the write tools
    # withheld there is nothing for one to tick against.
    planning_work = bool(coder) and _act_mode_now()
    searches = reads = nudges = 0
    # Pages the turn has opened, and whether the server has already stepped in
    # and opened some itself. Once only: if reading them still did not produce
    # an answer, reading two more is not the missing piece.
    read_urls: set = set()
    auto_read_done = False
    # The plan this turn is working against, and how many times the answer has
    # been pushed back for leaving part of it untouched. Capped separately from
    # the search gates: a fuzzy check must not be able to spend the whole
    # budget arguing about coverage.
    goals: List[str] = []
    # The last computed set of open goals, so the plan is only re-sent when it
    # actually changes rather than after every tool call.
    last_open: List[str] = []
    goal_nudges = 0
    # How many times the plan has been revised against what the run found.
    # Capped: a plan rewritten every round is a running commentary.
    replans = 0
    # Steps handed to another model. Capped per turn because each one can be a
    # frontier call on a metered account: the tool exists so a cheap model can
    # buy one hard step, and a model that can buy an unlimited number of them
    # has simply been re-routed to the expensive model without the user
    # choosing that.
    delegations = 0
    coverage_nudges = 0
    # Capped at one. It costs a model call and a round, and a checker allowed
    # to argue twice about the same paragraph would spend the budget on style.
    support_nudges = 0
    final_text = ""
    stalled = False
    # What the provider said when it stopped talking to us, if it did. This
    # used to be an exception that escaped the generator: the SSE response had
    # already been committed as a 200, so the connection simply closed and the
    # browser rendered "(no response)" with no error anywhere. Every previous
    # fix for that was *inside* this loop, and none of them ran, because the
    # throw happened before they could.
    failure = ""
    # Everything the turn has looked at, used to judge whether a follow-up
    # query is a deepening or a change of subject.
    topic = set()
    rejected = 0
    # Whether this turn actually changed anything, which is what ACT mode
    # promises and what a pasted code block is not.
    wrote = 0
    # What the turn actually gathered, kept small on purpose. This is what a
    # forced answer is written from, so it has to fit in a context window that
    # the full transcript already overran.
    evidence = []
    # Answer prose written in rounds that also called a tool. See where it is
    # appended for why dropping it deleted the first half of real answers.
    carried: List[str] = []
    # What the coding turn has actually run, as opposed to what it says it has.
    # Kept separate from `evidence`, which is search and page text and feeds the
    # forced-answer digest — a list of edit_file calls is not material to write
    # an answer from.
    work = []

    # Written before any searching, so the turn has something to be finished
    # against. Shown to the user because "what is it actually trying to find
    # out" is the thing a long multi-turn run gives no sense of.
    if gated:
        goals = _research_plan(resolved, question)
        if goals:
            working.append({
                "role": "user",
                # "Answer every one of these" got taken literally: the model
                # made each planning question a heading and replied underneath
                # it, so the answer came back as its own worksheet — "What are
                # the engine specifications of the c8 zr1X?" as a section title,
                # four times over. The plan is how it decides what to go and
                # find; it is not the shape of what comes back.
                "content": ("Work to this plan — every one of these has to be covered"
                            " before you stop:\n"
                            + "\n".join(f"- {goal}" for goal in goals)
                            + "\n\nThis list is for your own working. Do not answer it"
                              " question by question and do not use these questions as"
                              " headings: write one piece of prose to the request as it"
                              " was actually asked, and let the facts land where they"
                              " belong in it."),
            })
            last_open = list(goals)
            yield {"plan": {"goals": goals, "done": []}}
    elif planning_work:
        goals = _coder_plan(resolved, question)
        if goals:
            working.append({
                "role": "user",
                "content": ("Work to this plan. Carry out every one of these with the"
                            " tools before you stop:\n"
                            + "\n".join(f"- {step}" for step in goals)),
            })
            last_open = list(goals)
            yield {"plan": {"goals": goals, "done": []}}

    # In gated mode the model's prose is held back until the gates are met,
    # because a premature answer is one we intend to throw away — streaming it
    # first would print an answer to the user and then silently replace it.
    def emit_text(text):
        return [] if gated else [{"chunk": text}]

    # A question the model then answers itself is not a question. The gate cuts
    # the reply at the marker as it streams, so the text after it never reaches
    # the user and never reaches the transcript. See coder.QuestionGate.
    asked: Optional[coder_mod.QuestionGate] = None

    for round_index in range(MAX_TOOL_ROUNDS_CEILING):
        # What the next request will cost, before sending it. Emitted every
        # round whether or not it is near the limit, because the meter this
        # feeds is most useful while there is still room to act on it — a bar
        # that only appears once the turn is doomed is an epitaph.
        used = tools_tokens + ctxwin_mod.estimate_tokens(
            json.dumps(working, default=str))
        # How many more rounds there is room for, which is what the nudges and
        # the replanner mean when they say "rounds left".
        #
        # This has to be derived from the window now. It used to be
        # `rounds - round_index - 1` against a fixed ceiling of eight, and
        # when the loop stopped stopping at eight that arithmetic went
        # negative — so every `rounds_left > 1` guard turned false and the
        # machinery that keeps a long turn honest (the coverage nudge, the
        # unmet-goal nudge, the replanner) switched itself off at exactly the
        # point where turns got long enough to need it.
        if window and round_index:
            grown = max(1, (used - first_used) // max(1, round_index))
            rounds_left = max(0, int((window * CONTEXT_STOP_FRACTION - used) // grown))
        else:
            rounds_left = rounds
        if not round_index:
            first_used = used
        if window:
            yield {"context": {"used": used, "window": window,
                               "fraction": round(used / window, 3),
                               "round": round_index + 1}}
            if used > window * CONTEXT_STOP_FRACTION and round_index:
                # Make room before giving up the tools.
                #
                # Ending the turn here is right for a question and wrong for
                # work: a coding turn that has read six files, run the tests
                # and found the failure hits this line holding everything it
                # needs, and is told to stop and write up what it could not
                # get to. But a transcript at the ceiling is mostly tool
                # output, and tool output is the one part that can be thrown
                # away safely — the file is still on disk, and re-reading it
                # costs one round. See carrot/pruning.py for why the budgets
                # are separate and why nothing here calls a model.
                before = used
                want = int(used - window * CONTEXT_RESUME_FRACTION)
                if pruning_mod.prunable_tokens(working) >= min(
                        want, int(window * MIN_WORTHWHILE_PRUNE)):
                    working, pruned = pruning_mod.prune(working, want)
                    used = tools_tokens + ctxwin_mod.estimate_tokens(
                        json.dumps(working, default=str))
                    trimmed = pruned["tool_results"] + pruned["replies"]
                    yield {"stage": "context",
                           "detail": f"context was {int(before / window * 100)}% full — "
                                     f"trimmed {trimmed} earlier "
                                     f"{'result' if trimmed == 1 else 'results'} "
                                     f"to keep working, now "
                                     f"{int(used / window * 100)}%"}
                    yield {"context": {"used": used, "window": window,
                                       "fraction": round(used / window, 3),
                                       "round": round_index + 1, "pruned": pruned}}
            if window and used > window * CONTEXT_STOP_FRACTION and round_index:
                # Nothing left worth trimming. Said plainly rather than by
                # quietly writing up whatever is to hand: "it ran out of room"
                # and "it decided it was done" produce the same short answer
                # and call for opposite things from the user — a bigger window
                # versus a better question.
                yield {"stage": "context",
                       "detail": f"the context window is {int(used / window * 100)}% full — "
                                 "answering now with what has been gathered"}
                working.append({
                    "role": "user",
                    "content": ("You are nearly out of context. Do not call any more "
                                "tools. Write the best answer you can from what you "
                                "already have, and say plainly what you could not "
                                "get to."),
                })
                tools = []
        content_parts = []
        tool_calls = []
        gate = coder_mod.QuestionGate()
        # A provider can fail mid-turn for reasons that have nothing to do with
        # the model: a 429, a dropped socket, or — after four rounds of full web
        # pages have been appended to `working` — a hard context-length error.
        # None of those may reach the user as silence.
        try:
            for event in router_mod.stream_events(resolved, working, tools=tools or None):
                if event["type"] == "thinking":
                    yield {"thinking": event["text"]}
                elif event["type"] == "tool_calls":
                    tool_calls.extend(event["calls"])
                else:
                    # Through the gate rather than straight out. Text held back
                    # for one chunk is the price of catching a marker split
                    # across a boundary; text after a marker is never released.
                    safe = gate.feed(event["text"])
                    if safe:
                        content_parts.append(safe)
                        for out in emit_text(safe):
                            yield out
                    if gate.tripped:
                        break
                    # Checked on the token loop rather than only between
                    # rounds: the thing a user presses stop during is usually
                    # the answer streaming, and a stop that waits for the
                    # provider to finish the paragraph is not a stop.
                    if stopped():
                        break
            tail = gate.flush()
            if tail:
                content_parts.append(tail)
                for out in emit_text(tail):
                    yield out
        except Exception as exc:
            failure = str(exc)
            yield {"provider_error": {"message": failure}}
            final_text = "".join(content_parts) + gate.flush()
            stalled = True
            break

        content_str = "".join(content_parts)

        # Stopped mid-answer. What was already written is kept and stored:
        # a stop is "that is enough", not "throw it away", and half an answer
        # the user chose to stop is usually the half they wanted.
        if stopped():
            final_text = content_str
            stalled = False
            break

        # The model asked something it cannot proceed without. That ends the
        # turn here: no more tool rounds, no forced answer, no gate nudge. Any
        # of those would be Carrot doing the very thing the cut exists to stop
        # — supplying an answer to a question the user has not answered yet.
        if gate.tripped:
            asked = gate
            final_text = gate.prose()
            if gate.blocking():
                # Nothing was answered, so nothing is missing when the turn
                # stops. Marking it stalled would offer Research as a fallback
                # for a turn that is waiting on the user, not on evidence.
                stalled = False
            break

        if not tool_calls:
            # ACT mode's one promise is that you do not have to copy code out
            # of a chat window. A model that prints a file and stops has broken
            # it, so it gets told once — the same structural push-back the
            # search gate uses, for the same reason: an instruction is a
            # request, and a check is a guarantee.
            if (_act_mode_now() and wrote == 0 and nudges < MAX_GATE_NUDGES
                    and coder_mod.looks_like_a_pasted_file(content_str)):
                nudges += 1
                yield {"gate": {"reason": coder_mod.ACT_NOT_ACTING,
                                "searches": searches, "reads": reads}}
                working.append({"role": "assistant", "content": content_str})
                working.append({"role": "user", "content": coder_mod.ACT_NOT_ACTING})
                continue
            # The model wants to finish. In multi-turn that is only allowed
            # once it has actually searched and read.
            gap = _search_gate_gap(searches, reads) if gated else None

            # Asked once and ignored means asked twice will be ignored too. The
            # reported turn nudged the full three times, never got a read, and
            # fell back to writing from snippets — the exact failure the gate
            # exists to prevent. So the second time round the server opens the
            # pages itself rather than repeating itself at a model that cannot
            # do it.
            if gap is GATE_NUDGE_NO_READ and nudges >= 1 and not auto_read_done:
                auto_read_done = True
                fetched = []
                for event in _auto_read(evidence, read_urls, conversation_id):
                    if "_auto_read" in event:
                        fetched = event["_auto_read"]
                    else:
                        yield event
                if fetched:
                    reads += len(fetched)
                    for page in fetched:
                        read_urls.add(page["url"])
                        evidence.append({"tool": "read_url", "source": page["url"],
                                         "text": page["text"][:EVIDENCE_CHARS]})
                    working.append({"role": "assistant", "content": content_str})
                    working.append({"role": "user", "content": AUTO_READ_NOTE + "\n\n" + "\n\n".join(
                        f"--- {page['url']} ---\n{page['text'][:4000]}" for page in fetched)})
                    continue
                # Nothing could be opened either — every result blocked us, or
                # there were none. Fall through: the model is not at fault and
                # another nudge would only spend a round.
                gap = None

            if gap and nudges < MAX_GATE_NUDGES:
                nudges += 1
                yield {"gate": {"reason": gap, "searches": searches, "reads": reads}}
                working.append({"role": "assistant", "content": content_str})
                working.append({"role": "user", "content": gap})
                continue
            if gap:
                # Out of nudges: the model cannot or will not do it. Keep the
                # answer, but say so rather than passing it off as researched.
                stalled = True

            # The searching gates are satisfied; these two are about the answer
            # itself. Checked in this order because a coverage report is a
            # stronger signal than an untouched goal — it is the model telling
            # us, in its own words, that it stopped short.
            if gated and not stalled:
                if (rounds_left > 1 and coverage_nudges < MAX_GOAL_NUDGES
                        and _reads_like_a_coverage_report(content_str)):
                    coverage_nudges += 1
                    yield {"gate": {"reason": GATE_NUDGE_COVERAGE,
                                    "searches": searches, "reads": reads}}
                    working.append({"role": "assistant", "content": content_str})
                    working.append({"role": "user", "content": GATE_NUDGE_COVERAGE})
                    continue

                unmet = _unmet_goals(goals, question, content_str) if goals else []
                if unmet and rounds_left > 1 and goal_nudges < MAX_GOAL_NUDGES:
                    goal_nudges += 1
                    reason = _goal_nudge(unmet, rounds_left)
                    yield {"gate": {"reason": reason, "unmet": unmet,
                                    "searches": searches, "reads": reads}}
                    working.append({"role": "assistant", "content": content_str})
                    working.append({"role": "user", "content": reason})
                    continue

            # The same refusal to stop short, against the work rather than the
            # answer. `work` and not `content_str` is the whole point: ACT mode
            # exists because a model will happily describe four changes and
            # make one, and prose is where that lie lives.
            if planning_work and goals and not stalled:
                unmet = _unmet_goals(goals, question, " ".join(work))
                if unmet and rounds_left > 1 and goal_nudges < MAX_GOAL_NUDGES:
                    goal_nudges += 1
                    reason = _coder_gate_nudge(unmet, rounds_left)
                    yield {"gate": {"reason": reason, "unmet": unmet,
                                    "searches": searches, "reads": reads}}
                    working.append({"role": "assistant", "content": content_str})
                    working.append({"role": "user", "content": reason})
                    continue

            answer = _restore_carried(carried, content_str)

            # Last, and only once. The searching gates are about effort and the
            # goal check is about coverage; this is the only one that asks
            # whether the answer is *true to the pages*. It runs after the
            # others so it grades the finished text rather than a draft that
            # was going to be replaced anyway.
            # Not `gated`. This was multi-turn only, and the reported failure
            # came back "no matter which mode I try" — correctly, because a
            # single-pass turn that reads a page can misattribute exactly as
            # easily. Single-pass promises one round of *searching*, not that
            # it will hand over a fact it can see is wrong.
            if not stalled and evidence and support_nudges < MAX_SUPPORT_NUDGES:
                # The window-based estimate, like the others. Against the old
                # fixed ceiling this went negative once the loop outgrew it,
                # and the check that catches a claim no source supports would
                # have stopped running at round eight.
                unsupported = (_unsupported_claims(resolved, answer, evidence)
                               if rounds_left >= 1 else [])
                if unsupported:
                    support_nudges += 1
                    reason = _support_nudge(unsupported)
                    yield {"gate": {"reason": reason, "unsupported": unsupported,
                                    "searches": searches, "reads": reads}}
                    working.append({"role": "assistant", "content": content_str})
                    working.append({"role": "user", "content": reason})
                    # The carried opening was part of the answer just judged, so
                    # it must not be prepended a second time to the rewrite.
                    carried = []
                    continue

            final_text = answer
            break

        # Prose written in a round that also called a tool.
        #
        # It was dropped. `content_parts` resets each round, so only the last
        # round's text became the answer — and a model that writes its opening
        # paragraph, realises it needs one more lookup, and then continues had
        # its opening deleted. The user saw an answer starting mid-sentence at
        # a bullet, with the heading and the introduction simply gone. The text
        # went into `working` as something the assistant had already said, so
        # the model never wrote it again: it believed it had been delivered.
        #
        # Kept here and put back at the end. In gated mode none of it was
        # streamed, so restoring it cannot double anything on screen.
        if content_str.strip():
            carried.append(content_str)

        working.append({"role": "assistant", "content": content_str, "tool_calls": tool_calls})
        for call in tool_calls:
            # Between tools as well as between rounds. A round can be six page
            # fetches, and stopping "after this round" means waiting out all
            # six — which is the wait the button exists to end.
            if stopped():
                break
            function = call.get("function", {})
            name = function.get("name", "")
            args = function.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}

            # Tools are offered to the model namespaced as `carrot__web_search`,
            # so every comparison below has to be against the bare name. Getting
            # this wrong meant the gate counters never incremented: the model
            # searched, read six pages, and was told after each one that it had
            # not searched yet — which is exactly the thrashing users saw, and
            # why the turn always ended stalled with nothing written.
            bare = name.split("__", 1)[-1]

            # A query about something else cannot answer the question asked.
            # Refuse it and say why, instead of spending a round on it — but
            # only a couple of times. A refusal the model does not act on is a
            # round spent producing nothing, and the turn that prompted this
            # fix spent its last four that way.
            if (bare == "web_search" and rejected < MAX_QUERY_REJECTIONS
                    and _query_drifted(question, str(args.get("query", "")), topic)):
                rejected += 1
                correction = QUERY_DRIFT_CORRECTION.format(
                    query=str(args.get("query", "")), question=question[:200])
                yield {"tool": {"name": name, "args": args, "rejected": True,
                                "reason": "off-topic for what you asked"}}
                working.append({
                    "role": "tool", "content": correction,
                    "name": name, "tool_call_id": call.get("id", name),
                })
                continue

            # The opening search must keep the question's model numbers. Drift
            # cannot catch a query that keeps one identifier, drops another and
            # substitutes a subject the model has heard of — "c8 zr1X" became
            # "Toyota C-HR ZR1X", which shares a word and is a different car.
            # Only the first search: a later query narrowing past the model
            # number is doing its job, the opening move has not earned that.
            dropped = (_dropped_identifiers(question, str(args.get("query", "")))
                       if bare == "web_search" and searches == 0 else set())
            if dropped and rejected < MAX_QUERY_REJECTIONS:
                rejected += 1
                named = ", ".join(f"'{d}'" for d in sorted(dropped))
                correction = QUERY_IDENTIFIER_CORRECTION.format(
                    dropped=named, query=str(args.get("query", "")))
                yield {"tool": {"name": name, "args": args, "rejected": True,
                                "reason": f"dropped {named} from what you asked"}}
                working.append({
                    "role": "tool", "content": correction,
                    "name": name, "tool_call_id": call.get("id", name),
                })
                continue

            # A third page from a site already read twice. Refused rather than
            # discouraged: a rule about balance in the directive is a request,
            # and the turn that prompted this read one press office five times
            # with the directive in front of it the whole way.
            crowded = (_over_read_host(str(args.get("url", "")), read_urls)
                       if bare == "read_url" and rejected < MAX_QUERY_REJECTIONS else "")
            if crowded:
                rejected += 1
                correction = HOST_CONCENTRATION_CORRECTION.format(
                    count=MAX_READS_PER_HOST, host=crowded)
                yield {"tool": {"name": name, "args": args, "rejected": True,
                                "reason": f"already read {MAX_READS_PER_HOST} pages from {crowded}"}}
                working.append({
                    "role": "tool", "content": correction,
                    "name": name, "tool_call_id": call.get("id", name),
                })
                continue

            # Delegation, spent. Refused here rather than discouraged in the
            # tool's description for the same reason as everything else on
            # this path: a limit stated in a prompt is a request, and the
            # thing being limited costs the user money.
            if bare == "ask_model":
                if delegations >= MAX_DELEGATIONS:
                    yield {"tool": {"name": name, "args": args, "rejected": True,
                                    "reason": f"already consulted another model "
                                              f"{MAX_DELEGATIONS} times this turn"}}
                    working.append({
                        "role": "tool",
                        "content": DELEGATION_EXHAUSTED.format(count=MAX_DELEGATIONS),
                        "name": name, "tool_call_id": call.get("id", name),
                    })
                    continue
                delegations += 1

            yield {"tool": {"name": name, "args": args}}
            result = ""
            for event in _run_tool(name, args, conversation_id):
                if "_tool_result" in event:
                    result = event["_tool_result"]
                else:
                    yield event
            if bare in coder_mod.WRITE_TOOLS and not result.startswith("error:"):
                wrote += 1
            # Tick the plan as the evidence arrives, rather than only judging
            # it at the end. A list that sits inert for two minutes and then
            # resolves all at once tells you nothing while you are waiting,
            # which is the whole complaint about a long run.
            if goals:
                # A search goal is met by what was found; a coding step is met
                # by what was run. Checking a coding plan against page text
                # would leave every step open, and checking it against the
                # model's prose would mark them all done on a description.
                if planning_work:
                    work.append(_work_terms(bare, args, result))
                    gathered = " ".join(work)
                else:
                    gathered = " ".join(item["text"] for item in evidence)
                still_open = _unmet_goals(goals, question, gathered)
                if still_open != last_open:
                    last_open = still_open
                    yield {"plan": {"goals": goals,
                                    "done": [g for g in goals if g not in still_open]}}

            if bare == "web_search":
                searches += 1
                # A query that ran is part of the subject now, so the next one
                # is judged against it rather than against the opening question
                # alone. This is what makes a chain of narrowing searches legal.
                from . import websearch as _ws
                topic |= _ws._terms(str(args.get("query", "")))
                topic |= _ws._terms(result[:1500])
            elif bare == "read_url":
                reads += 1
                read_urls.add(str(args.get("url", "")))
            yield {"tool_result": {"name": name, "result": result[:2000]}}
            if bare in ("web_search", "read_url") and not result.startswith("error:"):
                evidence.append({
                    "tool": bare,
                    "source": str(args.get("url") or args.get("query") or ""),
                    "text": result[:EVIDENCE_CHARS],
                })
            working.append(
                {
                    "role": "tool",
                    "content": result,
                    "name": name,
                    "tool_call_id": call.get("id", name),
                }
            )
        if stopped():
            break

        # --- the plan, revised against what this round actually found ---
        #
        # At the end of a round rather than after each tool: a revision judged
        # on one page read mid-round is judged on less than the model itself
        # has, and it costs a model call each time. Once the round is over,
        # everything it learned is in `evidence` and the plan can be revised
        # against all of it at once.
        #
        # Only while there is still something open and a round left to do it
        # in. Revising a plan that is finished, or one there is no budget to
        # act on, changes nothing except the picture the user is looking at.
        # The same estimate the nudges use. Against the old fixed ceiling this
        # went negative once the loop outgrew it, which silently retired the
        # replanner — the plan stopped being revised at round eight of a turn
        # that now runs until the window fills.
        rounds_left_now = rounds_left
        # Open goals mean the plan can still be corrected. A *complete* plan is
        # the other case worth revising, and the more valuable one: the opening
        # plan was written before anything was known about the subject, so it
        # could only ask what the thing is. Once that is answered the plan
        # reads as finished while the answer is still thin — the ZR1X turn
        # established the model year and the production date, ticked every box,
        # and never went after the acceleration figures a reader of a
        # performance-car answer is obviously waiting for.
        #
        # Allowed once in that state, not every round, because a finished plan
        # that keeps growing never finishes.
        plan_complete = bool(goals) and not last_open
        if (goals and rounds_left_now > 0 and replans < MAX_REPLANS
                and (evidence or work)
                and (last_open or (plan_complete and replans == 0))):
            gathered_all = (" ".join(work) if planning_work
                            else " ".join(item["text"] for item in evidence))
            revision = _replan(resolved, question, goals, last_open, gathered_all)
            if revision["add"] or revision["drop"]:
                replans += 1
                dropped = {d["step"] for d in revision["drop"]}
                goals = [g for g in goals if g not in dropped] + revision["add"]
                last_open = _unmet_goals(goals, question, gathered_all)
                # Named, not just applied. A plan that silently rearranges
                # itself is worse than one that cannot change at all: you can
                # no longer tell adapting from giving up, and the drop reason
                # is the only thing that distinguishes them.
                yield {"plan": {
                    "goals": goals,
                    "done": [g for g in goals if g not in last_open],
                    "added": revision["add"],
                    "dropped": revision["drop"],
                }}
                if revision["add"]:
                    working.append({
                        "role": "user",
                        "content": ("The plan has grown from what you found. These"
                                    " also have to be covered before you stop:\n"
                                    + "\n".join(f"- {step}" for step in revision["add"])
                                    + "\n\nThis is for your own working — do not use"
                                      " these as headings in your reply."),
                    })
    else:
        # Every round went to tool calls and the budget ran out, so the model
        # was never asked to write an answer.
        stalled = True

    # An empty answer is never acceptable, and the first attempt at this fix
    # was not enough. Re-asking with the *same* history fails the same way:
    # by then `working` holds several full web pages, which overruns a small
    # local model's context window, and a model with no room left produces
    # nothing. So the retry is asked with a compact digest instead of the
    # transcript — a few thousand characters that fit anywhere.
    # A turn that ends in a question is *supposed* to have little or no prose.
    # Running the empty-answer recovery over it would manufacture exactly the
    # thing the gate just removed — an answer written without the answers —
    # and would do it with more conviction, since the recovery path is built
    # never to come back empty.
    # A stopped turn is also exempt: the recovery exists for a turn that tried
    # to answer and came back empty, and going off to write one anyway is the
    # opposite of what the user just pressed.
    if not final_text.strip() and not (asked and asked.blocking()) and not stopped():
        final_text = ""
        for event in _forced_answer(resolved, question, evidence):
            if "_answer" in event:
                final_text = event["_answer"]
            else:
                yield event
        # And if even that returns nothing, the answer is written from the
        # evidence here, deterministically. "(no response)" after the user
        # watched ten searches scroll past is the worst outcome in the app,
        # and it must not be reachable.
        if not final_text.strip():
            final_text = _evidence_answer(question, evidence, failure)
        for out in emit_text(final_text):
            yield out
        stalled = True

    if gated:
        # Held back above; send the answer we actually kept.
        if final_text:
            yield {"chunk": final_text}
        # The user escalates to Research by hand — this only offers it, and
        # only when the turn was visibly thin. A turn waiting on a question is
        # thin by design, and offering to go and research the point instead of
        # answering it is how the question gets bypassed again.
        if asked and asked.blocking():
            pass
        elif stalled or reads < MULTI_MIN_READS or searches < MULTI_MIN_SEARCHES:
            yield {"suggest_research": {
                "question": question,
                "reason": "this turn answered from "
                          f"{searches} search(es) and {reads} page(s) read",
            }}

    # Whitespace repairs, applied once at the end so the stored message and the
    # rendered one agree. Anything that rewrites words belongs in the directive.
    final_text = _tidy_answer(final_text)

    # Emitted from inside the turn, not re-parsed from the finished prose by
    # the caller. By the time the caller sees the text the block has already
    # been cut out of it, and `blocking` is a fact about how the turn ended
    # that cannot be recovered from the words that survived.
    if stopped():
        # Said plainly. A reply that just stops mid-sentence is indistinguishable
        # from a crash, and the user who pressed the button is the one person
        # who should never have to wonder which it was.
        yield {"stopped": True}
        if not final_text.strip():
            final_text = "_Stopped before there was anything to show._"

    if asked:
        # Guarded because a broken block must cost the form, never the answer.
        # This runs inside the SSE body, after the 200 and the headers have
        # gone out: an exception here is a closed socket rather than an error
        # response, and the user gets a turn that ends with no text at all.
        try:
            questions = asked.questions()
        except Exception:
            LOG.exception("could not parse clarifying questions")
            questions = []
        if questions:
            yield {"questions": questions, "blocking": asked.blocking()}

    # It asked, but not in a shape that makes buttons.
    #
    # The prompt says prose questions are ignored and the model will be made
    # to guess — and it was, silently, which is how a turn ended on "Key
    # Decisions Needed:" with the panel reporting Done underneath it. The
    # model is waiting; the only thing missing was anybody saying so.
    if not (asked and questions):
        try:
            in_prose = coder_mod.prose_questions(final_text)
        except Exception:
            LOG.debug("could not scan for prose questions", exc_info=True)
            in_prose = []
        if in_prose:
            yield {"questions_in_prose": in_prose}

    yield {"_final_text": final_text}


# What is worth keeping out of the stream, and how much of it.
#
# Not everything: `chunk` is the answer, which is stored as the message, and
# re-storing it would double the row. What is kept is the evidence — what was
# searched, what was opened, what came back, what sent the turn back — because
# that is what a reader cannot reconstruct from the prose.
TRACE_EVENTS = ("tool", "tool_result", "plan", "gate", "route", "search_mode",
                "skill", "source", "document", "suggest_research",
                # Which steps went to a different model. Kept for the same
                # reason the searches are: a turn that quietly spent four
                # frontier calls must not read afterwards as one local turn.
                "delegation",
                "provider_error", "error")
# A trace is stored as one JSON column on one row. A turn that read six pages
# would otherwise carry the whole of all six into the transcript.
TRACE_RESULT_CHARS = 400
MAX_TRACE_EVENTS = 200


# Reasoning arrives token by token, so it cannot be stored event by event —
# a single turn's thinking is hundreds of them and would spend the whole cap
# before the first tool call. It is accumulated into one entry instead, which
# is also how it is displayed: one collapsible block, not a stream.
MAX_THINKING_CHARS = 12000


def _remember_trace(trace: List[Dict[str, Any]], event: Dict[str, Any]):
    """Keep the parts of an event worth reopening the conversation for."""
    if "thinking" in event:
        # Appended to the open block, or started if the last thing that
        # happened was something else — so a turn that thinks, calls a tool,
        # then thinks again keeps those as two blocks in the right places,
        # which is what makes the trace readable rather than one lump at top.
        if trace and "thinking" in trace[-1]:
            if len(trace[-1]["thinking"]) < MAX_THINKING_CHARS:
                trace[-1]["thinking"] += event["thinking"]
        elif len(trace) < MAX_TRACE_EVENTS:
            trace.append({"thinking": event["thinking"]})
        return

    if len(trace) >= MAX_TRACE_EVENTS:
        return
    kind = next((k for k in TRACE_EVENTS if k in event), "")
    if not kind:
        return
    kept = event[kind]
    if kind == "tool_result" and isinstance(kept, dict):
        kept = {**kept, "result": str(kept.get("result", ""))[:TRACE_RESULT_CHARS]}
    trace.append({kind: kept})


def _memory_origin(req, override=None) -> str:
    """Which part of Carrot produced this turn, for the memory's provenance.

    Derived here rather than taken from the request body: origin is a claim
    about what Carrot was doing, and the answer is already in front of us.
    """
    if override:
        return override
    return memory_mod.ORIGIN_CODE if getattr(req, "coder", False) else memory_mod.ORIGIN_CHAT


def _post_turn(conversation_id, user_message, assistant_text, message_id,
               origin=memory_mod.ORIGIN_CHAT):
    """Extract memories and refresh the rolling summary after a turn.

    Both call the model, so they run on a worker thread — the user gets their
    answer without waiting for Carrot's bookkeeping.
    """
    def work():
        # A temporary chat is exempt from all of it. This is the only place
        # that decides, so there is one thing to get right rather than three.
        try:
            if conv_mod.is_temporary(conversation_id):
                return
        except Exception:
            pass
        # Reading the settings is itself a database call, and it sat outside
        # every guard — so a database that went away underneath this thread
        # (a shutdown, a test's temporary directory) killed the whole worker
        # with an unhandled exception rather than skipping bookkeeping the
        # user was never waiting on. Everything below is best-effort; this line
        # has to be too.
        try:
            settings = config.get_config()
        except Exception:
            LOG.debug("post-turn bookkeeping skipped: settings unavailable", exc_info=True)
            return
        if settings.get("memory_enabled", True):
            try:
                memory_mod.extract_from_turn(
                    user_text=user_message,
                    assistant_text=assistant_text,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    min_confidence=settings.get("memory_min_confidence", 0.6),
                    origin=origin,
                )
            except Exception:
                pass
        # A commitment in the turn becomes a *proposal*, not a goal. The chip
        # in the thread is the question; a tick is the answer. Same place as
        # memory extraction because it asks the same question of the same
        # text, and best-effort for the same reason — nobody is waiting on it,
        # and a goal Carrot failed to notice is a smaller harm than a turn that
        # died doing bookkeeping.
        if settings.get("goal_chips_enabled", True):
            try:
                # Progress first, and if it was progress, do not also propose.
                # "Finished chapter 3 of the thesis" carries committing
                # language, so without this precedence one sentence about an
                # existing goal both updates it and offers to create a second
                # one — which is how a tracker starts double-counting what
                # somebody has on.
                progressed = commitments_mod.note_progress_from_turn(user_message)
                if not progressed:
                    commitments_mod.propose_from_turn(
                        user_text=user_message,
                        conversation_id=conversation_id,
                        message_id=str(message_id or ""),
                    )
            except Exception:
                LOG.debug("commitment proposal skipped", exc_info=True)
        if settings.get("summarize_enabled", True):
            try:
                summarize_mod.maybe_summarize(conversation_id)
            except Exception:
                pass

    threading.Thread(target=work, daemon=True, name="carrot-post-turn").start()


def _open_conversation(req):
    """Resolve (or create) the conversation a chat request targets."""
    if req.conversation_id is None:
        temporary = bool(getattr(req, "temporary", False))
        meta = {}
        if temporary:
            meta[conv_mod.TEMPORARY_KEY] = True
        surface = (getattr(req, "surface", None) or "").strip()
        if surface:
            meta[conv_mod.SURFACE_KEY] = surface
        created = conv_mod.create_conversation(
            title=req.message[:80],
            metadata=meta or None,
        )
        req.conversation_id = created["id"]
        # Only on creation: filing an existing conversation elsewhere because
        # of one message would move it out from under the user. A temporary
        # chat is filed nowhere at all.
        workspace_id = None if temporary else getattr(req, "workspace_id", None)
        if workspace_id:
            try:
                workspaces_mod.file_item(
                    workspaces_mod.KIND_CONVERSATION, created["id"], workspace_id)
            except Exception:
                pass          # a stale workspace id must not lose the message
    conv = conv_mod.get_conversation(req.conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


def _resolve_chat_route(req):
    """Pick the model for this turn, failing early if nothing can serve it."""
    resolved = None
    # Auto only gets to decide when nobody already did: an explicit model is
    # what the user picked in the UI, and a named task is a caller that already
    # knows what kind of work this is. Reading the message over the top of
    # either would be overriding an answer, not supplying a missing one.
    if _auto_requested(req) and not req.model and not getattr(req, "task", None):
        resolved = router_mod.auto_route(
            getattr(req, "message", "") or "",
            coder=bool(getattr(req, "coder", False)),
            prefer_cloud=bool(getattr(req, "cloud", False)),
        )
    if resolved is None:
        resolved = router_mod.route(
            task=getattr(req, "task", None) or router_mod.TASK_CHAT,
            model=req.model,
            provider=getattr(req, "provider", None),
            prefer_cloud=bool(getattr(req, "cloud", False)),
        )
    if resolved.local:
        client = ollama_mod.OllamaClient()
        if not client.is_available():
            raise HTTPException(status_code=503, detail="Ollama is not available")
        # Naming a model Ollama has never pulled used to fail deep inside the
        # stream as an empty answer. Say which model is missing, and where it
        # would have to come from, before a single token is spent.
        _require_installed_model(client, resolved.model)
    return resolved


def _auto_requested(req) -> bool:
    """Whether this turn should route by message. ``None`` defers to the setting."""
    asked = getattr(req, "auto", None)
    return router_mod.auto_enabled() if asked is None else bool(asked)


def _require_installed_model(client, model: str) -> None:
    """Fail early, and legibly, when an on-device model is not installed."""
    if not model:
        return
    try:
        installed = {m.get("name", "") for m in client.list_models()}
    except Exception:
        return  # Listing is best-effort; never block a turn on it.
    if not installed or model in installed:
        return
    # Ollama treats a bare name as ":latest", so match that way round too.
    base = model.split(":", 1)[0]
    if any(name == model or name.split(":", 1)[0] == base for name in installed):
        return
    raise HTTPException(
        status_code=400,
        detail=(
            f"'{model}' is not installed on this machine. Pull it from the model "
            f"picker, or pick a hosted provider's model in Settings → Providers."
        ),
    )


def _chat_stream_response(req, conv, history, skill, resolved, prelude=None,
                          mode=SEARCH_SINGLE, origin=None):
    """Shared SSE body for the chat and doc-send endpoints.

    ``prelude`` is emitted as the first event, which is how a doc send reports
    its resolved citations before any tokens arrive.

    Deliberately a plain ``def``, not ``async def``. Everything below it is
    synchronous — blocking HTTP to the provider, and a blocking queue drain in
    ``_run_tool`` while a tool waits on approval. Inside an ``async def`` all of
    that runs *on the event loop*, so one chat turn starved the whole server:
    every other request stalled for as long as the model was thinking. The
    approval prompt was the worst of it, because answering one is itself an
    HTTP call the starved loop could not serve — the turn could not finish
    until it was answered and it could not be answered until the turn finished.
    Starlette runs a sync iterator in a threadpool, which is what the
    notification stream below already relies on.
    """
    # Prompts this turn has raised and not yet had answered. Kept out here so
    # the `finally` below can reach them however the generator ends.
    outstanding: set = set()

    # What the stop button aims at. Registered inside the turn and released in
    # the `finally` below rather than by the turn itself: a generator that is
    # closed early — the browser going away mid-answer — never reaches its own
    # last line, and a run left in the kernel's table is a leak that also makes
    # `active_runs` lie about what is happening.
    turn_id = uuid.uuid4().hex[:12]

    def _body():
        final_text = ""
        # Set if the turn ended by asking rather than answering.
        pending_questions: Optional[Dict[str, Any]] = None
        # What the turn did, kept so it survives the page. The searches, the
        # pages read and the plan were rendered live and then thrown away —
        # reopen the conversation and only the prose came back, which is the
        # half you can already read. The evidence is the half you cannot
        # reconstruct, and it is the reason to trust the answer at all.
        trace: List[Dict[str, Any]] = []
        # First frame out, before any model call: the stop button has to exist
        # from the moment there is something to stop, and the longest part of a
        # multi-turn run happens before a single token is streamed.
        yield f"data: {json.dumps({'turn_id': turn_id})}\n\n"
        if prelude:
            yield f"data: {json.dumps({'document': prelude})}\n\n"
        # The last line of defence, and the one that was missing. By the time
        # this generator runs, FastAPI has already sent a 200 and the headers —
        # so an exception here is not an error response, it is a closed socket.
        # The browser sees a stream that ended, has no text, and prints
        # "(no response)". Whatever breaks, the user gets told what broke, the
        # turn is saved, and `done` is sent.
        try:
            for event in _agentic_chat_events(
                    history, resolved, skill, req.conversation_id, mode,
                    coder=bool(getattr(req, "coder", False)),
                    turn_id=turn_id):
                if "_final_text" in event:
                    final_text = event["_final_text"]
                    continue
                if "questions" in event:
                    # Kept so the stored message can record that this turn is
                    # waiting on an answer. A reopened conversation that shows
                    # the prose without the form is a turn that looks abandoned.
                    pending_questions = event
                # Watched as they go past rather than plumbed through every
                # layer between here and the approval gate: the ids are already
                # in the stream, and the stream is the thing that knows whether
                # anyone is still receiving it.
                if "approval_request" in event:
                    outstanding.add(event["approval_request"]["id"])
                elif "approval_resolved" in event:
                    outstanding.discard(event["approval_resolved"]["id"])
                _remember_trace(trace, event)
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:
            LOG.exception("chat turn failed")
            final_text = final_text or (
                f"The turn stopped before I could answer: {exc}\n\n"
                "That is a fault in Carrot or in the provider, not in what you "
                "asked. The trace above shows how far it got."
            )
            yield f"data: {json.dumps({'chunk': final_text})}\n\n"
        try:
            meta = {"trace": trace} if trace else {}
            if pending_questions:
                # Recorded on the row, so reopening the conversation restores
                # the form rather than a paragraph that stops mid-thought.
                meta["questions"] = pending_questions["questions"]
                meta["awaiting_answers"] = bool(pending_questions.get("blocking"))
            stored = conv_mod.add_message(
                req.conversation_id, "assistant", final_text,
                metadata=meta or None)
            # A turn that ended in a question has not concluded anything, and
            # the memory extractor works by reading conclusions out of a turn.
            # Letting it run here files the guesses the model was asking about
            # as things now known about the user — the exact failure the gate
            # exists to prevent, made durable.
            if not (pending_questions and pending_questions.get("blocking")):
                _post_turn(
                    req.conversation_id, req.message, final_text,
                    stored.get("id") if isinstance(stored, dict) else None,
                    origin=_memory_origin(req, origin),
                )
        except Exception:
            # Bookkeeping must never cost the user the answer they can see.
            LOG.exception("could not store the assistant turn")
        yield f"data: {json.dumps({'done': True, 'conversation_id': req.conversation_id})}\n\n"

    def stream():
        """The body, plus the guarantee that nothing is left waiting on it.

        When the browser goes away, Starlette closes this generator, which
        raises GeneratorExit at whichever yield it was sitting on — and the
        approval heartbeat is what makes sure there *is* a recent yield to
        raise it at. A turn blocked with no client used to keep its thread and
        its unanswered question for the full timeout: observed at twenty-seven
        minutes, twenty-six of them with nobody on the other end.

        `finally` rather than `except GeneratorExit`, because a turn that ends
        by crashing has the same loose end as one that ends by being closed,
        and on a normal finish the set is empty and this costs nothing.
        """
        try:
            yield from _body()
        finally:
            policy_mod.release_run(turn_id)
            gone = [approval for approval in list(outstanding)
                    if agent_mod.abandon(approval)]
            if gone:
                LOG.info("client left; abandoned %d unanswered approval(s)", len(gone))

    return StreamingResponse(stream(), media_type="text/event-stream")



def _apply_attachments(req, resolved):
    """Turn a request's attachments into (images, extra_system, note).

    Documents become prompt text and work with any model. Images only go
    through when the resolved model can actually see — otherwise this
    raises, because a model that silently ignores your screenshot and
    answers anyway is worse than a clear error.
    """
    raw = [a.model_dump() if hasattr(a, "model_dump") else dict(a)
           for a in (req.attachments or [])]
    if not raw:
        return None, None, ""
    try:
        images, documents = attach_mod.process(raw)
    except attach_mod.AttachmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if images:
        model = getattr(resolved, "model", "") or ""
        if getattr(resolved, "local", True):
            can_see = ollama_mod.OllamaClient().supports_vision(model)
        else:
            can_see = attach_mod.model_supports_vision(model)
        if not can_see:
            raise HTTPException(
                status_code=400,
                detail=(f"{model} cannot read images. Pick a vision model — "
                        "the Model Hub's 'image: Y' filter lists the ones that fit "
                        "your machine — or attach a PDF or text file instead."),
            )
    return images or None, attach_mod.documents_prompt(documents) or None, \
        attach_mod.describe(images, documents)


@app.post("/api/chat")
async def chat(req: ChatRequest):
    conv = _open_conversation(req)
    resolved = _resolve_chat_route(req)
    mode = search_mode(req.search_mode)
    images, docs_system, note = _apply_attachments(req, resolved)
    history, skill = _prepare_history(conv, req.message, req.skill,
                                      extra_system=docs_system, mode=mode, images=images,
                                      coder=bool(req.coder), memory=req.memory,
                                      replay=bool(getattr(req, "replay", False)))
    # A rerun runs an existing turn again, and the question is already in the
    # transcript — storing it again grew a duplicate on every go.
    if not getattr(req, "replay", False):
        conv_mod.add_message(req.conversation_id, "user",
                             f"{req.message}\n\n[{note}]" if note else req.message)

    if req.stream:
        return _chat_stream_response(req, conv, history, skill, resolved, mode=mode)

    # The non-streaming path used to call the model once, directly, with no
    # tools at all — so the quick-ask overlay, which is the only caller, could
    # not search, could not read a page, and had none of the never-answer-with-
    # nothing guarantees the streaming path has. It runs the same loop now and
    # collects the result, so the two doors into chat behave the same.
    parts, tools_used, route_info = [], [], resolved.as_dict()
    final = ""
    for event in _agentic_chat_events(history, resolved, skill, req.conversation_id, mode,
                                      coder=bool(getattr(req, "coder", False))):
        if "_final_text" in event:
            final = event["_final_text"]
        elif "chunk" in event:
            parts.append(event["chunk"])
        elif "tool" in event:
            tools_used.append(event["tool"].get("name", ""))
    response = final or "".join(parts)
    stored = conv_mod.add_message(req.conversation_id, "assistant", response)
    _post_turn(
        req.conversation_id, req.message, response,
        stored.get("id") if isinstance(stored, dict) else None,
        origin=_memory_origin(req),
    )
    return {
        "conversation_id": req.conversation_id,
        "response": response,
        "route": route_info,
        "tools": tools_used,
        "search_mode": mode,
    }


@app.get("/api/chat/search-modes")
async def list_search_modes():
    """The three search postures and which one is currently the default."""
    return {
        "modes": [
            {"id": name, "label": spec["label"], "help": spec["help"],
             "tools": sorted(spec["tools"])}
            for name, spec in SEARCH_MODES.items()
        ],
        "current": search_mode(),
    }


@app.post("/api/chat/turns/{turn_id}/stop")
async def stop_chat_turn(turn_id: str):
    """Stop a chat turn in flight.

    The same kill switch Research and Agent have used since they were written,
    pointed at the one long-running thing in the app that did not have it. It
    returns ``false`` for a turn that already finished, which is not an error:
    pressing stop as the last token lands is a race the user cannot see and
    should not be told about.
    """
    return {"stopped": policy_mod.cancel_run(turn_id)}


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    """Dedicated SSE streaming endpoint."""
    conv = _open_conversation(req)
    resolved = _resolve_chat_route(req)
    mode = search_mode(req.search_mode)
    images, docs_system, note = _apply_attachments(req, resolved)
    history, skill = _prepare_history(conv, req.message, req.skill,
                                      extra_system=docs_system, mode=mode, images=images,
                                      coder=bool(req.coder), memory=req.memory,
                                      replay=bool(getattr(req, "replay", False)))
    # A rerun runs an existing turn again, and the question is already in the
    # transcript — storing it again grew a duplicate on every go.
    if not getattr(req, "replay", False):
        conv_mod.add_message(req.conversation_id, "user",
                             f"{req.message}\n\n[{note}]" if note else req.message)
    return _chat_stream_response(req, conv, history, skill, resolved, mode=mode)


# ===== Conversations =====

@app.get("/api/conversations")
async def list_conversations(limit: int = 50, workspace: Optional[str] = None):
    """Recent conversations, scoped to the active workspace unless told otherwise.

    Returns a bare list rather than an envelope — three callers already expect
    that shape, and the UI knows the active workspace without being told again.
    """
    return conv_mod.list_conversations(
        limit=limit, workspace_id=workspaces_mod.resolve_scope(workspace)
    )


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


@app.post("/api/conversations/{conv_id}/branch")
async def branch_conversation(conv_id: str, req: BranchRequest):
    """Fork a conversation at a message, leaving the original untouched."""
    try:
        return conv_mod.branch_conversation(conv_id, req.message_id, title=req.title or "")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/conversations/{conv_id}/rewind")
async def rewind_conversation(conv_id: str, req: BranchRequest):
    """Drop a message and everything after it, so the turn can be run again."""
    if conv_mod.get_conversation(conv_id) is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"removed": conv_mod.drop_messages_from(conv_id, req.message_id)}


@app.post("/api/conversations/temporary/purge")
async def purge_temporary_conversations():
    """Delete every temporary chat now, without waiting for a restart."""
    return {"deleted": conv_mod.purge_temporary()}


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
async def search_get(q: str, limit: int = 20, hybrid_weight: float = 0.5,
                     workspace: Optional[str] = None):
    """Search conversations, scoped to the active workspace unless told otherwise.

    ``workspace`` omitted means "whatever is active" — that default is what
    makes a workspace behave like a mode rather than a filter you re-apply on
    every screen. Pass ``workspace=all`` to search everything regardless.
    """
    scope = workspaces_mod.resolve_scope(workspace)
    result = search.search_conversations(
        q, limit=limit, hybrid_weight=hybrid_weight, workspace_id=scope
    )
    return {**result, "workspace_id": scope}


@app.post("/api/search")
async def search_post(req: SearchQuery):
    scope = workspaces_mod.resolve_scope(req.workspace)
    result = search.search_conversations(
        req.query, limit=req.limit, hybrid_weight=req.hybrid_weight, workspace_id=scope
    )
    return {**result, "workspace_id": scope}


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
    # `index_computer_use` never existed; this endpoint has been raising
    # AttributeError since it was written. The scan itself is computer_use_scan.
    count = cpu_mod.computer_use_scan()
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
    """Run a shell command, screening destructive ones for confirmation first.

    A 428 means "this needs a second look" — the client re-sends with
    confirm=true once the user has agreed.
    """
    verdict = security_mod.check_command(req.command, confirmed=bool(req.confirm))
    if not verdict["allowed"]:
        raise HTTPException(
            status_code=428,
            detail={
                "message": verdict["message"],
                "reasons": verdict["reasons"],
                "command": req.command,
                "needs_confirmation": True,
            },
        )
    try:
        cwd = security_mod.resolve_cwd(req.cwd)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    return term_mod.execute_command(req.command, cwd=cwd, timeout=req.timeout)


@app.post("/api/terminal/check")
async def check_terminal_command(req: CommandRequest):
    """Screen a command without running it, so the UI can warn as the user types."""
    return security_mod.classify_command(req.command)


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


@app.get("/api/recap/interests")
async def recap_interests(days: int = 7):
    """What Carrot thinks you have been asking about, and why.

    Readable on its own, not only as a side effect of running a briefing: an
    assistant that has formed a view about what you care about should let you
    look at that view, and at the evidence for it, without having to trigger
    a two-minute research run to find out.
    """
    return interests_mod.derive_topics(days=max(1, min(int(days or 7), 60)))


@app.post("/api/recap/run/interests")
async def run_interest_recap(req: RecapRequest = RecapRequest()):
    """The briefing built from your own recent questions, through Research.

    Streams the same event shapes the Research trace already renders, so the
    UI needs no new vocabulary for it.
    """
    def event_stream():
        try:
            for event in recap_mod.run_interest_recap_stream():
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:
            LOG.exception("interest recap failed")
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


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


@app.get("/api/goals/proposals")
async def list_goal_proposals(conversation_id: str = ""):
    """Undecided proposals, so reopening a conversation shows the question again.

    A chip that evaporates on refresh is a question the user never got to
    answer, which is the same as not having asked.
    """
    if conversation_id:
        return {"proposals": goals_mod.proposals_for(conversation_id)}
    return {"proposals": goals_mod.by_status(goals_mod.STATUS_PROPOSED)}


@app.post("/api/goals/{goal_id}/decide")
async def decide_goal(goal_id: str, req: GoalDecisionRequest):
    """Tick or dismiss. Accepting writes a memory as well as keeping the goal;
    dismissing keeps the row only so the subject is not offered again."""
    decided = goals_mod.decide(goal_id, accepted=bool(req.accepted))
    if decided is None:
        raise HTTPException(status_code=404, detail="No undecided proposal with that id")
    return decided


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
    # System docs first: they are pinned, and "what have I committed to" is a
    # document-shaped question that belongs beside the documents. Only at the
    # top level — a folder is somewhere you filed things, and these were never
    # filed anywhere.
    docs = [] if folder else systemdocs_mod.listing()
    notes = notes_mod.list_notes(folder=folder)
    # Which workspace each document is in, so the browser can filter by it.
    # One query for all of them rather than one per document.
    try:
        homes = workspaces_mod.workspace_map(workspaces_mod.KIND_NOTE)
        names = {w["id"]: w.get("name") or "" for w in workspaces_mod.list_workspaces()}
    except Exception:
        homes, names = {}, {}
    for note in notes:
        home = homes.get(note.get("id"))
        note["workspace"] = home or ""
        note["workspace_name"] = names.get(home, "")
    return docs + notes


@app.get("/api/write/start")
async def write_start():
    """What the Write start screen offers, and what it should not.

    The LaTeX card exists only when the Academia pack is on. Offering a card
    that opens an editor whose validate and compile do nothing is worse than
    not offering it — the person finds out after they have started writing.
    When it is off, the card is still described so the screen can say what to
    switch on rather than silently having one fewer option.
    """
    latex_on = extensions_mod.is_enabled("academia")
    return {
        "cards": [
            {
                "id": "blank",
                "title": "Blank document",
                "subtitle": "",
                "format": notes_mod.FORMAT_MARKDOWN,
                "available": True,
            },
            # Subtitles are a qualifier, not a description. They sit on one
            # line under the title and are ellipsized, so a sentence here
            # becomes a card three lines taller than the one beside it and the
            # row stops reading as a row. What the thing is for belongs in the
            # card's `title` attribute, where it is available on hover without
            # costing every card the height.
            {
                "id": "latex",
                "title": "LaTeX",
                "subtitle": "Equations",
                "detail": "Paper, thesis, anything with equations",
                "format": notes_mod.FORMAT_LATEX,
                "available": latex_on,
                "requires": None if latex_on else {
                    "pack": "academia",
                    "label": "Academia Pack",
                    "detail": "Turn on the Academia Pack in Extensions for LaTeX "
                              "documents — it brings validation, compiling and "
                              "citation checking.",
                },
            },
            {
                "id": "canvas",
                "title": "Canvas",
                "subtitle": "Infinite surface",
                "detail": "An endless surface for boxes, notes and arrows",
                "format": notes_mod.FORMAT_CANVAS,
                "available": True,
            },
            {
                "id": "slides",
                "title": "Slides",
                "subtitle": "Presentation",
                "detail": "A deck you write in Markdown and present",
                "format": notes_mod.FORMAT_SLIDES,
                "available": True,
            },
        ],
    }


# ===== Work =====
#
# One listing for everything Work holds. Documents you wrote and files you
# pointed Carrot at are different things in the database and the same thing on
# this screen — something with a name, a kind and a date, that you are trying
# to find again. Merging them here rather than in the browser means the sort is
# done once, over the whole set, instead of over two lists that each look
# sorted and interleave wrongly.

def _work_preview(doc_format, body):
    """The first lines of a document, at a size nobody reads.

    You recognise your own writing by its shape — which only works if what is
    shown is writing. A canvas is JSON and a deck is JSON-adjacent markup, so
    for those the raw body is a tile of `{"type":"excalidraw","version":2,…`,
    which is not a preview of anything. A canvas shows nothing rather than
    showing its file format; a deck shows the words on its slides.
    """
    body = body or ""
    if doc_format == notes_mod.FORMAT_CANVAS:
        return ""
    if doc_format == notes_mod.FORMAT_SLIDES:
        try:
            deck = json.loads(body)
        except (ValueError, TypeError):
            return re.sub(r"^-{3,}$", " ", body, flags=re.MULTILINE)[:220]
        words = []
        for slide in (deck.get("slides") or []):
            for el in (slide.get("elements") or []):
                if el.get("type") == "text" and el.get("text"):
                    words.append(el["text"])
        return " · ".join(words)[:220]
    return body[:220]


def _work_haystack(doc_format, body):
    """Everything a search should look at, which is not what a tile shows.

    The preview is 220 characters because that is what fits; searching it would
    make a document findable by its opening paragraph and invisible by its
    fourth. A canvas previews as nothing — showing a tile of `{"type":
    "excalidraw"…` recognises nothing — but its labels are still words someone
    typed and still worth finding, so the search reads them even though the
    tile does not show them.
    """
    body = body or ""
    if doc_format == notes_mod.FORMAT_CANVAS:
        try:
            scene = json.loads(body)
        except (ValueError, TypeError):
            return ""
        return " ".join(
            el["text"] for el in (scene.get("elements") or [])
            if isinstance(el, dict) and el.get("text")
        )
    if doc_format == notes_mod.FORMAT_SLIDES:
        try:
            deck = json.loads(body)
        except (ValueError, TypeError):
            return body
        words = []
        for slide in (deck.get("slides") or []):
            for el in (slide.get("elements") or []):
                if el.get("text"):
                    words.append(el["text"])
            if slide.get("notes"):
                words.append(slide["notes"])
        return " ".join(words)
    return body


def _work_document_items(homes, names):
    for note in notes_mod.list_notes():
        home = homes.get(note.get("id"))
        doc_format = notes_mod.normalize_format(note.get("format"))
        body = note.get("body")
        name = note.get("title") or note.get("id")
        yield {
            "id": note.get("id"),
            "kind": "document",
            "name": name,
            "format": doc_format,
            "workspace": home or "",
            "workspace_name": names.get(home, ""),
            "updated": note.get("created_at") or 0,
            "path": note.get("path") or "",
            "preview": _work_preview(doc_format, body),
            # Stripped before the response goes out: it is the whole document,
            # and sending every body to draw a grid of tiles would make the
            # listing weigh what the vault weighs.
            "_haystack": (name + " " + _work_haystack(doc_format, body)).lower(),
        }


def _work_file_items(limit):
    """Indexed files. `updated` is normalised to epoch seconds to match the
    documents, because a list sorted on two different time formats is a list
    that is not sorted."""
    for doc in indexer_mod.list_documents(limit=limit):
        indexed = doc.get("indexed_at")
        if isinstance(indexed, str):
            try:
                indexed = datetime.fromisoformat(indexed).timestamp()
            except ValueError:
                indexed = 0
        path = doc.get("path") or ""
        name = os.path.basename(path) or path
        yield {
            "id": path,
            "kind": "file",
            "name": name,
            "format": (os.path.splitext(path)[1].lstrip(".") or "file").lower(),
            "workspace": "",
            "workspace_name": "",
            "updated": indexed or 0,
            "path": path,
            "preview": "",
            # The whole path, so "notes/2024" finds a file by where it lives.
            "_haystack": path.lower(),
        }


@app.get("/api/work/items")
async def work_items(workspace: str = "", kind: str = "", q: str = "", limit: int = 500):
    try:
        homes = workspaces_mod.workspace_map(workspaces_mod.KIND_NOTE)
        names = {w["id"]: w.get("name") or "" for w in workspaces_mod.list_workspaces()}
    except Exception:
        homes, names = {}, {}

    items = list(_work_document_items(homes, names))
    # A file is not filed in a workspace, so asking for one excludes them all
    # rather than showing every file under every workspace.
    partial = ""
    if not workspace:
        try:
            items.extend(_work_file_items(limit))
        except Exception:
            # Said out loud rather than swallowed. A listing that quietly drops
            # every file looks exactly like a listing with no files in it, and
            # the person reading it has no way to tell those apart.
            LOG.exception("work: could not list indexed files")
            partial = "Indexed files could not be read, so only documents are shown."

    if workspace:
        items = [i for i in items if i["workspace"] == workspace]
    if kind:
        items = [i for i in items if i["kind"] == kind or i["format"] == kind]
    if q:
        needle = q.lower()
        items = [i for i in items if needle in i["_haystack"]]

    items.sort(key=lambda i: i["updated"] or 0, reverse=True)
    total = len(items)
    shown = [{k: v for k, v in i.items() if k != "_haystack"} for i in items[:limit]]
    return {"items": shown, "total": total, "partial": partial}


class BulkDeleteRequest(BaseModel):
    ids: List[str]


@app.post("/api/work/delete")
async def work_delete(req: BulkDeleteRequest):
    """Delete several documents at once.

    One request rather than one per document, because the case this exists for
    is a hundred and sixty copies of the same note — and a hundred and sixty
    round trips is a progress bar nobody asked for and a half-finished delete
    if the window closes partway.

    System docs are refused individually rather than failing the whole call: a
    selection that happens to include Goals should delete everything else and
    say what it skipped.
    """
    deleted, skipped = [], []
    for note_id in req.ids:
        if note_id in systemdocs_mod.SYSTEM_IDS:
            skipped.append(note_id)
            continue
        try:
            if notes_mod.delete_note(note_id):
                deleted.append(note_id)
            else:
                skipped.append(note_id)
        except Exception:
            skipped.append(note_id)
    return {"deleted": len(deleted), "skipped": len(skipped), "ids": deleted}


@app.get("/api/work/places")
async def work_places():
    """The left rail: workspaces, with how much of Work is in each."""
    try:
        homes = workspaces_mod.workspace_map(workspaces_mod.KIND_NOTE)
        spaces = workspaces_mod.list_workspaces()
    except Exception:
        homes, spaces = {}, []
    counts = {}
    for home in homes.values():
        counts[home] = counts.get(home, 0) + 1
    return {
        "workspaces": [
            {"id": w["id"], "name": w.get("name") or "Untitled",
             "count": counts.get(w["id"], 0)}
            for w in spaces
        ],
    }


# ===== Links between documents =====

@app.get("/api/links/graph")
async def links_graph():
    return links_mod.graph()


@app.get("/api/links/backlinks/{note_id}")
async def links_backlinks(note_id: str):
    return links_mod.backlinks(note_id)


@app.get("/api/links/suggest")
async def links_suggest(q: str = "", limit: int = 8):
    return links_mod.suggest(q, limit=limit)


@app.get("/api/links/resolve")
async def links_resolve(title: str):
    """Where a `[[title]]` goes when clicked.

    A miss is not a 404. Linking to something unwritten is normal, and the
    caller's next move is to offer to create it — which needs the title back,
    spelled the way it was written.
    """
    note = links_mod.resolve(title)
    if note is None:
        return {"found": False, "title": title}
    return {"found": True, "id": note["id"], "title": note["title"],
            "format": note.get("format") or notes_mod.FORMAT_MARKDOWN}


@app.get("/api/notes/{note_id}")
async def get_note(note_id: str):
    system = systemdocs_mod.get(note_id)
    if system is not None:
        return system
    note = notes_mod.get_note(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


@app.post("/api/notes")
async def create_note(req: NoteRequest):
    return notes_mod.create_note(req.title, req.content, req.folder or None,
                                 doc_format=req.format or notes_mod.FORMAT_MARKDOWN)


@app.put("/api/notes/{note_id}")
async def update_note(note_id: str, req: NoteUpdateRequest):
    # Refused, not ignored. A save that appears to work and changes nothing is
    # how somebody loses an afternoon's edits — and the edit is meaningless
    # anyway, because this document is regenerated from the goals table every
    # time it is opened.
    if note_id in systemdocs_mod.SYSTEM_IDS:
        raise HTTPException(
            status_code=409,
            detail="This page is a view of your goals, not a file. Change a goal "
                   "by ticking a chip in a conversation or by asking Carrot.")
    result = notes_mod.update_note(note_id, req.content, title=req.title)
    if result is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return result


@app.delete("/api/notes/{note_id}")
async def delete_note(note_id: str):
    if note_id in systemdocs_mod.SYSTEM_IDS:
        raise HTTPException(status_code=409,
                            detail="This page is a view of your goals and cannot be deleted.")
    ok = notes_mod.delete_note(note_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Note not found")
    return {"status": "deleted"}


# ===== Config =====

@app.get("/api/config")
async def get_app_config():
    """The full config with secrets redacted.

    Secrets are reported as a boolean so the UI can show "configured" without
    the value ever leaving the process.
    """
    return config.redact(config.get_config())


@app.put("/api/config/{key}")
async def set_config_value(key: str, value: Any = Body(...)):
    """Set one config key.

    Secrets are refused here. They have dedicated endpoints that validate the
    name and never read the value back, and leaving a second write path open
    would mean a value could be stored in a shape the vault does not expect.
    """
    if key in config.SECRET_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"'{key}' holds secrets and cannot be set here — use its own endpoint",
        )
    config.set_config(key, value)
    return {"key": key, "value": value}


# ===== Artifacts =====
# Charts, diagrams and images the assistant made. The content is model-authored
# markup, so the API hands back a *document* to drop into a sandboxed iframe
# rather than a fragment to inline — see carrot/artifacts.py.

@app.get("/api/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str):
    artifact = artifacts_mod.get(artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="artifact not found")
    return {**artifact, "document": artifacts_mod.html_document(artifact)}


@app.get("/api/conversations/{conversation_id}/artifacts")
async def list_conversation_artifacts(conversation_id: str):
    items = artifacts_mod.for_conversation(conversation_id)
    # The list view does not need the payloads, and a conversation with fifty
    # charts in it would be megabytes of JSON.
    return {"artifacts": [{k: v for k, v in a.items() if k != "content"} for a in items]}


@app.delete("/api/artifacts/{artifact_id}")
async def delete_artifact(artifact_id: str):
    if not artifacts_mod.delete(artifact_id):
        raise HTTPException(status_code=404, detail="artifact not found")
    return {"status": "deleted"}


class ArtifactRequest(BaseModel):
    kind: str
    content: str = ""
    title: str = ""
    path: str = ""
    conversation_id: str = ""


@app.post("/api/artifacts")
async def create_artifact(req: ArtifactRequest):
    """Used by the UI to re-render an artifact in the current theme, and by
    tests. The model reaches artifacts through the show_artifact tool."""
    try:
        artifact = artifacts_mod.create(
            req.kind, req.content, title=req.title, path=req.path,
            conversation_id=req.conversation_id)
    except artifacts_mod.ArtifactError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {**artifact, "document": artifacts_mod.html_document(artifact)}


# ===== Widgets =====

@app.get("/api/widgets/catalog")
async def widget_catalog():
    return {"catalog": widgets_mod.list_catalog()}


@app.get("/api/system/meters")
async def system_meters():
    """Live CPU, memory and GPU load.

    `leaderboard.get_hardware_profile()` says what the machine *is*; this says
    what it is doing. They are different questions and only the second one
    explains why a turn is slow.
    """
    return sysmon_mod.meters()


@app.get("/api/system/throughput")
async def system_throughput():
    """How fast the local model is actually generating.

    Read from Ollama's own `eval_count`/`eval_duration` rather than timed from
    outside — an external stopwatch includes the queue, the prompt evaluation
    and the socket, and reports a figure well below what the model is
    producing.
    """
    return sysmon_mod.throughput.snapshot()


@app.get("/api/markets")
async def market_quotes(symbols: str = ""):
    wanted = [s for s in (symbols or "").split(",") if s.strip()]
    return markets_mod.quotes(wanted or None)


@app.get("/api/markets/catalogue")
async def market_catalogue():
    return {"catalogue": markets_mod.CATALOGUE,
            "default": markets_mod.DEFAULT_SYMBOLS}


@app.get("/api/news/headlines")
async def news_headlines(limit: int = 12):
    """Headlines for the dashboard, from the recap's own feeds.

    Deliberately the same feed list the morning recap uses: two separate
    notions of "your news sources" would be two things to configure and two
    places to be surprised by what turned up.
    """
    try:
        items = recap_mod.fetch_all_feeds()
    except Exception as exc:
        LOG.info("could not fetch headlines: %s", exc)
        return {"items": [], "error": str(exc)}
    seen, out = set(), []
    for item in items:
        link = item.get("link", "")
        title = (item.get("title") or "").strip()
        if not title or link in seen:
            continue
        seen.add(link)
        out.append({
            "title": title[:200],
            "url": link,
            "source": (item.get("source") or "")[:60],
            "published": item.get("published", ""),
            # The normalised one is what the widget renders. Feeds disagree
            # wildly about the raw string's format, and the browser's Date
            # parser returns "Invalid Date" for the least common of them —
            # which is how a headline list ends up with dates on some rows
            # and nothing on others.
            "published_iso": item.get("published_iso", ""),
        })
        if len(out) >= max(1, min(int(limit or 12), 40)):
            break
    return {"items": out, "error": ""}


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


# ===== Memory =====

@app.get("/api/memory")
async def list_memories(kind: str = None, status: str = "active", subject: str = None,
                        limit: int = 200, origin: str = None, workspace: str = "all"):
    """What Carrot remembers, filterable by what produced it and where it lives.

    ``workspace`` defaults to "all" rather than to the active one: this screen
    is the audit of everything Carrot believes about you, and an audit that
    silently hides two thirds of its subject is not an audit. Scoping is a
    thing you ask for here, not a mode you are already in.
    """
    return {
        "memories": memory_mod.list_memories(
            kind=kind, status=status or None, subject=subject, limit=limit,
            origin=origin or None,
            workspace_id=workspaces_mod.resolve_scope(workspace),
        ),
        "stats": memory_mod.stats(),
        # Two labels, because they read differently: `label` is the word on the
        # row's tag, `filter` is the whole dropdown line. "Learned in you" is
        # what one label for both produces.
        "origins": [
            {
                "id": origin_id,
                "label": memory_mod.ORIGIN_LABELS[origin_id],
                "filter": memory_mod.ORIGIN_FILTER_LABELS[origin_id],
            }
            for origin_id in memory_mod.ORIGINS
        ],
    }


@app.get("/api/memory/search")
async def search_memories(q: str, limit: int = 10, include_superseded: bool = False,
                          workspace: str = "all"):
    # Typing in the search box used to drop whatever scope was set, so a search
    # inside a workspace quietly answered from every workspace.
    return {"results": memory_mod.search(
        q, limit=limit, include_superseded=include_superseded,
        workspace_id=workspaces_mod.resolve_scope(workspace),
    )}


@app.get("/api/memory/history/{subject}")
async def memory_history(subject: str, kind: str = None):
    """Every version of a belief, oldest first."""
    return {"subject": subject, "versions": memory_mod.history(subject, kind=kind)}


@app.post("/api/memory")
async def create_memory(req: MemoryRequest):
    return memory_mod.create(
        kind=req.kind, subject=req.subject, content=req.content,
        confidence=req.confidence, pinned=bool(req.pinned),
        # Nothing extracted this — it arrived through the API because someone
        # typed it, and that is the strongest provenance there is.
        origin=memory_mod.ORIGIN_MANUAL,
    )


@app.put("/api/memory/{memory_id}")
async def update_memory(memory_id: str, req: MemoryUpdateRequest):
    updated = memory_mod.update(memory_id, **req.model_dump(exclude_none=True))
    if updated is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return updated


@app.post("/api/memory/{memory_id}/reject")
async def reject_memory(memory_id: str):
    """Mark a memory wrong. Its subject is excluded from future extraction."""
    rejected = memory_mod.reject(memory_id)
    if rejected is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return rejected


@app.delete("/api/memory/{memory_id}")
async def delete_memory(memory_id: str):
    if not memory_mod.delete(memory_id):
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"deleted": True}


@app.post("/api/memory/extract")
async def extract_memory(req: MemoryExtractRequest):
    """Run extraction over a turn on demand."""
    return {
        "extracted": memory_mod.extract_from_turn(
            user_text=req.user_text,
            assistant_text=req.assistant_text or "",
            conversation_id=req.conversation_id,
        )
    }


# ===== Conversation summaries =====

@app.get("/api/conversations/{conv_id}/summary")
async def conversation_summary(conv_id: str):
    return summarize_mod.get_summary(conv_id) or {"conversation_id": conv_id, "summary": ""}


@app.post("/api/conversations/{conv_id}/summarize")
async def summarize_conversation(conv_id: str):
    return summarize_mod.maybe_summarize(conv_id) or {"conversation_id": conv_id, "summary": ""}


# ===== Vectors =====

@app.get("/api/vectors/stats")
async def vector_stats():
    return vectors_mod.stats()


@app.post("/api/vectors/backfill")
async def vector_backfill(namespace: str = None, limit: int = 200):
    """Embed anything written while the embedding model was unavailable."""
    if namespace:
        return vectors_mod.backfill(namespace, limit=limit)
    return vectors_mod.backfill_all(limit_per_namespace=limit)


# ===== Document index =====

@app.get("/api/index/status")
async def index_status():
    return {**indexer_mod.scan_state(), "stats": indexer_mod.stats()}


@app.get("/api/index/documents")
async def index_documents(limit: int = 100, offset: int = 0):
    return {"documents": indexer_mod.list_documents(limit=limit, offset=offset)}


@app.post("/api/index/dirs")
async def add_index_dir(req: IndexDirRequest):
    try:
        return {"dirs": indexer_mod.add_index_dir(req.path)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/index/dirs")
async def remove_index_dir(path: str):
    return {"dirs": indexer_mod.remove_index_dir(path)}


@app.post("/api/index/scan")
async def start_index_scan(req: IndexScanRequest = IndexScanRequest()):
    if not indexer_mod.index_dirs():
        raise HTTPException(status_code=400, detail="No directories are configured for indexing")
    return indexer_mod.start_scan_async(force=bool(req.force))


@app.get("/api/index/search")
async def search_index(q: str, limit: int = 10, hybrid_weight: float = 0.5,
                       workspace: Optional[str] = None):
    return indexer_mod.search_documents(
        q, limit=limit, hybrid_weight=hybrid_weight,
        workspace_id=workspaces_mod.resolve_scope(workspace),
    )


@app.get("/api/search/all")
async def search_everything(q: str, limit: int = 10, workspace: Optional[str] = None):
    """One query across conversations, documents and memory, in one scope."""
    scope = workspaces_mod.resolve_scope(workspace)
    conversations = search.search_conversations(q, limit=limit, workspace_id=scope)
    return {
        "query": q,
        "workspace_id": scope,
        "conversations": conversations["results"],
        "documents": indexer_mod.search_documents(q, limit=limit, workspace_id=scope)["results"],
        "memories": memory_mod.search(q, limit=limit, workspace_id=scope),
    }


# ===== Agent tools =====

@app.get("/api/agent/tools")
async def list_agent_tools():
    return {
        "tools": [
            {"name": name, "mutating": spec["mutating"], "risk": spec["risk"],
             "description": spec["description"]}
            for name, spec in agent_mod.TOOLS.items()
        ],
        "enabled": config.get_config().get("agent_tools_enabled", True),
        "require_approval": config.get_config().get("agent_require_approval", True),
    }


@app.get("/api/agent/approvals")
async def list_approvals():
    return {"pending": agent_mod.pending_approvals()}


@app.post("/api/agent/approvals/{approval_id}")
async def resolve_approval(approval_id: str, req: ApprovalRequest):
    """Answer a pending prompt.

    ``remember`` is honoured only for prompts the policy marked rememberable,
    and an allow on a prompt carrying a confirmation phrase becomes a deny
    unless the phrase was typed back — both enforced in ``agent_tools``, not
    here, so every caller gets the same treatment.
    """
    if not agent_mod.resolve_approval(
        approval_id, req.decision,
        remember=bool(req.remember),
        confirmation=req.confirmation or "",
    ):
        raise HTTPException(status_code=404, detail="No such pending approval")
    return {"resolved": True, "decision": req.decision}


@app.get("/api/agent/journal")
async def agent_journal(limit: int = 50):
    """Agent file edits, newest first, each with its diff."""
    return {"entries": agent_mod.list_journal(limit=limit)}


@app.post("/api/agent/journal/{entry_id}/revert")
async def revert_journal(entry_id: str):
    result = agent_mod.revert_journal_entry(entry_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# ===== Workspaces and folders =====

@app.get("/api/workspaces")
async def workspace_tree(include_archived: bool = False):
    """Folders, workspaces and which one is active — the sidebar's whole state."""
    return workspaces_mod.tree(include_archived=include_archived)


@app.get("/api/workspaces/status")
async def workspace_status():
    return workspaces_mod.status()


@app.post("/api/workspaces/active")
async def set_active_workspace(req: ActiveWorkspaceRequest):
    """Switch context. An empty id means all workspaces."""
    try:
        return {"active": workspaces_mod.set_active_workspace(req.workspace_id or "")}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/workspaces")
async def create_workspace(req: WorkspaceRequest):
    try:
        return workspaces_mod.create_workspace(
            name=req.name, description=req.description or "",
            folder_id=req.folder_id or None, color=req.color or "",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/workspaces/{workspace_id}")
async def get_workspace(workspace_id: str):
    workspace = workspaces_mod.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="No such workspace")
    return workspace


@app.get("/api/workspaces/{workspace_id}/contents")
async def workspace_contents(workspace_id: str, limit: int = 50):
    """What is filed here, resolved to titles."""
    contents = workspaces_mod.contents(workspace_id, limit=limit)
    if not contents:
        raise HTTPException(status_code=404, detail="No such workspace")
    return contents


@app.patch("/api/workspaces/{workspace_id}")
async def update_workspace(workspace_id: str, req: WorkspaceRequest):
    try:
        workspace = workspaces_mod.update_workspace(
            workspace_id, name=req.name, description=req.description,
            folder_id=req.folder_id, color=req.color, archived=req.archived,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if workspace is None:
        raise HTTPException(status_code=404, detail="No such workspace")
    return workspace


@app.delete("/api/workspaces/{workspace_id}")
async def delete_workspace(workspace_id: str):
    """Delete the grouping. Its chats, memories and files are unfiled, not deleted."""
    if not workspaces_mod.delete_workspace(workspace_id):
        raise HTTPException(status_code=404, detail="No such workspace")
    return {"deleted": True}


@app.post("/api/workspaces/{workspace_id}/items")
async def file_workspace_items(workspace_id: str, req: WorkspaceItemsRequest):
    """File items here. An empty workspace id in the path is not accepted; use DELETE."""
    if workspaces_mod.get_workspace(workspace_id) is None:
        raise HTTPException(status_code=404, detail="No such workspace")
    filed = workspaces_mod.move_items(workspace_id, [i.model_dump() for i in req.items])
    return {"filed": filed, "counts": workspaces_mod.item_counts(workspace_id)}


@app.delete("/api/workspaces/items/{kind}/{item_id}")
async def unfile_workspace_item(kind: str, item_id: str):
    return {"unfiled": workspaces_mod.unfile_item(kind, item_id)}


@app.post("/api/folders")
async def create_folder(req: FolderNodeRequest):
    try:
        return workspaces_mod.create_folder(req.name, req.parent_id or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.patch("/api/folders/{folder_id}")
async def update_folder(folder_id: str, req: FolderNodeRequest):
    try:
        folder = workspaces_mod.update_folder(folder_id, name=req.name, parent_id=req.parent_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if folder is None:
        raise HTTPException(status_code=404, detail="No such folder")
    return folder


@app.delete("/api/folders/{folder_id}")
async def delete_workspace_folder(folder_id: str):
    """Delete a folder. Its workspaces move to the top level rather than vanishing."""
    if not workspaces_mod.delete_folder(folder_id):
        raise HTTPException(status_code=404, detail="No such folder")
    return {"deleted": True}


# ===== Help and tutorial =====

@app.get("/api/help")
async def help_index(q: str = ""):
    """Every help topic, or the ones matching a query."""
    return {
        "topics": help_mod.search_topics(q) if q else help_mod.topics(),
        "sections": help_mod.SECTIONS,
        "query": q,
    }


@app.get("/api/help/tutorial")
async def help_tutorial():
    """Getting-started steps, each checked against the live install."""
    return help_mod.tutorial()


@app.get("/api/help/{topic_id}")
async def help_topic(topic_id: str):
    topic = help_mod.get_topic(topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="No such help topic")
    return topic


# ===== Carrot Research =====

def _sse(generator):
    """Wrap a trace generator as an SSE response.

    Every research and agent event goes out the same way, so the UI has one
    parser for both and a new event type never needs a transport change.

    And one exception handler, for the same reason chat needed one: the status
    line and the headers are already on the wire by the time this runs, so a
    throw here is not an error response — it is a socket that closes. A deep
    research run that dies on its ninth page fetch would end with a spinner and
    no explanation. It ends with the reason instead.

    Sync for the same reason as the chat stream: the generators handed to this
    are synchronous, and running them on the event loop starves every other
    request for the length of a research run.
    """
    def stream():
        try:
            for event in generator:
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:
            LOG.exception("streamed run failed")
            yield f"data: {json.dumps({'type': 'error', 'error': str(exc)})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'error': str(exc)})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/research/depths")
async def research_depths():
    """Which depths the current research route can sustain."""
    return research_mod.available_depths()


@app.get("/api/research")
async def list_research_runs(limit: int = 30):
    return {"runs": research_mod.list_runs(limit=limit)}


@app.get("/api/research/{run_id}")
async def get_research_run(run_id: str):
    run = research_mod.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No such research run")
    return run


@app.delete("/api/research/{run_id}")
async def delete_research_run(run_id: str):
    if not research_mod.delete_run(run_id):
        raise HTTPException(status_code=404, detail="No such research run")
    return {"deleted": True}


@app.post("/api/research/run")
async def start_research(req: ResearchRequest):
    """Run the research pipeline, streaming its trace."""
    if not (req.question or "").strip():
        raise HTTPException(status_code=400, detail="A research question is required")
    depth = req.depth or config.get_config().get("research_default_depth", research_mod.DEFAULT_DEPTH)
    return _sse(research_mod.run_research_stream(
        req.question, depth=depth, conversation_id=req.conversation_id,
    ))


@app.post("/api/research/{run_id}/cancel")
async def cancel_research(run_id: str):
    return {"cancelled": policy_mod.cancel_run(run_id)}


# ===== Carrot Agent =====

@app.get("/api/agent/status")
async def agent_status():
    """What the agent can reach right now, and under which limits."""
    return carrot_agent.status()


@app.get("/api/agent/runs")
async def list_agent_runs(limit: int = 30):
    return {"runs": carrot_agent.list_runs(limit=limit)}


@app.get("/api/agent/runs/{run_id}")
async def get_agent_run(run_id: str):
    """A run with its full audit trail: every action, decision, and result."""
    run = carrot_agent.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No such agent run")
    return run


@app.post("/api/agent/run")
async def start_agent_run(req: AgentRunRequest):
    if not (req.task or "").strip():
        raise HTTPException(status_code=400, detail="A task is required")
    overrides = {}
    if req.max_steps:
        overrides["max_steps"] = int(req.max_steps)
    if req.max_seconds:
        overrides["max_seconds"] = int(req.max_seconds)
    return _sse(carrot_agent.run_agent_stream(
        req.task,
        surface=req.surface or carrot_agent.SURFACE_BROWSER,
        conversation_id=req.conversation_id,
        budget_overrides=overrides,
        require_plan_approval=req.require_plan_approval,
    ))


@app.post("/api/agent/runs/{run_id}/stop")
async def stop_agent_run(run_id: str):
    """The kill switch. Takes effect before the run's next action."""
    return {"stopped": policy_mod.cancel_run(run_id)}


# ===== Agent policy: what Carrot is allowed to touch =====

@app.get("/api/policy")
async def get_policy():
    return policy_mod.status()


@app.post("/api/policy/domains")
async def add_allowed_domain(req: DomainRequest):
    try:
        return {"allowed_domains": policy_mod.allow_domain(req.domain)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/policy/domains/{domain}")
async def remove_allowed_domain(domain: str):
    return {"allowed_domains": policy_mod.revoke_domain(domain)}


@app.get("/api/policy/secrets")
async def list_secrets():
    """Names only. There is no endpoint that returns a stored value."""
    return {"secrets": policy_mod.secret_names()}


@app.post("/api/policy/secrets")
async def store_secret(req: SecretRequest):
    try:
        return {"secrets": policy_mod.set_secret(req.name, req.value)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/policy/secrets/{name}")
async def remove_secret(name: str):
    return {"secrets": policy_mod.delete_secret(name)}


@app.get("/api/policy/check-url")
async def check_url(url: str):
    """What the kernel would say about a URL — used by the UI to explain a block."""
    return policy_mod.check_url(url).as_dict()


# ===== Model routing =====

@app.get("/api/router/status")
async def router_status():
    return router_mod.status()


@app.put("/api/router/route")
async def set_router_route(req: RouteRequest):
    """Pin a task to a provider and model."""
    try:
        routes = router_mod.set_route(req.task, req.model, provider=req.provider, effort=req.effort)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"routes": routes, "route": router_mod.route(req.task).as_dict()}


@app.delete("/api/router/route/{task}")
async def clear_router_route(task: str):
    """Drop an assignment so the task falls back to the automatic rules."""
    return {"routes": router_mod.clear_route(task), "route": router_mod.route(task).as_dict()}


@app.get("/api/router/recommendation")
async def router_recommendation():
    """A local model sized to this machine's memory."""
    return router_mod.recommend_local_model()


# ===== Optional components =====
#
# The parts of Carrot that arrive as an optional package. The app is already
# running in the interpreter that needs them, so it can put them there — which
# is the whole point: `pip install carrot[browser]` followed by
# `python -m playwright install chromium` is a fine instruction for somebody
# with a terminal open and the end of the road for everybody else.

@app.get("/api/components")
async def list_components():
    return {"components": components_mod.status()}


@app.post("/api/components/{component_id}/install")
async def install_component(component_id: str):
    """Start an install and return at once; the row polls for progress.

    Not synchronous — a few hundred megabytes is minutes, and a request held
    open that long is one a browser or proxy abandons, leaving the install
    running and the screen convinced it failed.
    """
    result = components_mod.install(component_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "unknown component"))
    return result


# ===== Extension packs =====

@app.get("/api/extensions")
async def list_extensions():
    return {"extensions": extensions_mod.list_packs()}


@app.get("/api/extensions/catalog")
async def extension_catalog():
    """Everything on the shelf, with whether it has been added.

    Separate from /api/extensions, which answers "what is in my app". The two
    questions are different and were the same list, so the page read as a
    settings panel with a switch per pack rather than a shelf you take things
    off.
    """
    return {"extensions": extensions_mod.catalog()}


@app.post("/api/extensions/{pack_id}/install")
async def install_extension(pack_id: str):
    """Add a pack to this installation. It arrives switched off."""
    try:
        return extensions_mod.install(pack_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.delete("/api/extensions/{pack_id}/install")
async def uninstall_extension(pack_id: str):
    try:
        return extensions_mod.uninstall(pack_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/extensions/tabs")
async def extension_tabs():
    """Which nav tabs belong to a pack, and which of those are switched on.

    Both halves are needed. The page ships the full nav markup, so to hide a
    tab whose pack is off it has to know which tabs are pack-managed at all —
    otherwise one belonging to a disabled pack is indistinguishable from an
    ordinary tab and simply stays visible.
    """
    return extensions_mod.pack_tabs()


@app.get("/api/extensions/{pack_id}")
async def get_extension(pack_id: str):
    """One pack in full: tools, skills, probed capabilities and settings."""
    try:
        return extensions_mod.require_pack(pack_id).as_dict(deep=True)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.put("/api/extensions/{pack_id}/enabled")
async def set_extension_enabled(pack_id: str, req: ProviderEnabledRequest):
    """Turn a pack on or off. Enabling installs its skills; disabling removes them."""
    # 404 means there is no such pack. A pack that exists and has not been
    # added is a different answer and needs a different code, or the client
    # cannot tell "you have a typo" from "press Add first".
    if extensions_mod.get_pack(pack_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown extension: {pack_id}")
    try:
        return extensions_mod.set_enabled(pack_id, req.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.put("/api/extensions/{pack_id}/settings/{key}")
async def set_extension_setting(pack_id: str, key: str, value: Any = Body(...)):
    try:
        return {"settings": extensions_mod.set_pack_setting(pack_id, key, value)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ===== LaTeX workbench =====

@app.post("/api/latex/analyze")
async def latex_analyze(req: LatexRequest):
    """Validation, outline and math blocks — everything that needs no TeX engine."""
    from carrot.packs.academia import latex as latex_mod

    return {
        **latex_mod.validate(req.source),
        "outline": latex_mod.outline(req.source),
        "math": latex_mod.math_blocks(req.source),
        "engine": latex_mod.available_engine(),
    }


@app.post("/api/latex/compile")
async def latex_compile(req: LatexRequest):
    from carrot.packs.academia import latex as latex_mod

    try:
        return latex_mod.compile_document(req.source, req.out_path or "document.pdf")
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/latex/bibliography")
async def latex_bibliography(req: BibliographyRequest):
    from carrot.packs.academia import bibliography as bib_mod

    return bib_mod.check(req.bib, req.tex)


# ===== Doc to agent =====

@app.get("/api/doc/destinations")
async def doc_destinations():
    """The places a note can be sent, for the picker beside the Send button."""
    return {
        "destinations": [
            {"id": name, **{k: v for k, v in spec.items()}}
            for name, spec in doc_agent.DESTINATIONS.items()
        ]
    }


@app.get("/api/doc/candidates")
async def doc_candidates(kind: str = "file", q: str = "", provider: str = "", limit: int = 40):
    """Completions for the editor's '@' menu.

    ``model`` needs a provider first — the list is whatever that provider
    reports for the key on file, not a hardcoded set.
    """
    if kind == "to":
        return {"kind": "to", "candidates": doc_agent.destination_candidates(q)}
    if kind == "provider":
        return {"kind": "provider", "candidates": doc_agent.provider_candidates(q)}
    if kind == "model":
        if not provider:
            return {"kind": "provider", "candidates": doc_agent.provider_candidates(q)}
        return {"kind": "model", "provider": provider,
                "candidates": doc_agent.model_candidates(provider, q)}
    return {"kind": "file", "candidates": doc_agent.file_candidates(q, limit=limit)}


@app.post("/api/doc/parse")
async def doc_parse(req: DocSendRequest):
    """Resolve a document's references without sending it.

    This is what the editor shows under the note: which files will be attached,
    which model will serve it, and what could not be resolved.
    """
    resolved = doc_agent.resolve(req.text, task=req.task or router_mod.TASK_CHAT)
    return resolved.as_dict()


@app.post("/api/doc/send")
async def doc_send(req: DocSendRequest):
    """Send a note to wherever it says it goes.

    Three destinations, one document format. Chat reuses the ordinary turn: the
    note's text becomes the user message, its cited files a system context
    block, and memory, tools, summaries and approvals behave exactly as they do
    in chat. Research and Agent take the same note and hand it to their own
    pipeline — the citations follow it either way, as evidence or as background.

    An explicit ``destination`` on the request wins over the note's own
    ``@/to``: the picker in the UI is an override, not a second source of truth.
    """
    if not (req.text or "").strip():
        raise HTTPException(status_code=400, detail="Nothing to send")

    resolved = doc_agent.resolve(req.text, task=req.task or router_mod.TASK_CHAT)
    destination = (req.destination or resolved.destination or doc_agent.DESTINATION_CHAT).lower()
    if destination not in doc_agent.DESTINATIONS:
        raise HTTPException(status_code=400, detail=f"Unknown destination '{destination}'")
    option = req.option or (
        resolved.option if destination == resolved.destination
        else doc_agent.DESTINATIONS[destination]["default"]
    )

    if destination == doc_agent.DESTINATION_RESEARCH:
        # The note is the question. Its citations become numbered evidence, so
        # a claim drawn from a paper the user supplied is verified against that
        # paper's text rather than taken on trust.
        return _sse(research_mod.run_research_stream(
            resolved.prompt,
            depth=option or research_mod.DEFAULT_DEPTH,
            conversation_id=req.conversation_id,
            seed_sources=resolved.seed_sources(),
        ))

    if destination == doc_agent.DESTINATION_AGENT:
        # The note is the task. Citations are background rather than evidence —
        # the agent's evidence is the page in front of it.
        task_text = resolved.prompt
        if resolved.context:
            task_text += "\n\n" + resolved.context
        return _sse(carrot_agent.run_agent_stream(
            task_text,
            surface=option or carrot_agent.SURFACE_BROWSER,
            conversation_id=req.conversation_id,
        ))

    chat_req = ChatRequest(
        message=resolved.prompt,
        conversation_id=req.conversation_id,
        task=req.task,
        skill=req.skill,
        stream=True,
    )
    if chat_req.conversation_id is None:
        title = (req.title or resolved.prompt[:80]).strip() or "Untitled note"
        chat_req.conversation_id = conv_mod.create_conversation(title=title)["id"]
    conv = conv_mod.get_conversation(chat_req.conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    route = resolved.route
    if route is None:
        route = _resolve_chat_route(chat_req)
    elif route.local and not ollama_mod.OllamaClient().is_available():
        raise HTTPException(status_code=503, detail="Ollama is not available")

    mode = search_mode(req.search_mode)
    history, skill = _prepare_history(
        conv, resolved.prompt, chat_req.skill,
        extra_system=resolved.context or None, mode=mode,
    )
    conv_mod.add_message(chat_req.conversation_id, "user", resolved.prompt)
    return _chat_stream_response(
        chat_req, conv, history, skill, route, prelude=resolved.as_dict(), mode=mode,
        origin=memory_mod.ORIGIN_DOCUMENT,
    )


# ===== Providers (BYOK) =====

@app.get("/api/router/providers")
async def list_providers():
    """Every configured provider. Keys are reported as booleans, never values."""
    return {
        "providers": providers_mod.list_providers(),
        "presets": providers_mod.PRESETS,
        "kinds": list(providers_mod.KINDS),
    }


@app.post("/api/router/providers")
async def upsert_provider(req: ProviderRequest):
    """Add or update a provider. An ``api_key`` in the body is stored separately."""
    try:
        provider = providers_mod.upsert_provider(
            req.id, label=req.label, kind=req.kind, base_url=req.base_url,
            models=req.models, env_var=req.env_var,
        )
        if req.api_key is not None and req.api_key.strip():
            providers_mod.set_api_key(provider["id"], req.api_key.strip())
            provider = providers_mod.require_provider(provider["id"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return provider


@app.delete("/api/router/providers/{provider_id}")
async def delete_provider(provider_id: str):
    try:
        if not providers_mod.delete_provider(provider_id):
            raise HTTPException(status_code=404, detail="Provider not found")
    except providers_mod.ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"deleted": provider_id}


@app.put("/api/router/providers/{provider_id}/key")
async def set_provider_key(provider_id: str, req: ProviderKeyRequest):
    """Store or clear a provider's key. An empty string forgets it."""
    try:
        providers_mod.require_provider(provider_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    providers_mod.set_api_key(provider_id, req.api_key.strip())
    return providers_mod.require_provider(provider_id)


@app.put("/api/router/providers/{provider_id}/enabled")
async def set_provider_enabled(provider_id: str, req: ProviderEnabledRequest):
    try:
        return providers_mod.set_enabled(provider_id, req.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/router/providers/{provider_id}/models")
async def provider_models(provider_id: str):
    """Ask the provider what it serves — Carrot never hardcodes model names."""
    try:
        return providers_mod.list_models(provider_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/router/providers/{provider_id}/test")
async def test_provider(provider_id: str):
    """Probe a provider so a bad key surfaces here rather than mid-chat."""
    try:
        return providers_mod.test_provider(provider_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ===== Tasks =====

@app.get("/api/router/tasks")
async def list_tasks():
    return {"tasks": router_mod.tasks(), "assignments": router_mod.assignments()}


@app.post("/api/router/tasks")
async def create_task(req: TaskRequest):
    """Define a custom routing target, callable as ``task=<id>``."""
    try:
        return router_mod.add_task(
            req.id, label=req.label, description=req.description, local_only=req.local_only
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/router/tasks/{task_id}")
async def delete_task(task_id: str):
    try:
        if not router_mod.delete_task(task_id):
            raise HTTPException(status_code=404, detail="Task not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"deleted": task_id}


# ===== Notifications =====

@app.get("/api/notifications")
async def list_notifications(unread_only: bool = False, limit: int = 50):
    return {
        "notifications": proactive_mod.list_notifications(unread_only=unread_only, limit=limit),
        "unread": proactive_mod.unread_count(),
    }


@app.post("/api/notifications/check")
async def run_notification_checks():
    return proactive_mod.run_checks()


@app.post("/api/notifications/read-all")
async def read_all_notifications():
    return {"marked": proactive_mod.mark_all_read()}


@app.post("/api/notifications/{notification_id}/read")
async def read_notification(notification_id: str):
    if not proactive_mod.mark_read(notification_id):
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"read": True}


@app.delete("/api/notifications/{notification_id}")
async def dismiss_notification(notification_id: str):
    if not proactive_mod.dismiss(notification_id):
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"dismissed": True}


@app.get("/api/notifications/stream")
async def stream_notifications():
    """SSE feed of notifications as they are raised."""
    def stream():
        try:
            for notification in proactive_mod.stream_new():
                yield f"data: {json.dumps(notification)}\n\n"
        except Exception as exc:
            # A crash here used to end the feed silently, and notifications
            # simply stopped arriving with nothing to say they had.
            LOG.exception("notification stream failed")
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# ===== Security =====

@app.get("/api/security/status")
async def security_status():
    return security_mod.status()


@app.post("/api/security/rotate-token")
async def rotate_session_token():
    """Invalidate every existing client. The caller must reload to get the new token."""
    return {"token": security_mod.rotate_token()}


# ===== Backup =====

@app.get("/api/backup")
async def list_backups():
    return {"backups": backup_mod.list_backups()}


@app.post("/api/backup/export")
async def export_backup(req: BackupExportRequest = BackupExportRequest()):
    try:
        return backup_mod.export_archive(
            destination=req.path, include_vectors=bool(req.include_vectors)
        )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Export failed: {exc}")


@app.post("/api/backup/inspect")
async def inspect_backup(req: BackupImportRequest):
    return backup_mod.inspect_archive(req.path)


@app.post("/api/backup/import")
async def import_backup(req: BackupImportRequest):
    """Replace this instance's data with an archive. A safety copy is taken first."""
    result = backup_mod.import_archive(req.path, safety_copy=bool(req.safety_copy))
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result.get("error", "Import failed"))
    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8181)
