#!/usr/bin/env python3
"""Generate a static Markdown Kindle Life OS from Notion.

This script is designed for GitHub Actions and KOReader on a jailbroken
Kindle. Notion is the source of truth; the output is a folder of plain
Markdown files. There is no web framework and no local database.

Confirmed Notion schemas from the uploaded exports:
- Areas: Areas, Goals, Progress
- Goals: Nombre, Actions, Area, Completed Actions, Count (Numerical), Date,
  Done, Goal, Month, Number Of Actions, Place, Progress, Related resources, Type
- Actions: Nombre, Area, Count, Date, Hide/Show, Importance Level,
  Related Goal, Relative amount, Status, Urgency
- Resources: Name, Related goals, Type, Author, Date, URL
- Workouts: Name, Created, target, time, type
- Finance/expenses: Name, created, expended, type

Reading database schema was not present in the uploaded files. If a
NOTION_READING_DB_ID is supplied, the script auto-detects reasonable fields
from that database schema and still generates reading.md.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from notion_client import Client
from notion_client.errors import APIResponseError


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DONE_WORDS = {
    "done",
    "complete",
    "completed",
    "cancelled",
    "canceled",
    "archive",
    "archived",
}

ACTIVE_GOAL_DONE_WORDS = DONE_WORDS | {"yes", "true", "1"}

FALSE_WORDS = {"", "no", "false", "0", "none", "not started"}

DEFAULT_TZ = "America/New_York"
DEFAULT_OUTPUT_DIR = "kindle"
DEFAULT_MAX_ITEMS = 20


@dataclass(frozen=True)
class DatabaseConfig:
    """Environment variable and confirmed property names for one Notion DB."""

    key: str
    display_name: str
    env_var: str
    required: bool
    title_prop: Optional[str] = None
    props: dict[str, str] = field(default_factory=dict)


DB_CONFIGS: dict[str, DatabaseConfig] = {
    "areas": DatabaseConfig(
        key="areas",
        display_name="Areas",
        env_var="NOTION_AREAS_DB_ID",
        required=True,
        title_prop="Areas",
        props={"goals": "Goals", "progress": "Progress"},
    ),
    "goals": DatabaseConfig(
        key="goals",
        display_name="Goals",
        env_var="NOTION_GOALS_DB_ID",
        required=True,
        title_prop="Nombre",
        props={
            "actions": "Actions",
            "area": "Area",
            "completed_actions": "Completed Actions",
            "count_numerical": "Count (Numerical)",
            "date": "Date",
            "done": "Done",
            "goal": "Goal",
            "month": "Month",
            "number_of_actions": "Number Of Actions",
            "place": "Place",
            "progress": "Progress",
            "related_resources": "Related resources",
            "type": "Type",
        },
    ),
    "actions": DatabaseConfig(
        key="actions",
        display_name="Actions",
        env_var="NOTION_ACTIONS_DB_ID",
        required=True,
        title_prop="Nombre",
        props={
            "area": "Area",
            "count": "Count",
            "date": "Date",
            "hide_show": "Hide/Show",
            "importance_level": "Importance Level",
            "related_goal": "Related Goal",
            "relative_amount": "Relative amount",
            "status": "Status",
            "urgency": "Urgency",
        },
    ),
    "resources": DatabaseConfig(
        key="resources",
        display_name="Resources",
        env_var="NOTION_RESOURCES_DB_ID",
        required=True,
        title_prop="Name",
        props={
            "related_goals": "Related goals",
            "type": "Type",
            "author": "Author",
            "date": "Date",
            "url": "URL",
        },
    ),
    "workouts": DatabaseConfig(
        key="workouts",
        display_name="Workouts",
        env_var="NOTION_WORKOUTS_DB_ID",
        required=True,
        title_prop="Name",
        props={
            "created": "Created",
            "target": "target",
            "time": "time",
            "type": "type",
        },
    ),
    "finance": DatabaseConfig(
        key="finance",
        display_name="Finance",
        env_var="NOTION_FINANCE_DB_ID",
        required=True,
        title_prop="Name",
        props={
            "created": "created",
            "amount": "expended",
            "category": "type",
        },
    ),
    "reading": DatabaseConfig(
        key="reading",
        display_name="Reading",
        env_var="NOTION_READING_DB_ID",
        required=False,
        title_prop=None,
        props={},
    ),
}


@dataclass
class RuntimeConfig:
    notion_token: str
    output_dir: Path
    timezone: ZoneInfo
    today: date
    strict_databases: bool
    max_items: int
    database_ids: dict[str, Optional[str]]


@dataclass
class DatabaseBundle:
    config: DatabaseConfig
    database_id: Optional[str]
    schema: dict[str, Any] = field(default_factory=dict)
    pages: list[dict[str, Any]] = field(default_factory=list)
    title_prop: Optional[str] = None
    missing: bool = False

    @property
    def properties(self) -> dict[str, Any]:
        return self.schema.get("properties", {}) if self.schema else {}


@dataclass
class AreaItem:
    id: str
    name: str
    progress: Optional[float]
    goals_text: str = ""
    url: str = ""


@dataclass
class GoalItem:
    id: str
    name: str
    area_ids: list[str]
    area_names: list[str]
    date_text: str
    due_date: Optional[date]
    done: bool
    goal_value: Optional[float]
    month: str
    progress_text: str
    progress_number: Optional[float]
    completed_actions: Optional[float]
    count_numerical: Optional[float]
    number_of_actions: Optional[float]
    why: str = ""
    description: str = ""
    type_text: str = ""
    place: str = ""
    resource_ids: list[str] = field(default_factory=list)
    resource_names: list[str] = field(default_factory=list)
    url: str = ""


@dataclass
class ActionItem:
    id: str
    name: str
    area_ids: list[str]
    area_names: list[str]
    goal_ids: list[str]
    goal_names: list[str]
    date_text: str
    due_date: Optional[date]
    count: Optional[float]
    status: str
    hide_show: str
    importance: Optional[float]
    urgency: Optional[float]
    relative_amount_text: str
    relative_amount_number: Optional[float]
    next_action: bool = False
    url: str = ""

    @property
    def done(self) -> bool:
        return normalize(self.status) in DONE_WORDS


@dataclass
class ResourceItem:
    id: str
    name: str
    goal_ids: list[str]
    goal_names: list[str]
    type_text: str
    author: str
    date_text: str
    url_value: str
    notion_url: str = ""


@dataclass
class WorkoutItem:
    id: str
    name: str
    created_text: str
    workout_date: Optional[date]
    target: str
    duration_text: str
    duration_minutes: Optional[float]
    type_text: str
    url: str = ""


@dataclass
class FinanceItem:
    id: str
    name: str
    created_text: str
    transaction_date: Optional[date]
    amount_text: str
    amount_number: Optional[float]
    currency_symbol: str
    category: str
    url: str = ""


@dataclass
class ReadingItem:
    id: str
    name: str
    status: str
    progress_text: str
    progress_number: Optional[float]
    goal_text: str
    author: str
    date_text: str
    url: str = ""

    @property
    def currently_reading(self) -> bool:
        status = normalize(self.status)
        if not status:
            return True
        return any(word in status for word in ("reading", "current", "active", "in progress"))


@dataclass
class LifeOSData:
    areas: list[AreaItem]
    goals: list[GoalItem]
    actions: list[ActionItem]
    resources: list[ResourceItem]
    workouts: list[WorkoutItem]
    finance: list[FinanceItem]
    reading: list[ReadingItem]
    warnings: list[str]
    next_action_property_found: bool
    reading_configured: bool


# ---------------------------------------------------------------------------
# Logging and small utilities
# ---------------------------------------------------------------------------


def log(message: str) -> None:
    print(message, file=sys.stderr)


def warn(warnings: list[str], message: str) -> None:
    warnings.append(message)
    log(f"WARNING: {message}")


def normalize(value: Any) -> str:
    return str(value or "").strip().lower()


def clean_text(value: Any) -> str:
    """Collapse whitespace and remove text artifacts that read poorly on Kindle."""
    if value is None:
        return ""
    text = str(value)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def safe_filename(name: str, fallback: str = "untitled") -> str:
    """Return a filesystem-safe Markdown slug."""
    cleaned = clean_text(name)
    cleaned = unicodedata.normalize("NFKD", cleaned)
    cleaned = cleaned.encode("ascii", "ignore").decode("ascii")
    cleaned = cleaned.lower()
    cleaned = re.sub(r"['\"]", "", cleaned)
    cleaned = re.sub(r"[^a-z0-9]+", "-", cleaned)
    cleaned = cleaned.strip("-")
    if not cleaned:
        cleaned = fallback
    return cleaned[:90].rstrip("-") + ".md"


def first_non_empty(values: Iterable[Any]) -> str:
    for value in values:
        text = clean_text(value)
        if text:
            return text
    return ""


def as_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    text = clean_text(value)
    if not text:
        return None
    # Handles values like "0%", "$12.30", "€9.35", "1,200".
    match = re.search(r"-?\d+(?:,\d{3})*(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def as_percent_number(value: Any) -> Optional[float]:
    number = as_number(value)
    if number is None:
        return None
    text = clean_text(value)
    if "%" in text:
        return number / 100.0
    # Notion progress can be 0.25 or 25 depending on the property format.
    if number > 1:
        return number / 100.0
    return number


def progress_bar(value: Optional[float], width: int = 10) -> str:
    if value is None:
        return ""
    value = max(0.0, min(1.0, value))
    filled = round(value * width)
    return "█" * filled + "░" * (width - filled) + f" {value:.0%}"


def truthy_text(value: Any) -> bool:
    text = normalize(value)
    if text in FALSE_WORDS:
        return False
    if text in {"yes", "true", "1", "done", "next", "next action"}:
        return True
    return bool(text)


def parse_iso_or_notion_date(value: Any) -> Optional[date]:
    """Parse dates from Notion API output or simple text.

    The Notion API returns ISO strings. The exported PDFs/CSVs show human dates,
    but production generation uses the API, so ISO is the primary path.
    """
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()

    text = clean_text(value)
    if not text:
        return None

    # First try ISO formats: 2026-06-09, 2026-06-09T20:10:00Z.
    iso = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass

    # Fallback for strings like "June 7, 2026 5:37 PM".
    for fmt in ("%B %d, %Y %I:%M %p", "%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%m-%d-%Y", "%m-%d-%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def date_text(prop_value: dict[str, Any]) -> tuple[str, Optional[date]]:
    if not prop_value:
        return "", None
    kind = prop_value.get("type")
    raw: Any = None
    if kind == "date":
        raw = (prop_value.get("date") or {}).get("start")
    elif kind in {"created_time", "last_edited_time"}:
        raw = prop_value.get(kind)
    else:
        raw = property_to_text(prop_value)
    parsed = parse_iso_or_notion_date(raw)
    if parsed:
        return parsed.isoformat(), parsed
    return clean_text(raw), None


def duration_to_minutes(value: Any) -> Optional[float]:
    """Parse workout duration strings like 30:00 or 1:15:00."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = clean_text(value)
    if not text:
        return None
    parts = text.split(":")
    try:
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = int(parts[1])
            return minutes + seconds / 60.0
        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = int(parts[2])
            return hours * 60 + minutes + seconds / 60.0
    except ValueError:
        pass
    number = as_number(text)
    return number


