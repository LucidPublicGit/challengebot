"""Bound decoded HTTP response bytes, including compressed/chunked responses."""
import httpx

MAX_JSON_BYTES = 2 * 1024 * 1024


async def bounded_request(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    async with client.stream(method, url, **kwargs) as response:
        content = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
            if len(content) + len(chunk) > MAX_JSON_BYTES:
                raise ValueError("API response exceeds byte limit")
            content.extend(chunk)
        return httpx.Response(response.status_code, headers={
            key: value for key, value in response.headers.items()
            if key.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
        }, content=bytes(content), request=response.request)
