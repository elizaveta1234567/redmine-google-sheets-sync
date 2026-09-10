from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import gspread
from google.oauth2.service_account import Credentials

# Нормальный UTF-8 вывод в Windows/Git Bash.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from redmine_client import (
    REDMINE_API_KEY,
    REDMINE_PASSWORD,
    REDMINE_URL,
    REDMINE_USERNAME,
    VERIFY_SSL,
    RedmineAPIError,
    RedmineClient,
)

# =============================================================================
# НАСТРОЙКИ
# =============================================================================
SCRIPT_VERSION = "5.6-google-sheets-errors-only-nitro-rush-060"

PROJECT_NAME = "Nitro Rush"
VERSION_FILTER = "0.6.0"
INCLUDE_CLOSED = True
ISSUES_SORT = "id:desc"
TRACKER_FILTER = "Ошибка"

# ID Google-таблицы — часть URL между /d/ и /edit.
# https://docs.google.com/spreadsheets/d/ЭТОТ_ID/edit
GOOGLE_SHEET_ID = "1PBEBm5SY_D92_nA7FCsTlK02A88ZFsETY2xbbvp8U9s"
# Конкретная вкладка из URL: ...#gid=896698793
GOOGLE_WORKSHEET_GID = 0

# JSON-ключ service account. Положи файл рядом со скриптом.
SERVICE_ACCOUNT_FILE = Path(__file__).resolve().parent / "service_account.json"

COMMENT_CUSTOM_FIELD = "Комментарий"
FETCH_LAST_COMMENT_IF_FIELD_EMPTY = True
MAX_COMMENT_LENGTH: int | None = None


# Цвета для колонки «Приоритет». Значения приводятся к нижнему регистру.
PRIORITY_COLORS = {
    "низкий": {"red": 0.82, "green": 0.94, "blue": 0.82},
    "очень низкий": {"red": 0.82, "green": 0.94, "blue": 0.82},
    "нормальный": {"red": 1.0, "green": 0.95, "blue": 0.70},
    "высокий": {"red": 1.0, "green": 0.80, "blue": 0.80},
    "срочный": {"red": 1.0, "green": 0.68, "blue": 0.68},
    "немедленный": {"red": 1.0, "green": 0.58, "blue": 0.58},
}

# Цвета статусов — по палитре из Redmine на скриншоте пользователя.
STATUS_COLORS = {
    "новая": {"red": 0.90, "green": 0.91, "blue": 0.92},          # светло-серый
    "на тестировании": {"red": 0.91, "green": 0.82, "blue": 0.95}, # светло-фиолетовый
    "решена": {"red": 0.82, "green": 0.94, "blue": 0.75},         # светло-зелёный
    "возвращена": {"red": 0.98, "green": 0.82, "blue": 0.79},     # светло-красный
    "в работе": {"red": 1.00, "green": 0.88, "blue": 0.63},       # светло-оранжевый
    "бэклог": {"red": 0.86, "green": 0.87, "blue": 0.88},         # серый
}

# Иерархия отображения задач в Google Sheets.
# 1) Активные задачи — по приоритету.
# 2) Бэклог — почти в самом низу.
# 3) Решённые/закрытые — в самом низу.
PRIORITY_ORDER = {
    "срочный": 0,
    "немедленный": 0,
    "высокий": 1,
    "нормальный": 2,
    "низкий": 3,
    "очень низкий": 4,
}

BACKLOG_STATUSES = {"бэклог"}
SOLVED_STATUSES = {"решена", "закрыта", "закрыт"}


def row_hierarchy_key(row: list[str]) -> tuple[int, int]:
    """Ключ сортировки строки: активные -> бэклог -> решённые."""
    status = normalize_text(row[2]).casefold().strip() if len(row) > 2 else ""
    priority = normalize_text(row[5]).casefold().strip() if len(row) > 5 else ""

    if status in SOLVED_STATUSES:
        # Решённые всегда в самом низу, независимо от приоритета.
        return (2, 0)

    if status in BACKLOG_STATUSES:
        # Бэклог идёт после всех активных, но перед решёнными.
        return (1, 0)

    # Активные: Срочный -> Высокий -> Нормальный -> Низкий -> Очень низкий.
    # Неизвестный/пустой приоритет ставим после известных активных приоритетов.
    return (0, PRIORITY_ORDER.get(priority, 5))



HEADERS = [
    "Название",
    "Ссылка",
    "Статус",
    "Комментарий",
    "Версия",
    "Приоритет",
    "Назначена",
]