def infer_currency_symbol(value: Any) -> str:
    text = clean_text(value)
    match = re.search(r"[$€£¥]", text)
    return match.group(0) if match else ""


def format_amount(amount: Optional[float], symbol: str = "") -> str:
    if amount is None:
        return "—"
    if abs(amount - round(amount)) < 0.005:
        return f"{symbol}{amount:.0f}"
    return f"{symbol}{amount:.2f}"


def comma_join(items: Iterable[str]) -> str:
    return ", ".join([clean_text(item) for item in items if clean_text(item)])


def group_by(items: Iterable[Any], names_attr: str) -> dict[str, list[Any]]:
    groups: dict[str, list[Any]] = {}
    for item in items:
        names = getattr(item, names_attr, []) or ["Unlinked"]
        for name in names:
            key = clean_text(name) or "Unlinked"
            groups.setdefault(key, []).append(item)
    return dict(sorted(groups.items(), key=lambda kv: kv[0].lower()))


# ---------------------------------------------------------------------------
# Notion property parsing
# ---------------------------------------------------------------------------


def property_to_text(prop_value: Optional[dict[str, Any]]) -> str:
    """Convert any Notion property value into a readable string.

    Relation names require cross-database lookup, so relation properties return
    their raw ids here. Higher-level builders replace ids with names.
    """
    if not prop_value:
        return ""
    kind = prop_value.get("type")
    if not kind:
        return ""

    if kind == "title":
        return clean_text("".join(part.get("plain_text", "") for part in prop_value.get("title", [])))
    if kind == "rich_text":
        return clean_text("".join(part.get("plain_text", "") for part in prop_value.get("rich_text", [])))
    if kind in {"select", "status"}:
        selected = prop_value.get(kind)
        return clean_text((selected or {}).get("name", ""))
    if kind == "multi_select":
        return comma_join(option.get("name", "") for option in prop_value.get("multi_select", []))
    if kind == "date":
        value = prop_value.get("date") or {}
        start = value.get("start", "")
        end = value.get("end", "")
        if start and end:
            return f"{start} – {end}"
        return clean_text(start)
    if kind == "checkbox":
        return "Yes" if prop_value.get("checkbox") else "No"
    if kind == "number":
        number = prop_value.get("number")
        return "" if number is None else str(number)
    if kind in {"url", "email", "phone_number", "created_time", "last_edited_time"}:
        return clean_text(prop_value.get(kind, ""))
    if kind == "relation":
        return comma_join(item.get("id", "") for item in prop_value.get("relation", []))
    if kind == "people":
        names: list[str] = []
        for person in prop_value.get("people", []):
            names.append(person.get("name") or (person.get("person") or {}).get("email") or "")
        return comma_join(names)
    if kind == "files":
        names = []
        for file_obj in prop_value.get("files", []):
            name = file_obj.get("name", "")
            if name:
                names.append(name)
        return comma_join(names)
    if kind == "formula":
        formula = prop_value.get("formula") or {}
        return formula_to_text(formula)
    if kind == "rollup":
        rollup = prop_value.get("rollup") or {}
        return rollup_to_text(rollup)
    if kind == "unique_id":
        value = prop_value.get("unique_id") or {}
        prefix = value.get("prefix") or ""
        number = value.get("number")
        return f"{prefix}-{number}" if prefix else ("" if number is None else str(number))

    # Unknown future Notion property type: make a safe best effort.
    raw = prop_value.get(kind)
    if isinstance(raw, str):
        return clean_text(raw)
    return ""


