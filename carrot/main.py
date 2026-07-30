#!/usr/bin/env python3
import sys
import os
import json

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
        os.system(f"python -m carrot.app")

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
        print(f"  Ollama: {config.get('ollama_host')}")
        print(f"  Default model: {config.get('ollama_model')}")

    else:
        print(f"Unknown command: {command}")
        print("Run 'carrot' with no args to see available commands")


if __name__ == "__main__":
    main()