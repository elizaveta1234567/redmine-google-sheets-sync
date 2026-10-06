from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import gspread
from google.oauth2.service_account import Credentials

# Нормальный UTF-8 вывод в Windows/Git Bash.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from redmine_client import (
    VERIFY_SSL,
    RedmineAPIError,
    RedmineClient,
)

# =============================================================================
# ВЕРСИЯ И КОНСТАНТЫ
# =============================================================================
SCRIPT_VERSION = "1.4-saved-key-countdown"

INCLUDE_CLOSED = True
ISSUES_SORT = "id:desc"
TRACKER_FILTER = "Ошибка"
SYNC_INTERVAL_MINUTES = 5
DEFAULT_REDMINE_URL = "https://redmine.justmoby.com/"

# JSON-ключ service account. Положи файл рядом со скриптом.
SERVICE_ACCOUNT_FILE = Path(__file__).resolve().parent / "service_account.json"

COMMENT_CUSTOM_FIELD = "Комментарий"
FETCH_LAST_COMMENT_IF_FIELD_EMPTY = True
MAX_COMMENT_LENGTH: int | None = None

# Цвета для колонки «Приоритет».
PRIORITY_COLORS = {
    "низкий": {"red": 0.82, "green": 0.94, "blue": 0.82},
    "очень низкий": {"red": 0.82, "green": 0.94, "blue": 0.82},
    "нормальный": {"red": 1.0, "green": 0.95, "blue": 0.70},
    "высокий": {"red": 1.0, "green": 0.80, "blue": 0.80},
    "срочный": {"red": 1.0, "green": 0.68, "blue": 0.68},
    "немедленный": {"red": 1.0, "green": 0.58, "blue": 0.58},
}

# Цвета статусов
STATUS_COLORS = {
    "новая": {"red": 0.90, "green": 0.91, "blue": 0.92},
    "на тестировании": {"red": 0.91, "green": 0.82, "blue": 0.95},
    "решена": {"red": 0.82, "green": 0.94, "blue": 0.75},
    "возвращена": {"red": 0.98, "green": 0.82, "blue": 0.79},
    "в работе": {"red": 1.00, "green": 0.88, "blue": 0.63},
    "бэклог": {"red": 0.86, "green": 0.87, "blue": 0.88},
}

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

HEADERS = [
    "Название",
    "Ссылка",
    "Статус",
    "Комментарий",
    "Версия",
    "Приоритет",
    "Назначена",
]

# Файл конфигурации
CONFIG_FILE = Path(__file__).resolve().parent / "config.json"


# =============================================================================
# CONFIG MANAGEMENT
# =============================================================================
def load_config() -> dict[str, Any]:
    """Загрузить конфиг из файла."""
    if not CONFIG_FILE.exists():
        return {"projects": {}}
    
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"projects": {}}


