import asyncio
from asgiref.sync import iscoroutinefunction, sync_to_async
from django.http import HttpResponse
from rest_framework.renderers import BaseRenderer
import orjson


class OrjsonRenderer(BaseRenderer):
    media_type = "application/json"
    format = "json"
    charset = None  # orjson outputs raw UTF-8 bytes directly

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if data is None:
            return b""
        return orjson.dumps(
            data,
            default=str,
            option=orjson.OPT_SERIALIZE_NUMPY | orjson.OPT_UTC_Z,
        )


class AdaptiveBackpressureMiddleware:
    """
    Caps concurrent in-flight execution per worker to prevent DB lock thrashing.
    Excess burst requests wait up to 40ms; if still saturated, return HTTP 429
    so the React PWA retries cleanly after 250ms.
    """

    sync_capable = True
    async_capable = True

    MAX_CONCURRENT_PER_WORKER = 64
    ACQUIRE_TIMEOUT_SEC = 0.040

    def __init__(self, get_response):
        self.get_response = get_response
        self._is_coroutine = iscoroutinefunction(get_response)
        self._semaphores = {}

    def _get_semaphore(self):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop not in self._semaphores:
            self._semaphores[loop] = asyncio.Semaphore(self.MAX_CONCURRENT_PER_WORKER)
        return self._semaphores[loop]

    def __call__(self, request):
        # Sync dispatch (WSGI or test runner)
        return self.get_response(request)

    async def __acall__(self, request):
        # Pure async dispatch (Granian ASGI Hyper engine)
        semaphore = self._get_semaphore()
        try:
            await asyncio.wait_for(
                semaphore.acquire(),
                timeout=self.ACQUIRE_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            return HttpResponse(
                b'{"error":"server_busy","retry_ms":250}',
                status=429,
                content_type="application/json",
                headers={"Retry-After": "0.25"},
            )
        try:
            if self._is_coroutine:
                return await self.get_response(request)
            return await sync_to_async(self.get_response, thread_sensitive=True)(request)
        finally:
            semaphore.release()
