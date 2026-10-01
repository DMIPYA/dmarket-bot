"""Клиент DMarket Trading API v2: подпись HMAC-SHA256, async-запросы, backoff на 429.

Базовые ключи DMarket подписываются HMAC-SHA256 (stdlib hmac), Ed25519 не нужен.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Any
from urllib.parse import quote, urlencode

import httpx

log = logging.getLogger("dmarket")

BASE_URL = "https://api.dmarket.com"


class DMarketError(RuntimeError):
    """Неуспешный запрос к DMarket API."""


class DMarketRateLimited(DMarketError):
    """429 после всех повторов."""


def _sign(secret: bytes, method: str, path: str, query: str, body: str, date: str) -> str:
    """X-Request-Sign = base64(HMAC-SHA256(method + path + query + body + date))."""
    string_to_sign = f"{method}{path}{query}{body}{date}"
    digest = hmac.new(secret, string_to_sign.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


class DMarketClient:
    def __init__(self, public_key: str, secret_key: str, timeout: float = 15.0) -> None:
        self._public_key = public_key
        self._secret_key = secret_key.encode()
        self._http = httpx.AsyncClient(base_url=BASE_URL, timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        max_retries: int = 5,
    ) -> Any:
        # ponytail: query собираем вручную — подписанная строка обязана совпадать с URL байт-в-байт
        query = urlencode(params, quote_via=quote) if params else ""
        body = json.dumps(json_body, separators=(",", ":")) if json_body else ""
        url = f"{path}?{query}" if query else path
        for attempt in range(max_retries + 1):
            date = format_datetime(datetime.now(timezone.utc), usegmt=True)
            headers = {
                "X-Api-Key": self._public_key,
                "X-Sign-Date": date,
                "X-Request-Sign": _sign(self._secret_key, method, path, query, body, date),
                "Content-Type": "application/json",
            }
            try:
                resp = await self._http.request(
                    method, url, headers=headers, content=body.encode() if body else None
                )
            except httpx.HTTPError as exc:
                raise DMarketError(f"{method} {path}: {exc}") from exc
            if resp.status_code == 429:
                delay = min(2.0**attempt, 60.0)
                log.warning("429 от %s — пауза %.1fs", path, delay)
                await asyncio.sleep(delay)
                continue
            if resp.status_code >= 500 and attempt < max_retries:
                await asyncio.sleep(1.0)
                continue
            if resp.status_code >= 400:
                raise DMarketError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
            return resp.json() if resp.content else {}
        raise DMarketRateLimited(f"{method} {path}: 429 после {max_retries + 1} попыток")

    # --- endpoints ---

    async def user_inventory(
        self, game_id: str, title: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """GET /marketplace-api/v1/user-inventory — предметы пользователя."""
        params: dict[str, Any] = {
            "Basic": "false",
            "Limit": limit,
            "OrderBy": "CreatedAt",
            "OrderDir": "asc",
            "GameID": game_id,
        }
        if title:
            params["Title"] = title
        data = await self._request("GET", "/marketplace-api/v1/user-inventory", params=params)
        return list(data.get("Items") or data.get("items") or [])

    async def lowest_ask(self, market_name: str, game_id: str) -> float | None:
        """Минимальный ask из публичного стакана (GET /exchange/v1/market/items)."""
        params = {
            "side": "market",
            "orderBy": "price",
            "orderDir": "asc",
            "title": market_name,
            "priceFrom": "0",
            "priceTo": "0",
            "types": "dmarket",
            "cursor": "",
            "limit": "1",
            "currency": "USD",
            "gameId": game_id,
        }
        try:
            data = await self._request("GET", "/exchange/v1/market/items", params=params)
        except DMarketError:
            return None
        items = data.get("items") or []
        if not items:
            return None
        raw = items[0].get("bestPrice") or (items[0].get("price") or {}).get("amount")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    async def create_sell_offers(self, offers: list[dict[str, Any]]) -> Any:
        """POST /marketplace-api/v2/offers:batchCreate — выставить предметы на продажу."""
        return await self._request(
            "POST", "/marketplace-api/v2/offers:batchCreate", json_body={"offers": offers}
        )


def _self_check() -> None:
    """Регрессионный пин формата подписи: ловит случайный дрейф строки подписи."""
    sig = _sign(b"sec", "GET", "/exchange/v1/market/items", "limit=1", "", "Mon, 01 Jan 2024 00:00:00 GMT")
    assert sig == "LOpvDRAzWFTsfiFCnnkMc94vluSE77Sv6xkWin4ivRM=", f"формат подписи изменился: {sig}"
    print("dmarket: подпись OK —", sig)


if __name__ == "__main__":
    _self_check()
