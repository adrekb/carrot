import os
import json
import glob
import re
import base64
from datetime import datetime

from carrot.database import get_db
from carrot.ollama_client import OllamaClient


DEFAULT_SCAN_DIRS = [
    os.path.expanduser("~/Documents"),
    os.path.expanduser("~/Desktop"),
    os.path.expanduser("~/Downloads"),
    os.path.expanduser("~/workspace"),
]

ASSIGNMENT_PATTERNS = [
    r"assign",
    r"homework",
    r"coursework",
    r"canvas",
    r"submission",
    r"lab\s*\d",
    r"problem\s*\d",
    r"project",
    r"report",
    r"essay",
    r"thesis",
    r"dissertation",
]

CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".go", ".rb", ".c", ".h",
    ".java", ".cfg", ".toml", ".ini", ".yaml", ".yml", ".json", ".sql",
    ".sh", ".bat", ".ps1", ".md", ".txt", ".r", ".m", ".swift", ".kt",
    ".scala", ".lua", ".r", ".ex", ".exs", ".clj", ".hs", ".ml", ".fs",
}

UI_TARS_MODEL = "ui-tars:7b"
SCREENSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "screenshots")


def scan_directory(directory: str, extensions: set = None):
    results = []
    for root, dirs, files in os.walk(directory):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if extensions is None or ext in extensions:
                filepath = os.path.join(root, f)
                try:
                    size = os.path.getsize(filepath)
                    mtime = datetime.fromtimestamp(
                        os.path.getmtime(filepath)
                    ).isoformat()
                    results.append(
                        {
                            "path": filepath,
                            "name": f,
                            "extension": ext,
                            "size": size,
                            "modified": mtime,
                        }
                    )
                except (OSError, PermissionError):
                    continue
    return results


def find_assignments(scan_dirs: list = None):
    if scan_dirs is None:
        scan_dirs = DEFAULT_SCAN_DIRS
    assignment_files = []
    for d in scan_dirs:
        if not os.path.isdir(d):
            continue
        for root, dirs, files in os.walk(d):
            for f in files:
                lower = f.lower()
                for pattern in ASSIGNMENT_PATTERNS:
                    if re.search(pattern, lower):
                        filepath = os.path.join(root, f)
                        ext = os.path.splitext(f)[1].lower()
                        try:
                            size = os.path.getsize(filepath)
                            mtime = datetime.fromtimestamp(
                                os.path.getmtime(filepath)
                            ).isoformat()
                            assignment_files.append(
                                {
                                    "path": filepath,
                                    "name": f,
                                    "directory": root,
                                    "extension": ext,
                                    "size": size,
                                    "modified": mtime,
                                    "matched_pattern": pattern,
                                }
                            )
                        except (OSError, PermissionError):
                            continue
                        break
    return assignment_files


def read_file(filepath: str, max_chars: int = 5000):
    try:
        ext = os.path.splitext(filepath)[1].lower()
        if ext in {".py", ".js", ".ts", ".rs", ".go", ".rb", ".c", ".h", ".java", ".sh", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".cfg"}:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(max_chars)
            return {"path": filepath, "content": content, "truncated": len(content) >= max_chars}
        else:
            with open(filepath, "rb") as f:
                data = f.read(max_chars)
            return {"path": filepath, "content": f"<binary file, first {len(data)} bytes>", "truncated": len(data) >= max_chars, "extension": ext}
    except Exception as e:
        return {"path": filepath, "error": str(e)}


def parse_html_canvas(html_content: str):
    deadline_patterns = [
        r'(?:deadline|due|submission)\s*:?\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})',
        r'(?:deadline|due|submission)\s*:?\s*(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})',
        r'(?:deadline|due|submission)\s*:?\s*(\d{4}-\d{2}-\d{2})',
        r'(?:Due|DUE)\s*(?:on|by|before)?\s*([A-Za-z]+\s+\d{1,2})',
        r'assignment.*?(?:due|deadline|submit).*?(\d{1,2}[/\-.]\d{1,2})',
    ]
    deadlines = []
    for pattern in deadline_patterns:
        matches = re.findall(pattern, html_content, re.IGNORECASE)
        for m in matches:
            deadlines.append({"date": m, "raw_match": m})

    title_match = re.search(r'<title>(.*?)</title>', html_content, re.IGNORECASE)
    title = title_match.group(1) if title_match else ""

    assignment_elements = []
    for tag in ["h1", "h2", "h3", "h4"]:
        for match in re.finditer(rf'<{tag}[^>]*>(.*?)</{tag}>', html_content, re.IGNORECASE | re.DOTALL):
            text = re.sub(r'<[^>]+>', '', match.group(1)).strip()
            if any(kw in text.lower() for kw in ["assignment", "homework", "due", "deadline", "submission", "project"]):
                assignment_elements.append({"tag": tag, "text": text})

    return {
        "title": title,
        "deadlines": deadlines,
        "assignment_elements": assignment_elements,
        "scanned_at": datetime.now().isoformat(),
    }


