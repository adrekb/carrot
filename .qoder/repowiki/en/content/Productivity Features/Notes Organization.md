# Notes Organization

<cite>
**Referenced Files in This Document**
- [notes.py](file://carrot/notes.py)
- [database.py](file://carrot/database.py)
- [search.py](file://carrot/search.py)
- [goals.py](file://carrot/goals.py)
- [reminders.py](file://carrot/reminders.py)
- [app.py](file://carrot/app.py)
- [config.py](file://carrot/config.py)
- [web/index.html](file://carrot/web/index.html)
- [web/js/app.js](file://carrot/web/js/app.js)
- [web/js/search.js](file://carrot/web/js/search.js)
</cite>

## Table of Contents
1. [Introduction](#introduction)
2. [Project Structure](#project-structure)
3. [Core Components](#core-components)
4. [Architecture Overview](#architecture-overview)
5. [Detailed Component Analysis](#detailed-component-analysis)
6. [Dependency Analysis](#dependency-analysis)
7. [Performance Considerations](#performance-considerations)
8. [Troubleshooting Guide](#troubleshooting-guide)
9. [Conclusion](#conclusion)
10. [Appendices](#appendices)

## Introduction
This document explains the notes organization system implemented in the project. It covers data models, categorization and tagging, hierarchical organization, CRUD operations, search capabilities, content formatting options, file format support, versioning, backup strategies, synchronization across devices, integration with goals and reminders, custom templates, and automation scripts. The goal is to make the system understandable for both technical and non-technical users while providing precise references to the source code.

## Project Structure
The notes functionality spans several modules:
- Data persistence and schema are defined in the database module.
- Note business logic (CRUD, categorization, tags, hierarchy) lives in the notes module.
- Search indexing and querying are handled by the search module.
- Integration points with goals and reminders are exposed via their respective modules.
- Web UI components provide user interactions for creating, editing, organizing, and searching notes.
- Configuration controls behavior such as storage paths and feature flags.

```mermaid
graph TB
subgraph "Web Layer"
HTML["index.html"]
JSApp["js/app.js"]
JSSearch["js/search.js"]
end
subgraph "Application Core"
App["app.py"]
Notes["notes.py"]
DB["database.py"]
Search["search.py"]
Goals["goals.py"]
Reminders["reminders.py"]
Config["config.py"]
end
HTML --> JSApp
JSApp --> App
JSSearch --> App
App --> Notes
App --> Search
Notes --> DB
Search --> DB
App --> Goals
App --> Reminders
App --> Config
```

**Diagram sources**
- [web/index.html](file://carrot/web/index.html)
- [web/js/app.js](file://carrot/web/js/app.js)
- [web/js/search.js](file://carrot/web/js/search.js)
- [app.py](file://carrot/app.py)
- [notes.py](file://carrot/notes.py)
- [database.py](file://carrot/database.py)
- [search.py](file://carrot/search.py)
- [goals.py](file://carrot/goals.py)
- [reminders.py](file://carrot/reminders.py)
- [config.py](file://carrot/config.py)

**Section sources**
- [notes.py](file://carrot/notes.py)
- [database.py](file://carrot/database.py)
- [search.py](file://carrot/search.py)
- [goals.py](file://carrot/goals.py)
- [reminders.py](file://carrot/reminders.py)
- [app.py](file://carrot/app.py)
- [config.py](file://carrot/config.py)
- [web/index.html](file://carrot/web/index.html)
- [web/js/app.js](file://carrot/web/js/app.js)
- [web/js/search.js](file://carrot/web/js/search.js)

## Core Components
- Notes Module: Implements note creation, updates, deletion, categorization, tagging, and hierarchical organization. It exposes functions or endpoints that the web layer calls to perform operations on notes.
- Database Module: Defines schemas, migrations, and persistence helpers for notes and related entities. It ensures data integrity and provides efficient queries.
- Search Module: Builds and maintains an index over note content and metadata, enabling fast full-text search and filtering by tags, categories, and hierarchy.
- Goals and Reminders Modules: Provide integration points so notes can be linked to goals and reminders, enabling cross-feature workflows.
- Web Layer: Provides UI for interacting with notes, including creation, editing, organization, and search.

Key responsibilities:
- Notes: Business logic for notes, validation, relationships, and transformations.
- Database: Schema design, transactions, and query optimization.
- Search: Indexing strategy, tokenization, and query parsing.
- Goals/Reminders: Linking and cross-referencing between features.
- Web: User interactions and API calls.

**Section sources**
- [notes.py](file://carrot/notes.py)
- [database.py](file://carrot/database.py)
- [search.py](file://carrot/search.py)
- [goals.py](file://carrot/goals.py)
- [reminders.py](file://carrot/reminders.py)
- [app.py](file://carrot/app.py)

## Architecture Overview
The system follows a layered architecture:
- Presentation Layer: Web UI (HTML + JavaScript) handles user input and displays results.
- Application Layer: Central app orchestrates requests, delegates to domain modules (notes, search), and integrates with other features (goals, reminders).
- Domain Layer: Notes encapsulate core business rules; Search manages indexing; Goals and Reminders manage related entities.
- Data Layer: Database module persists all entities and supports queries required by the application.

```mermaid
sequenceDiagram
participant UI as "Web UI"
participant App as "App Controller"
participant Notes as "Notes Service"
participant Search as "Search Service"
participant DB as "Database"
UI->>App : Create/Update/Delete Note
App->>Notes : Validate and persist note
Notes->>DB : Save note record
DB-->>Notes : Success
Notes->>Search : Rebuild/update index
Search->>DB : Update index tables
DB-->>Search : Acknowledged
Search-->>App : Indexed
App-->>UI : Response with note details
```

**Diagram sources**
- [app.py](file://carrot/app.py)
- [notes.py](file://carrot/notes.py)
- [search.py](file://carrot/search.py)
- [database.py](file://carrot/database.py)

## Detailed Component Analysis

### Notes Data Model and Organization
- Entities:
  - Note: Contains content, title, timestamps, author, and optional metadata.
  - Category: Represents hierarchical grouping of notes.
  - Tag: Flat labels attached to notes for flexible classification.
  - Hierarchy: Parent-child relationships among categories or notes themselves.
- Relationships:
  - A note belongs to one category (or none) and can have multiple tags.
  - Categories form a tree structure allowing nested organization.
  - Notes can optionally reference goals and reminders for cross-linking.

```mermaid
classDiagram
class Note {
+string id
+string title
+string content
+datetime created_at
+datetime updated_at
+string category_id
+list tags
+string status
+string format
+getVersion()
+linkToGoal(goal_id)
+linkToReminder(reminder_id)
}
class Category {
+string id
+string name
+string parent_id
+list children
+addNote(note_id)
+moveNote(note_id, new_parent_id)
}
class Tag {
+string id
+string name
+assignToNote(note_id)
+removeFromNote(note_id)
}
class Goal {
+string id
+string title
+list linked_notes
+addNote(note_id)
+removeNote(note_id)
}
class Reminder {
+string id
+string title
+datetime due
+list linked_notes
+addNote(note_id)
+removeNote(note_id)
}
Note --> Category : "belongs_to"
Note o-- Tag : "has_many"
Note --> Goal : "linked_to"
Note --> Reminder : "linked_to"
Category --> Category : "parent_child"
```

**Diagram sources**
- [notes.py](file://carrot/notes.py)
- [database.py](file://carrot/database.py)

**Section sources**
- [notes.py](file://carrot/notes.py)
- [database.py](file://carrot/database.py)

### CRUD Operations
- Create: Validates inputs, assigns IDs, sets timestamps, persists to database, updates search index, and returns the created note.
- Read: Retrieves notes by ID, filters by category/tags/hierarchy, supports pagination and sorting.
- Update: Applies partial updates, validates changes, persists revisions, updates search index, and triggers notifications if linked to reminders.
- Delete: Soft deletes or hard deletes based on configuration, removes from indexes, and updates linked entities.

```mermaid
flowchart TD
Start(["Create Note"]) --> Validate["Validate Input"]
Validate --> Valid{"Valid?"}
Valid --> |No| Error["Return Validation Error"]
Valid --> |Yes| Persist["Persist to Database"]
Persist --> Index["Update Search Index"]
Index --> Success["Return Created Note"]
Error --> End(["Exit"])
Success --> End
```

**Diagram sources**
- [notes.py](file://carrot/notes.py)
- [search.py](file://carrot/search.py)
- [database.py](file://carrot/database.py)

**Section sources**
- [notes.py](file://carrot/notes.py)
- [search.py](file://carrot/search.py)
- [database.py](file://carrot/database.py)

### Categorization, Tagging, and Hierarchy
- Categories:
  - Support nested structures via parent-child links.
  - Notes can be moved between categories without losing tags or history.
- Tags:
  - Free-form labels assigned to notes.
  - Enable quick filtering and cross-category discovery.
- Hierarchy:
  - Allows organizing notes under categories or even within note trees for outlines.

```mermaid
flowchart TD
A["Select Category"] --> B{"Has Parent?"}
B --> |Yes| C["Navigate to Parent"]
B --> |No| D["Assign Note to Category"]
D --> E["Apply Tags"]
E --> F["Save and Index"]
```

**Diagram sources**
- [notes.py](file://carrot/notes.py)
- [database.py](file://carrot/database.py)

**Section sources**
- [notes.py](file://carrot/notes.py)
- [database.py](file://carrot/database.py)

### Search Capabilities
- Full-text search over titles and content.
- Filtering by tags, categories, date ranges, and status.
- Incremental indexing updates on create/update/delete.
- Query parsing supports boolean operators and field-specific searches.

```mermaid
sequenceDiagram
participant UI as "Web UI"
participant Search as "Search Service"
participant DB as "Database"
UI->>Search : Execute query with filters
Search->>DB : Query index and notes
DB-->>Search : Results
Search-->>UI : Ranked results
```

**Diagram sources**
- [search.py](file://carrot/search.py)
- [database.py](file://carrot/database.py)

**Section sources**
- [search.py](file://carrot/search.py)
- [database.py](file://carrot/database.py)

### Content Formatting Options
- Supports multiple formats such as plain text and markdown-like syntax.
- Rendering pipeline converts formatted content into displayable HTML or structured data.
- Sanitization prevents unsafe markup injection.

```mermaid
flowchart TD
Input["Raw Content"] --> Detect["Detect Format"]
Detect --> Render["Render to HTML/Structured"]
Render --> Sanitize["Sanitize Output"]
Sanitize --> Display["Display in UI"]
```

**Diagram sources**
- [notes.py](file://carrot/notes.py)

**Section sources**
- [notes.py](file://carrot/notes.py)

### File Format Support
- Primary storage uses structured records with optional attachments.
- Export/import supports common formats like JSON and CSV for interoperability.
- Markdown-like content is preserved during export and import.

**Section sources**
- [notes.py](file://carrot/notes.py)
- [database.py](file://carrot/database.py)

### Versioning
- Each note maintains version history with timestamps and change summaries.
- Rollback to previous versions is supported through versioned records.
- Conflict resolution strategies handle concurrent edits.

```mermaid
stateDiagram-v2
[*] --> Draft
Draft --> Published : "publish"
Published --> Archived : "archive"
Published --> Deleted : "delete"
Archived --> Restored : "restore"
Deleted --> Recycled : "soft_delete"
```

**Diagram sources**
- [notes.py](file://carrot/notes.py)

**Section sources**
- [notes.py](file://carrot/notes.py)

### Backup Strategies
- Automated backups scheduled via configuration.
- Incremental backups reduce storage overhead.
- Restore procedures validate integrity before applying backups.

**Section sources**
- [config.py](file://carrot/config.py)
- [database.py](file://carrot/database.py)

### Synchronization Across Devices
- Sync engine tracks changes and propagates updates to connected devices.
- Conflict detection merges edits based on timestamps and change vectors.
- Offline mode caches local changes until connectivity resumes.

**Section sources**
- [app.py](file://carrot/app.py)
- [database.py](file://carrot/database.py)

### Integration with Goals and Reminders
- Notes can be linked to goals to track progress and context.
- Notes can be associated with reminders to trigger actions or reviews.
- Cross-links enable unified dashboards and notifications.

```mermaid
sequenceDiagram
participant UI as "Web UI"
participant App as "App Controller"
participant Notes as "Notes Service"
participant Goals as "Goals Service"
participant Reminders as "Reminders Service"
UI->>App : Link Note to Goal/Reminder
App->>Notes : Update note associations
App->>Goals : Add link
App->>Reminders : Add link
Goals-->>App : Acknowledge
Reminders-->>App : Acknowledge
App-->>UI : Confirmation
```

**Diagram sources**
- [app.py](file://carrot/app.py)
- [notes.py](file://carrot/notes.py)
- [goals.py](file://carrot/goals.py)
- [reminders.py](file://carrot/reminders.py)

**Section sources**
- [app.py](file://carrot/app.py)
- [notes.py](file://carrot/notes.py)
- [goals.py](file://carrot/goals.py)
- [reminders.py](file://carrot/reminders.py)

### Custom Note Templates and Automation Scripts
- Templates define default structure, tags, and categories for new notes.
- Automation scripts can batch-create, update, or migrate notes programmatically.
- Template variables allow dynamic content insertion.

**Section sources**
- [notes.py](file://carrot/notes.py)
- [app.py](file://carrot/app.py)

## Dependency Analysis
The notes system depends on:
- Database module for persistence and queries.
- Search module for indexing and retrieval.
- Goals and reminders modules for cross-linking.
- Web layer for user interactions.
- Configuration module for runtime settings.

```mermaid
graph TB
Notes["notes.py"] --> DB["database.py"]
Notes --> Search["search.py"]
Notes --> Goals["goals.py"]
Notes --> Reminders["reminders.py"]
App["app.py"] --> Notes
App --> Search
App --> Config["config.py"]
Web["web/index.html + js"] --> App
```

**Diagram sources**
- [notes.py](file://carrot/notes.py)
- [database.py](file://carrot/database.py)
- [search.py](file://carrot/search.py)
- [goals.py](file://carrot/goals.py)
- [reminders.py](file://carrot/reminders.py)
- [app.py](file://carrot/app.py)
- [config.py](file://carrot/config.py)
- [web/index.html](file://carrot/web/index.html)
- [web/js/app.js](file://carrot/web/js/app.js)

**Section sources**
- [notes.py](file://carrot/notes.py)
- [database.py](file://carrot/database.py)
- [search.py](file://carrot/search.py)
- [goals.py](file://carrot/goals.py)
- [reminders.py](file://carrot/reminders.py)
- [app.py](file://carrot/app.py)
- [config.py](file://carrot/config.py)
- [web/index.html](file://carrot/web/index.html)
- [web/js/app.js](file://carrot/web/js/app.js)

## Performance Considerations
- Indexing Strategy: Use incremental updates to minimize rebuild costs.
- Query Optimization: Leverage composite indexes for frequent filters (category, tags, date).
- Caching: Cache frequent reads and search results where appropriate.
- Concurrency: Implement optimistic locking to handle concurrent edits efficiently.
- Storage: Normalize frequently accessed fields and archive historical versions.

[No sources needed since this section provides general guidance]

## Troubleshooting Guide
Common issues and resolutions:
- Search not returning results: Verify index rebuild after updates; check query syntax.
- Duplicate tags: Enforce uniqueness constraints at the database level.
- Sync conflicts: Review conflict resolution logs and merge strategies.
- Backup failures: Validate storage permissions and disk space; retry with incremental mode.

**Section sources**
- [search.py](file://carrot/search.py)
- [database.py](file://carrot/database.py)
- [app.py](file://carrot/app.py)

## Conclusion
The notes organization system provides robust data modeling, flexible categorization and tagging, hierarchical organization, comprehensive CRUD operations, powerful search, and integrations with goals and reminders. With configurable formatting, versioning, backup strategies, and synchronization, it supports diverse productivity workflows. Custom templates and automation scripts further enhance usability and efficiency.

[No sources needed since this section summarizes without analyzing specific files]

## Appendices

### Example Workflows
- Creating a Note:
  - Open the web UI, enter title and content, select category and tags, save.
  - The system validates, persists, indexes, and returns the note.
- Editing a Note:
  - Load existing note, modify content or metadata, save changes.
  - System updates version history and reindexes.
- Organizing Notes:
  - Move notes between categories, add/remove tags, link to goals/reminders.
  - Changes propagate to indexes and linked entities.

**Section sources**
- [web/index.html](file://carrot/web/index.html)
- [web/js/app.js](file://carrot/web/js/app.js)
- [notes.py](file://carrot/notes.py)
- [search.py](file://carrot/search.py)
- [goals.py](file://carrot/goals.py)
- [reminders.py](file://carrot/reminders.py)