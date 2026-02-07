import re
from pathlib import Path
from typing import Dict, List
from bs4 import BeautifulSoup
from collections import defaultdict
import time
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ────────────────────────────────────────────────
# Настройки
# ────────────────────────────────────────────────

DECKLIST_PATH = "decklist.txt"
DELAY_BETWEEN_REQUESTS = 3.0          # секунды, защита от бана
TIMEOUT_MS = 15000                    # 15 секунд на ожидание таблицы

# ────────────────────────────────────────────────
# Вспомогательные функции
# ────────────────────────────────────────────────

def normalize_card_name(name: str) -> str:
    name = name.strip()
    name = re.sub(r'\s*//.*', '', name)          # убираем // Sanctum of the Sun и подобные
    name = re.sub(r'\s+', ' ', name)
    return name.lower()


def load_decklist(path: str | Path) -> List[dict]:
    deck = []
    path = Path(path)
    if not path.exists():
        print(f"Файл не найден: {path}")
        return deck

    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith(('#', '//')):
                continue

            # Поддерживаем 1x Name (set), 1 Name (set), 1x Name
            m = re.match(r'^(\d+)[xX]?\s+(.+?)(?:\s*\(([^)]+)\))?$', line.strip())
            if m:
                qty, name, set_code = m.groups()
                qty = int(qty)
                set_code = (set_code or "").strip().lower()
                deck.append({
                    "qty": qty,
                    "name": name.strip(),
                    "normalized": normalize_card_name(name),
                    "set": set_code
                })
            else:
                print(f"Не распознана строка: {line}")

    return deck


def fetch_page_with_playwright(url: str) -> str | None:
    """Получаем HTML с выполнением JavaScript"""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
                locale="ru-RU",
            )
            page = context.new_page()

            page.goto(url, wait_until="networkidle", timeout=30000)

            # Ждём появления хотя бы одной строки в таблице
            page.wait_for_selector(
                "table.js-singles-search tbody tr",
                timeout=TIMEOUT_MS
            )

            html = page.content()
            browser.close()
            return html

    except PlaywrightTimeoutError:
        print(f"Таймаут ожидания таблицы: {url}")
        return None
    except Exception as e:
        print(f"Ошибка Playwright при загрузке {url}: {e}")
        return None


def parse_single_card_table(html: str, card_query: str) -> Dict[str, float]:
    """Парсит таблицу и возвращает {продавец: мин. цена за 1 шт.}"""
    if not html:
        return {}
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table.js-singles-search")
    if not table:
        print(f"Таблица не найдена для {card_query}")
        return {}

    seller_to_min_price = defaultdict(lambda: float('inf'))

    rows = table.select("tbody tr")
    if not rows:
        print(f"Строки в таблице не найдены (tbody tr) → {card_query}")
        return {}

    for row in rows:
        tds = row.select("td")
        if len(tds) < 4:
            continue

        # Количество
        qty_span = tds[0].select_one("span[data-bind='text: qty']")
        qty = int(qty_span.get_text(strip=True)) if qty_span else 1

        # Цена
        price_td = tds[1]
        price_text = price_td.get_text(strip=True).replace("р.", "").strip()
        try:
            price = float(price_text)
        except ValueError:
            continue

        # Продавец
        seller_a = tds[2].find("a")  
        if not seller_a:
            continue
        seller = seller_a.get_text(strip=True)

        # Цена за единицу
        price_per_unit = price / qty if qty > 0 else price

        seller_to_min_price[seller] = min(seller_to_min_price[seller], price_per_unit)

    return dict(seller_to_min_price)


def main():
    deck = load_decklist(DECKLIST_PATH)
    if not deck:
        print("Список карт пустой")
        return

    print(f"Загружено карт: {len(deck)}\n")

    # продавец → {нормализованное_имя_карты → мин_цена_за_штуку}
    seller_data: Dict[str, Dict[str, float]] = defaultdict(dict)

    for card in deck:
        print(f"Обрабатываем: {card['qty']}× {card['name']} ({card['set']}) ... ", end="")

        query = card['name']
        if card['set']:
            query += f" ({card['set']})"

        url = f"https://topdeck.ru/apps/toptrade/singles/search?q={query.replace(' ', '+')}"

        html = fetch_page_with_playwright(url)
        if not html:
            print("не удалось загрузить страницу")
            continue

        prices = parse_single_card_table(html, card['name'])
        if not prices:
            print("предложений не найдено")
            continue

        norm_name = card['normalized']
        for seller, price in prices.items():
            # сохраняем минимальную цену у продавца для этой карты
            if norm_name not in seller_data[seller] or price < seller_data[seller][norm_name]:
                seller_data[seller][norm_name] = price

        print(f"{len(prices)} предложений")
        time.sleep(DELAY_BETWEEN_REQUESTS)

    # ────────────────────────────────────────────────
    # Вывод результатов
    # ────────────────────────────────────────────────

    print("\n" + "═" * 70)
    print("Собранные данные (продавец → карты → мин. цена за 1 шт.)")
    print("═" * 70)

    # Сортируем по количеству карт у продавца (убывание)
    for seller, cards in sorted(seller_data.items(), key=lambda x: len(x[1]), reverse=True):
        print(f"\n{seller}  ({len(cards)} карт)")
        for card_name, price in sorted(cards.items(), key=lambda x: x[1]):
            print(f"  • {card_name:.<40} {price:>6.0f} ₽")

    print("\n" + "═" * 70)
    print("Топ по количеству покрытых карт:")
    print("═" * 70)

    all_card_names = {c['normalized'] for c in deck}

    for seller, cards in sorted(seller_data.items(), key=lambda x: len(x[1]), reverse=True):
        covered = len(cards)
        if covered >= 1:  # можно поднять порог
            total = sum(cards.values())
            print(f"{seller:.<30} {covered:>2} карт   ≈ {total:>6.0f} ₽")

    with open("sellers_report.txt", "w", encoding="utf-8") as f:
        f.write("Собранные данные по продавцам\n")
        f.write("=" * 60 + "\n\n")
    
        for seller, cards in sorted(seller_data.items(), key=lambda x: len(x[1]), reverse=True):
            f.write(f"{seller}  ({len(cards)} карт)\n")
            for card, price in sorted(cards.items(), key=lambda x: x[1]):
                f.write(f"  • {card:.<40} {price:>6.0f} ₽\n")
            f.write("\n")

    print("Отчёт сохранён в sellers_report.txt")

if __name__ == "__main__":
    main()