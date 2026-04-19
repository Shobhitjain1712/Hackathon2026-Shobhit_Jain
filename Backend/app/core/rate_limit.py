from collections import defaultdict, deque
from threading import Lock
from time import time

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class InMemoryRateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_requests_per_minute: int = 80) -> None:
        super().__init__(app)
        self.max_requests = max_requests_per_minute
        self.window_seconds = 60
        self.events: dict[str, deque[float]] = defaultdict(deque)
        self.lock = Lock()

    async def dispatch(self, request: Request, call_next):
        if request.url.path not in {"/upload", "/process"}:
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        current_time = time()

        with self.lock:
            queue = self.events[client_ip]
            while queue and current_time - queue[0] > self.window_seconds:
                queue.popleft()

            if len(queue) >= self.max_requests:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded. Try again in a minute."},
                )

            queue.append(current_time)

        return await call_next(request)
