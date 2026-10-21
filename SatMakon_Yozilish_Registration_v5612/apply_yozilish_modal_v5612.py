#!/usr/bin/env python3
"""SatMakon v5612: open registration modal from every Yozilish price button."""
from pathlib import Path
from datetime import datetime
import sys

CHANGES = [
    (
        "function PricingSection({\n  t,\n  openPicker\n}) {",
        "function PricingSection({\n  t,\n  openReg\n}) {",
    ),
    (
        "onClick: () => openPicker && openPicker()\n  }, p.enroll)",
        "onClick: () => openReg && openReg()\n  }, p.enroll)",
    ),
    (
        "React.createElement(PricingSection, {\n    t: t,\n    openPicker: openPicker\n  })",
        "React.createElement(PricingSection, {\n    t: t,\n    openReg: openReg\n  })",
    ),
]

def find_target():
    here = Path(__file__).resolve().parent
    candidates = [Path.cwd(), here.parent, here]
    for candidate in candidates:
        file = candidate / "public" / "index.html"
        if file.is_file():
            return file
    raise FileNotFoundError(
        "Не найден public/index.html. Распакуйте папку патча в корень SatMakon "
        "(там, где находятся public/ и README.md), затем запустите скрипт."
    )

def main():
    file = find_target()
    content = file.read_text(encoding="utf-8")
    states = [(content.count(old), content.count(new)) for old, new in CHANGES]
    if all(old_count == 0 and new_count == 1 for old_count, new_count in states):
        print("✅ Патч уже установлен. Дополнительные изменения не нужны.")
        return 0
    if not all(old_count == 1 and new_count == 0 for old_count, new_count in states):
        print("❌ Код проекта отличается от ожидаемой версии или изменён частично.")
        print("Файл не изменён. Текущие совпадения:", states)
        return 1
    updated = content
    for old, new in CHANGES:
        updated = updated.replace(old, new, 1)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = file.with_name("index.html.backup_yozilish_" + stamp + ".bak")
    # Preserve the exact original bytes as a backup before touching the page.
    backup.write_bytes(file.read_bytes())
    file.write_text(updated, encoding="utf-8")
    print("✅ Установлено:", file)
    print("↩️ Резервная копия:", backup)
    print("Все три кнопки Yozilish открывают Konsultatsiyaga yozilish.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
