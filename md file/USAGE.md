# Руководство по использованию Unified Sync

## Что нового в v1.0?

Вместо отдельных скриптов для каждого проекта (`sync_nitro_rush.py`, `sync_sinstreets.py`), теперь есть один универсальный скрипт `sync_unified.py`, который:

✅ Поддерживает **любой** проект в твоём Redmine
✅ Позволяет **выбирать версии** при каждом запуске
✅ **Интерактивный** ввод (не нужно редактировать код)
✅ Работает с **любой Google Sheets таблицей**

## Пошаговая инструкция

### Первый запуск

```bash
# 1. Установи зависимости
pip install -r requirements.txt

# 2. Положи service_account.json рядом со скриптом
# (закачай его с https://console.cloud.google.com)

# 3. Поделись Google Sheets с email из service_account.json

# 4. Запусти скрипт
python sync_unified.py
```

### Интерактивные вопросы

**Вопрос 1: Redmine URL**
```
Введите URL Redmine [https://redmine.justmoby.com/]:
```
Просто нажми Enter если используешь redmine.justmoby.com, или введи свой URL.

**Вопрос 2: API ключ**
```
Введите Redmine API ключ:
```
Найди в Redmine: `My account` → `API access key` → Скопируй

**Вопрос 3: Выбери проект**
```
Доступные варианты:
  1. [1] Nitro Rush
  2. [2] Sin Streets
  3. [3] Main Project
  
Выберите номер (1-3):
```
Введи номер проекта и нажми Enter.

**Вопрос 4: Выбери версии**
```
Доступные варианты (введите номера через запятую или 'все' для всех, или Enter для пропуска):
  1. [15] 0.6.0 (пуши, улучшения, награды)
  2. [16] 0.6.1 iOS
  3. [17] 0.6.2 Android
  
Выбор:
```

Варианты:
- Введи `1` — выбрать только первую версию
- Введи `1,3` — выбрать версии 1 и 3
- Введи `все` — выбрать все версии
- Нажми Enter — использовать все версии

**Вопрос 5: Google Sheets**
```
Введите Google Sheets ID или URL:
```

Варианты:
- Скопируй ID из URL: `1PBEBm5SY_D92_nA7FCsTlK02A88ZFsETY2xbbvp8U9s`
- Или скопируй полный URL: `https://docs.google.com/spreadsheets/d/1PBEBm5SY_D92_nA7FCsTlK02A88ZFsETY2xbbvp8U9s/edit`

**Результат:**
```
✓ Google Sheets обновлена: My Spreadsheet / Sheet1
  Проект: [1] Nitro Rush
  Версии: 0.6.0; 0.6.1 iOS
  Задач записано: 42
  Время синхронизации: 2026-09-14 14:32:15
```

## Примеры использования

### Сценарий 1: Синхронизировать все баги Nitro Rush

1. Запусти скрипт
2. Введи API ключ
3. Выбери "Nitro Rush"
4. Нажми Enter (выбери все версии)
5. Введи ID Google Sheets
6. ✅ Готово!

### Сценарий 2: Синхронизировать только версию 0.6.0

1. Запусти скрипт
2. Введи API ключ
3. Выбери "Nitro Rush"
4. Выбери только версию "0.6.0" (ввести `1` если это первая в списке)
5. Введи ID Google Sheets
6. ✅ Готово!

### Сценарий 3: Использовать разные таблицы для разных версий

1. Для версии 0.6.0: запусти скрипт, выбери версию, указови одну Google Sheets
2. Для версии 0.6.1: запусти скрипт, выбери версию, укажи другую Google Sheets

## Частые вопросы

**Q: Где найти API ключ?**
A: В Redmine твой профиль → My account → API access key (скопируй оттуда)

**Q: Как добавить новый проект?**
A: Просто добавь его в Redmine, скрипт автоматически покажет его в списке

**Q: Могу ли я синхронизировать несколько версий одновременно?**
A: Да! Введи номера через запятую, например: `1,2,3`

**Q: Что если я выберу неправильный проект?**
A: Просто запусти скрипт снова и выбери правильный

**Q: Как часто запускать синхронизацию?**
A: Когда тебе нужны свежие данные. Можно вручную или настроить через Windows Task Scheduler

## Миграция из старых скриптов

Если ты использовал `redmine_google_sheets_nitro_rush_060/redmine_export_google_sheets.py`:

**Старый способ:**
```bash
cd redmine_google_sheets_nitro_rush_060
python redmine_export_google_sheets.py
# Нужно редактировать hardcoded переменные в коде
```

**Новый способ:**
```bash
python sync_unified.py
# Просто выбираешь проект и версию!
```

## Структура папок

```
d:/Project/redmine-google-sheets-sync/
├── sync_unified.py              # ИСПОЛЬЗУЙ ЭТО ✅
├── redmine_client.py            # Библиотека API
├── requirements.txt             # Зависимости
├── start_sync.bat               # Быстрый запуск (Windows)
├── service_account.json         # ⚠️ Не грузить в git!
│
├── redmine_google_sheets_nitro_rush_060/    # СТАРОЕ (архив)
│   ├── redmine_export_google_sheets.py
│   └── redmine_client.py
│
├── redmine_google_sheets_sinstreets_051/    # СТАРОЕ (архив)
│   ├── redmine_export_google_sheets.py
│   └── redmine_client.py
│
└── README.md                    # Основная документация
```

## Решение проблем

### Ошибка: "Network error while calling Redmine"
- Проверь URL Redmine (например, может быть забыл `/` в конце)
- Проверь интернет-соединение

### Ошибка: "Redmine API returned HTTP 401"
- Проверь API ключ — может быть неправильный
- Попробуй скопировать ключ снова из Redmine

### Ошибка: "Не найден service_account.json"
- Положи файл в ту же папку, что и скрипт
- Переименуй его точно в `service_account.json`

### Ошибка: "В таблице не найдена вкладка"
- Убедись, что используешь правильный Google Sheets ID
- Проверь, что таблица первая (gid=0) или укажи другой номер вкладки

## Получение Google Service Account

1. Перейди на https://console.cloud.google.com
2. Выбери проект или создай новый
3. Включи API: Sheets API и Drive API
4. Создай Service Account (IAM & Admin → Service Accounts)
5. Создай ключ JSON
6. Положи JSON-файл в папку со скриптом
7. Поделись Google Sheets с email из JSON файла

## Напишите, если есть вопросы! 💬