def save_config(config: dict[str, Any]) -> None:
    """Сохранить конфиг в файл."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def get_redmine_config(config: dict[str, Any]) -> dict[str, Any]:
    """Получить сохранённые параметры подключения к Redmine."""
    redmine_config = config.get("redmine")
    return redmine_config if isinstance(redmine_config, dict) else {}


def save_redmine_config(config: dict[str, Any], url: str, api_key: str) -> None:
    """Сохранить URL и API ключ Redmine."""
    config["redmine"] = {"url": url, "api_key": api_key}
    save_config(config)


def get_project_config(config: dict[str, Any], project_name: str) -> dict[str, Any] | None:
    """Получить конфиг для проекта."""
    project_config = config.get("projects", {}).get(project_name)
    if project_config:
        # Конвертируем 0 gid обратно в None
        if project_config.get("worksheet_gid") == 0:
            project_config["worksheet_gid"] = None
    return project_config


def save_project_config(config: dict[str, Any], project_name: str, sheets_id: str, gid: Optional[int]) -> None:
    """Сохранить конфиг для проекта."""
    if "projects" not in config:
        config["projects"] = {}
    
    config["projects"][project_name] = {
        "sheets_id": sheets_id,
        "worksheet_gid": gid if gid is not None else 0,  # Сохраняем None как 0
    }
    save_config(config)


# =============================================================================
# HELPERS
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


def row_hierarchy_key(row: list[str]) -> tuple[int, int]:
    """Ключ сортировки строки: активные -> бэклог -> решённые."""
    status = normalize_text(row[2]).casefold().strip() if len(row) > 2 else ""
    priority = normalize_text(row[5]).casefold().strip() if len(row) > 5 else ""

    if status in SOLVED_STATUSES:
        return (2, 0)

    if status in BACKLOG_STATUSES:
        return (1, 0)

    return (0, PRIORITY_ORDER.get(priority, 5))


def trim_comment(text: str) -> str:
    if MAX_COMMENT_LENGTH is None or len(text) <= MAX_COMMENT_LENGTH:
        return text
    return text[: MAX_COMMENT_LENGTH - 1].rstrip() + "…"


class BackgroundConsoleWriter:
    """Пишет в консоль из отдельного потока.

    Консоль Windows приостанавливает вывод, пока в окне выделен текст. Без этой
    обёртки на таком выводе замирает и сама синхронизация.
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._queue: queue.Queue[Optional[str]] = queue.Queue()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def write(self, text: str) -> int:
        self._queue.put(text)
        return len(text)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        try:
            return bool(self._stream.isatty())
        except Exception:
            return False

    def close_and_flush(self, timeout: float = 5.0) -> None:
        self._queue.put(None)
        self._thread.join(timeout)

    def _drain(self) -> None:
        while True:
            text = self._queue.get()
            if text is None:
                break
            try:
                self._stream.write(text)
                self._stream.flush()
            except Exception:
                pass


def enable_background_console_output() -> list[BackgroundConsoleWriter]:
    """Перевести вывод в фоновый поток, чтобы пауза консоли не блокировала работу."""
    writers = [BackgroundConsoleWriter(sys.stdout), BackgroundConsoleWriter(sys.stderr)]
    sys.stdout, sys.stderr = writers
    return writers


def wait_until_next_sync(interval_minutes: int, reason: str = "Следующее обновление") -> None:
    """Ждать до следующего запуска, показывая обратный отсчёт."""
    next_run = datetime.now() + timedelta(minutes=interval_minutes)
    print(f"\n⏭ {reason} в {next_run.strftime('%H:%M:%S')}")
    print("Нажми Ctrl+C для остановки")

    while True:
        remaining = (next_run - datetime.now()).total_seconds()
        if remaining <= 0:
            break
        minutes, seconds = divmod(int(remaining), 60)
        print(f"⏳ Осталось {minutes:02d}:{seconds:02d}", end="\r", flush=True)
        time.sleep(min(1.0, remaining))

    print(" " * 40, end="\r")


# =============================================================================
# INTERACTIVE INPUT
# =============================================================================
def prompt(message: str, default: str = "") -> str:
    """Интерактивный ввод с опциональным значением по умолчанию."""
    if default:
        prompt_text = f"{message} [{default}]: "
    else:
        prompt_text = f"{message}: "
    
    result = input(prompt_text).strip()
    return result if result else default


def prompt_password(message: str) -> str:
    """Ввод пароля (скрытый ввод)."""
    import getpass
    return getpass.getpass(f"{message}: ")