def formula_to_text(formula: dict[str, Any]) -> str:
    kind = formula.get("type")
    if kind == "string":
        return clean_text(formula.get("string"))
    if kind == "number":
        number = formula.get("number")
        return "" if number is None else str(number)
    if kind == "boolean":
        return "Yes" if formula.get("boolean") else "No"
    if kind == "date":
        value = formula.get("date") or {}
        return clean_text(value.get("start"))
    return ""


def rollup_to_text(rollup: dict[str, Any]) -> str:
    kind = rollup.get("type")
    if kind == "number":
        number = rollup.get("number")
        return "" if number is None else str(number)
    if kind == "date":
        value = rollup.get("date") or {}
        return clean_text(value.get("start"))
    if kind == "array":
        values: list[str] = []
        for item in rollup.get("array", []):
            item_type = item.get("type")
            if not item_type:
                continue
            values.append(property_to_text(item))
        return comma_join(values)
    return ""


def page_prop(page: dict[str, Any], prop_name: Optional[str]) -> dict[str, Any]:
    if not prop_name:
        return {}
    return page.get("properties", {}).get(prop_name, {})


def get_text(page: dict[str, Any], prop_name: Optional[str]) -> str:
    return property_to_text(page_prop(page, prop_name))


def get_number(page: dict[str, Any], prop_name: Optional[str]) -> Optional[float]:
    return as_number(get_text(page, prop_name))


def get_relation_ids(page: dict[str, Any], prop_name: Optional[str]) -> list[str]:
    prop = page_prop(page, prop_name)
    if not prop or prop.get("type") != "relation":
        return []
    return [item.get("id", "") for item in prop.get("relation", []) if item.get("id")]


def relation_names(page: dict[str, Any], prop_name: Optional[str], id_to_name: Mapping[str, str]) -> tuple[list[str], list[str]]:
    ids = get_relation_ids(page, prop_name)
    if ids:
        return ids, [id_to_name.get(item_id, item_id[:8]) for item_id in ids]
    text = get_text(page, prop_name)
    names = [part.strip() for part in text.split(",") if part.strip()]
    return [], names


def title_property_from_schema(schema: Mapping[str, Any], preferred: Optional[str]) -> Optional[str]:
    props = schema.get("properties", {}) if schema else {}
    if preferred and preferred in props:
        return preferred
    for name, meta in props.items():
        if meta.get("type") == "title":
            return name
    return preferred


def prop_or_none(bundle: DatabaseBundle, configured_name: Optional[str]) -> Optional[str]:
    if not configured_name:
        return None
    if configured_name in bundle.properties:
        return configured_name
    return configured_name  # Missing fields are handled gracefully at extraction time.


def env_override(key: str, logical_prop: str, default: Optional[str]) -> Optional[str]:
    env_name = f"NOTION_{key.upper()}_{logical_prop.upper()}_PROP"
    return os.getenv(env_name, default or "") or None


def find_property(schema: Mapping[str, Any], candidates: Iterable[str]) -> Optional[str]:
    props = schema.get("properties", {}) if schema else {}
    normalized = {normalize(name): name for name in props.keys()}
    for candidate in candidates:
        name = normalized.get(normalize(candidate))
        if name:
            return name
    lowered_candidates = [normalize(c) for c in candidates]
    for prop_name in props.keys():
        low = normalize(prop_name)
        if any(candidate in low for candidate in lowered_candidates):
            return prop_name
    return None


# ---------------------------------------------------------------------------
# Notion API access
# ---------------------------------------------------------------------------


def load_runtime_config() -> RuntimeConfig:
    token = os.getenv("NOTION_TOKEN", "").strip()
    if not token:
        raise SystemExit("Missing NOTION_TOKEN. Add it as a GitHub Actions secret.")

    tz_name = os.getenv("TZ", DEFAULT_TZ)
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise SystemExit(f"Invalid TZ value {tz_name!r}. Use an IANA timezone like 'America/New_York'.") from exc

    max_items_raw = os.getenv("MAX_ITEMS_PER_SECTION", str(DEFAULT_MAX_ITEMS))
    try:
        max_items = max(1, int(max_items_raw))
    except ValueError:
        max_items = DEFAULT_MAX_ITEMS

    db_ids = {key: os.getenv(config.env_var, "").strip() or None for key, config in DB_CONFIGS.items()}
    return RuntimeConfig(
        notion_token=token,
        output_dir=Path(os.getenv("OUTPUT_DIR", DEFAULT_OUTPUT_DIR)).resolve(),
        timezone=tz,
        today=datetime.now(tz).date(),
        strict_databases=os.getenv("STRICT_DATABASES", "false").strip().lower() in {"1", "true", "yes"},
        max_items=max_items,
        database_ids=db_ids,
    )


def retrieve_database(client: Client, database_id: str) -> dict[str, Any]:
    return client.databases.retrieve(database_id=database_id)


