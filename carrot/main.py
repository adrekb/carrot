#!/usr/bin/env python3
import sys
import os
import json
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from carrot.config import get_config, set_config, DEFAULTS
from carrot.database import init_db


def main():
    if len(sys.argv) < 2:
        print("Usage: carrot <command> [options]")
        print("Commands:")
        print("  start           Start the Carrot server and open the web UI")
        print("  terminal        Open the Carrot terminal")
        print("  search <query>  Search conversations")
        print("  recap           Run a daily recap")
        print("  scan            Scan computer for assignments")
        print("  goals           List goals")
        print("  reminders       List reminders")
        print("  notes <folder>  List notes in a folder")
        print("  memory [query]  Show or search what Carrot remembers about you")
        print("  index <dir>     Add a folder to the local document index")
        print("  index-scan      Scan indexed folders for new or changed files")
        print("  find <query>    Search files, conversations and memory at once")
        print("  backup [path]   Export everything to an archive")
        print("  restore <path>  Restore from an archive (replaces current data)")
        print("  route           Show which model serves each task")
        print("  token           Print the API session token")
        print("  status          Show system status")
        return

    command = sys.argv[1]

    if command == "start":
        init_db()
        config = get_config()
        port = config.get("server_port", 8181)
        host = config.get("server_host", "127.0.0.1")
        url = f"http://{host}:{port}"
        print(f"Carrot server starting on {url}")
        print(f"Opening browser at {url}")
        print(f"Press Ctrl+C to stop")
        import webbrowser
        webbrowser.open(url)
        # `sys.executable`, not "python": inside a virtualenv the bare name
        # resolves against PATH, which may be a different interpreter or none
        # at all — and `os.system` threw the child's exit code away, so a
        # server that failed to start looked like one that had stopped
        # normally. Same interpreter, and the code comes back.
        raise SystemExit(subprocess.call([sys.executable, "-m", "carrot.app"]))

    elif command == "terminal":
        init_db()
        print("Carrot Terminal - Type 'exit' to quit")
        print("Ollama-powered coding assistant")
        while True:
            try:
                code = input("carrot> ")
                if code.strip().lower() in ("exit", "quit"):
                    break
                if code.strip().lower() == "recap":
                    from carrot.recap import run_recap
                    result = run_recap()
                    print(json.dumps(result, indent=2))
                    continue
                if code.strip().lower().startswith("search "):
                    query = code.strip()[7:]
                    from carrot.search import search_conversations
                    result = search_conversations(query)
                    for r in result.get("results", [])[:10]:
                        print(f"[{r['timestamp']}] ({r['role']}) {r['content'][:200]}")
                    continue
                if not code.strip():
                    continue
                from carrot.ollama_client import OllamaClient
                client = OllamaClient()
                if not client.is_available():
                    print("Error: Ollama is not running")
                    continue
                response = client.generate(code, system="You are Carrot, a helpful coding assistant. Execute the user's request and provide the result.")
                print(response)
            except KeyboardInterrupt:
                print("\nGoodbye!")
                break

    elif command == "search":
        if len(sys.argv) < 3:
            print("Usage: carrot search <query>")
            print("Example: carrot search '5 months ago bench press stats'")
            return
        query = " ".join(sys.argv[2:])
        from carrot.search import search_conversations
        result = search_conversations(query)
        print(f"Query: {result['query']}")
        if result.get("time_range"):
            print(f"Time range: {result['time_range']['start']} to {result['time_range']['end']}")
        print(f"Found {result['count']} results:\n")
        for r in result.get("results", [])[:20]:
            print(f"  [{r['timestamp']}] [{r['role']}] {r['conversation_title']}")
            print(f"    {r['content'][:300]}")
            print()

    elif command == "recap":
        init_db()
        from carrot.recap import run_recap
        result = run_recap()
        print(json.dumps(result, indent=2))

    elif command == "scan":
        init_db()
        from carrot.computer_use import index_computer_use
        count = index_computer_use()
        print(f"Indexed {count} files")

    elif command == "goals":
        init_db()
        from carrot.goals import list_goals
        goals = list_goals()
        for g in goals:
            status = "  " if not g.get("metadata", {}).get("data_points") else "●"
            print(f"{status} {g['title']} ({g['category']})")

    elif command == "reminders":
        init_db()
        from carrot.reminders import list_reminders, get_overdue_reminders, get_reminders_today
        print("Today's reminders:")
        for r in get_reminders_today():
            done = "✓" if r["completed"] else " "
            print(f"  [{done}] {r['title']} at {r['due_at']}")
        print("\nOverdue:")
        for r in get_overdue_reminders():
            print(f"  ! {r['title']} (was due {r['due_at']})")

    elif command == "notes":
        init_db()
        folder = sys.argv[2] if len(sys.argv) > 2 else None
        from carrot.notes import list_notes
        notes = list_notes(folder=folder)
        for n in notes:
            print(f"  {n['id']}: {n.get('title', n['filename'])}")

    elif command == "memory":
        init_db()
        from carrot.memory import list_memories, search as search_memory, stats

        query = " ".join(sys.argv[2:])
        entries = search_memory(query) if query else list_memories()
        if not entries:
            print("Nothing remembered yet." if not query else "No matching memories.")
            return
        for m in entries:
            pin = "*" if m.get("pinned") else " "
            print(f" {pin} ({m['kind']}/{m['subject']}) {m['content']}")
        if not query:
            print(f"\n{stats()['total']} total")

    elif command == "index":
        init_db()
        from carrot.indexer import add_index_dir, index_dirs

        if len(sys.argv) < 3:
            configured = index_dirs()
            print("Indexed folders:" if configured else "No folders indexed yet.")
            for directory in configured:
                print(f"  {directory}")
            return
        try:
            add_index_dir(sys.argv[2])
            print(f"Added {sys.argv[2]}. Run 'carrot index-scan' to index it.")
        except ValueError as exc:
            print(f"Error: {exc}")

    elif command == "index-scan":
        init_db()
        from carrot.indexer import index_dirs, run_scan, stats

        if not index_dirs():
            print("No folders configured. Add one with 'carrot index <dir>'.")
            return
        print("Scanning…")
        state = run_scan()
        print(f"Scanned {state['scanned']}, indexed {state['indexed']}, "
              f"skipped {state['skipped']}, failed {state['failed']}")
        print(f"Index now holds {stats()['documents']} documents.")

    elif command == "find":
        init_db()
        if len(sys.argv) < 3:
            print("Usage: carrot find <query>")
            return
        query = " ".join(sys.argv[2:])
        from carrot.indexer import search_documents
        from carrot.memory import search as search_memory
        from carrot.search import search_conversations

        memories = search_memory(query, limit=5)
        if memories:
            print("Memory:")
            for m in memories:
                print(f"  ({m['kind']}) {m['content']}")

        documents = search_documents(query, limit=5)["results"]
        if documents:
            print("\nYour files:")
            for d in documents:
                print(f"  {d['path']}")
                print(f"    {d['content'][:200]}")

        conversations = search_conversations(query, limit=5)["results"]
        if conversations:
            print("\nConversations:")
            for c in conversations:
                print(f"  [{c['timestamp'][:10]}] {c['content'][:200]}")

        if not (memories or documents or conversations):
            print("No matches.")

    elif command == "backup":
        init_db()
        from carrot.backup import export_archive

        destination = sys.argv[2] if len(sys.argv) > 2 else None
        result = export_archive(destination)
        print(f"Wrote {result['path']} ({result['size_bytes'] // 1024} KB)")

    elif command == "restore":
        if len(sys.argv) < 3:
            print("Usage: carrot restore <archive.zip>")
            return
        from carrot.backup import import_archive

        print("This replaces everything currently in Carrot. A safety copy is taken first.")
        if input("Continue? [y/N] ").strip().lower() != "y":
            print("Cancelled.")
            return
        result = import_archive(sys.argv[2])
        if result["success"]:
            print(f"Restored. Safety copy: {result.get('safety_copy', 'none')}")
        else:
            print(f"Failed: {result.get('error')}")

    elif command == "route":
        init_db()
        from carrot.router import status as router_status

        state = router_status()
        print("Providers:")
        for provider in state.get("providers", []):
            if not provider["enabled"]:
                mark, note = "-", "disabled"
            elif provider["configured"]:
                mark, note = "*", "ready"
            else:
                mark, note = " ", "no key"
            print(f"  {mark} {provider['id']:<14} {provider['kind']:<10} {note}")

        print("\nAutomatic escalation:", "on" if state["cloud_enabled"] else "off")
        print("\nTasks:")
        for task, route in state["routes"].items():
            where = "on-device" if route.get("local") else route["provider"]
            print(f"  {task:<12} {route['model']:<24} ({where})")
        recommendation = state.get("recommendation") or {}
        if recommendation.get("model"):
            print(f"\nSuggested local model: {recommendation['model']} — {recommendation['reason']}")

    elif command == "token":
        from carrot.security import session_token

        print(session_token())

    elif command == "status":
        init_db()
        config = get_config()
        from carrot.database import get_db
        conn = get_db()
        conv_count = conn.execute("SELECT COUNT(*) as c FROM conversations").fetchone()["c"]
        msg_count = conn.execute("SELECT COUNT(*) as c FROM messages").fetchone()["c"]
        goal_count = conn.execute("SELECT COUNT(*) as c FROM goals").fetchone()["c"]
        rem_count = conn.execute("SELECT COUNT(*) as c FROM reminders").fetchone()["c"]
        conn.close()
        print(f"Carrot Status")
        print(f"  Conversations: {conv_count}")
        print(f"  Messages: {msg_count}")
        print(f"  Goals: {goal_count}")
        print(f"  Reminders: {rem_count}")

        from carrot.indexer import stats as index_stats
        from carrot.memory import stats as memory_stats
        from carrot.vectors import stats as vector_stats

        print(f"  Memories: {memory_stats()['total']}")
        documents = index_stats()
        print(f"  Indexed files: {documents['documents']} ({documents['chunks']} chunks)")
        vectors = vector_stats()
        pending = sum(n["pending"] for n in vectors["namespaces"])
        print(f"  Vectors: {vectors['total']} ({pending} pending, {vectors['backend']} backend)")
        print(f"  Ollama: {config.get('ollama_host')}")
        print(f"  Default model: {config.get('ollama_model')}")

    else:
        print(f"Unknown command: {command}")
        print("Run 'carrot' with no args to see available commands")


if __name__ == "__main__":
    main()