def take_screenshot(window_title: str = None):
    try:
        import pyautogui
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"screenshot_{timestamp}.png"
        filepath = os.path.join(SCREENSHOT_DIR, filename)
        screenshot = pyautogui.screenshot()
        screenshot.save(filepath)
        return {"path": filepath, "filename": filename, "success": True}
    except ImportError:
        return {"path": None, "error": "pyautogui not installed", "success": False}
    except Exception as e:
        return {"path": None, "error": str(e), "success": False}


def analyze_screenshot_with_vlm(image_path: str):
    client = OllamaClient()
    if not client.is_available():
        return {"success": False, "error": "Ollama not available"}

    try:
        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")

        prompt = (
            "Analyze this screenshot of a screen. Look for any assignment deadlines, "
            "due dates, course names, assignment titles, or any actionable academic information. "
            "Return a structured JSON with fields: 'assignments' (array of objects with 'title', 'due_date', 'course'), "
            "'key_dates' (array of date strings found), and 'summary' (brief description of what the screen shows)."
        )

        response = client.generate(
            prompt=prompt,
            model=UI_TARS_MODEL,
            system="You are a vision-language model agent that analyzes screenshots for academic deadlines and assignments.",
        )

        try:
            cleaned = response.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                cleaned = "\n".join(line for line in lines if not line.strip().startswith("```"))
            cleaned = cleaned.strip()
            if cleaned.startswith("{") and cleaned.endswith("}"):
                return {"success": True, "analysis": json.loads(cleaned), "image_path": image_path}
            return {"success": True, "analysis": {"raw_response": cleaned}, "image_path": image_path}
        except json.JSONDecodeError:
            return {"success": True, "analysis": {"raw_response": response}, "image_path": image_path}

    except Exception as e:
        return {"success": False, "error": str(e), "image_path": image_path}


def analyze_html_with_vlm(html_path: str):
    client = OllamaClient()
    if not client.is_available():
        return {"success": False, "error": "Ollama not available"}

    try:
        with open(html_path, "r", encoding="utf-8", errors="replace") as f:
            html_content = f.read()

        parsed = parse_html_canvas(html_content)

        prompt = (
            f"Here is a parsed HTML document from a Canvas-like assignment page. "
            f"Extract all assignment deadlines, course names, and due dates.\n\n"
            f"Page title: {parsed['title']}\n"
            f"Deadlines found: {json.dumps(parsed['deadlines'])}\n"
            f"Key elements: {json.dumps(parsed['assignment_elements'])}\n\n"
            f"Return a structured JSON with: 'assignments' (array of {title, course, due_date, details}), "
            f"'all_deadlines' (array of date strings), and 'summary'."
        )

        response = client.generate(
            prompt=prompt,
            model=UI_TARS_MODEL,
            system="You extract structured assignment data from Canvas syllabus HTML pages.",
        )

        try:
            cleaned = response.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                cleaned = "\n".join(line for line in lines if not line.strip().startswith("```"))
            cleaned = cleaned.strip()
            if cleaned.startswith("{") and cleaned.endswith("}"):
                return {"success": True, "analysis": json.loads(cleaned), "html_path": html_path}
            return {"success": True, "analysis": {"raw_response": response}, "html_path": html_path}
        except json.JSONDecodeError:
            return {"success": True, "analysis": {"raw_response": response}, "html_path": html_path}

    except Exception as e:
        return {"success": False, "error": str(e), "html_path": html_path}


def computer_use_scan(scan_dirs: list = None, use_vlm: bool = False):
    assignments = find_assignments(scan_dirs)
    conn = get_db()
    for a in assignments:
        conn.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (f"computer_use:{a['path']}", json.dumps(a)),
        )
    conn.commit()
    conn.close()

    if use_vlm:
        for a in assignments:
            if a["extension"] in {".html", ".htm"}:
                html_analysis = analyze_html_with_vlm(a["path"])
                if html_analysis.get("success"):
                    conn = get_db()
                    conn.execute(
                        "UPDATE config SET value = ? WHERE key = ?",
                        (json.dumps({**a, "vlm_analysis": html_analysis.get("analysis", {})}), f"computer_use:{a['path']}"),
                    )
                    conn.commit()
                    conn.close()

    return len(assignments)