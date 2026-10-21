# SatMakon — ZIP-патч v5612

## Что изменено

**Tariflar → Yozilish** (все три кнопки) теперь открывает точно ту же форму
**Konsultatsiyaga yozilish** (имя, телефон), что и кнопка **Boshlash**.

Ранее Yozilish открывало выбор филиала (LocationPicker). Это устранено.
Сама форма регистрации, API `/api/leads`, тексты, цены и остальные кнопки не менялись.

## Установка (Windows / PowerShell)

1. Распакуйте эту папку `SatMakon_Yozilish_Registration_v5612` в корень сайта SatMakon,
   рядом с `public/`, `README.md` и другими файлами проекта.
2. Из корня проекта запустите:

```powershell
python .\SatMakon_Yozilish_Registration_v5612\apply_yozilish_modal_v5612.py
```

3. Перезапустите сайт или обновите страницу (Ctrl+F5).
4. Нажмите Yozilish на любом из тарифов. Откроется модальное окно регистрации;
   закрыть его можно крестиком, Esc или кликом по затемнению.

Скрипт изменяет **только `public/index.html`** и до изменения сохраняет резервную
копию вида `public/index.html.backup_yozilish_YYYYMMDD_HHMMSS.bak`.
Повторный запуск безопасен: если патч уже применён, файл не меняется.
Если структура/код страницы не совпадают, скрипт завершится без изменений.

**Важно:** На сайте под папками `public/v2/` и `public/v3/` код не меняется:
патч рассчитан на актуальную `public/index.html` из присланного архива
`SatMakon.uz-main(2).zip`.

## Технические изменения

- `PricingSection`: prop `openPicker` → `openReg`.
- Обработчик `p-cta`: вызывает `openReg`.
- `App`: передаёт `openReg` в `PricingSection`.

Окно `RegisterModal` используется существующее, новое не создаётся.
