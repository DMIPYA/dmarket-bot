"""Точка входа: мониторинг инвентаря DMarket + Telegram-уведомления и управление.

Запуск: python bot.py
"""
import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

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

    async def check_once(self) -> None:
        items = await self.dm.user_inventory(self.cfg.game_id, self.cfg.market_name)
        self.last_items = items
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


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    raw_chat = settings.telegram_chat_id.strip()
    chat_id: int | str = int(raw_chat) if raw_chat.lstrip("-").isdigit() else raw_chat

    bot = Bot(settings.telegram_bot_token)
    dm = DMarketClient(settings.dmarket_public_key, settings.dmarket_secret_key)

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
        await message.answer(
            f"Статус: {len(fresh)} доступно / {len(monitor.last_items)} в инвентаре\n"
            f"{price_line}\n"
            f"Автопродажа: {'вкл' if settings.auto_sell else 'выкл'}"
        )

    @dp.message(Command("sell"))
    async def sell_cmd(message: Message) -> None:
        fresh = [i for i in monitor.last_items if is_tradable(i) and item_id(i) not in monitor.listed]
        if not fresh:
            await message.answer("Нет предметов, доступных для продажи")
            return
        await message.answer(f"Выставляю {len(fresh)} шт…")
        await monitor.sell_items(fresh)

    async def monitor_loop() -> None:
        await notify(
            f"🚀 Бот запущен: слежу за «{settings.market_name}» "
            f"каждые {settings.monitor_interval:g} c"
        )
        while True:
            try:
                await monitor.check_once()
            except DMarketError as exc:
                log.warning("DMarket: %s", exc)
            except Exception:
                log.exception("сбой цикла мониторинга")
            await asyncio.sleep(settings.monitor_interval)

    try:
        await asyncio.gather(monitor_loop(), dp.start_polling(bot))
    finally:
        await dm.aclose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