# =============================================================================
# REDMINE HELPERS
# =============================================================================
def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(normalize_text(item) for item in value if item is not None)
    if isinstance(value, dict):
        if "name" in value:
            return str(value["name"])
        return str(value)
    return str(value).strip()


def get_custom_field_with_presence(
    issue: dict[str, Any], field_names: str | Iterable[str]
) -> tuple[bool, str]:
    names = [field_names] if isinstance(field_names, str) else list(field_names)
    wanted = {str(name).casefold().strip() for name in names}
    for field in issue.get("custom_fields", []) or []:
        name = str(field.get("name", "")).casefold().strip()
        if name in wanted:
            return True, normalize_text(field.get("value"))
    return False, ""


def get_custom_field(issue: dict[str, Any], field_name: str) -> str:
    return get_custom_field_with_presence(issue, field_name)[1]


def find_project_by_name(redmine: RedmineClient, project_name: str) -> dict[str, Any]:
    projects = redmine.get_projects()
    target = project_name.casefold().strip()

    exact = [
        p for p in projects
        if str(p.get("name", "")).casefold().strip() == target
        or str(p.get("identifier", "")).casefold().strip() == target
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise RuntimeError(f"Нашлось несколько проектов с именем/identifier '{project_name}'.")

    partial = [
        p for p in projects
        if target in str(p.get("name", "")).casefold()
        or target in str(p.get("identifier", "")).casefold()
    ]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        visible = "\n".join(
            f"  - {p.get('name', '?')} ({p.get('identifier', '-')})" for p in projects[:50]
        )
        raise RuntimeError(
            f"Проект '{project_name}' не найден.\nДоступные проекты (первые 50):\n{visible}"
        )

    options = "\n".join(
        f"  - {p.get('name', '?')} ({p.get('identifier', '-')})" for p in partial
    )
    raise RuntimeError(
        f"Название '{project_name}' неоднозначно. Подходят:\n{options}\n"
        "Укажи точное название проекта или identifier."
    )


def find_versions_by_filter(
    redmine: RedmineClient,
    project: dict[str, Any],
    version_filter: str,
) -> list[dict[str, Any]]:
    """Возвращает все версии, в названии которых встречается marker.

    Например, VERSION_FILTER = "0.5.1" найдёт:
      - 0.5.1 (пуши, улучшения, награды)
      - Улучшение версии 0.5.1 Android
      - 0.5.1 iOS
    """
    target = version_filter.strip()
    if not target:
        return []

    project_id = int(project["id"])
    get_versions = getattr(redmine, "get_project_versions", None)
    if callable(get_versions):
        versions = get_versions(project_id)
    else:
        data = redmine.get(f"projects/{project_id}/versions.json")
        versions = data.get("versions", []) if isinstance(data, dict) else []

    target_cf = target.casefold()
    matches = [
        v for v in versions
        if target_cf in str(v.get("name", "")).casefold()
    ]

    if not matches:
        available = "\n".join(
            f"  - [{v.get('id', '?')}] {v.get('name', '?')} ({v.get('status', '-')})"
            for v in versions
        ) or "  (версии не найдены)"
        raise RuntimeError(
            f"Не найдено ни одной версии с пометкой '{version_filter}' "
            f"в проекте '{project.get('name', '')}'.\n"
            f"Доступные версии:\n{available}"
        )

    # На всякий случай убираем дубли по ID и сохраняем порядок Redmine.
    unique: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for version in matches:
        version_id = int(version["id"])
        if version_id not in seen_ids:
            seen_ids.add(version_id)
            unique.append(version)
    return unique


def get_last_comment(redmine: RedmineClient, issue_id: int) -> str:
    issue = redmine.get_issue(issue_id, include="journals")
    for journal in reversed(issue.get("journals", []) or []):
        note = normalize_text(journal.get("notes"))
        if note:
            return note
    return ""


def trim_comment(text: str) -> str:
    if MAX_COMMENT_LENGTH is None or len(text) <= MAX_COMMENT_LENGTH:
        return text
    return text[: MAX_COMMENT_LENGTH - 1].rstrip() + "…"


def build_rows(redmine: RedmineClient, issues: list[dict[str, Any]]) -> list[list[str]]:
    """Build rows from fresh per-issue data.

    The collection endpoint /issues.json is useful for finding issue IDs, but on some
    Redmine/proxy configurations its nested fields may lag behind. For synchronization
    we therefore re-read every issue via /issues/<id>.json and use that response as
    the source of truth for all exported fields.
    """
    rows: list[list[str]] = []
    total = len(issues)

    for index, issue_stub in enumerate(issues, start=1):
        issue_id = int(issue_stub["id"])
        print(f"[{index}/{total}] Обновляю задачу #{issue_id}...", end="\r")

        try:
            # journals are fetched in the same request, so the latest comment is also fresh.
            issue = redmine.get_issue(issue_id, include="journals")
        except RedmineAPIError as exc:
            print(f"\nПредупреждение: не удалось обновить задачу #{issue_id}: {exc}")
            issue = issue_stub

        tracker = normalize_text(issue.get("tracker"))
        if tracker.casefold().strip() != TRACKER_FILTER.casefold().strip():
            continue

        subject = normalize_text(issue.get("subject"))
        url = f"{redmine.base_url}/issues/{issue_id}"
        status = normalize_text(issue.get("status"))
        version = normalize_text(issue.get("fixed_version"))
        assigned_to = normalize_text(issue.get("assigned_to"))

        priority = normalize_text(issue.get("priority"))
        comment = get_custom_field(issue, COMMENT_CUSTOM_FIELD)

        if not comment and FETCH_LAST_COMMENT_IF_FIELD_EMPTY:
            for journal in reversed(issue.get("journals", []) or []):
                note = normalize_text(journal.get("notes"))
                if note:
                    comment = note
                    break

        rows.append([
            subject,
            url,
            status,
            trim_comment(comment),
            version,
            priority,
            assigned_to,
        ])

    if total:
        print(" " * 120, end="\r")

    # Стабильная сортировка: внутри одной ступени сохраняется исходный порядок
    # (issues приходят по ID по убыванию, поэтому новые задачи остаются выше).
    rows.sort(key=row_hierarchy_key)
    return rows

# =============================================================================
# GOOGLE SHEETS
# =============================================================================
def connect_google_sheet():
    if GOOGLE_SHEET_ID == "PASTE_GOOGLE_SHEET_ID_HERE" or not GOOGLE_SHEET_ID.strip():
        raise RuntimeError("Укажи GOOGLE_SHEET_ID в настройках скрипта.")
    if not SERVICE_ACCOUNT_FILE.exists():
        raise RuntimeError(
            f"Не найден {SERVICE_ACCOUNT_FILE.name}. "
            "Положи JSON-ключ Google service account рядом со скриптом."
        )

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(str(SERVICE_ACCOUNT_FILE), scopes=scopes)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID.strip())

    worksheet = None
    for candidate in spreadsheet.worksheets():
        if int(candidate.id) == int(GOOGLE_WORKSHEET_GID):
            worksheet = candidate
            break

    if worksheet is None:
        raise RuntimeError(
            f"В таблице не найдена вкладка с gid={GOOGLE_WORKSHEET_GID}. "
            "Проверь ссылку на нужную вкладку."
        )

    return spreadsheet, worksheet


