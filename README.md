# YouTrack skill для Claude Code

Скилл `youtrack` даёт Claude доступ к YouTrack через REST API:

- карточка задачи, поиск, связанные задачи, история статусов;
- аналитика по задаче и её графу связей: время в статусах, переоткрытия, трудозатраты, прогресс и открытые зависимости;
- выгрузка текстов задачи и связанных задач для чеклиста требований (чеклист составляет Claude по шаблону из `SKILL.md`).

Клиент — `.claude/skills/youtrack/scripts/yt.py`, только стандартная библиотека Python 3.

## Настройка

Адрес YouTrack задан в `.claude/skills/youtrack/config.env` (`https://youtrack.wildberries.ru`). Его можно переопределить переменной `YOUTRACK_URL`.

Токен выпускается в YouTrack: профиль → Account Security → Tokens → New token (scope YouTrack). Не коммитьте токен в репозиторий.

**Облачная сессия Claude Code (рекомендуется):** окружение → **Edit environment** → **Network secrets** → **Add secret**:
- **Name**: `YouTrack`
- **Allowed websites**: `youtrack.wildberries.ru`
- **Custom headers**: Name `Authorization`, Prefix `Bearer`, Value — токен `perm:...`
- **Connect**

Хост из секрета становится доступен сам, менять Network access не нужно. Сессия токен не видит. Если YouTrack доступен только из корпоративной сети или через VPN, облако до него не достанет — тогда запускайте локально.

**Локально:** склонируйте репозиторий, создайте `~/.config/youtrack/env` со строкой `YOUTRACK_TOKEN=perm:...` и запустите `claude` в папке репозитория. Чтобы скилл был доступен во всех проектах, скопируйте `.claude/skills/youtrack` в `~/.claude/skills/`.

## Команды

```bash
YT=.claude/skills/youtrack/scripts/yt.py
python3 $YT me                          # проверить доступ
python3 $YT issue ABC-123               # карточка задачи
python3 $YT links ABC-123               # связанные задачи
python3 $YT history ABC-123             # изменения полей
python3 $YT search "project: ABC #Unresolved"
python3 $YT analyze ABC-123 --depth 1   # аналитика
python3 $YT context ABC-123 --depth 1   # тексты для чеклиста требований
python3 $YT comment ABC-123 --file checklist.md   # опубликовать комментарий
```

`--json` перед командой — сырой JSON.

## Примеры запросов к Claude

- «Собери аналитику по ABC-123»
- «Пройди по связанным задачам ABC-123 на глубину 2 и найди блокеры»
- «Составь чеклист требований по ABC-123»