def query_database(client: Client, database_id: str) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        payload: dict[str, Any] = {"database_id": database_id, "page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        response = client.databases.query(**payload)
        pages.extend(response.get("results", []))
        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")
    return pages


def load_databases(client: Client, cfg: RuntimeConfig, warnings: list[str]) -> dict[str, DatabaseBundle]:
    bundles: dict[str, DatabaseBundle] = {}
    for key, db_config in DB_CONFIGS.items():
        database_id = cfg.database_ids.get(key)
        bundle = DatabaseBundle(config=db_config, database_id=database_id)
        bundles[key] = bundle

        if not database_id:
            msg = f"{db_config.env_var} is not set; {db_config.display_name} page will be generated with available data only."
            if db_config.required and cfg.strict_databases:
                raise SystemExit(msg)
            warn(warnings, msg)
            bundle.missing = True
            continue

        try:
            bundle.schema = retrieve_database(client, database_id)
            bundle.title_prop = title_property_from_schema(bundle.schema, db_config.title_prop)
            bundle.pages = query_database(client, database_id)
            log(f"Loaded {len(bundle.pages)} rows from {db_config.display_name}.")
        except APIResponseError as exc:
            message = f"Notion API error while loading {db_config.display_name}: {exc}"
            if db_config.required or cfg.strict_databases:
                raise SystemExit(message) from exc
            warn(warnings, message)
            bundle.missing = True
        except Exception as exc:  # Defensive: GitHub Actions should fail clearly.
            message = f"Unexpected error while loading {db_config.display_name}: {exc}"
            if db_config.required or cfg.strict_databases:
                raise SystemExit(message) from exc
            warn(warnings, message)
            bundle.missing = True
    return bundles


# ---------------------------------------------------------------------------
# Data builders
# ---------------------------------------------------------------------------


def build_id_to_name(bundle: DatabaseBundle) -> dict[str, str]:
    title_prop = bundle.title_prop or bundle.config.title_prop
    result: dict[str, str] = {}
    for page in bundle.pages:
        name = get_text(page, title_prop) or "Untitled"
        result[page["id"]] = name
    return result


def build_areas(bundle: DatabaseBundle) -> list[AreaItem]:
    title_prop = env_override("areas", "title", bundle.title_prop or "Areas")
    goals_prop = env_override("areas", "goals", DB_CONFIGS["areas"].props["goals"])
    progress_prop = env_override("areas", "progress", DB_CONFIGS["areas"].props["progress"])

    areas: list[AreaItem] = []
    for page in bundle.pages:
        name = get_text(page, title_prop) or "Untitled Area"
        progress_text = get_text(page, progress_prop)
        areas.append(
            AreaItem(
                id=page["id"],
                name=name,
                progress=as_percent_number(progress_text),
                goals_text=get_text(page, goals_prop),
                url=page.get("url", ""),
            )
        )
    return sorted(areas, key=lambda item: item.name.lower())


def detect_why_description_props(bundle: DatabaseBundle) -> tuple[Optional[str], Optional[str]]:
    props = bundle.properties
    why_prop = None
    description_prop = None
    for name in props.keys():
        low = normalize(name)
        if why_prop is None and low in {"why", "purpose"}:
            why_prop = name
        if description_prop is None and low in {"description", "notes", "note"}:
            description_prop = name
    return why_prop, description_prop


def build_goals(bundle: DatabaseBundle, area_id_to_name: Mapping[str, str], resource_id_to_name: Mapping[str, str]) -> list[GoalItem]:
    props = DB_CONFIGS["goals"].props
    title_prop = env_override("goals", "title", bundle.title_prop or "Nombre")
    area_prop = env_override("goals", "area", props["area"])
    actions_prop = env_override("goals", "actions", props["actions"])
    completed_prop = env_override("goals", "completed_actions", props["completed_actions"])
    count_prop = env_override("goals", "count_numerical", props["count_numerical"])
    date_prop_name = env_override("goals", "date", props["date"])
    done_prop = env_override("goals", "done", props["done"])
    goal_prop = env_override("goals", "goal", props["goal"])
    month_prop = env_override("goals", "month", props["month"])
    number_actions_prop = env_override("goals", "number_of_actions", props["number_of_actions"])
    place_prop = env_override("goals", "place", props["place"])
    progress_prop = env_override("goals", "progress", props["progress"])
    resource_prop = env_override("goals", "related_resources", props["related_resources"])
    type_prop = env_override("goals", "type", props["type"])
    why_prop, description_prop = detect_why_description_props(bundle)

    goals: list[GoalItem] = []
    for page in bundle.pages:
        name = get_text(page, title_prop) or "Untitled Goal"
        area_ids, area_names = relation_names(page, area_prop, area_id_to_name)
        resource_ids, resource_names = relation_names(page, resource_prop, resource_id_to_name)
        date_display, due = date_text(page_prop(page, date_prop_name))
        done_raw = get_text(page, done_prop)
        done_bool = normalize(done_raw) in ACTIVE_GOAL_DONE_WORDS
        progress_text = get_text(page, progress_prop)
        progress_num = as_percent_number(progress_text)
        if progress_num is not None and progress_num >= 0.999:
            done_bool = True
        # Some Notion setups use status/select in the Goal or Type fields.
        if normalize(get_text(page, goal_prop)) in DONE_WORDS:
            done_bool = True

        goals.append(
            GoalItem(
                id=page["id"],
                name=name,
                area_ids=area_ids,
                area_names=area_names,
                date_text=date_display,
                due_date=due,
                done=done_bool,
                goal_value=get_number(page, goal_prop),
                month=get_text(page, month_prop),
                progress_text=progress_text,
                progress_number=progress_num,
                completed_actions=get_number(page, completed_prop),
                count_numerical=get_number(page, count_prop),
                number_of_actions=get_number(page, number_actions_prop),
                why=get_text(page, why_prop),
                description=get_text(page, description_prop),
                type_text=get_text(page, type_prop),
                place=get_text(page, place_prop),
                resource_ids=resource_ids,
                resource_names=resource_names,
                url=page.get("url", ""),
            )
        )
    return sorted(goals, key=lambda item: item.name.lower())


def detect_next_action_prop(bundle: DatabaseBundle) -> Optional[str]:
    return find_property(
        bundle.schema,
        ["Next Action", "Next", "Is Next", "Is Next Action", "Priority", "Action Type"],
    )


def build_actions(bundle: DatabaseBundle, area_id_to_name: Mapping[str, str], goal_id_to_name: Mapping[str, str]) -> tuple[list[ActionItem], bool]:
    props = DB_CONFIGS["actions"].props
    title_prop = env_override("actions", "title", bundle.title_prop or "Nombre")
    area_prop = env_override("actions", "area", props["area"])
    count_prop = env_override("actions", "count", props["count"])
    date_prop_name = env_override("actions", "date", props["date"])
    hide_show_prop = env_override("actions", "hide_show", props["hide_show"])
    importance_prop = env_override("actions", "importance_level", props["importance_level"])
    goal_prop = env_override("actions", "related_goal", props["related_goal"])
    relative_prop = env_override("actions", "relative_amount", props["relative_amount"])
    status_prop = env_override("actions", "status", props["status"])
    urgency_prop = env_override("actions", "urgency", props["urgency"])
    next_prop = env_override("actions", "next_action", detect_next_action_prop(bundle))
    next_prop_found = bool(next_prop and next_prop in bundle.properties)

    actions: list[ActionItem] = []
    for page in bundle.pages:
        name = get_text(page, title_prop) or "Untitled Action"
        area_ids, area_names = relation_names(page, area_prop, area_id_to_name)
        goal_ids, goal_names = relation_names(page, goal_prop, goal_id_to_name)
        date_display, due = date_text(page_prop(page, date_prop_name))
        status = get_text(page, status_prop) or ""
        next_text = get_text(page, next_prop)
        next_action = False
        if next_prop_found:
            next_action = truthy_text(next_text)
        elif "next" in normalize(status):
            next_action = True

        relative_text = get_text(page, relative_prop)
        actions.append(
            ActionItem(
                id=page["id"],
                name=name,
                area_ids=area_ids,
                area_names=area_names,
                goal_ids=goal_ids,
                goal_names=goal_names,
                date_text=date_display,
                due_date=due,
                count=get_number(page, count_prop),
                status=status,
                hide_show=get_text(page, hide_show_prop),
                importance=get_number(page, importance_prop),
                urgency=get_number(page, urgency_prop),
                relative_amount_text=relative_text,
                relative_amount_number=as_percent_number(relative_text),
                next_action=next_action,
                url=page.get("url", ""),
            )
        )
    return sorted(actions, key=action_sort_key), next_prop_found


def action_sort_key(action: ActionItem) -> tuple[Any, ...]:
    far_future = date(2999, 12, 31)
    return (
        action.done,
        not action.next_action,
        action.due_date or far_future,
        -(action.urgency or 0),
        -(action.importance or 0),
        action.name.lower(),
    )


def build_resources(bundle: DatabaseBundle, goal_id_to_name: Mapping[str, str]) -> list[ResourceItem]:
    props = DB_CONFIGS["resources"].props
    title_prop = env_override("resources", "title", bundle.title_prop or "Name")
    goal_prop = env_override("resources", "related_goals", props["related_goals"])
    type_prop = env_override("resources", "type", props["type"])
    author_prop = env_override("resources", "author", props["author"])
    date_prop_name = env_override("resources", "date", props["date"])
    url_prop = env_override("resources", "url", props["url"])

    resources: list[ResourceItem] = []
    for page in bundle.pages:
        goal_ids, goal_names = relation_names(page, goal_prop, goal_id_to_name)
        resources.append(
            ResourceItem(
                id=page["id"],
                name=get_text(page, title_prop) or "Untitled Resource",
                goal_ids=goal_ids,
                goal_names=goal_names,
                type_text=get_text(page, type_prop),
                author=get_text(page, author_prop),
                date_text=get_text(page, date_prop_name),
                url_value=get_text(page, url_prop),
                notion_url=page.get("url", ""),
            )
        )
    return sorted(resources, key=lambda item: item.name.lower())


def build_workouts(bundle: DatabaseBundle) -> list[WorkoutItem]:
    props = DB_CONFIGS["workouts"].props
    title_prop = env_override("workouts", "title", bundle.title_prop or "Name")
    created_prop = env_override("workouts", "created", props["created"])
    target_prop = env_override("workouts", "target", props["target"])
    time_prop = env_override("workouts", "time", props["time"])
    type_prop = env_override("workouts", "type", props["type"])

    workouts: list[WorkoutItem] = []
    for page in bundle.pages:
        created_display, created_date = date_text(page_prop(page, created_prop))
        duration_text = get_text(page, time_prop)
        workouts.append(
            WorkoutItem(
                id=page["id"],
                name=get_text(page, title_prop) or "Untitled Workout",
                created_text=created_display,
                workout_date=created_date,
                target=get_text(page, target_prop),
                duration_text=duration_text,
                duration_minutes=duration_to_minutes(duration_text),
                type_text=get_text(page, type_prop),
                url=page.get("url", ""),
            )
        )
    return sorted(workouts, key=lambda item: (item.workout_date or date.min, item.name.lower()), reverse=True)


def build_finance(bundle: DatabaseBundle) -> list[FinanceItem]:
    props = DB_CONFIGS["finance"].props
    title_prop = env_override("finance", "title", bundle.title_prop or "Name")
    created_prop = env_override("finance", "created", props["created"])
    amount_prop = env_override("finance", "amount", props["amount"])
    category_prop = env_override("finance", "category", props["category"])

    finance: list[FinanceItem] = []
    for page in bundle.pages:
        created_display, transaction_date = date_text(page_prop(page, created_prop))
        amount_text = get_text(page, amount_prop)
        amount_number = as_number(amount_text)
        finance.append(
            FinanceItem(
                id=page["id"],
                name=get_text(page, title_prop) or "Untitled Transaction",
                created_text=created_display,
                transaction_date=transaction_date,
                amount_text=amount_text,
                amount_number=amount_number,
                currency_symbol=infer_currency_symbol(amount_text),
                category=get_text(page, category_prop) or "Uncategorized",
                url=page.get("url", ""),
            )
        )
    return sorted(finance, key=lambda item: (item.transaction_date or date.min, item.name.lower()), reverse=True)


def build_reading(bundle: DatabaseBundle) -> list[ReadingItem]:
    if not bundle.pages:
        return []
    title_prop = bundle.title_prop or title_property_from_schema(bundle.schema, None)
    status_prop = find_property(bundle.schema, ["Status", "Reading Status", "State"])
    progress_prop = find_property(bundle.schema, ["Progress", "Percent", "%", "Pages", "Current Page"])
    goal_prop = find_property(bundle.schema, ["Goal", "Reading Goal", "Target"])
    author_prop = find_property(bundle.schema, ["Author", "Writer"])
    date_prop_name = find_property(bundle.schema, ["Date", "Started", "Finished", "Created"])

    reading: list[ReadingItem] = []
    for page in bundle.pages:
        progress_text = get_text(page, progress_prop)
        reading.append(
            ReadingItem(
                id=page["id"],
                name=get_text(page, title_prop) or "Untitled Book",
                status=get_text(page, status_prop),
                progress_text=progress_text,
                progress_number=as_percent_number(progress_text),
                goal_text=get_text(page, goal_prop),
                author=get_text(page, author_prop),
                date_text=get_text(page, date_prop_name),
                url=page.get("url", ""),
            )
        )
    return sorted(reading, key=lambda item: (not item.currently_reading, item.name.lower()))


def build_life_os_data(bundles: dict[str, DatabaseBundle], warnings: list[str]) -> LifeOSData:
    # First pass: simple id maps for relations.
    area_id_to_name = build_id_to_name(bundles["areas"])
    goal_id_to_name = build_id_to_name(bundles["goals"])
    resource_id_to_name = build_id_to_name(bundles["resources"])

    areas = build_areas(bundles["areas"])
    goals = build_goals(bundles["goals"], area_id_to_name, resource_id_to_name)
    # Goal map is rebuilt from parsed goals to support empty/missing titles more consistently.
    goal_id_to_name = {goal.id: goal.name for goal in goals}
    resources = build_resources(bundles["resources"], goal_id_to_name)
    resource_id_to_name = {resource.id: resource.name for resource in resources}
    goals = build_goals(bundles["goals"], area_id_to_name, resource_id_to_name)
    actions, next_prop_found = build_actions(bundles["actions"], area_id_to_name, goal_id_to_name)
    workouts = build_workouts(bundles["workouts"])
    finance = build_finance(bundles["finance"])
    reading = build_reading(bundles["reading"])

    return LifeOSData(
        areas=areas,
        goals=goals,
        actions=actions,
        resources=resources,
        workouts=workouts,
        finance=finance,
        reading=reading,
        warnings=warnings,
        next_action_property_found=next_prop_found,
        reading_configured=bool(bundles["reading"].database_id),
    )


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def md_link(label: str, href: str) -> str:
    label = clean_text(label).replace("[", "(").replace("]", ")") or "Untitled"
    href = href.replace(" ", "%20")
    return f"[{label}]({href})"


def item_link(name: str, folder: str) -> str:
    return md_link(name, f"{folder}/{safe_filename(name)}")


def goal_link(name: str) -> str:
    return item_link(name, "goals")


def area_link(name: str) -> str:
    return item_link(name, "areas")


def bullet_list(lines: Iterable[str], empty: str = "Nothing to show.") -> str:
    items = [line for line in lines if clean_text(line)]
    if not items:
        return f"- {empty}\n"
    return "".join(f"- {line}\n" for line in items)


def page_header(title: str, generated_on: date) -> list[str]:
    return [
        f"# {title}",
        "",
        f"Generated: {generated_on.isoformat()}",
        "",
        "---",
        "",
    ]


def quick_nav(prefix: str = "") -> str:
    return (
        f"[Dashboard]({prefix}dashboard.md) | "
        f"[Today]({prefix}today.md) | "
        f"[Reading]({prefix}reading.md) | "
        f"[Workouts]({prefix}workouts.md) | "
        f"[Finance]({prefix}finance.md)"
    )


def active_goals(goals: Iterable[GoalItem]) -> list[GoalItem]:
    return [goal for goal in goals if not goal.done]


def active_actions(actions: Iterable[ActionItem]) -> list[ActionItem]:
    return [action for action in actions if not action.done and normalize(action.hide_show) not in {"hide", "hidden"}]


def due_today(actions: Iterable[ActionItem], today: date) -> list[ActionItem]:
    return [action for action in active_actions(actions) if action.due_date == today]


def current_week_range(today: date) -> tuple[date, date]:
    start = today - timedelta(days=today.weekday())
    end = start + timedelta(days=6)
    return start, end


def render_action_line(action: ActionItem) -> str:
    parts = [clean_text(action.name)]
    if action.status:
        parts.append(f"Status: {action.status}")
    if action.date_text:
        parts.append(f"Due: {action.date_text}")
    if action.goal_names:
        parts.append("Goal: " + comma_join(action.goal_names))
    if action.area_names:
        parts.append("Area: " + comma_join(action.area_names))
    if action.next_action:
        parts.append("NEXT")
    return " — ".join(parts)


def render_goal_line(goal: GoalItem, include_area: bool = True) -> str:
    parts = [goal_link(goal.name)]
    if include_area and goal.area_names:
        parts.append("Area: " + comma_join(goal.area_names))
    if goal.progress_number is not None:
        parts.append(progress_bar(goal.progress_number))
    elif goal.progress_text:
        parts.append("Progress: " + goal.progress_text)
    if goal.date_text:
        parts.append("Date: " + goal.date_text)
    return " — ".join(parts)


def render_resource_line(resource: ResourceItem) -> str:
    label = resource.name
    if resource.url_value:
        label = md_link(resource.name, resource.url_value)
    parts = [label]
    if resource.type_text:
        parts.append(resource.type_text)
    if resource.author:
        parts.append("Author: " + resource.author)
    if resource.goal_names:
        parts.append("Goal: " + comma_join(resource.goal_names))
    return " — ".join(parts)


def finance_summary(finance: list[FinanceItem], today: date) -> tuple[float, str, dict[str, float], list[FinanceItem]]:
    month_items = [
        item
        for item in finance
        if item.transaction_date and item.transaction_date.year == today.year and item.transaction_date.month == today.month
    ]
    symbol = first_non_empty(item.currency_symbol for item in month_items) or first_non_empty(item.currency_symbol for item in finance)
    total = sum(item.amount_number or 0 for item in month_items)
    by_category: dict[str, float] = {}
    for item in month_items:
        by_category[item.category] = by_category.get(item.category, 0.0) + (item.amount_number or 0)
    return total, symbol, dict(sorted(by_category.items(), key=lambda kv: kv[1], reverse=True)), month_items


def render_dashboard(data: LifeOSData, cfg: RuntimeConfig) -> str:
    active_goal_items = active_goals(data.goals)
    active_action_items = active_actions(data.actions)
    due_items = due_today(data.actions, cfg.today)
    active_area_names = sorted({name for goal in active_goal_items for name in goal.area_names})

    current_reading = [item for item in data.reading if item.currently_reading]
    week_start, week_end = current_week_range(cfg.today)
    week_workouts = [w for w in data.workouts if w.workout_date and week_start <= w.workout_date <= week_end]
    duration_total = sum(w.duration_minutes or 0 for w in week_workouts)
    month_total, currency, by_category, _ = finance_summary(data.finance, cfg.today)

    lines = page_header("Kindle Life OS", cfg.today)
    lines += [
        "## Today",
        "",
        f"- Due today: **{len(due_items)}**",
        f"- Active actions: **{len(active_action_items)}**",
        f"- Next actions: **{sum(1 for action in active_action_items if action.next_action)}**",
        "",
        "## Areas and Goals",
        "",
        f"- Active areas: **{len(active_area_names) or len(data.areas)}**",
        f"- Active goals: **{len(active_goal_items)}**",
        "",
        "### Active Areas",
        "",
    ]
    lines.append(bullet_list((area_link(area.name) for area in data.areas if not active_area_names or area.name in active_area_names), "No active areas."))

    lines += [
        "## Reading Summary",
        "",
    ]
    if not data.reading_configured:
        lines.append("- Reading database is not configured. Set NOTION_READING_DB_ID to enable this section.\n")
    else:
        avg_progress = None
        progress_values = [item.progress_number for item in current_reading if item.progress_number is not None]
        if progress_values:
            avg_progress = sum(progress_values) / len(progress_values)
        lines.append(f"- Currently reading: **{len(current_reading)}**\n")
        if avg_progress is not None:
            lines.append(f"- Average progress: **{avg_progress:.0%}**\n")

    lines += [
        "## Workout Summary",
        "",
        f"- Workouts this week: **{len(week_workouts)}**",
        f"- Duration this week: **{duration_total:.0f} min**" if duration_total else "- Duration this week: **not tracked**",
        "",
        "## Finance Summary",
        "",
        f"- Spending this month: **{format_amount(month_total, currency)}**",
    ]
    if by_category:
        top_category, top_amount = next(iter(by_category.items()))
        lines.append(f"- Top category: **{top_category}** ({format_amount(top_amount, currency)})")
    else:
        lines.append("- Top category: **not tracked**")

    lines += [
        "",
        "## Quick Navigation",
        "",
        f"- {md_link('Today', 'today.md')}",
        f"- {md_link('Reading', 'reading.md')}",
        f"- {md_link('Workouts', 'workouts.md')}",
        f"- {md_link('Finance', 'finance.md')}",
        "",
        "### Areas",
        "",
    ]
    lines.append(bullet_list((area_link(area.name) for area in data.areas), "No areas found."))
    lines += ["", "### Active Goals", ""]
    lines.append(bullet_list((render_goal_line(goal) for goal in active_goal_items[: cfg.max_items]), "No active goals."))
    lines += ["", "### Warnings", ""]
    if data.warnings:
        lines.append(bullet_list(data.warnings, "No warnings."))
    else:
        lines.append("- No warnings.\n")
    return "\n".join(lines).rstrip() + "\n"


def render_today(data: LifeOSData, cfg: RuntimeConfig) -> str:
    active = active_actions(data.actions)
    due = due_today(data.actions, cfg.today)
    next_actions = [action for action in active if action.next_action]
    if not next_actions:
        next_actions = active[: cfg.max_items]

    lines = page_header("Today", cfg.today)
    lines += [quick_nav(), "", "## Due Today", ""]
    lines.append(bullet_list((render_action_line(action) for action in due), "No tasks due today."))

    header = "## Next Actions" if data.next_action_property_found else "## Suggested Next Actions"
    lines += ["", header, ""]
    lines.append(bullet_list((render_action_line(action) for action in next_actions[: cfg.max_items]), "No next actions."))

    lines += ["", "## Tasks Grouped by Area", ""]
    for area_name, actions in group_by(active, "area_names").items():
        lines += [f"### {area_name}", ""]
        lines.append(bullet_list((render_action_line(action) for action in actions[: cfg.max_items]), "No active actions."))
        lines.append("")

    lines += ["## Tasks Grouped by Goal", ""]
    for goal_name, actions in group_by(active, "goal_names").items():
        lines += [f"### {goal_name}", ""]
        lines.append(bullet_list((render_action_line(action) for action in actions[: cfg.max_items]), "No active actions."))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_area_page(area: AreaItem, data: LifeOSData, cfg: RuntimeConfig) -> str:
    goals = [goal for goal in data.goals if area.id in goal.area_ids or area.name in goal.area_names]
    if not goals and area.goals_text:
        # Fallback for rollups without relation ids.
        goals = [goal for goal in data.goals if goal.name in area.goals_text]

    lines = page_header(area.name, cfg.today)
    lines += [quick_nav("../"), ""]
    if area.progress is not None:
        lines += [f"Progress: {progress_bar(area.progress)}", ""]

    if not goals:
        lines += ["No goals linked to this area.", ""]
    for goal in goals:
        goal_actions = [action for action in active_actions(data.actions) if goal.id in action.goal_ids or goal.name in action.goal_names]
        goal_resources = [resource for resource in data.resources if goal.id in resource.goal_ids or goal.name in resource.goal_names]
        # Also include goal's directly related resources if Notion exposes them on Goals.
        goal_resource_names = set(goal.resource_names)
        if goal_resource_names:
            goal_resources.extend([r for r in data.resources if r.name in goal_resource_names and r not in goal_resources])

        lines += [f"## {goal_link(goal.name)}", ""]
        if goal.description:
            lines += [goal.description, ""]
        if goal.why:
            lines += ["**Why:**", "", goal.why, ""]
        if goal.progress_number is not None:
            lines += [f"Progress: {progress_bar(goal.progress_number)}", ""]
        elif goal.progress_text:
            lines += [f"Progress: {goal.progress_text}", ""]

        lines += ["### Next Actions", ""]
        ordered_actions = sorted(goal_actions, key=action_sort_key)
        lines.append(bullet_list((render_action_line(action) for action in ordered_actions[: cfg.max_items]), "No active actions."))

        lines += ["", "### Resources", ""]
        lines.append(bullet_list((render_resource_line(resource) for resource in goal_resources[: cfg.max_items]), "No resources linked."))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_goal_page(goal: GoalItem, data: LifeOSData, cfg: RuntimeConfig) -> str:
    actions = [action for action in data.actions if goal.id in action.goal_ids or goal.name in action.goal_names]
    resources = [resource for resource in data.resources if goal.id in resource.goal_ids or goal.name in resource.goal_names]
    if goal.resource_names:
        resources.extend([r for r in data.resources if r.name in goal.resource_names and r not in resources])

    lines = page_header(goal.name, cfg.today)
    lines += [quick_nav("../"), ""]

    lines += ["## Goal Information", ""]
    lines.append(f"- Status: {'Done' if goal.done else 'Active'}")
    if goal.area_names:
        lines.append("- Area: " + comma_join(area_link(name) for name in goal.area_names))
    if goal.date_text:
        lines.append(f"- Date: {goal.date_text}")
    if goal.month:
        lines.append(f"- Month: {goal.month}")
    if goal.type_text:
        lines.append(f"- Type: {goal.type_text}")
    if goal.place:
        lines.append(f"- Place: {goal.place}")
    lines.append("")

    if goal.description:
        lines += ["## Description", "", goal.description, ""]
    if goal.why:
        lines += ["## Why", "", goal.why, ""]

    lines += ["## Progress", ""]
    if goal.progress_number is not None:
        lines.append(f"- {progress_bar(goal.progress_number)}")
    elif goal.progress_text:
        lines.append(f"- Progress: {goal.progress_text}")
    if goal.completed_actions is not None:
        lines.append(f"- Completed Actions: {goal.completed_actions:g}")
    if goal.number_of_actions is not None:
        lines.append(f"- Number Of Actions: {goal.number_of_actions:g}")
    if goal.count_numerical is not None:
        lines.append(f"- Count (Numerical): {goal.count_numerical:g}")
    if goal.goal_value is not None:
        lines.append(f"- Goal: {goal.goal_value:g}")
    if not lines[-1].startswith("-"):
        lines.append("- No progress fields found.")
    lines.append("")

    lines += ["## Related Actions", ""]
    lines.append(bullet_list((render_action_line(action) for action in sorted(actions, key=action_sort_key)[: cfg.max_items]), "No related actions."))
    lines += ["", "## Related Resources", ""]
    lines.append(bullet_list((render_resource_line(resource) for resource in resources[: cfg.max_items]), "No related resources."))
    return "\n".join(lines).rstrip() + "\n"


def render_reading(data: LifeOSData, cfg: RuntimeConfig) -> str:
    lines = page_header("Reading", cfg.today)
    lines += [quick_nav(), ""]
    if not data.reading_configured:
        lines += [
            "Reading database was not configured.",
            "",
            "Set `NOTION_READING_DB_ID` to generate this page from Notion.",
            "",
        ]
        return "\n".join(lines).rstrip() + "\n"

    current = [item for item in data.reading if item.currently_reading]
    progress_values = [item.progress_number for item in data.reading if item.progress_number is not None]
    avg_progress = sum(progress_values) / len(progress_values) if progress_values else None

    lines += ["## Currently Reading", ""]
    lines.append(bullet_list((render_reading_line(item) for item in current[: cfg.max_items]), "No current books."))
    lines += ["", "## Reading Goals", ""]
    lines.append(bullet_list((f"{item.name}: {item.goal_text}" for item in data.reading if item.goal_text), "No reading goals found."))
    lines += ["", "## Reading Statistics", ""]
    lines.append(f"- Total books: {len(data.reading)}")
    lines.append(f"- Currently reading: {len(current)}")
    if avg_progress is not None:
        lines.append(f"- Average progress: {avg_progress:.0%}")
    else:
        lines.append("- Average progress: not tracked")
    lines.append("")
    lines += ["## All Reading Items", ""]
    lines.append(bullet_list((render_reading_line(item) for item in data.reading[: cfg.max_items]), "No reading items."))
    return "\n".join(lines).rstrip() + "\n"


def render_reading_line(item: ReadingItem) -> str:
    parts = [item.name]
    if item.author:
        parts.append("Author: " + item.author)
    if item.status:
        parts.append("Status: " + item.status)
    if item.progress_number is not None:
        parts.append(progress_bar(item.progress_number))
    elif item.progress_text:
        parts.append("Progress: " + item.progress_text)
    return " — ".join(parts)


def render_workouts(data: LifeOSData, cfg: RuntimeConfig) -> str:
    week_start, week_end = current_week_range(cfg.today)
    week_workouts = [w for w in data.workouts if w.workout_date and week_start <= w.workout_date <= week_end]
    week_duration = sum(w.duration_minutes or 0 for w in week_workouts)

    lines = page_header("Workouts", cfg.today)
    lines += [quick_nav(), "", "## Weekly Summary", ""]
    lines.append(f"- Week: {week_start.isoformat()} to {week_end.isoformat()}")
    lines.append(f"- Workout count: {len(week_workouts)}")
    lines.append(f"- Duration total: {week_duration:.0f} min" if week_duration else "- Duration total: not tracked")
    lines.append("")

    lines += ["## Recent Workouts", ""]
    lines.append(bullet_list((render_workout_line(item) for item in data.workouts[: cfg.max_items]), "No workouts found."))
    return "\n".join(lines).rstrip() + "\n"


def render_workout_line(item: WorkoutItem) -> str:
    parts = [item.name]
    if item.workout_date:
        parts.append(item.workout_date.isoformat())
    elif item.created_text:
        parts.append(item.created_text)
    if item.type_text:
        parts.append(item.type_text)
    if item.target:
        parts.append("Target: " + item.target)
    if item.duration_text:
        parts.append("Time: " + item.duration_text)
    return " — ".join(parts)


def render_finance(data: LifeOSData, cfg: RuntimeConfig) -> str:
    month_total, currency, by_category, month_items = finance_summary(data.finance, cfg.today)

    lines = page_header("Finance", cfg.today)
    lines += [quick_nav(), "", "## Monthly Spending", ""]
    lines.append(f"- Month: {cfg.today:%B %Y}")
    lines.append(f"- Total: {format_amount(month_total, currency)}")
    lines.append(f"- Transactions this month: {len(month_items)}")
    lines.append("")

    lines += ["## Spending by Category", ""]
    if by_category:
        for category, amount in by_category.items():
            lines.append(f"- {category}: {format_amount(amount, currency)}")
    else:
        lines.append("- No categorized spending this month.")
    lines.append("")

    lines += ["## Recent Transactions", ""]
    lines.append(bullet_list((render_finance_line(item, currency) for item in data.finance[: cfg.max_items]), "No transactions found."))
    return "\n".join(lines).rstrip() + "\n"


def render_finance_line(item: FinanceItem, fallback_currency: str) -> str:
    symbol = item.currency_symbol or fallback_currency
    amount = item.amount_text if item.amount_number is None and item.amount_text else format_amount(item.amount_number, symbol)
    parts = [item.name]
    if amount != "—":
        parts.append(amount)
    if item.category:
        parts.append(item.category)
    if item.transaction_date:
        parts.append(item.transaction_date.isoformat())
    elif item.created_text:
        parts.append(item.created_text)
    return " — ".join(parts)


# ---------------------------------------------------------------------------
# File writing
# ---------------------------------------------------------------------------


def prepare_output_dir(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename in ["dashboard.md", "today.md", "reading.md", "workouts.md", "finance.md"]:
        path = output_dir / filename
        if path.exists():
            path.unlink()
    for folder in ["areas", "goals"]:
        path = output_dir / folder
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    log(f"Wrote {path}")


def write_all_pages(data: LifeOSData, cfg: RuntimeConfig) -> None:
    prepare_output_dir(cfg.output_dir)
    write_text(cfg.output_dir / "dashboard.md", render_dashboard(data, cfg))
    write_text(cfg.output_dir / "today.md", render_today(data, cfg))
    write_text(cfg.output_dir / "reading.md", render_reading(data, cfg))
    write_text(cfg.output_dir / "workouts.md", render_workouts(data, cfg))
    write_text(cfg.output_dir / "finance.md", render_finance(data, cfg))

    used_area_files: set[str] = set()
    for area in data.areas:
        filename = unique_filename(area.name, used_area_files, area.id)
        write_text(cfg.output_dir / "areas" / filename, render_area_page(area, data, cfg))

    used_goal_files: set[str] = set()
    for goal in data.goals:
        filename = unique_filename(goal.name, used_goal_files, goal.id)
        write_text(cfg.output_dir / "goals" / filename, render_goal_page(goal, data, cfg))


def unique_filename(name: str, used: set[str], page_id: str) -> str:
    base = safe_filename(name, fallback=page_id[:8] or "untitled")
    if base not in used:
        used.add(base)
        return base
    stem = base[:-3]
    candidate = f"{stem}-{page_id[:8]}.md"
    used.add(candidate)
    return candidate


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        cfg = load_runtime_config()
        warnings: list[str] = []
        client = Client(auth=cfg.notion_token)
        bundles = load_databases(client, cfg, warnings)
        data = build_life_os_data(bundles, warnings)
        write_all_pages(data, cfg)
        log("Kindle Life OS generation complete.")
        return 0
    except SystemExit:
        raise
    except APIResponseError as exc:
        print(f"Notion API error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Dashboard generation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
