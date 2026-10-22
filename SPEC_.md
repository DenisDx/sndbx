# SPEC.md corrections

This file records required changes to the read-only `SPEC.md`. Each section
contains the exact old text, the reason for the correction and the recommended
new text.

## Configuration locations include per-image overlay files

**Old text (exact):**

```text
3. ВСЯ конфигурация системы хранится в двух местах- файлах .env и config.json5 в корне проекта. Все секреты и машино-зависимые настройки (пути) - в .env.
```

**Reason:** the aiset integration moved per-sandbox defaults out of the root
`config.json5` into `images/<image-dir>/sndbx.overlay.json5`. The root remains the
registry and override layer; overlay values are merged under it and the root
wins every conflict. Secrets and machine-specific paths still belong in `.env`.

**Recommended new text:**

```text
3. Конфигурация системы хранится в трёх видах мест: `.env` (секреты и
   машино-зависимые настройки, пути), корневой `config.json5` (реестр песочниц,
   глобальные настройки и override-слой) и
   `images/<image-dir>/sndbx.overlay.json5` (значения по умолчанию для песочниц
   этого образа). Корневой конфиг перекрывает overlay при совпадении ключей;
   overlay-ключи, отсутствующие в корне, добавляются. Схема проверяется
   `config.descriptor.json5` и `config.types.json5`.
```

## Settings tab edits root and overlays

**Old text (exact):**

```text
2. settings: изменение настроек. Позволяет редактировать config.json5 ключи. Надо учитвать что значения любого поля могут быть плейсхолдерами
```

**Reason:** the Settings tab now edits the root configuration and every
per-image overlay through aiset. Placeholders are preserved; the descriptor
validates types, ranges and `env.allowed`; writes are revision-checked.

**Recommended new text:**

```text
2. settings: изменение настроек через aiset. Позволяет редактировать ключи
   корневого `config.json5` и каждого
   `images/<image-dir>/sndbx.overlay.json5`. Значения любого поля могут быть
   плейсхолдерами; они сохраняются в файле, а не заменяются resolved-значением.
   Корневой конфиг перекрывает overlay. Изменения, требующие перезапуска,
   показываются как `restart_required`.
```
