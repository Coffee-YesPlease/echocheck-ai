"""Request guards shared by every JSON endpoint and the ASGI stack."""
from __future__ import annotations
import json

from pydantic import BaseModel, ConfigDict, model_validator

TEXT_ERROR = 'Text contains characters that cannot be stored. Remove them and try again.'


def storable(value):
    """True when every string inside value encodes as UTF-8 (JSON allows lone surrogates; SQLite does not)."""
    if isinstance(value, str):
        try:
            value.encode('utf-8')
        except UnicodeEncodeError:
            return False
        return True
    if isinstance(value, dict):
        return all(storable(key) and storable(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return all(storable(item) for item in value)
    return True


class StrictBody(BaseModel):
    model_config = ConfigDict(extra='forbid')

    @model_validator(mode='before')
    @classmethod
    def text_must_be_storable(cls, data):
        if not storable(data):
            raise ValueError(TEXT_ERROR)
        return data


class BodyLimit:
    """Reject request bodies above a per-path byte limit before the application buffers them."""

    def __init__(self, app, default_limit, path_limits=()):
        self.app = app
        self.default_limit = default_limit
        self.path_limits = tuple(path_limits)  # (predicate(path), limit)

    def limit_for(self, path):
        for matches, limit in self.path_limits:
            if matches(path):
                return limit
        return self.default_limit

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        limit = self.limit_for(scope.get('path', ''))
        for name, value in scope.get('headers', ()):
            if name == b'content-length':
                try:
                    declared = int(value)
                except ValueError:
                    return await self.reject(send, 400, 'Request size is invalid.')
                if declared > limit:
                    return await self.reject(send, 413, 'This request is too large.')
        received = 0
        responded = False

        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message['type'] == 'http.request':
                received += len(message.get('body', b''))
                if received > limit:
                    raise RequestTooLarge()
            return message

        async def tracking_send(message):
            nonlocal responded
            if message['type'] == 'http.response.start':
                responded = True
            await send(message)

        try:
            await self.app(scope, bounded_receive, tracking_send)
        except RequestTooLarge:
            if not responded:
                await self.reject(send, 413, 'This request is too large.')

    @staticmethod
    async def reject(send, status, detail):
        body = json.dumps({'detail': detail}).encode('utf-8')
        await send({'type': 'http.response.start', 'status': status,
                    'headers': [(b'content-type', b'application/json'), (b'content-length', str(len(body)).encode()),
                                (b'cache-control', b'no-store'), (b'x-content-type-options', b'nosniff')]})
        await send({'type': 'http.response.body', 'body': body})


class RequestTooLarge(Exception):
    pass