def format_sheet(spreadsheet, worksheet, row_count: int, rows: list[list[str]]) -> None:
    sheet_id = worksheet.id
    end_row = max(1, row_count + 1)
    requests = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 7},
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.4, "green": 0.4, "blue": 0.4},
                        "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True},
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment": "MIDDLE",
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": end_row, "startColumnIndex": 0, "endColumnIndex": 7},
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)",
            }
        },
        {
            "setBasicFilter": {
                "filter": {
                    "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": end_row, "startColumnIndex": 0, "endColumnIndex": 7}
                }
            }
        },
    ]

    # Сбрасываем фон колонки «Статус» (C), чтобы при смене статуса
    # не оставался цвет от предыдущего значения.
    if row_count > 0:
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": end_row,
                    "startColumnIndex": 2,
                    "endColumnIndex": 3,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 1, "green": 1, "blue": 1},
                        "textFormat": {"foregroundColor": {"red": 0, "green": 0, "blue": 0}},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat.foregroundColor)",
            }
        })

        # Красим ячейку статуса (колонка C) в зависимости от значения.
        for row_index, row in enumerate(rows, start=1):
            status = normalize_text(row[2]).casefold().strip() if len(row) > 2 else ""
            color = STATUS_COLORS.get(status)
            if not color:
                continue
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_index,
                        "endRowIndex": row_index + 1,
                        "startColumnIndex": 2,
                        "endColumnIndex": 3,
                    },
                    "cell": {"userEnteredFormat": {"backgroundColor": color}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            })

    # Сбрасываем фон колонки «Приоритет», чтобы после смены приоритета
    # не оставался цвет от предыдущего значения.
    if row_count > 0:
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": end_row,
                    "startColumnIndex": 5,
                    "endColumnIndex": 6,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 1, "green": 1, "blue": 1},
                        "textFormat": {"foregroundColor": {"red": 0, "green": 0, "blue": 0}},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat.foregroundColor)",
            }
        })

        # Красим только ячейку приоритета (колонка F) для каждой строки.
        for row_index, row in enumerate(rows, start=1):
            priority = normalize_text(row[5]).casefold().strip() if len(row) > 5 else ""
            color = PRIORITY_COLORS.get(priority)
            if not color:
                continue
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_index,
                        "endRowIndex": row_index + 1,
                        "startColumnIndex": 5,
                        "endColumnIndex": 6,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": color,
                            "textFormat": {"bold": priority in {"высокий", "срочный", "немедленный"}},
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat.bold)",
                }
            })

    widths = [360, 250, 150, 360, 140, 190, 190]
    for i, width in enumerate(widths):
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": width},
                "fields": "pixelSize",
            }
        })

    spreadsheet.batch_update({"requests": requests})


