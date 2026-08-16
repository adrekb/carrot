# Carrot 🥕

**A local AI operating system.** Not a chat app with plugins — a kernel that
holds your memory, your files, your policy and your model routing, with a small
shell on top and everything else as a pack.

It runs on your machine. Your conversations, notes, documents and keys live in a
SQLite file you own. With no network at all, it still works.

- **A memory, not a chat log.** Structured beliefs about you — preferences,
  decisions, projects, commitments — each traceable to the message it came from,
  each editable and deletable.
- **Your files in the same search as your chats.** A local index over PDFs,
  markdown, code and saved pages.
- **It can act, and it can be stopped.** Agent tools and a real browser, behind
  a policy kernel that answers allow / ask / refuse and never asks the model
  what it thinks.
- **Your hardware, or your key.** Ollama on-device by default; bring an
  Anthropic, OpenAI, Google or OpenAI-compatible key and pin any task to any
  model.

- **It follows you out.** `carrot mcp` serves that memory over MCP, so Cursor,
  VS Code or Claude Desktop can ask Carrot what you decided three months ago
  without you leaving them. No plugin on the other side.

Everything Carrot does, in detail — including what is off by default and what
does less than its name suggests — is in **[FEATURES.md](FEATURES.md)**.

---

## Running it

**Prerequisites:** Python 3.10+. Ollama is *not* required — Carrot installs it
on first launch and pulls a model sized to your machine.

```bash
pip install -e .
carrot start
```

Then open <http://127.0.0.1:8181>.

As a desktop app (needs Node.js 18+):

```bash
cd gui && npm install && npm start
```

To build the one-click installer for your platform — `build.bat` on Windows,
`./build.sh` on macOS and Linux. Output lands in `gui/dist/`.

Optional extras are opt-in and named for what they unlock. Settings → Add-ons
installs any of them with one click, into the same interpreter that will do the
importing; `pip install -e ".[cloud,vectors,browser,speech,charts]"` is the same
thing from a terminal.

### Tests

```bash
pip install -e ".[dev]"
pytest
```

The suite runs with no Ollama, no network and no API keys, and
[CI](.github/workflows/test.yml) enforces that on Linux and Windows.

---

## Threat model

Read this before pointing Carrot at anything you care about.

**What Carrot protects you from.**

- *The network.* Carrot binds `127.0.0.1` and every `/api` call needs a
  per-install session token injected into the app's own HTML. Loopback keeps it
  off the network; the token keeps other pages in your browser from reaching it.
- *A hostile web page.* Page text the agent reads is enveloped and screened. A
  run that reads flagged content is **tainted**: it loses its remembered
  approvals, every later action is confirmed one at a time, and you are shown
  the text that did it. URLs resolving to loopback, private ranges or
  link-local are refused, and every redirect hop is re-checked.
- *The model talking its way past a rule.* The policy kernel
  (`carrot/policy.py`) decides, not the model. Irreversible actions always ask
  and are never offered "don't ask again" — the server refuses to record one
  even if a client sends it. Purchases, transfers and account deletions are
  refused outright unless enabled, and then need a typed `CONFIRM`.
- *Your credentials reaching the model.* Secrets live in a local vault keyed by
  name. The agent asks to type `secret:canvas`; the value is substituted at the
  keyboard layer and appears in no transcript, no audit row and no screenshot.
  Typing one into a site not on your allowed list is refused.
- *A runaway agent.* Steps, wall-clock time, navigations and distinct domains
  are all capped, with a kill switch that lands before the next action. Every
  file write is journaled and revertible with its diff.
- *Losing the record.* `agent_steps` holds every action proposed, what the
  policy decided and why, and what came back — secrets stripped before the row
  is written.

**What it does not protect you from.**

- *Anyone with your user account.* The database is not encrypted and the
  session token sits in your home directory. Carrot's boundary is the OS user,
  not the disk. Full-disk encryption is your job.
- *A model you route to.* Send a task to a hosted provider and that prompt
  leaves your machine under their terms. Escalation is off by default; what
  goes where is visible per turn in the UI and in `carrot route`.
- *Code you tell it to run.* `run_command` and the terminal are real shells.
  Destructive patterns are screened and working-directory containment is
  available, but neither is a sandbox. Do not run Carrot as an administrator.
- *A malicious pack or MCP server.* Packs ship with Carrot and are code you can
  read; MCP servers you add are code you chose to trust. Their tools go through
  the same approval gate, which limits blast radius but does not audit intent.
- *Prompt injection, completely.* Screening and tainting reduce what an
  injected instruction can achieve; they do not make the model immune to being
  lied to. The approval gate is the real backstop, which is why the
  irreversible actions cannot be waved through.
- *Two things Carrot refuses regardless of settings.* Working around a CAPTCHA
  or human-verification step, and downloading an executable.

What the agent may do at all is set in Settings, never by a prompt and never by
the agent itself. Defaults: no allowed sites, no stored credentials, desktop
control **off**, high-consequence actions **off**, no launchable apps, and
budgets of 40 steps / 15 minutes / 30 navigations / 10 sites per run.

---

## Talking to it from elsewhere

Over HTTP, gated by a per-install session token:

```bash
curl -H "X-Carrot-Token: $(carrot token)" http://127.0.0.1:8181/api/status
```

Or from any editor that speaks MCP — `carrot mcp-config` prints the block to
paste into `~/.cursor/mcp.json` or `claude_desktop_config.json`:

```bash
carrot mcp-config
```

That exposes five read-only tools: `search_memory`, `search_documents`,
`search_conversations`, `list_goals`, `list_reminders`. Read-only is
structural, not a setting — a stdio pipe has nowhere to show an approval
prompt, so nothing that would need one is offered.

## The name

The carrot and the rabbit — your rabbit assistant keeps you organised, and the
carrot is the reward at the end of the work.

## Licence

MIT — see [LICENSE](LICENSE). Third-party bundles in `carrot/web/vendor/` keep
their own; they are listed in [NOTICE.md](NOTICE.md).
