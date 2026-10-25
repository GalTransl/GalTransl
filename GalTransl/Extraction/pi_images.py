"""Pi image sniffing, BMP conversion and bounded inline resizing using Pillow."""
from __future__ import annotations

import base64
import io
import math
import warnings

from PIL import Image, ImageOps

from GalTransl.Agent.core import AgentStopRequested

INLINE_BYTES = int(4.5 * 1024 * 1024)  # base64 bytes, as in Pi 1.4.0


def sniff_image(data):
    if data.startswith(b'\xff\xd8\xff') and data[3:4] != b'\xf7':
        return 'image/jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n') and data[8:16] == b'\0\0\0\rIHDR':
        offset = 8
        while offset + 8 <= len(data):
            size = int.from_bytes(data[offset:offset + 4], 'big')
            kind = data[offset + 4:offset + 8]
            if kind == b'acTL':
                return None
            if kind == b'IDAT':
                break
            offset += size + 12
        return 'image/png'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if data[:2] == b'BM' and len(data) >= 26:
        size = int.from_bytes(data[2:6], 'little')
        offset = int.from_bytes(data[10:14], 'little')
        dib = int.from_bytes(data[14:18], 'little')
        pos = 22 if dib == 12 else 26 if 40 <= dib <= 124 and len(data) >= 30 else None
        if pos is not None and (not size or size >= 26) and offset >= 14 + dib and (not size or offset < size):
            if int.from_bytes(data[pos:pos + 2], 'little') == 1 and int.from_bytes(data[pos + 2:pos + 4], 'little') in (1, 4, 8, 16, 24, 32):
                return 'image/bmp'
    return None


def read_image_result(path, stop):
    def check_stop():
        if stop.is_set():
            raise AgentStopRequested()
    with path.open('rb') as stream:
        mime = sniff_image(stream.read(4100))
    if not mime:
        return None
    check_stop()
    hints = []
    try:
        data = path.read_bytes()
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                image = ImageOps.exif_transpose(source)
                image.load()
        check_stop()
        original_width, original_height = image.size
        if mime == 'image/bmp':
            buffer = io.BytesIO()
            image.save(buffer, 'PNG')
            data, mime = buffer.getvalue(), 'image/png'
            hints.append('[Image converted from image/bmp to image/png.]')
        width, height = image.size
        resized = width > 2000 or height > 2000 or math.ceil(len(data) / 3) * 4 >= INLINE_BYTES
        if resized:
            if width > 2000:
                height, width = max(1, math.floor(height * 2000 / width + .5)), 2000
            if height > 2000:
                width, height = max(1, math.floor(width * 2000 / height + .5)), 2000
            while True:
                check_stop()
                scaled = image.resize((width, height), Image.Resampling.LANCZOS)
                candidates = [('PNG', None), *[('JPEG', q) for q in (80, 85, 70, 55, 40)]]
                chosen = None
                for fmt, quality in candidates:
                    buffer = io.BytesIO()
                    picture = scaled if fmt == 'PNG' else scaled.convert('RGB')
                    picture.save(buffer, fmt, **({'quality': quality} if quality else {}))
                    candidate = buffer.getvalue()
                    if math.ceil(len(candidate) / 3) * 4 < INLINE_BYTES:
                        chosen = candidate, 'image/png' if fmt == 'PNG' else 'image/jpeg'
                        break
                if chosen:
                    data, mime = chosen
                    break
                if width == height == 1:
                    raise ValueError('image cannot fit inline')
                width, height = max(1, math.floor(width * .75)), max(1, math.floor(height * .75))
            hints.append(f'[Image: original {original_width}x{original_height}, displayed at {width}x{height}. Multiply coordinates by {original_width / width:.2f} to map to original image.]')
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        text = f'Read image file [{mime}]\n[Image omitted: could not be resized below the inline image size limit.]'
        return {'content': [{'type': 'text', 'text': text}], 'structuredContent': text}
    check_stop()
    note = f'Read image file [{mime}]' + (('\n' + '\n'.join(hints)) if hints else '')
    encoded = base64.b64encode(data).decode('ascii')
    return {'content': [{'type': 'text', 'text': note}, {'type': 'image', 'data': encoded, 'mimeType': mime}],
            'structuredContent': {'type': 'image', 'data': encoded, 'mimeType': mime, 'note': note}}
