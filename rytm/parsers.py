"""Bound JSON before decoding; Django's form-body limit does not bound DRF JSON."""
from io import BytesIO
from rest_framework.exceptions import ParseError
from rest_framework.parsers import JSONParser


class BoundedJSONParser(JSONParser):
    max_bytes = 1024 * 1024

    def parse(self, stream, media_type=None, parser_context=None):
        data = stream.read(self.max_bytes + 1)
        if len(data) > self.max_bytes:
            raise ParseError('JSON payload must be at most 1 MiB.')
        return super().parse(BytesIO(data), media_type, parser_context)
