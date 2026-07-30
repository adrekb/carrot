from carrot.database import get_db, init_db
from carrot.config import get_config, set_config
from carrot.search import (
    search_conversations,
    parse_time_offset,
    extract_keywords,
    classify_query,
    get_embedding,
    cosine_similarity,
    store_embedding,
    get_embedding_for_message,
)
from carrot.conversation import create_conversation, get_conversation, list_conversations, add_message
from carrot.ollama_client import OllamaClient
from carrot.computer_use import (
    find_assignments,
    scan_directory,
    read_file,
    parse_html_canvas,
    take_screenshot,
    analyze_screenshot_with_vlm,
    analyze_html_with_vlm,
    computer_use_scan,
)
from carrot.terminal import execute_command
from carrot.recap import (
    run_recap,
    fetch_live_tech_news,
    fetch_live_tech_articles,
    fetch_all_feeds,
    summarize_news,
)
from carrot.goals import create_goal, list_goals, add_data_point
from carrot.reminders import create_reminder, list_reminders, complete_reminder
from carrot.notes import create_note, list_notes, get_note
from carrot.leaderboard import (
    get_hardware_profile,
    submit_leaderboard_entry,
    update_leaderboard_model,
    get_leaderboard,
    get_leaderboard_stats,
    get_model_recommendations,
)

__all__ = [
    "get_db",
    "init_db",
    "get_config",
    "set_config",
    "store_embedding",
    "get_embedding_for_message",
    "search_conversations",
    "parse_time_offset",
    "extract_keywords",
    "classify_query",
    "get_embedding",
    "cosine_similarity",
    "create_conversation",
    "get_conversation",
    "list_conversations",
    "add_message",
    "OllamaClient",
    "find_assignments",
    "scan_directory",
    "read_file",
    "parse_html_canvas",
    "take_screenshot",
    "analyze_screenshot_with_vlm",
    "analyze_html_with_vlm",
    "computer_use_scan",
    "execute_command",
    "run_recap",
    "fetch_live_tech_news",
    "fetch_live_tech_articles",
    "fetch_all_feeds",
    "summarize_news",
    "create_goal",
    "list_goals",
    "add_data_point",
    "create_reminder",
    "list_reminders",
    "complete_reminder",
    "create_note",
    "list_notes",
    "get_note",
    "get_hardware_profile",
    "submit_leaderboard_entry",
    "update_leaderboard_model",
    "get_leaderboard",
    "get_leaderboard_stats",
    "get_model_recommendations",
]