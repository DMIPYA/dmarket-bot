"""Точка входа: мониторинг инвентаря DMarket + Telegram-уведомления и управление.

Запуск: python bot.py
"""
import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import Settings, settings
from dmarket import DMarketClient, DMarketError

log = logging.getLogger("bot")

# ponytail: строгий whitelist статусов DMarket; если API вернёт новый статус —
# предмет считается залоченным (fail-safe), расширьте набор при необходимости
TRADABLE_STATUSES = {"tradable", "active", "available"}


def item_id(item: dict[str, Any]) -> str:
    return str(((item.get("attributes") or {}).get("id")) or item.get("itemId") or item.get("id") or "")


def is_tradable(item: dict[str, Any]) -> bool:
    # v2: attributes.tradable (bool) + tradeLockDays; неизвестная схема → считаем залоченным
    attr = item.get("attributes") or {}
    if "tradable" in attr:
        return bool(attr.get("tradable")) and not attr.get("tradeLockDays")
    return str(item.get("status", "")).strip().lower() in TRADABLE_STATUSES


def item_name(item: dict[str, Any]) -> str:
    return str(((item.get("attributes") or {}).get("name")) or "?")


def group_inventory(items: list[dict[str, Any]]) -> list[tuple[str, int, int, float | None]]:
    """Группировка инвентаря: [(имя, всего, tradable, мин. реком. цена)]."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for i in items:
        groups.setdefault(item_name(i), []).append(i)
    out = []
    for name_, group in groups.items():
        trad = sum(1 for i in group if is_tradable(i))
        prices = []
        for i in group:
            try:
                prices.append(float(str((i.get("offerRecommendedPrice") or {}).get("Amount"))))
            except (TypeError, ValueError):
                pass
        out.append((name_, len(group), trad, min(prices) if prices else None))
    out.sort(key=lambda g: -g[1])
    return out


def overstock_states(items: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    """(blocked_ids, unblocked_ids) для предметов в Steam (не inMarket)."""
    blocked: set[str] = set()
    open_ids: set[str] = set()
    for i in items:
        if i.get("inMarket"):
            continue
        tgt = blocked if (i.get("attributes") or {}).get("overstocked") else open_ids
        tgt.add(item_id(i))
    return blocked, open_ids


def is_uuid(s: str) -> bool:
    return bool(re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", s))


def parse_sell_cb(data: str) -> tuple[str, int, int | None]:
    """'pick:3' / 'price:3:150' / 'go:3:150' → (action, n, cents | None)."""
    action, _, rest = data.partition(":")
    parts = rest.split(":")
    n = int(parts[0])
    cents = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    return action, n, cents


def rec_price(item: dict[str, Any]) -> float | None:
    try:
        return float(str((item.get("offerRecommendedPrice") or {}).get("Amount")))
    except (TypeError, ValueError):
        return None


def confirm_kb(n: int, cents: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Выставить за ${cents / 100:.2f}", callback_data=f"go:{n}:{cents}"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel"),
    ]])


def fmt_dur(sec: float) -> str:
    m, s = divmod(max(int(sec), 0), 60)
    h, m = divmod(m, 60)
    return f"{h}ч {m}м" if h else (f"{m}м {s}с" if m else f"{s}с")


class Monitor:
    """Следит за статусом предметов в инвентаре DMarket, уведомляет и продаёт при разблокировке."""

    def __init__(
        self,
        dmarket: DMarketClient,
        cfg: Settings,
        notify: Callable[[str], Awaitable[None]],
    ) -> None:
        self.dm = dmarket
        self.cfg = cfg
        self.notify = notify
        self.seen_tradable: set[str] = set()
        self.listed: set[str] = set()  # уже выставлены — защита от двойного листинга
        self.last_items: list[dict[str, Any]] = []
        self.started_at = time.monotonic()
        self.last_ok: float | None = None
        self.errors = 0
        self.sell_menu: list[dict[str, Any]] = []  # нумерованный список для /sell N
        self.overstock_blocked: set[str] = set()  # кейсы, ждущие открытия депозита

    async def check_once(self) -> None:
        items = await self.dm.user_inventory(self.cfg.game_id)
        self.last_items = items
        self.last_ok = time.monotonic()

        # ЦЕЛЬ ТЗ: как можно раньше узнать, что DMarket снова принимает кейс (overstocked → False)
        blocked, open_ids = overstock_states(items)
        cases_open = [i for i in items if item_id(i) in open_ids and self.cfg.market_name.lower() in item_name(i).lower()]
        if cases_open and self.overstock_blocked:  # переход: было заблокировано → открылось
            n = len(cases_open)
            await self.notify(
                f"🟢 ТОРГОВЛЯ ОТКРЫТА: DMarket снова принимает «{self.cfg.market_name}» "
                f"в депозит ({n} шт доступны). Команда: /deposit"
            )
            if self.cfg.auto_sell:
                await self.deposit_and_sell(cases_open)
        newly_blocked = blocked - self.overstock_blocked
        if newly_blocked:
            log.info("overstocked: %s", ", ".join(sorted(newly_blocked)[:3]))
        self.overstock_blocked = blocked

        fresh = [i for i in items if is_tradable(i) and item_id(i) not in self.listed]
        new = [i for i in fresh if item_id(i) not in self.seen_tradable]
        if new:
            self.seen_tradable.update(item_id(i) for i in new)
            await self.notify(
                f"🔓 {self.cfg.market_name}: доступно для торговли {len(new)} шт. "
                f"(всего в инвентаре {len(items)})"
            )
            if self.cfg.auto_sell:
                await self.sell_items(new)
        elif not fresh and self.seen_tradable:
            self.seen_tradable.clear()
            await self.notify(f"🔒 {self.cfg.market_name}: доступных предметов больше нет")

    async def deposit_and_sell(self, items: list[dict[str, Any]]) -> None:
        """AUTO_SELL: депозит открытых предметов, затем продажа по ask−1¢ (после подтверждения трейда в Steam)."""
        res = await self.deposit_items(items)
        await self.notify(res)
        # ponytail: продажа после депозита требует подтверждения Steam-трейда пользователем;
        # полноценный авто-флоу: опрос deposit-status до Done, затем batchCreate. Добавим, когда
        # авто-режим реально понадобится — сейчас уведомления + /deposit + /sell достаточно.

    async def execute_sell(self, n: int, cents: int) -> str:
        """Выставить предмет №n из sell_menu за cents. Возвращает текст результата."""
        if not (1 <= n <= len(self.sell_menu)):
            return "Неверный номер предмета — вызови /sell заново"
        item = self.sell_menu[n - 1]
        if item_id(item) in self.listed:
            return "Этот предмет уже выставлен"
        if not is_uuid(item_id(item)):
            return (
                "❌ Предмет ещё в Steam-инвентаре (не задепонирован на DMarket).\n"
                "Сначала /deposit — после депозита он получит ID для продажи."
            )
        try:
            resp = await self.dm.create_sell_offers([{"assetId": item_id(item), "priceCents": cents}])
        except DMarketError as exc:
            return f"❌ DMarket: {str(exc)[:200]}"
        failed = resp.get("failed") or []
        if failed:
            return f"❌ Отклонено: {str(failed[0])[:200]}"
        self.listed.add(item_id(item))
        return f"💰 Выставлено: {item_name(item)} за ${cents / 100:.2f}"

    async def deposit_items(self, items: list[dict[str, Any]]) -> str:
        """POST /marketplace-api/v1/deposit-assets — задепонировать предметы из Steam на DMarket."""
        ids = [item_id(i) for i in items]
        try:
            resp = await self.dm.deposit_assets(ids)
        except DMarketError as exc:
            if "UnavailableItem" in str(exc):
                return (
                    "⛔ DMarket не принимает этот предмет в депозит сейчас (overstocked — "
                    "их боты переполнены им). Повтори позже, сток расходится."
                )
            return f"❌ Депозит: {str(exc)[:200]}"
        dep_id = resp.get("DepositID") or resp.get("depositId") or "?"
        return (
            f"📦 Депозит запрошен ({len(ids)} шт, ID {dep_id}).\n"
            "Подтверди трейд в Steam (могут прийти 2 оффера), затем подожди пару минут "
            "и проверь /invent — предметы станут inMarket."
        )

    async def execute_sell_if_confirmed(self, n: int, cents: int, args: list[str]) -> str:
        if len(args) < 3 or args[2].lower() != "подтвердить":
            if not (1 <= n <= len(self.sell_menu)):
                return "Неверный номер — вызови /sell заново"
            return (
                f"Выставить «{item_name(self.sell_menu[n - 1])}» за ${cents / 100:.2f}?\n"
                f"Подтверждение: /sell {n} {cents / 100:.2f} подтвердить"
            )
        return await self.execute_sell(n, cents)

    async def sell_items(self, items: list[dict[str, Any]]) -> None:
        # ponytail: цена = лучший ask стакана − $0.01 (фронт книги, продажа за секунды);
        # если DMarket откроет прямой sell-to-buy-order эндпоинт — заменить этот расчёт
        ask = await self.dm.lowest_ask(self.cfg.market_name, self.cfg.game_id)
        if ask is None:
            await self.notify("⚠️ Не удалось получить цену стакана — продажа отложена до следующего цикла")
            return
        cents = max(round((ask - 0.01) * 100), 1)
        offers = [{"assetId": item_id(i), "priceCents": cents} for i in items]
        try:
            resp = await self.dm.create_sell_offers(offers)
        except DMarketError as exc:
            await self.notify(f"❌ Ошибка выставления на продажу: {exc}")
            return
        failed = resp.get("failed") or []
        self.listed.update(item_id(i) for i in items)
        await self.notify(
            f"💰 Выставлено {len(offers) - len(failed)} × {self.cfg.market_name} "
            f"по ${cents / 100:.2f} (стакан: ${ask:.2f})"
            + (f"; отклонено: {len(failed)}" if failed else "")
        )


async def _health(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def start_http() -> None:
    """Render требует открытый порт; /healthz + само-пинг против spin-down (15 мин)."""
    app = web.Application()
    app.router.add_get("/healthz", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", "10000"))).start()


async def keepalive() -> None:
    base = os.environ.get("RENDER_EXTERNAL_URL")
    if not base:
        return  # локальный запуск — пингать некого
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await client.get(f"{base}/healthz", timeout=10)
            except Exception:
                log.warning("keepalive: пинг не прошёл")
            await asyncio.sleep(10 * 60)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    raw_chat = settings.telegram_chat_id.strip()
    chat_id: int | str = int(raw_chat) if raw_chat.lstrip("-").isdigit() else raw_chat

    bot = Bot(settings.telegram_bot_token)
    dm = DMarketClient(settings.dmarket_public_key, settings.dmarket_secret_key)
    await bot.set_my_commands([
        BotCommand(command="status", description="Состояние бота и кейса"),
        BotCommand(command="invent", description="Инвентарь DMarket"),
        BotCommand(command="sell", description="Выставить предмет на продажу"),
        BotCommand(command="deposit", description="Перенести предмет из Steam на DMarket"),
    ])

    async def notify(text: str) -> None:
        try:
            await bot.send_message(chat_id, text)
        except Exception:
            log.exception("не удалось отправить сообщение в Telegram")

    monitor = Monitor(dm, settings, notify)
    dp = Dispatcher()
    dp.message.filter(F.chat.id == chat_id)  # команды только из нашего чата

    @dp.message(Command("status"))
    async def status_cmd(message: Message) -> None:
        fresh = [i for i in monitor.last_items if is_tradable(i) and item_id(i) not in monitor.listed]
        ask = await dm.lowest_ask(settings.market_name, settings.game_id)
        price_line = f"Лучший ask: ${ask:.2f}" if ask is not None else "Цена стакана недоступна"
        last_ok = "—" if monitor.last_ok is None else f"{fmt_dur(time.monotonic() - monitor.last_ok)} назад"
        cases_waiting = len(monitor.overstock_blocked)
        gate_line = (
            f"Депозит кейсов: ⛔ закрыт (overstocked, {cases_waiting} шт ждут)"
            if cases_waiting else "Депозит кейсов: 🟢 открыт"
        )
        await message.answer(
            f"🟢 Работает. Аптайм: {fmt_dur(time.monotonic() - monitor.started_at)}\n"
            f"Инвентарь: {len(monitor.last_items)} шт, из них tradable: {len(fresh)}\n"
            f"Кейс: {price_line}\n"
            f"{gate_line}\n"
            f"Успешных циклов назад: {last_ok} (ошибок: {monitor.errors})\n"
            f"Автопродажа: {'вкл' if settings.auto_sell else 'выкл'}"
        )

    @dp.message(Command("invent"))
    async def invent_cmd(message: Message) -> None:
        items = await dm.user_inventory(settings.game_id)
        if not items:
            await message.answer("Инвентарь пуст")
            return
        lines = ["📦 Инвентарь DMarket:"]
        for name_, total, trad, price in group_inventory(items)[:30]:
            p = f" ~${price:.2f}" if price is not None else ""
            lines.append(f"• {name_}: {total} шт ({trad} tradable{p})")
        if len(items) > 30:
            lines.append("…")
        await message.answer("\n".join(lines))

    @dp.message(Command("sell"))
    async def sell_cmd(message: Message) -> None:
        args = (message.text or "").split()[1:]
        fresh = [i for i in monitor.last_items if is_tradable(i) and item_id(i) not in monitor.listed]
        if not fresh:
            # последний опрос не видел tradable — перепроверим живым запросом
            fresh = [i for i in await dm.user_inventory(settings.game_id) if is_tradable(i)]
        fresh = [i for i in fresh if item_id(i) not in monitor.listed]
        if not fresh:
            await message.answer("Нет предметов, доступных для продажи")
            return
        if not args:
            monitor.sell_menu = fresh[:20]
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"{item_name(i)[:30]} — ${rec_price(i)}", callback_data=f"pick:{n}")]
                for n, i in enumerate(monitor.sell_menu, 1)
            ])
            await message.answer("Выбери предмет для продажи:", reply_markup=kb)
            return
        try:
            num = int(args[0])
            price = round(float(args[1].replace(",", ".")) * 100)
        except (IndexError, ValueError):
            await message.answer("Формат: /sell (список) или /sell <№> <цена$> <подтвердить>")
            return
        await message.answer(await monitor.execute_sell_if_confirmed(num, price, args))

    @dp.callback_query(F.data.startswith(("pick:", "price:", "go:", "cancel")))
    async def sell_cb(cb: CallbackQuery) -> None:
        async def edit(text: str, kb: InlineKeyboardMarkup | None = None) -> None:
            if isinstance(cb.message, Message):
                await cb.message.edit_text(text, reply_markup=kb)

        data = cb.data or ""
        if data == "cancel":
            await edit("Продажа отменена")
            await cb.answer()
            return
        action, n, cents = parse_sell_cb(data)
        if not (1 <= n <= len(monitor.sell_menu)):
            await cb.answer("Список устарел — вызови /sell заново", show_alert=True)
            return
        item = monitor.sell_menu[n - 1]
        if action == "pick":
            ask = await dm.lowest_ask(item_name(item), settings.game_id)
            rec = rec_price(item)
            in_market = bool(item.get("inMarket"))
            price_hint = f"Лучший ask: {'${:.2f}'.format(ask) if ask else '—'}, рек. цена: ${rec:.2f}" if rec else (
                f"Лучший ask: {'${:.2f}'.format(ask) if ask else '—'}"
            )
            if not in_market:
                await edit(
                    f"«{item_name(item)}» — ещё в Steam (не задепонирован).\n"
                    f"Сначала /deposit, после депозита появится ID для продажи."
                )
            else:
                await edit(
                    f"«{item_name(item)}»\n{price_hint}\n\n"
                    f"Напиши в чат цену: sell {n} <цена$>  (например: sell {n} 1.93)"
                )
        elif action == "price" and cents is not None:
            await edit(f"Выставить «{item_name(item)}» за ${cents / 100:.2f}?", confirm_kb(n, cents))
        elif action == "go" and cents is not None:
            await edit(await monitor.execute_sell(n, cents))
        await cb.answer()

    @dp.message(Command("deposit"))
    async def deposit_cmd(message: Message) -> None:
        args = (message.text or "").split()[1:]
        steam_items = [i for i in monitor.last_items if not i.get("inMarket")]
        if not steam_items:
            # перепроверка живым запросом
            steam_items = [i for i in await dm.user_inventory(settings.game_id) if not i.get("inMarket")]
        if not steam_items:
            await message.answer("Нет предметов в Steam, доступных для депозита")
            return
        if not args:
            steam_items = [i for i in steam_items if not (i.get("attributes") or {}).get("overstocked")][:20]
            if not steam_items:
                await message.answer("Все предметы из Steam сейчас overstocked — DMarket их не принимает. Повтори позже.")
                return
            monitor.sell_menu = steam_items  # переиспользуем нумерацию
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"📦 {item_name(i)[:30]}", callback_data=f"dep:{n}")]
                for n, i in enumerate(monitor.sell_menu, 1)
            ])
            await message.answer("Что задепонировать на DMarket?", reply_markup=kb)
            return
        try:
            num = int(args[0])
            item = monitor.sell_menu[num - 1]
        except (IndexError, ValueError):
            await message.answer("Формат: /deposit (список) или /deposit <№>")
            return
        await message.answer(await monitor.deposit_items([item]))

    @dp.callback_query(F.data.startswith("dep:"))
    async def dep_cb(cb: CallbackQuery) -> None:
        n = int((cb.data or "").partition(":")[2])
        if not (1 <= n <= len(monitor.sell_menu)):
            await cb.answer("Список устарел — вызови /deposit заново", show_alert=True)
            return
        if isinstance(cb.message, Message):
            await cb.message.edit_text(await monitor.deposit_items([monitor.sell_menu[n - 1]]))
        await cb.answer()

    @dp.message(F.text & ~F.text.startswith("/"))
    async def price_input(message: Message) -> None:
        """Текстовый ввод цены: 'sell 3 1.93' (без слэша) после выбора предмета."""
        parts = (message.text or "").split()
        if len(parts) < 3 or parts[0].lower() != "sell":
            return
        try:
            n = int(parts[1])
            cents = round(float(parts[2].replace(",", ".")) * 100)
        except ValueError:
            await message.answer("Формат: sell <№> <цена$> — например: sell 3 1.93")
            return
        if not (1 <= n <= len(monitor.sell_menu)):
            await message.answer("Неверный номер — вызови /sell заново")
            return
        item = monitor.sell_menu[n - 1]
        await message.answer(
            f"Выставить «{item_name(item)}» за ${cents / 100:.2f}?",
            reply_markup=confirm_kb(n, cents),
        )

    async def monitor_loop() -> None:
        await notify(
            f"🚀 Бот запущен: слежу за «{settings.market_name}» "
            f"каждые {settings.monitor_interval:g} c"
        )
        while True:
            try:
                await monitor.check_once()
            except DMarketError as exc:
                monitor.errors += 1
                log.warning("DMarket: %s", exc)
            except Exception:
                monitor.errors += 1
                log.exception("сбой цикла мониторинга")
            await asyncio.sleep(settings.monitor_interval)

    try:
        await start_http()
        await asyncio.gather(monitor_loop(), dp.start_polling(bot), keepalive())
    finally:
        await dm.aclose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
