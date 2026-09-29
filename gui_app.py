from __future__ import annotations

import json
import os
import queue
import sys
import threading
import uuid
from datetime import datetime
from typing import Any

import tkinter as tk
from tkinter import messagebox, ttk

from redmine_client import VERIFY_SSL, RedmineClient
from sync_unified import (
    DEFAULT_REDMINE_URL,
    CONFIG_FILE,
    connect_google_sheet,
    extract_sheet_id,
    fetch_issues,
    sync_to_google_sheets,
)


APP_TITLE = "Redmine → Google Sheets Sync"
DEFAULT_INTERVAL_MINUTES = 5


class SyncDesktopApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1180x760")
        self.root.minsize(980, 650)

        self.config = self._load_config()
        self.redmine: RedmineClient | None = None
        self.projects: list[dict[str, Any]] = []
        self.projects_by_label: dict[str, dict[str, Any]] = {}
        self.versions: list[dict[str, Any]] = []
        self.worksheets_by_label: dict[str, Any] = {}
        self.selected_profile_id: str | None = None
        self.pending_version_ids: set[int] = set()
        self.pending_worksheet_gid: int | None = None
        self.requested_project_id: int | None = None

        self.ui_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.workers: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self.profile_status: dict[str, str] = {}

        self.url_var = tk.StringVar()
        self.api_key_var = tk.StringVar()
        self.connection_var = tk.StringVar(value="Не подключено")
        self.profile_name_var = tk.StringVar()
        self.project_var = tk.StringVar()
        self.sheet_var = tk.StringVar()
        self.worksheet_var = tk.StringVar()
        self.interval_var = tk.IntVar(value=DEFAULT_INTERVAL_MINUTES)
        self.enabled_var = tk.BooleanVar(value=True)

        self._build_ui()
        self._load_saved_values()
        self._refresh_profiles()
        self.root.after(100, self._drain_ui_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        if self.api_key_var.get().strip():
            self.root.after(300, self.connect_redmine)

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    def _load_config(self) -> dict[str, Any]:
        if not CONFIG_FILE.exists():
            return {"projects": {}, "sync_profiles": []}
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"projects": {}, "sync_profiles": []}
            data.setdefault("projects", {})
            data.setdefault("sync_profiles", [])
            return data
        except (OSError, json.JSONDecodeError):
            return {"projects": {}, "sync_profiles": []}

    def _save_config(self) -> None:
        CONFIG_FILE.write_text(
            json.dumps(self.config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _profiles(self) -> list[dict[str, Any]]:
        profiles = self.config.setdefault("sync_profiles", [])
        return profiles if isinstance(profiles, list) else []

    def _profile_by_id(self, profile_id: str) -> dict[str, Any] | None:
        return next((p for p in self._profiles() if p.get("id") == profile_id), None)

    def _migrate_legacy_projects(self) -> None:
        """Перенести привязки из старой секции projects в профили синхронизации."""
        legacy = self.config.get("projects", {})
        if not isinstance(legacy, dict) or not legacy:
            return

        known = {
            (p.get("project_name"), p.get("sheet_id"), p.get("worksheet_gid"))
            for p in self._profiles()
        }
        created = 0
        for project_name, entry in legacy.items():
            if not isinstance(entry, dict):
                continue
            sheet_id = str(entry.get("sheets_id") or "").strip()
            if not sheet_id:
                continue
            raw_gid = entry.get("worksheet_gid")
            worksheet_gid = None if raw_gid in (None, 0) else int(raw_gid)
            if (project_name, sheet_id, worksheet_gid) in known:
                continue
            project = next(
                (p for p in self.projects if str(p.get("name", "")) == project_name), None
            )
            if project is None:
                continue

            self._profiles().append(
                {
                    "id": uuid.uuid4().hex,
                    "name": project_name,
                    "project_id": int(project["id"]),
                    "project_name": project_name,
                    "versions": [],
                    "sheet_id": sheet_id,
                    "worksheet_gid": worksheet_gid,
                    "worksheet_title": (
                        "Первая вкладка" if worksheet_gid is None else f"gid={worksheet_gid}"
                    ),
                    "interval_minutes": DEFAULT_INTERVAL_MINUTES,
                    "enabled": True,
                }
            )
            created += 1

        if created:
            self._save_config()
            self._log(
                f"Перенесено профилей из старого конфига: {created}. "
                "Проверьте версии и вкладку перед запуском."
            )

    def _load_saved_values(self) -> None:
        redmine = self.config.get("redmine", {})
        self.url_var.set(str(redmine.get("url") or DEFAULT_REDMINE_URL))
        self.api_key_var.set(str(redmine.get("api_key") or ""))

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        style = ttk.Style()
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"))

        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text=APP_TITLE, style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="Настройка и одновременный запуск нескольких проектов",
        ).pack(anchor="w", pady=(0, 10))

        connection = ttk.LabelFrame(
            outer, text="1. Подключение к Redmine", padding=10, style="Section.TLabelframe"
        )
        connection.pack(fill="x")
        connection.columnconfigure(1, weight=1)

        ttk.Label(connection, text="URL").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(connection, textvariable=self.url_var).grid(
            row=0, column=1, sticky="ew", padx=(0, 8)
        )
        ttk.Label(connection, text="API-ключ").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(connection, textvariable=self.api_key_var, show="•").grid(
            row=1, column=1, sticky="ew", padx=(0, 8), pady=(8, 0)
        )
        self.connect_button = ttk.Button(connection, text="Подключиться", command=self.connect_redmine)
        self.connect_button.grid(row=0, column=2, rowspan=2, sticky="ns")
        ttk.Label(connection, textvariable=self.connection_var).grid(
            row=2, column=1, sticky="w", pady=(8, 0)
        )
        ttk.Label(
            connection,
            text="Ключ хранится локально в config.json, который исключён из Git.",
            foreground="#666666",
        ).grid(row=3, column=1, sticky="w", pady=(3, 0))

        content = ttk.Panedwindow(outer, orient="horizontal")
        content.pack(fill="both", expand=True, pady=10)

        profiles_frame = ttk.LabelFrame(
            content, text="2. Профили синхронизации", padding=8, style="Section.TLabelframe"
        )
        editor_frame = ttk.LabelFrame(
            content, text="3. Настройки профиля", padding=10, style="Section.TLabelframe"
        )
        content.add(profiles_frame, weight=3)
        content.add(editor_frame, weight=2)

        columns = ("project", "versions", "sheet", "interval", "status")
        self.profile_tree = ttk.Treeview(
            profiles_frame, columns=columns, show="tree headings", selectmode="browse"
        )
        self.profile_tree.heading("#0", text="Название")
        self.profile_tree.heading("project", text="Проект")
        self.profile_tree.heading("versions", text="Версии")
        self.profile_tree.heading("sheet", text="Вкладка")
        self.profile_tree.heading("interval", text="Интервал")
        self.profile_tree.heading("status", text="Статус")
        self.profile_tree.column("#0", width=150)
        self.profile_tree.column("project", width=140)
        self.profile_tree.column("versions", width=210)
        self.profile_tree.column("sheet", width=120)
        self.profile_tree.column("interval", width=75, anchor="center")
        self.profile_tree.column("status", width=120)
        self.profile_tree.pack(side="left", fill="both", expand=True)
        self.profile_tree.bind("<<TreeviewSelect>>", self._on_profile_selected)

        tree_scroll = ttk.Scrollbar(profiles_frame, orient="vertical", command=self.profile_tree.yview)
        tree_scroll.pack(side="right", fill="y")
        self.profile_tree.configure(yscrollcommand=tree_scroll.set)

        editor_frame.columnconfigure(1, weight=1)
        ttk.Label(editor_frame, text="Название профиля").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(editor_frame, textvariable=self.profile_name_var).grid(
            row=0, column=1, sticky="ew", pady=4
        )

        ttk.Label(editor_frame, text="Проект").grid(row=1, column=0, sticky="w", pady=4)
        self.project_combo = ttk.Combobox(
            editor_frame, textvariable=self.project_var, state="readonly"
        )
        self.project_combo.grid(row=1, column=1, sticky="ew", pady=4)
        self.project_combo.bind("<<ComboboxSelected>>", self._on_project_selected)

        ttk.Label(editor_frame, text="Версии").grid(row=2, column=0, sticky="nw", pady=4)
        versions_holder = ttk.Frame(editor_frame)
        versions_holder.grid(row=2, column=1, sticky="nsew", pady=4)
        versions_holder.columnconfigure(0, weight=1)
        self.version_list = tk.Listbox(
            versions_holder, selectmode="extended", exportselection=False, height=7
        )
        self.version_list.grid(row=0, column=0, sticky="nsew")
        version_scroll = ttk.Scrollbar(
            versions_holder, orient="vertical", command=self.version_list.yview
        )
        version_scroll.grid(row=0, column=1, sticky="ns")
        self.version_list.configure(yscrollcommand=version_scroll.set)

        ttk.Label(editor_frame, text="Google Sheets URL/ID").grid(
            row=3, column=0, sticky="w", pady=4
        )
        sheet_holder = ttk.Frame(editor_frame)
        sheet_holder.grid(row=3, column=1, sticky="ew", pady=4)
        sheet_holder.columnconfigure(0, weight=1)
        ttk.Entry(sheet_holder, textvariable=self.sheet_var).grid(row=0, column=0, sticky="ew")
        self.load_tabs_button = ttk.Button(
            sheet_holder, text="Загрузить вкладки", command=self.load_worksheets
        )
        self.load_tabs_button.grid(row=0, column=1, padx=(6, 0))

        ttk.Label(editor_frame, text="Вкладка").grid(row=4, column=0, sticky="w", pady=4)
        self.worksheet_combo = ttk.Combobox(
            editor_frame, textvariable=self.worksheet_var, state="readonly"
        )
        self.worksheet_combo.grid(row=4, column=1, sticky="ew", pady=4)

        ttk.Label(editor_frame, text="Интервал, минут").grid(row=5, column=0, sticky="w", pady=4)
        ttk.Spinbox(
            editor_frame, from_=1, to=1440, textvariable=self.interval_var, width=8
        ).grid(row=5, column=1, sticky="w", pady=4)

        ttk.Checkbutton(
            editor_frame, text="Запускать кнопкой «Запустить все»", variable=self.enabled_var
        ).grid(row=6, column=1, sticky="w", pady=4)

        editor_buttons = ttk.Frame(editor_frame)
        editor_buttons.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Button(editor_buttons, text="Новый", command=self.new_profile).pack(side="left")
        ttk.Button(editor_buttons, text="Сохранить профиль", command=self.save_profile).pack(
            side="left", padx=6
        )
        ttk.Button(editor_buttons, text="Удалить", command=self.delete_profile).pack(side="left")

        controls = ttk.Frame(outer)
        controls.pack(fill="x")
        ttk.Button(controls, text="▶ Запустить выбранный", command=self.start_selected).pack(side="left")
        ttk.Button(controls, text="▶▶ Запустить все", command=self.start_all).pack(
            side="left", padx=6
        )
        ttk.Button(controls, text="■ Остановить выбранный", command=self.stop_selected).pack(
            side="left"
        )
        ttk.Button(controls, text="■ Остановить все", command=self.stop_all).pack(
            side="left", padx=6
        )

        log_frame = ttk.LabelFrame(outer, text="Журнал", padding=6)
        log_frame.pack(fill="both", expand=False, pady=(10, 0))
        self.log_text = tk.Text(log_frame, height=9, state="disabled", wrap="word")
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=log_scroll.set)

    # ------------------------------------------------------------------
    # Redmine and Google discovery
    # ------------------------------------------------------------------
    def connect_redmine(self) -> None:
        url = self.url_var.get().strip()
        api_key = self.api_key_var.get().strip()
        if not url or not api_key:
            messagebox.showwarning(APP_TITLE, "Укажите URL и API-ключ Redmine.")
            return

        self.connect_button.configure(state="disabled")
        self.connection_var.set("Подключение…")
        threading.Thread(
            target=self._connect_worker, args=(url, api_key), daemon=True
        ).start()

    def _connect_worker(self, url: str, api_key: str) -> None:
        try:
            client = RedmineClient(url, api_key=api_key, verify_ssl=VERIFY_SSL)
            user = client.get_current_user()
            projects = client.get_projects()
            self.ui_queue.put(("connected", (client, user, projects, url, api_key)))
        except Exception as exc:
            self.ui_queue.put(("connection_error", exc))

    def _on_project_selected(self, _event: Any = None) -> None:
        project = self.projects_by_label.get(self.project_var.get())
        if not project or not self.redmine:
            return
        self.version_list.delete(0, "end")
        self.version_list.insert("end", "Загрузка…")
        self.requested_project_id = int(project["id"])
        threading.Thread(
            target=self._versions_worker,
            args=(self.redmine, self.requested_project_id),
            daemon=True,
        ).start()

    def _versions_worker(self, client: RedmineClient, project_id: int) -> None:
        try:
            versions = client.get_project_versions(project_id)
            self.ui_queue.put(("versions", (project_id, versions)))
        except Exception as exc:
            self.ui_queue.put(("versions_error", exc))

    def load_worksheets(self) -> None:
        try:
            sheet_id = extract_sheet_id(self.sheet_var.get().strip())
        except ValueError as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            return
        self.load_tabs_button.configure(state="disabled")
        self.worksheet_var.set("Загрузка…")
        threading.Thread(target=self._worksheets_worker, args=(sheet_id,), daemon=True).start()

    def _worksheets_worker(self, sheet_id: str) -> None:
        try:
            _, _, worksheets = connect_google_sheet(sheet_id)
            self.ui_queue.put(("worksheets", (sheet_id, worksheets)))
        except Exception as exc:
            self.ui_queue.put(("worksheets_error", exc))

    # ------------------------------------------------------------------
    # Profile editor
    # ------------------------------------------------------------------
    def new_profile(self) -> None:
        self.selected_profile_id = None
        self.pending_version_ids.clear()
        self.pending_worksheet_gid = None
        self.profile_name_var.set("")
        self.project_var.set("")
        self.version_list.delete(0, "end")
        self.sheet_var.set("")
        self.worksheet_var.set("")
        self.worksheet_combo["values"] = ()
        self.interval_var.set(DEFAULT_INTERVAL_MINUTES)
        self.enabled_var.set(True)
        for item in self.profile_tree.selection():
            self.profile_tree.selection_remove(item)

    def save_profile(self) -> None:
        project = self.projects_by_label.get(self.project_var.get())
        if not project:
            messagebox.showwarning(APP_TITLE, "Сначала подключитесь и выберите проект.")
            return
        try:
            sheet_id = extract_sheet_id(self.sheet_var.get().strip())
            interval = max(1, int(self.interval_var.get()))
        except (ValueError, tk.TclError) as exc:
            messagebox.showwarning(APP_TITLE, f"Проверьте таблицу и интервал: {exc}")
            return

        selected_indices = self.version_list.curselection()
        selected_versions = [
            {"id": int(self.versions[i]["id"]), "name": str(self.versions[i].get("name", ""))}
            for i in selected_indices
            if i < len(self.versions)
        ]
        profile_id = self.selected_profile_id or uuid.uuid4().hex
        existing = self._profile_by_id(profile_id)
        worksheet = self.worksheets_by_label.get(self.worksheet_var.get())
        if worksheet is not None:
            worksheet_gid = int(worksheet.id)
            worksheet_title = str(worksheet.title)
        elif existing:
            worksheet_gid = existing.get("worksheet_gid")
            worksheet_title = str(existing.get("worksheet_title") or "Первая вкладка")
        else:
            worksheet_gid = None
            worksheet_title = "Первая вкладка"

        profile = {
            "id": profile_id,
            "name": self.profile_name_var.get().strip() or str(project.get("name", "Проект")),
            "project_id": int(project["id"]),
            "project_name": str(project.get("name", "")),
            "versions": selected_versions,
            "sheet_id": sheet_id,
            "worksheet_gid": worksheet_gid,
            "worksheet_title": worksheet_title or "Первая вкладка",
            "interval_minutes": interval,
            "enabled": bool(self.enabled_var.get()),
        }

        if existing:
            existing.clear()
            existing.update(profile)
        else:
            self._profiles().append(profile)
        self.selected_profile_id = profile_id
        self._save_config()
        self._refresh_profiles(select_id=profile_id)
        self._log(f"Профиль «{profile['name']}» сохранён.")

    def delete_profile(self) -> None:
        profile_id = self._selected_tree_id()
        if not profile_id:
            return
        if profile_id in self.workers:
            messagebox.showwarning(APP_TITLE, "Сначала остановите этот профиль.")
            return
        profile = self._profile_by_id(profile_id)
        if not profile or not messagebox.askyesno(APP_TITLE, f"Удалить «{profile.get('name')}»?"):
            return
        self.config["sync_profiles"] = [
            p for p in self._profiles() if p.get("id") != profile_id
        ]
        self._save_config()
        self.new_profile()
        self._refresh_profiles()

    def _on_profile_selected(self, _event: Any = None) -> None:
        profile_id = self._selected_tree_id()
        profile = self._profile_by_id(profile_id) if profile_id else None
        if not profile:
            return
        self.selected_profile_id = profile_id
        self.profile_name_var.set(str(profile.get("name", "")))
        project_id = int(profile.get("project_id", 0))
        label = next(
            (label for label, p in self.projects_by_label.items() if int(p["id"]) == project_id),
            "",
        )
        self.project_var.set(label)
        self.pending_version_ids = {
            int(v["id"]) for v in profile.get("versions", []) if "id" in v
        }
        if label:
            self._on_project_selected()
        self.sheet_var.set(str(profile.get("sheet_id", "")))
        worksheet_title = str(profile.get("worksheet_title", ""))
        worksheet_gid = profile.get("worksheet_gid")
        self.pending_worksheet_gid = int(worksheet_gid) if worksheet_gid is not None else None
        if worksheet_title:
            display = (
                f"{worksheet_title} (gid={worksheet_gid})"
                if worksheet_gid is not None
                else worksheet_title
            )
            self.worksheet_var.set(display)
        self.interval_var.set(int(profile.get("interval_minutes", DEFAULT_INTERVAL_MINUTES)))
        self.enabled_var.set(bool(profile.get("enabled", True)))

    # ------------------------------------------------------------------
    # Synchronization workers
    # ------------------------------------------------------------------
    def start_selected(self) -> None:
        profile_id = self._selected_tree_id()
        if not profile_id:
            messagebox.showinfo(APP_TITLE, "Выберите профиль в списке.")
            return
        self._start_profile(profile_id)

    def start_all(self) -> None:
        profiles = [p for p in self._profiles() if p.get("enabled", True)]
        if not profiles:
            messagebox.showinfo(APP_TITLE, "Нет включённых профилей.")
            return
        for index, profile in enumerate(profiles):
            self._start_profile(str(profile["id"]), start_delay_seconds=index * 8)

    def _start_profile(self, profile_id: str, start_delay_seconds: int = 0) -> None:
        if profile_id in self.workers:
            return
        profile = self._profile_by_id(profile_id)
        redmine_config = self.config.get("redmine", {})
        if not profile or not redmine_config.get("api_key"):
            messagebox.showwarning(APP_TITLE, "Сначала подключитесь к Redmine.")
            return
        target = (str(profile.get("sheet_id")), profile.get("worksheet_gid"))
        for running_id in self.workers:
            running = self._profile_by_id(running_id)
            if running and (str(running.get("sheet_id")), running.get("worksheet_gid")) == target:
                messagebox.showwarning(
                    APP_TITLE,
                    "Другой запущенный профиль уже пишет в эту же таблицу и вкладку.",
                )
                return
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._sync_worker,
            args=(dict(profile), dict(redmine_config), stop_event, start_delay_seconds),
            daemon=True,
        )
        self.workers[profile_id] = (thread, stop_event)
        self._set_status(profile_id, "Запуск…")
        thread.start()

    def _sync_worker(
        self,
        profile: dict[str, Any],
        redmine_config: dict[str, Any],
        stop_event: threading.Event,
        start_delay_seconds: int = 0,
    ) -> None:
        profile_id = str(profile["id"])
        try:
            if start_delay_seconds and stop_event.wait(start_delay_seconds):
                return
            redmine = RedmineClient(
                str(redmine_config["url"]),
                api_key=str(redmine_config["api_key"]),
                verify_ssl=VERIFY_SSL,
            )
            project = {
                "id": int(profile["project_id"]),
                "name": str(profile["project_name"]),
            }
            versions = list(profile.get("versions", []))
            version_ids = [int(v["id"]) for v in versions] or None
            interval = max(1, int(profile.get("interval_minutes", DEFAULT_INTERVAL_MINUTES)))

            while not stop_event.is_set():
                self.ui_queue.put(("sync_started", (profile_id, profile["name"])))
                try:
                    issues = fetch_issues(redmine, int(project["id"]), version_ids)
                    sync_to_google_sheets(
                        redmine,
                        project,
                        issues,
                        versions,
                        str(profile["sheet_id"]),
                        worksheet_gid=profile.get("worksheet_gid"),
                    )
                    next_run = datetime.now().timestamp() + interval * 60
                    self.ui_queue.put(
                        ("sync_ok", (profile_id, profile["name"], len(issues), next_run))
                    )
                except Exception as exc:
                    self.ui_queue.put(("sync_error", (profile_id, profile["name"], exc)))
                if stop_event.wait(interval * 60):
                    break
        finally:
            self.ui_queue.put(("worker_stopped", (profile_id, profile["name"])))

    def stop_selected(self) -> None:
        profile_id = self._selected_tree_id()
        if profile_id:
            self._stop_profile(profile_id)

    def stop_all(self) -> None:
        for profile_id in list(self.workers):
            self._stop_profile(profile_id)

    def _stop_profile(self, profile_id: str) -> None:
        worker = self.workers.get(profile_id)
        if worker:
            worker[1].set()
            self._set_status(profile_id, "Остановка…")

    # ------------------------------------------------------------------
    # Queue and UI updates
    # ------------------------------------------------------------------
    def _drain_ui_queue(self) -> None:
        try:
            while True:
                event, payload = self.ui_queue.get_nowait()
                self._handle_event(event, payload)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_ui_queue)

    def _handle_event(self, event: str, payload: Any) -> None:
        if event == "connected":
            client, user, projects, url, api_key = payload
            self.redmine = client
            self.projects = projects
            self.projects_by_label = {
                f"[{p.get('id')}] {p.get('name', '')}": p for p in projects
            }
            self.project_combo["values"] = tuple(self.projects_by_label)
            username = user.get("login") or user.get("firstname") or user.get("id")
            self.connection_var.set(f"Подключено: {username}. Проектов: {len(projects)}")
            self.connect_button.configure(state="normal")
            self.config["redmine"] = {"url": url, "api_key": api_key}
            self._save_config()
            self._log(f"Redmine подключён: {username}.")
            self._migrate_legacy_projects()
            self._refresh_profiles()
        elif event == "connection_error":
            self.connect_button.configure(state="normal")
            self.connection_var.set("Ошибка подключения")
            messagebox.showerror(APP_TITLE, str(payload))
        elif event == "versions":
            project_id, versions = payload
            if project_id != self.requested_project_id:
                return
            self.versions = versions
            self.version_list.delete(0, "end")
            for version in self.versions:
                self.version_list.insert("end", f"[{version.get('id')}] {version.get('name', '')}")
            if self.pending_version_ids:
                for index, version in enumerate(self.versions):
                    if int(version.get("id", 0)) in self.pending_version_ids:
                        self.version_list.selection_set(index)
                self.pending_version_ids.clear()
        elif event == "versions_error":
            self.version_list.delete(0, "end")
            messagebox.showerror(APP_TITLE, str(payload))
        elif event == "worksheets":
            sheet_id, worksheets = payload
            self.sheet_var.set(sheet_id)
            self.worksheets_by_label = {
                f"{ws.title} (gid={ws.id})": ws for ws in worksheets
            }
            values = tuple(self.worksheets_by_label)
            self.worksheet_combo["values"] = values
            selected = next(
                (
                    label
                    for label, ws in self.worksheets_by_label.items()
                    if self.pending_worksheet_gid is not None
                    and int(ws.id) == self.pending_worksheet_gid
                ),
                values[0] if values else "",
            )
            self.worksheet_var.set(selected)
            self.pending_worksheet_gid = None
            self.load_tabs_button.configure(state="normal")
        elif event == "worksheets_error":
            self.load_tabs_button.configure(state="normal")
            self.worksheet_var.set("")
            messagebox.showerror(APP_TITLE, str(payload))
        elif event == "sync_started":
            profile_id, name = payload
            self._set_status(profile_id, "Синхронизация…")
            self._log(f"{name}: синхронизация началась.")
        elif event == "sync_ok":
            profile_id, name, issue_count, next_timestamp = payload
            next_time = datetime.fromtimestamp(next_timestamp).strftime("%H:%M:%S")
            self._set_status(profile_id, f"Следующая {next_time}")
            self._log(f"{name}: готово, получено задач: {issue_count}; следующая в {next_time}.")
        elif event == "sync_error":
            profile_id, name, exc = payload
            self._set_status(profile_id, "Ошибка")
            self._log(f"{name}: ошибка — {exc}")
        elif event == "worker_stopped":
            profile_id, name = payload
            self.workers.pop(profile_id, None)
            self._set_status(profile_id, "Остановлен")
            self._log(f"{name}: остановлен.")

    def _refresh_profiles(self, select_id: str | None = None) -> None:
        selected = select_id or self._selected_tree_id()
        self.profile_tree.delete(*self.profile_tree.get_children())
        for profile in self._profiles():
            profile_id = str(profile.get("id", ""))
            versions = profile.get("versions", [])
            version_text = ", ".join(str(v.get("name", "")) for v in versions) or "Все"
            status = self.profile_status.get(profile_id, "Остановлен")
            self.profile_tree.insert(
                "",
                "end",
                iid=profile_id,
                text=str(profile.get("name", "")),
                values=(
                    profile.get("project_name", ""),
                    version_text,
                    profile.get("worksheet_title", "Первая"),
                    f"{profile.get('interval_minutes', DEFAULT_INTERVAL_MINUTES)} мин",
                    status,
                ),
            )
        if selected and self.profile_tree.exists(selected):
            self.profile_tree.selection_set(selected)
            self.profile_tree.focus(selected)

    def _set_status(self, profile_id: str, status: str) -> None:
        self.profile_status[profile_id] = status
        if self.profile_tree.exists(profile_id):
            values = list(self.profile_tree.item(profile_id, "values"))
            if len(values) >= 5:
                values[4] = status
                self.profile_tree.item(profile_id, values=values)

    def _selected_tree_id(self) -> str | None:
        selection = self.profile_tree.selection()
        return str(selection[0]) if selection else None

    def _log(self, text: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {text}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        if self.workers and not messagebox.askyesno(
            APP_TITLE, "Остановить все синхронизации и закрыть приложение?"
        ):
            return
        for _, stop_event in self.workers.values():
            stop_event.set()
        self.root.destroy()


def main() -> None:
    # pythonw запускается без консольных потоков, а переиспользуемая логика
    # синхронизации всё ещё пишет диагностические сообщения через print.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    root = tk.Tk()
    SyncDesktopApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
