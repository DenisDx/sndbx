# SSH Key Sync и Shared Data Setup

Реализована гибридная схема (вариант 3) для управления SSH ключами и общей папкой данных.

## Архитектура

### 1. SSH ключи - Трёхуровневый подход

#### При старте ВМ (on_system_start)
- Ядро передает `SNDBX_CONTEXT_JSON` с полем `ssh_keys` из конфига
- Скрипт `app.py` записывает эти ключи в `/root/.ssh/authorized_keys`
- Права: `700` (папка), `600` (файл), владелец `root:root`

#### Периодическая синхронизация (systemd timer)
- Каждые 30 секунд после старта ВМ запускается systemd timer `sndbx-ssh-sync.timer`
- Скрипт проверяет `/root/shared/.ssh-sync.json` на хосте
- Если есть новые ключи → обновляет `/root/.ssh/authorized_keys`
- **Преимущество**: можно менять ключи без перезапуска ВМ

### 2. Общая папка /root/shared

- **На хосте**: `./shared/default_sandbox_1/shared/`
- **В ВМ**: `/root/shared/`
- **Права**: `0777` на хосте (все пользователи могут читать/писать)
- **Использование**: для обмена файлами между хостом и ВМ

---

## Как использовать

### Способ 1: Добавить SSH ключи при старте (вариант 1)

Редактируем `config.json5`:

```json5
"sandbox-1": {
  ssh_keys: [
    "ssh-rsa AAAA...",
    "ssh-ed25519 BBBB..."
  ],
  shared_directories: [...]
}
```

**Плюсы**: просто, безопасно (ключи в конфиге)  
**Минусы**: нужен перезапуск ВМ для изменений

### Способ 2: Синхронизация через JSON на shared mount (вариант 2)

Создаем файл на хосте:

```bash
# На хосте (локальный пользователь может это делать)
cat > ./shared/default_sandbox_1/shared/.ssh-sync.json <<EOF
{
  "keys": [
    "ssh-rsa AAAA...",
    "ssh-ed25519 BBBB..."
  ]
}
EOF
```

**Плюсы**: можно менять ключи без перезапуска (через 30 секунд применяются)  
**Минусы**: нужен доступ к shared папке

**Как проверить**: 
```bash
# Внутри ВМ
cat /root/.ssh/authorized_keys
systemctl status sndbx-ssh-sync.timer
```

### Способ 3: Обоба способа вместе (вариант 3 - рекомендуемый)

1. В конфиге `config.json5` указываем базовые ключи (конфиг-ключи)
2. Дополнительно можем синхронизировать ключи через `/.ssh-sync.json`
3. При синхронизации используются ключи из JSON файла (перезаписывают конфиг-ключи)

Это позволяет:
- Иметь "постоянные" ключи в конфиге
- Динамически добавлять "временные" ключи через JSON

---

## Общая папка /root/shared

### На хосте

```bash
# Папка уже создана с правами 777
ls -la ./shared/default_sandbox_1/shared/

# Пользователь может создавать/редактировать файлы
touch ./shared/default_sandbox_1/shared/test.txt
echo "Hello from host" > ./shared/default_sandbox_1/shared/test.txt
```

### Внутри ВМ

```bash
# Файлы видны и доступны
ls -la /root/shared/
cat /root/shared/test.txt

# root может писать в эту папку
echo "Hello from VM" > /root/shared/response.txt
```

---

## Примеры

### Пример 1: Базовая конфигурация

Отредактируйте `config.json5`:

```json5
"sandbox-1": {
  title: "Default Sandbox",
  image: "default_sandbox_1",
  ssh_keys: [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA..."
  ],
  shared_directories: [
    {
      host_path: "${SNDBX_ROOT}/shared/default_sandbox_1/.ssh",
      guest_path: "/root/.ssh",
      permission: "rw"
    },
    {
      host_path: "${SNDBX_ROOT}/shared/default_sandbox_1/shared",
      guest_path: "/root/shared",
      permission: "rw",
      host_mode: "0777"
    }
  ]
}
```

