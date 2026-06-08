import asyncio
import json
import random
import time
from typing import Any, Literal

import bs4
import httpx

from . import logger
from .errors import RequestError
from .request_helpers import (
    RetryRequest,
    build_headers,
    build_url,
    captcha_headers,
    csrf_fetch_headers,
    decode_json_response,
    _debug_report,
    extract_c3vk,
    handle_http_status_error,
    handle_luogu_json_payload,
)
from .types import LuoguCookies, RequestParams


def _jittered_backoff(attempt: int) -> float:
    """指数退避 + jitter：1, 2, 4, 8, 10s 上限，每次加 0-0.5s 抖动。"""
    return min(2 ** attempt, 10) + random.uniform(0, 0.5)


class LuoguTransportBase:
    client: Any

    def _init_transport(
            self,
            base_url: str,
            cookies: LuoguCookies | None,
            max_retries: int,
    ) -> None:
        self.base_url = base_url
        self.cookies: dict[str, str] | None = None if cookies is None else cookies.to_json()
        self.max_retries = max_retries
        self.x_csrf_token: str | None = None

    def _build_url(self, endpoint: str) -> str:
        return build_url(self.base_url, endpoint)

    def _store_c3vk(self, c3vk: str) -> None:
        if self.cookies is None:
            self.cookies = {}
        self.cookies["C3VK"] = c3vk
        self.client.cookies.set("C3VK", c3vk)

    def _store_c3vk_from_html(self, html: str) -> bool:
        c3vk = extract_c3vk(html)
        if c3vk is None:
            return False
        self._store_c3vk(c3vk)
        logger.debug("C3VK token fetched successfully")
        return True

    def _csrf_token_or_store_c3vk(self, html: str) -> tuple[bool, str | None]:
        if self._store_c3vk_from_html(html):
            return True, None
        return False, self._extract_csrf_token(html)

    def _extract_csrf_token(self, html: str) -> str | None:
        soup = bs4.BeautifulSoup(html, "html.parser")
        csrf_meta = soup.select_one("meta[name='csrf-token']")
        csrf_token = csrf_meta.get("content") if csrf_meta else None
        return csrf_token if isinstance(csrf_token, str) else None

    def _log_request(
            self,
            method: str,
            url: str,
            params: dict[str, Any] | None,
            data: dict[str, Any] | None,
            attempt: int | None = None,
            async_: bool = False,
    ) -> None:
        prefix = ""
        if attempt is not None:
            prefix = f"({attempt}/{self.max_retries}) "
        if async_:
            prefix += "Async "

        if method == "GET":
            logger.info(f"{prefix}GET from {url} with params: {params}")
            return

        data_str = json.dumps(data)
        payload_str = data_str if data and len(data_str) < 50 else data_str[:50] + "..."
        logger.info(f"{prefix}POST to {url} with payload: {payload_str}")

    def _response_data(self, response: httpx.Response, endpoint: str) -> Any:
        response.raise_for_status()
        res_json = decode_json_response(response)
        logger.debug(f"{json.dumps(res_json)}")
        return handle_luogu_json_payload(res_json, endpoint)

    def _raw_response_data(
            self,
            response: httpx.Response,
            response_type: Literal["json", "bytes", "text", "response"],
            endpoint: str,
    ) -> Any:
        if response_type == "json":
            return self._response_data(response, endpoint)

        response.raise_for_status()
        if response_type == "bytes":
            return response.content
        if response_type == "text":
            return response.text
        return response

    def _response_data_or_retry(
            self,
            response: httpx.Response,
            endpoint: str,
            response_type: Literal["json", "bytes", "text", "response"],
    ) -> Any | RetryRequest:
        content_type = response.headers.get("content-type", "").lower()
        if response_type == "json" and "text/html" in content_type and self._store_c3vk_from_html(response.text):
            _debug_report(
                "B",
                "pyLuogu/transport.py:_response_data_or_retry",
                "[DEBUG] C3VK challenge detected, retrying request",
                {
                    "endpoint": endpoint,
                    "url": str(response.request.url),
                    "status": response.status_code,
                },
            )
            return RetryRequest(delay=0.2)
        try:
            return self._raw_response_data(response, response_type, endpoint)
        except httpx.HTTPStatusError as e:
            try:
                handle_http_status_error(response, e)
            except RetryRequest as retry:
                return retry
            raise
        except RetryRequest as retry:
            return retry