def select_from_list(items: list[dict[str, Any]], key_field: str, name_field: str = "name") -> dict[str, Any] | None:
    """Выбор элемента из списка."""
    if not items:
        return None
    
    if len(items) == 1:
        return items[0]
    
    print("\nДоступные варианты:")
    for idx, item in enumerate(items, 1):
        item_id = item.get(key_field, "?")
        item_name = item.get(name_field, "?")
        print(f"  {idx}. [{item_id}] {item_name}")
    
    while True:
        try:
            choice = input(f"Выберите номер (1-{len(items)}): ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(items):
                return items[idx]
            else:
                print(f"Пожалуйста, введите число от 1 до {len(items)}")
        except ValueError:
            print("Неверный ввод. Введите номер.")


def select_multiple_from_list(items: list[dict[str, Any]], key_field: str, name_field: str = "name") -> list[dict[str, Any]]:
    """Выбор нескольких элементов из списка."""
    if not items:
        return []
    
    print("\nДоступные варианты (введите номера через запятую или 'все' для всех, или Enter для пропуска):")
    for idx, item in enumerate(items, 1):
        item_id = item.get(key_field, "?")
        item_name = item.get(name_field, "?")
        print(f"  {idx}. [{item_id}] {item_name}")
    
    while True:
        choice = input("Выбор: ").strip().lower()
        
        if not choice:
            # Пропуск означает все версии
            return items
        
        if choice == "все":
            return items
        
        try:
            selected = []
            for part in choice.split(","):
                idx = int(part.strip()) - 1
                if 0 <= idx < len(items):
                    if items[idx] not in selected:
                        selected.append(items[idx])
                else:
                    print(f"Неверный номер: {idx + 1}")
                    break
            else:
                return selected
        except ValueError:
            print("Неверный ввод. Введите номера через запятую (например: 1,3,5)")


def extract_sheet_id(url_or_id: str) -> str:
    """Извлекает ID из Google Sheets URL или возвращает ID если это простой ID."""
    # Попробуем найти ID в URL
    match = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', url_or_id)
    if match:
        return match.group(1)
    
    # Если это простой ID
    if re.match(r'^[a-zA-Z0-9-_]+$', url_or_id):
        return url_or_id
    
    raise ValueError(f"Неверный формат Google Sheets ID или URL: {url_or_id}")


def prompt_for_google_sheets() -> tuple[str, Optional[int]]:
    """Спросить Google Sheets ID и номер вкладки (с автоматическим выбором из списка)."""
    sheet_url_or_id = prompt("Введите Google Sheets ID или URL")
    if not sheet_url_or_id:
        raise ValueError("Google Sheets ID не может быть пустым")
    
    sheet_id = extract_sheet_id(sheet_url_or_id)
    print(f"✓ Google Sheets ID: {sheet_id}")
    
    # Подключаемся чтобы получить список вкладок
    print("\n⏳ Загружаю список вкладок...")
    try:
        spreadsheet, worksheet, worksheets = connect_google_sheet(sheet_id, None)
    except Exception as exc:
        print(f"⚠ Не смог загрузить вкладки: {exc}")
        print("Используется первая вкладка по умолчанию")
        return sheet_id, None
    
    # Если только одна вкладка, используем её
    if len(worksheets) == 1:
        print(f"✓ Вкладка: {worksheets[0].title} (единственная доступная)")
        return sheet_id, None  # None означает первая/единственная
    
    # Если несколько вкладок, показываем список
    print(f"\n✓ Найдено {len(worksheets)} вкладок:")
    for idx, ws in enumerate(worksheets, 1):
        print(f"  {idx}. {ws.title} (gid={ws.id})")
    
    # Выбираем вкладку
    while True:
        try:
            choice = input(f"Выберите вкладку (1-{len(worksheets)}) или Enter для первой: ").strip()
            
            if not choice:
                # Если просто Enter, используем первую
                print(f"✓ Выбрана вкладка: {worksheets[0].title}")
                return sheet_id, None
            
            idx = int(choice) - 1
            if 0 <= idx < len(worksheets):
                selected_ws = worksheets[idx]
                print(f"✓ Выбрана вкладка: {selected_ws.title} (gid={selected_ws.id})")
                return sheet_id, selected_ws.id
            else:
                print(f"Пожалуйста, введите число от 1 до {len(worksheets)}")
        except ValueError:
            print(f"Неверный ввод. Введите номер вкладки или нажмите Enter")


def use_saved_config(project_name: str, saved_config: dict[str, Any]) -> bool:
    """Спросить использовать ли сохранённый конфиг."""
    sheets_id = saved_config.get("sheets_id", "?")
    gid = saved_config.get("worksheet_gid")
    gid_display = "первая вкладка (автоматическая)" if gid is None or gid == 0 else f"gid={gid}"
    
    print(f"\n✓ Найден сохранённый конфиг для '{project_name}':")
    print(f"  Google Sheets ID: {sheets_id}")
    print(f"  Вкладка: {gid_display}")
    
    choice = prompt("Использовать сохранённый конфиг? (y/n)", "y").lower().strip()
    return choice in ("y", "yes", "да", "д", "")


def prompt_update_config() -> bool:
    """Спросить обновить ли конфиг."""
    choice = prompt("Обновить конфиг для этого проекта? (y/n)", "n").lower().strip()
    return choice in ("y", "yes", "да", "д")


# =============================================================================
# REDMINE OPERATIONS
# =============================================================================

def fetch_projects(redmine: RedmineClient) -> list[dict[str, Any]]:
    """Получить список всех проектов."""
    return redmine.get_projects()


def fetch_versions(redmine: RedmineClient, project_id: int) -> list[dict[str, Any]]:
    """Получить версии проекта."""
    return redmine.get_project_versions(project_id)


def fetch_issues(
    redmine: RedmineClient,
    project_id: int,
    version_ids: list[int] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Получить задачи проекта (опционально отфильтрованные по версиям)."""
    issues_by_id: dict[int, dict[str, Any]] = {}

    def report(message: str) -> None:
        if on_progress:
            on_progress(message)
    
    print(f"\n🔍 DEBUG: Параметры запроса к Redmine:")
    print(f"   project_id={project_id}")
    print(f"   include_closed={INCLUDE_CLOSED}")
    print(f"   sort={ISSUES_SORT}")
    if version_ids:
        print(f"   version_ids={version_ids}")
        report(f"Загружаю {len(version_ids)} версий")
    else:
        report("Версии не выбраны: загружаю весь проект")
    
    if version_ids:
        for index, version_id in enumerate(version_ids, start=1):
            report(f"Версия {index}/{len(version_ids)}")
            version_issues = redmine.get_issues(
                project_id=project_id,
                include_closed=INCLUDE_CLOSED,
                sort=ISSUES_SORT,
                fixed_version_id=version_id,
                include="journals",
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
            project_id=project_id,
            include_closed=INCLUDE_CLOSED,
            sort=ISSUES_SORT,
            include="journals",
        )
    
    report(f"Получено задач: {len(issues)}")
    print(f"\n📊 DEBUG: Получено всего задач с сервера: {len(issues)}")
    if issues:
        first_issue = issues[0]
        print(f"   Первая (новейшая): #{first_issue.get('id')} - {first_issue.get('subject')[:50]}")
        print(f"   Создана: {first_issue.get('created_on', 'N/A')}")
        if len(issues) > 1:
            last_issue = issues[-1]
            print(f"   Последняя: #{last_issue.get('id')} - {last_issue.get('subject')[:50]}")
    
    return issues


# =============================================================================
# GOOGLE SHEETS
# =============================================================================
def connect_google_sheet(sheet_id: str, worksheet_gid: Optional[int] = None):
    """Подключиться к Google Sheets и вернуть список вкладок."""
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
    spreadsheet = client.open_by_key(sheet_id.strip())

    # Получаем все вкладки
    worksheets = spreadsheet.worksheets()
    
    if not worksheets:
        raise RuntimeError("В таблице нет ни одной вкладки!")
    
    # Если gid указан, ищем конкретную вкладку
    if worksheet_gid is not None:
        worksheet = None
        for candidate in worksheets:
            if int(candidate.id) == int(worksheet_gid):
                worksheet = candidate
                break
        
        if worksheet is None:
            available = ", ".join(f"{w.title} (gid={w.id})" for w in worksheets)
            raise RuntimeError(
                f"В таблице не найдена вкладка с gid={worksheet_gid}. "
                f"Доступные вкладки: {available}"
            )
    else:
        # Если gid не указан, используем первую вкладку
        worksheet = worksheets[0]

    return spreadsheet, worksheet, worksheets


def build_rows(
    redmine: RedmineClient,
    issues: list[dict[str, Any]],
    on_progress: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[list[str]]:
    """Построить строки для Google Sheets."""
    rows: list[list[str]] = []
    total = len(issues)
    skipped_count = 0
    tracker_mismatch = []

    for index, issue_stub in enumerate(issues, start=1):
        if should_stop and should_stop():
            raise KeyboardInterrupt
        issue_id = int(issue_stub["id"])
        stub_tracker = normalize_text(issue_stub.get("tracker"))
        if (
            TRACKER_FILTER
            and stub_tracker
            and stub_tracker.casefold().strip() != TRACKER_FILTER.casefold().strip()
        ):
            skipped_count += 1
            if len(tracker_mismatch) < 5:
                tracker_mismatch.append(
                    f"#{issue_id} (трекер: '{stub_tracker}', статус: {normalize_text(issue_stub.get('status'))})"
                )
            continue

        print(f"[{index}/{total}] Обновляю задачу #{issue_id}...", end="\r")
        if on_progress and (index == 1 or index % 25 == 0 or index == total):
            on_progress(f"Готовлю таблицу: {index}/{total}")

        issue = issue_stub
        comment_already_known = bool(get_custom_field(issue_stub, COMMENT_CUSTOM_FIELD))
        if "journals" not in issue_stub and not comment_already_known and FETCH_LAST_COMMENT_IF_FIELD_EMPTY:
            try:
                issue = redmine.get_issue(issue_id, include="journals")
            except RedmineAPIError as exc:
                print(f"\nПредупреждение: не удалось обновить задачу #{issue_id}: {exc}")
                issue = issue_stub

        tracker = normalize_text(issue.get("tracker"))
        tracker_name = issue.get("tracker", {}).get("name", "Unknown") if isinstance(issue.get("tracker"), dict) else tracker
        
        if tracker.casefold().strip() != TRACKER_FILTER.casefold().strip():
            skipped_count += 1
            if len(tracker_mismatch) < 5:
                tracker_mismatch.append(f"#{issue_id} (трекер: '{tracker_name}', статус: {normalize_text(issue.get('status'))})")
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

    # Показываем отладку
    print(f"\n📊 DEBUG: Обработано {total} задач")
    print(f"   ✓ Добавлено в таблицу: {len(rows)}")
    if skipped_count > 0:
        print(f"   ✗ Пропущено (неподходящий трекер '{TRACKER_FILTER}'): {skipped_count}")
        if tracker_mismatch:
            print(f"   Примеры пропущенных:")
            for example in tracker_mismatch:
                print(f"      - {example}")

    rows.sort(key=row_hierarchy_key)
    return rows


def format_sheet(spreadsheet, worksheet, row_count: int, rows: list[list[str]]) -> None:
    """Форматировать Google Sheets."""
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
    sheet_id: str,
    worksheet_gid: Optional[int] = None,
    on_progress: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    """Синхронизировать задачи в Google Sheets."""
    if on_progress:
        on_progress("Подключаюсь к Google Sheets")
    spreadsheet, worksheet, worksheets = connect_google_sheet(sheet_id, worksheet_gid)
    rows = build_rows(redmine, issues, on_progress=on_progress, should_stop=should_stop)
    if on_progress:
        on_progress(f"Записываю {len(rows)} строк")

    worksheet.batch_clear(["A:G"])
    values = [HEADERS] + rows
    worksheet.update(range_name=f"A1:G{len(values)}", values=values, value_input_option="USER_ENTERED")
    format_sheet(spreadsheet, worksheet, len(rows), rows)

    print(f"\n✓ Google Sheets обновлена: {spreadsheet.title} / {worksheet.title}")
    print(f"  Проект: [{project['id']}] {project['name']}")
    if selected_versions:
        print("  Версии: " + "; ".join(str(v.get("name", "")) for v in selected_versions))
    else:
        print("  Версии: Все версии")
    print(f"  Задач записано: {len(rows)}")
    print(f"  Время синхронизации: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


# =============================================================================
# MAIN
# =============================================================================
def run_sync_once(
    redmine: RedmineClient,
    project: dict[str, Any],
    selected_versions: list[dict[str, Any]],
    sheet_id: str,
    worksheet_gid: Optional[int],
) -> None:
    """Выполнить одну синхронизацию."""
    version_ids = [int(v["id"]) for v in selected_versions] if selected_versions else None

    # Получаем задачи
    print("\n⏳ Загружаю задачи...")
    issues = fetch_issues(redmine, int(project["id"]), version_ids)
    print(f"✓ Получено {len(issues)} задач")

    # Синхронизируем в Google Sheets
    print("\n⏳ Синхронизирую в Google Sheets...")
    sync_to_google_sheets(
        redmine,
        project,
        issues,
        selected_versions,
        sheet_id,
        worksheet_gid=worksheet_gid,
    )


def run_sync_periodically(
    redmine: RedmineClient,
    project: dict[str, Any],
    selected_versions: list[dict[str, Any]],
    sheet_id: str,
    worksheet_gid: Optional[int],
    interval_minutes: int = 5,
) -> int:
    """Выполнить синхронизацию периодически."""
    version_ids = [int(v["id"]) for v in selected_versions] if selected_versions else None
    sync_count = 0

    try:
        while True:
            try:
                sync_count += 1
                print(f"\n{'='*60}")
                print(f"⏳ Синхронизация #{sync_count} ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
                print(f"{'='*60}")

                # Получаем задачи
                print("\n⏳ Загружаю задачи...")
                issues = fetch_issues(redmine, int(project["id"]), version_ids)
                print(f"✓ Получено {len(issues)} задач")

                # Синхронизируем в Google Sheets
                print("\n⏳ Синхронизирую в Google Sheets...")
                sync_to_google_sheets(
                    redmine,
                    project,
                    issues,
                    selected_versions,
                    sheet_id,
                    worksheet_gid=worksheet_gid,
                )

                print(f"✓ Синхронизация #{sync_count} завершена в {datetime.now().strftime('%H:%M:%S')}")
                wait_until_next_sync(interval_minutes)

            except (RedmineAPIError, RuntimeError, ValueError, KeyError, gspread.GSpreadException) as exc:
                print(f"\n✗ ОШИБКА при синхронизации #{sync_count}: {exc}", file=sys.stderr)
                wait_until_next_sync(interval_minutes, "Повторная попытка")

            except Exception as exc:
                print(f"\n✗ Неожиданная ошибка: {exc}", file=sys.stderr)
                wait_until_next_sync(interval_minutes, "Повторная попытка")

    except KeyboardInterrupt:
        print(f"\n\n⚠ Синхронизация остановлена пользователем")
        print(f"Всего выполнено синхронизаций: {sync_count}")
        return 130


def main() -> int:
    try:
        print(f"Redmine → Google Sheets Sync v{SCRIPT_VERSION}")
        print("=" * 60)

        # Загружаем конфиг
        config = load_config()

        # 1. Redmine URL
        redmine_config = get_redmine_config(config)
        redmine_url = prompt("Введите URL Redmine", redmine_config.get("url") or DEFAULT_REDMINE_URL)

        # 2. Redmine API Key: переменная окружения -> сохранённый -> ручной ввод
        env_api_key = os.getenv("REDMINE_API_KEY", "").strip()
        saved_api_key = str(redmine_config.get("api_key") or "").strip()

        if env_api_key:
            redmine_api_key = env_api_key
            key_is_new = False
            print("✓ Использую Redmine API ключ из переменной REDMINE_API_KEY")
        elif saved_api_key:
            redmine_api_key = saved_api_key
            key_is_new = False
            print("✓ Использую сохранённый Redmine API ключ")
        else:
            redmine_api_key = prompt("Введите Redmine API ключ")
            key_is_new = True

        if not redmine_api_key:
            raise ValueError("API ключ не может быть пустым")

        # Подключаемся к Redmine. Если сохранённый ключ отозван, просим ввести новый.
        while True:
            print("\n⏳ Подключаюсь к Redmine...")
            try:
                redmine = RedmineClient(
                    base_url=redmine_url,
                    api_key=redmine_api_key,
                    verify_ssl=VERIFY_SSL,
                )
                me = redmine.get_current_user()
                break
            except RedmineAPIError as exc:
                if key_is_new:
                    raise
                print(f"✗ Сохранённый ключ не подошёл: {exc}", file=sys.stderr)
                redmine_api_key = prompt("Введите Redmine API ключ заново")
                if not redmine_api_key:
                    raise ValueError("API ключ не может быть пустым")
                key_is_new = True

        user_name = (
            me.get("login")
            or f"{me.get('firstname', '')} {me.get('lastname', '')}".strip()
            or str(me.get("id", "?"))
        )
        print(f"✓ Подключение успешно: {user_name}")

        if key_is_new:
            save_choice = prompt("Сохранить ключ, чтобы не вводить его каждый раз? (y/n)", "y").lower().strip()
            if save_choice in ("y", "yes", "да", "д", ""):
                save_redmine_config(config, redmine_url, redmine_api_key)
                print(f"✓ Ключ сохранён в {CONFIG_FILE.name} (файл не попадает в git)")
        elif not env_api_key and redmine_config.get("url") != redmine_url:
            save_redmine_config(config, redmine_url, redmine_api_key)

        # 3. Выбираем проект
        print("\n⏳ Загружаю список проектов...")
        projects = fetch_projects(redmine)
        if not projects:
            raise RuntimeError("Нет доступных проектов")
        
        print(f"Найдено {len(projects)} проектов")
        project = select_from_list(projects, "id", "name")
        if not project:
            raise RuntimeError("Проект не выбран")
        
        project_name = project.get("name", "Unknown")
        print(f"✓ Проект выбран: [{project['id']}] {project_name}")

        # 4. Выбираем версии
        print("\n⏳ Загружаю версии проекта...")
        versions = fetch_versions(redmine, int(project["id"]))
        
        if versions:
            print(f"Найдено {len(versions)} версий")
            selected_versions = select_multiple_from_list(versions, "id", "name")
            if selected_versions:
                print(f"✓ Версии выбраны ({len(selected_versions)}):")
                for v in selected_versions:
                    print(f"    - [{v['id']}] {v.get('name', '')}")
            else:
                selected_versions = []
                print("✓ Версии не выбраны (будут использованы все версии)")
        else:
            print("⚠ Версии не найдены, будут использованы все версии")
            selected_versions = []

        # 5. Google Sheets ID - проверяем сохранённый конфиг
        print("\n" + "=" * 60)
        sheet_id: Optional[str] = None
        worksheet_gid: Optional[int] = None
        
        saved_config = get_project_config(config, project_name)
        
        if saved_config:
            # Есть сохранённый конфиг
            if use_saved_config(project_name, saved_config):
                # Использование сохранённого конфига
                sheet_id = saved_config.get("sheets_id")
                worksheet_gid = saved_config.get("worksheet_gid")
                print(f"✓ Использую сохранённый конфиг")
                
                # Спрашиваем обновить ли конфиг
                if prompt_update_config():
                    print("\n⏳ Вводим новый конфиг...")
                    sheet_id, worksheet_gid = prompt_for_google_sheets()
                    save_project_config(config, project_name, sheet_id, worksheet_gid or 0)
                    print("✓ Конфиг обновлен и сохранён")
            else:
                # Не использовать сохранённый, ввести новый
                print("\n⏳ Вводим новый конфиг...")
                sheet_id, worksheet_gid = prompt_for_google_sheets()
                
                # Спрашиваем сохранить ли новый конфиг
                save_choice = prompt("Сохранить конфиг для этого проекта? (y/n)", "y").lower().strip()
                if save_choice in ("y", "yes", "да", "д", ""):
                    save_project_config(config, project_name, sheet_id, worksheet_gid or 0)
                    print("✓ Конфиг сохранён")
        else:
            # Нет сохранённого конфига
            print("\n⏳ Вводим конфиг для этого проекта...")
            sheet_id, worksheet_gid = prompt_for_google_sheets()
            
            # Спрашиваем сохранить ли конфиг
            save_choice = prompt("Сохранить конфиг для этого проекта? (y/n)", "y").lower().strip()
            if save_choice in ("y", "yes", "да", "д", ""):
                save_project_config(config, project_name, sheet_id, worksheet_gid or 0)
                print("✓ Конфиг сохранён")

        if not sheet_id:
            raise ValueError("Google Sheets ID не может быть пустым")

        # 6. Запускаем постоянную синхронизацию
        print("\n" + "=" * 60)
        print(f"✓ Автосинхронизация каждые {SYNC_INTERVAL_MINUTES} минут")
        print("Первая синхронизация запускается сейчас.")
        print("Для остановки нажми Ctrl+C.")

        # Вывод уходит в фоновый поток: выделение текста в консоли больше не
        # останавливает синхронизацию. Включаем после интерактивных вопросов.
        enable_background_console_output()

        return run_sync_periodically(
            redmine,
            project,
            selected_versions,
            sheet_id,
            worksheet_gid,
            SYNC_INTERVAL_MINUTES,
        )

    except (RedmineAPIError, RuntimeError, ValueError, KeyError, gspread.GSpreadException) as exc:
        print(f"\n✗ ОШИБКА: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n⚠ Операция отменена пользователем.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"\n✗ Неожиданная ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    exit_code = main()
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, BackgroundConsoleWriter):
            stream.close_and_flush()
    raise SystemExit(exit_code)