### Пример 2: Синхронизация SSH ключей

```bash
# 1. На хосте: создаём JSON с ключами
cat > ./shared/default_sandbox_1/shared/.ssh-sync.json <<'EOF'
{
  "keys": [
    "ssh-rsa AAAAB3NzaC1yc2EAAAA...",
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA..."
  ]
}
EOF

# 2. Проверяем права (должны быть 777 или хотя бы читаемы)
ls -la ./shared/default_sandbox_1/shared/.ssh-sync.json

# 3. Внутри ВМ (через 30 секунд):
# Ключи автоматически синхронизируются
ssh -i ~/.ssh/id_rsa root@sandbox-ip
```

### Пример 3: Обмен данными через /root/shared

**На хосте:**
```bash
echo "Job config" > ./shared/default_sandbox_1/shared/job.json
```

**В ВМ (читаем данные):**
```bash
cat /root/shared/job.json
```

**В ВМ (пишем результаты):**
```bash
echo '{"status": "done", "result": 42}' > /root/shared/result.json
```

**На хосте (читаем результаты):**
```bash
cat ./shared/default_sandbox_1/shared/result.json
```

---

## Внутренние детали

### Файлы в ВМ

- `/etc/systemd/system/sndbx-ssh-sync.service` — systemd unit для синхронизации
- `/etc/systemd/system/sndbx-ssh-sync.timer` — timer, запускает service каждые 30 сек
- `/root/.ssh/sync-from-share.py` — скрипт синхронизации
- `/root/.ssh/sync-from-share.sh` — wrapper для скрипта

### На хосте

- `./shared/default_sandbox_1/.ssh/` — SSH конфиг (смонтирован из ВМ)
- `./shared/default_sandbox_1/shared/` — общая папка (оба могут писать)
- `./shared/default_sandbox_1/shared/.ssh-sync.json` — файл для синхронизации ключей (опционально)

---

## Отладка

### Проверить статус SSH sync timer внутри ВМ

```bash
systemctl status sndbx-ssh-sync.timer
systemctl list-timers sndbx-ssh-sync.timer
journalctl -u sndbx-ssh-sync -n 20
```

### Ручно запустить синхронизацию

```bash
/root/.ssh/sync-from-share.py
```

### Проверить содержимое authorized_keys

```bash
cat /root/.ssh/authorized_keys
ls -la /root/.ssh/
```

### Проверить доступ на хосте

```bash
# На хосте
ls -la ./shared/default_sandbox_1/shared/
stat ./shared/default_sandbox_1/shared/
```

---

## Безопасность

### SSH ключи

- Внутри ВМ: `700` (папка), `600` (файл), `root:root` — стандарт OpenSSH
- На хосте: `700` (папка) — доступны только root, но монтируются в ВМ с правами `rw`
- JSON sync файл: может быть доступен пользователю хоста через папку `shared` (права `777`)

### Общая папка /root/shared

- На хосте: `777` — все пользователи могут писать
- В ВМ: монтируется с `rw` → root внутри может читать/писать
- **Внимание**: не кладите туда sensitive данные, если нужна изоляция от других пользователей хоста

---

## Технические подробности

### Как работает SSH sync

1. **На старте ВМ** → systemd timer активируется (`OnBootSec=5s`)
2. **Каждые 30 секунд** → timer вызывает `/root/.ssh/sync-from-share.py`
3. **Скрипт**:
   - Проверяет `/root/shared/.ssh-sync.json`
   - Парсит JSON, достает ключи
   - Записывает их в `/root/.ssh/authorized_keys` с правильными правами (`700/600, root:root`)
   - OpenSSH перечитывает ключи (не требует перезапуска sshd)

### Как работает host_mode

1. При запуске контейнера `sandbox.py` читает конфиг
2. Для каждой `shared_directories` проверяет наличие `host_mode`
3. После создания директории вызывает `os.chmod(path, int(host_mode, 8))`
4. В результате директория на хосте получает требуемые права (например, `777` для `/root/shared`)

---