class SyncLuoguTransportMixin(LuoguTransportBase):
    client: httpx.Client

    def _send_request(
            self,
            endpoint: str,
            method: str = "GET",
            params: RequestParams | None = None,
            data: dict[str, Any] | None = None,
            form: dict[str, Any] | None = None,
            response_type: Literal["json", "bytes", "text", "response"] = "json",
    ) -> Any:
        url = self._build_url(endpoint)
        param_final = None if params is None else params.to_json()
        self._log_request(method, url, param_final, data)

        for attempt in range(self.max_retries):
            request = self.client.build_request(
                method,
                url,
                headers=self._get_headers(method, json_body=form is None),
                params=param_final,
                json=data,
                data=form,
            )
            try:
                response = self.client.send(request)
            except httpx.TimeoutException as e:
                logger.warning(f"Attempt {attempt + 1}: Timeout error - {e}")
                time.sleep(_jittered_backoff(attempt))
                continue
            except httpx.HTTPError as e:
                logger.error(f"Request error: {e}")
                raise RequestError("Request error") from e

            result = self._response_data_or_retry(response, endpoint, response_type)
            if isinstance(result, RetryRequest):
                self._handle_retry(result)
                continue
            return result

        logger.error(f"Failed to send request after {self.max_retries} attempts")
        raise RequestError(f"Failed to send request after {self.max_retries} attempts")

    def _get_headers(self, method: str, json_body: bool = True) -> dict[str, str]:
        if method != "GET" and not self.x_csrf_token:
            self._get_csrf()
        return build_headers(method, self.x_csrf_token, json_body=json_body)

    def _handle_retry(self, retry: RetryRequest) -> None:
        if retry.refresh_csrf:
            logger.warning("CSRF token expired, refreshing token...")
            self._get_csrf()
        if retry.delay:
            time.sleep(retry.delay)

    def _get_csrf(self, endpoint: str = "") -> str:
        headers = csrf_fetch_headers()

        for attempt in range(self.max_retries):
            try:
                response = self.client.get(
                    self._build_url(endpoint),
                    headers=headers,
                    cookies=self.cookies,
                )
                response.raise_for_status()

                has_c3vk, csrf_token = self._csrf_token_or_store_c3vk(response.text)
                if has_c3vk:
                    continue

                if csrf_token is not None:
                    self.x_csrf_token = csrf_token
                    logger.info("CSRF token fetched successfully")
                    return csrf_token

                logger.warning("CSRF token not found, retrying...")
                time.sleep(_jittered_backoff(attempt))
            except httpx.TimeoutException as e:
                logger.warning(f"Attempt {attempt + 1}: Timeout error - {e}")
                time.sleep(_jittered_backoff(attempt))
            except httpx.HTTPError as e:
                logger.error("HTTP error: {e}")
                raise RequestError("HTTP error") from e

        logger.error(f"Failed to fetch CSRF token after {self.max_retries} attempts")
        raise RequestError(f"Failed to fetch CSRF token after {self.max_retries} attempts")

    def _get_captcha(self) -> bytes:
        headers = captcha_headers(self.x_csrf_token)
        for attempt in range(self.max_retries):
            try:
                response = self.client.get(
                    self._build_url("/api/verify/captcha"),
                    headers=headers,
                    cookies=self.cookies,
                )
                response.raise_for_status()
                return response.content
            except httpx.TimeoutException as e:
                logger.warning(f"Attempt {attempt + 1}: Timeout error - {e}")
                time.sleep(_jittered_backoff(attempt))
            except httpx.HTTPError as e:
                logger.error(f"HTTP error: {e}")
                raise RequestError("HTTP error") from e

        raise RequestError(f"Failed to fetch captcha after {self.max_retries} attempts")


