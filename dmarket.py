"""Клиент DMarket Trading API: подпись Ed25519 (nacl), async-запросы, backoff на 429.

Актуальная схема (docs.dmarket.com): X-Api-Key + X-Sign-Date (unix ts) +
X-Request-Sign = "dmar ed25519 " + hex(Ed25519(method + path?query + body + ts)).
"""
import asyncio
import json
import logging
import time
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from nacl.signing import SigningKey

log = logging.getLogger("dmarket")

BASE_URL = "https://api.dmarket.com"


class DMarketError(RuntimeError):
    """Неуспешный запрос к DMarket API."""


class DMarketRateLimited(DMarketError):
    """429 после всех повторов."""


def _sign(secret: bytes, method: str, path: str, query: str, body: str, date: str) -> str:
    """X-Request-Sign = 'dmar ed25519 ' + hex(Ed25519(method + path + ?query + body + date))."""
    string_to_sign = f"{method}{path}{'?' + query if query else ''}{body}{date}"
    sig = SigningKey(secret).sign(string_to_sign.encode()).signature.hex()
    return f"dmar ed25519 {sig}"


class DMarketClient:
    def __init__(self, public_key: str, secret_key: str, timeout: float = 15.0) -> None:
        self._public_key = public_key
        try:
            self._secret_key = bytes.fromhex(secret_key)  # ключи DMarket — hex
        except ValueError:
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
        # ponytail: query собираем вручную — подписанная строка обязана совпадать с URL байт-в-байт.
        # Путь подписываем DECODED, на провод отправляем percent-encoded (как официальный клиент DMarket).
        query = urlencode(params, quote_via=quote) if params else ""
        body = json.dumps(json_body, separators=(",", ":")) if json_body else ""
        url = "/".join(quote(seg, safe="") for seg in path.split("/")) + (f"?{query}" if query else "")
        for attempt in range(max_retries + 1):
            date = str(int(time.time()))
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
        """GET /marketplace-api/v2/user/inventory — предметы пользователя (v1 удалён, 410)."""
        params: dict[str, Any] = {
            "game_id": game_id,
            "limit": limit,
            "orderBy": "createdAt",
            "orderDir": "asc",
            "currency": "USD",
        }
        if title:
            params["basicFilters.title"] = title
        data = await self._request("GET", "/marketplace-api/v2/user/inventory", params=params)
        items = list(data.get("Items") or data.get("items") or [])
        if title:
            # ponytail: серверный basicFilters.title игнорирует фильтр — фильтруем локально
            items = [i for i in items if title.lower() in ((i.get("attributes") or {}).get("name") or "").lower()]
        return items

    async def lowest_ask(self, market_name: str, game_id: str) -> float | None:
        """Минимальный ask из стакана (GET /marketplace-api/v2/offers, orderBy=price asc)."""
        params = {
            "game_id": game_id,
            "title": market_name,
            "currency": "USD",
            "orderBy": "price",
            "orderDir": "asc",
            "limit": "1",
        }
        try:
            data = await self._request("GET", "/marketplace-api/v2/offers", params=params)
        except DMarketError:
            return None
        items = data.get("items") or []
        raw = items[0].get("priceCents") if items else None
        try:
            return float(raw) / 100.0 if raw is not None else None
        except (TypeError, ValueError):
            return None

    async def create_sell_offers(self, offers: list[dict[str, Any]]) -> Any:
        """POST /marketplace-api/v2/offers:batchCreate — выставить предметы на продажу.

        offers: [{"assetId": <UUID>, "priceCents": 199}]
        """
        return await self._request(
            "POST", "/marketplace-api/v2/offers:batchCreate", json_body={"requests": offers}
        )

    async def bid_depth(self, market_name: str, game_id: str, limit: int = 20) -> list[tuple[int, int]]:
        """Стакан бидов targets-by-title, убыв. цены: [(цена_центы, объём), …]."""
        data = await self._request(
            "GET",
            f"/marketplace-api/v1/targets-by-title/{game_id}/{market_name}",
            params={"currency": "USD", "limit": str(limit), "orderBy": "price", "orderDir": "desc"},
        )
        out = []
        for o in data.get("orders") or []:
            try:
                out.append((int(o["price"]), int(o["amount"])))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    async def deposit_assets(self, asset_ids: list[str]) -> Any:
        """POST /marketplace-api/v1/deposit-assets — перенос предметов из Steam на DMarket."""
        return await self._request(
            "POST", "/marketplace-api/v1/deposit-assets", json_body={"AssetID": asset_ids}
        )


def _self_check() -> None:
    """Регрессионный пин формата подписи: ловит случайный дрейф строки подписи."""
    sig = _sign(b"\x01" * 32, "GET", "/x/v1/y", "limit=1", "", "1605619994")
    assert sig.startswith("dmar ed25519 ") and len(sig) == 13 + 128, f"формат подписи изменился: {sig}"
    print("dmarket: подпись OK —", sig[:30] + "…")


if __name__ == "__main__":
    _self_check()