def sync_to_google_sheets(
    redmine: RedmineClient,
    project: dict[str, Any],
    issues: list[dict[str, Any]],
    selected_versions: list[dict[str, Any]],
) -> None:
    spreadsheet, worksheet = connect_google_sheet()
    rows = build_rows(redmine, issues)

    # Меняем только A:G. Всё справа от G остаётся нетронутым.
    worksheet.batch_clear(["A:G"])
    values = [HEADERS] + rows
    worksheet.update(range_name=f"A1:G{len(values)}", values=values, value_input_option="USER_ENTERED")
    format_sheet(spreadsheet, worksheet, len(rows), rows)

    print(f"Google Sheets обновлена: {spreadsheet.title} / {worksheet.title}")
    print(f"Проект: [{project['id']}] {project['name']}")
    if selected_versions:
        print("Версии: " + "; ".join(str(v.get("name", "")) for v in selected_versions))
    else:
        print("Версии: Все версии")
    print(f"Задач записано: {len(rows)}")
    print(f"Время синхронизации: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


def main() -> int:
    try:
        redmine = RedmineClient(
            base_url=REDMINE_URL,
            api_key=REDMINE_API_KEY,
            username=REDMINE_USERNAME,
            password=REDMINE_PASSWORD,
            verify_ssl=VERIFY_SSL,
        )

        me = redmine.get_current_user()
        user_name = (
            me.get("login")
            or f"{me.get('firstname', '')} {me.get('lastname', '')}".strip()
            or str(me.get("id", "?"))
        )
        print(f"Redmine -> Google Sheets Sync v{SCRIPT_VERSION}")
        print(f"Подключение к Redmine успешно: {user_name}")

        project = find_project_by_name(redmine, PROJECT_NAME)
        print(f"Проект найден: [{project['id']}] {project['name']}")

        selected_versions = find_versions_by_filter(redmine, project, VERSION_FILTER)

        if selected_versions:
            print(f"Найдено версий с пометкой '{VERSION_FILTER}': {len(selected_versions)}")
            for version in selected_versions:
                print(f"  - [{version['id']}] {version.get('name', '')}")
        else:
            print("Фильтр версии отключён: выгружаю задачи всех версий.")

        print("Получаю задачи...")
        issues_by_id: dict[int, dict[str, Any]] = {}

        if selected_versions:
            for version in selected_versions:
                version_issues = redmine.get_issues(
                    project_id=int(project["id"]),
                    include_closed=INCLUDE_CLOSED,
                    sort=ISSUES_SORT,
                    fixed_version_id=int(version["id"]),
                )
                print(
                    f"  {version.get('name', '')}: {len(version_issues)} задач"
                )
                for issue in version_issues:
                    issues_by_id[int(issue["id"])] = issue
            issues = sorted(
                issues_by_id.values(),
                key=lambda issue: int(issue.get("id", 0)),
                reverse=True,
            )
        else:
            issues = redmine.get_issues(
                project_id=int(project["id"]),
                include_closed=INCLUDE_CLOSED,
                sort=ISSUES_SORT,
            )

        print(f"Получено уникальных задач до фильтра по трекеру: {len(issues)}")
        print(f"В Google Sheets попадут только задачи с трекером: {TRACKER_FILTER}")

        sync_to_google_sheets(redmine, project, issues, selected_versions)
        return 0

    except (RedmineAPIError, RuntimeError, ValueError, KeyError, gspread.GSpreadException) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nОперация отменена пользователем.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