class AsyncLuoguTransportMixin(LuoguTransportBase):
    client: httpx.AsyncClient

    async def _send_request(
            self,
            endpoint: str,
            method: str = "GET",
            params: RequestParams | None = None,
            data: dict[str, Any] | None = None,
            form: dict[str, Any] | None = None,
            response_type: Literal["json", "bytes", "text", "response"] = "json",
    ) -> Any:
        url = self._build_url(endpoint)
        param_final = None if params is None else params.to_json()

        for attempt in range(self.max_retries):
            self._log_request(method, url, param_final, data, attempt=attempt, async_=True)
            request = self.client.build_request(
                method,
                url,
                headers=await self._get_headers(method, json_body=form is None),
                params=param_final,
                json=data,
                data=form,
            )
            try:
                response = await self.client.send(request)
            except httpx.TimeoutException as e:
                logger.warning(f"Attempt {attempt + 1}: Timeout error - {e}")
                await asyncio.sleep(_jittered_backoff(attempt))
                continue
            except httpx.HTTPError as e:
                logger.error(f"Request error: {e}")
                raise RequestError("Request error") from e

            result = self._response_data_or_retry(response, endpoint, response_type)
            if isinstance(result, RetryRequest):
                await self._handle_retry(result)
                continue
            return result

        logger.error(f"Failed to send request after {self.max_retries} attempts")
        raise RequestError(f"Failed to send request after {self.max_retries} attempts")

    async def _get_headers(self, method: str, json_body: bool = True) -> dict[str, str]:
        if method != "GET" and self.x_csrf_token is None:
            await self._get_csrf()
        return build_headers(method, self.x_csrf_token, json_body=json_body)

    async def _handle_retry(self, retry: RetryRequest) -> None:
        if retry.refresh_csrf:
            logger.warning("CSRF token expired, refreshing token...")
            await self._get_csrf()
        if retry.delay:
            await asyncio.sleep(retry.delay)

    async def _get_csrf(self, endpoint: str = "") -> str:
        headers = csrf_fetch_headers()

        for attempt in range(self.max_retries):
            try:
                logger.info(f"({attempt}/{self.max_retries}) Async GET CSRF token from {self._build_url(endpoint)}")
                response = await self.client.get(
                    self._build_url(endpoint),
                    headers=headers,
                    cookies=self.cookies,
                )
                response.raise_for_status()

                has_c3vk, csrf_token = self._csrf_token_or_store_c3vk(response.text)
                if has_c3vk:
                    continue

                if csrf_token is not None:
                    self.x_csrf_token = csrf_token
                    logger.info(f"new CSRF token : {csrf_token}")
                    return csrf_token

                logger.warning("CSRF token not found, retrying...")
                await asyncio.sleep(_jittered_backoff(attempt))
            except httpx.TimeoutException as e:
                logger.warning(f"Attempt {attempt + 1}: Timeout error - {e}")
                await asyncio.sleep(_jittered_backoff(attempt))
            except httpx.HTTPError as e:
                logger.error(f"HTTP error: {e}")
                raise RequestError(f"HTTP error: {e}") from e

        logger.error(f"Failed to fetch CSRF token after {self.max_retries} attempts")
        raise RequestError(f"Failed to fetch CSRF token after {self.max_retries} attempts")

    async def _get_captcha(self) -> bytes:
        headers = captcha_headers(self.x_csrf_token)
        for attempt in range(self.max_retries):
            try:
                logger.info(f"({attempt}/{self.max_retries}) Async GET captcha from {self._build_url('/api/verify/captcha')}")
                response = await self.client.get(
                    self._build_url("/api/verify/captcha"),
                    headers=headers,
                    cookies=self.cookies,
                )
                response.raise_for_status()
                return response.content
            except httpx.TimeoutException as e:
                logger.warning(f"Attempt {attempt + 1}: Timeout error - {e}")
                await asyncio.sleep(_jittered_backoff(attempt))
            except httpx.HTTPError as e:
                logger.error(f"HTTP error: {e}")
                raise RequestError("HTTP error") from e

        raise RequestError(f"Failed to fetch captcha after {self.max_retries} attempts")
