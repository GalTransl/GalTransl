"""Session-owned image assets. Journals contain references, never base64 blobs."""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import struct
import tempfile
from pathlib import Path

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_MESSAGE_IMAGE_BYTES = 6 * 1024 * 1024
IMAGE_ID = re.compile(r'^[0-9a-f]{64}\.(png|jpg|gif|webp)$')
MAX_TOOL_IMAGE_BYTES = int(4.5 * 1024 * 1024) * 3 // 4


def image_info(data: bytes, *, tool=False):
    if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 33 and data[12:16] == b'IHDR':
        width, height = struct.unpack('>II', data[16:24])
        mime, suffix = 'image/png', 'png'
        if not tool and not data.endswith(b'IEND\xaeB`\x82'):
            raise ValueError('Incomplete PNG image')
    elif data.startswith(b'\xff\xd8') and (tool or data.endswith(b'\xff\xd9')):
        width = height = 0
        offset = 2
        while offset + 4 <= len(data):
            if data[offset] != 255:
                break
            marker = data[offset + 1]
            offset += 2
            if marker == 255:
                offset -= 1
                continue
            if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
                continue
            length = int.from_bytes(data[offset:offset + 2], 'big')
            if length < 2 or offset + length > len(data):
                break
            if marker in {0xC0, 0xC1, 0xC2} and length >= 8:
                height, width = struct.unpack('>HH', data[offset + 3:offset + 7])
                break
            offset += length
        mime, suffix = 'image/jpeg', 'jpg'
    elif data[:6] in (b'GIF87a', b'GIF89a') or data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        import io
        from PIL import Image
        with Image.open(io.BytesIO(data)) as picture:
            picture.load()
            width, height = picture.size
            mime, suffix = ('image/gif', 'gif') if picture.format == 'GIF' else ('image/webp', 'webp')
    else:
        raise ValueError('Only PNG and JPEG image data are supported')
    if not 0 < width <= 8192 or not 0 < height <= 8192 or width * height > 16_777_216:
        raise ValueError('Invalid or oversized image dimensions')
    return mime, suffix, width, height


def asset_directory(store) -> Path:
    return Path(store.path + '.images')


def save_images(store, uploads) -> list[dict]:
    if uploads is None:
        return []
    if not isinstance(uploads, list) or len(uploads) > MAX_IMAGES:
        raise ValueError('A message may contain at most 4 images')
    decoded, total = [], 0
    for upload in uploads:
        if not isinstance(upload, dict) or not isinstance(upload.get('data_url'), str):
            raise ValueError('Invalid image attachment')
        url = upload['data_url']
        if len(url) > MAX_IMAGE_BYTES * 4 // 3 + 128:
            raise ValueError('Each image must be at most 2 MiB')
        header, separator, encoded = url.partition(',')
        if not separator or header not in {'data:image/png;base64', 'data:image/jpeg;base64'}:
            raise ValueError('Invalid image data URL')
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError('Invalid image base64') from exc
        total += len(data)
        if len(data) > MAX_IMAGE_BYTES or total > MAX_MESSAGE_IMAGE_BYTES:
            raise ValueError('Image attachments exceed the size budget')
        mime, suffix, width, height = image_info(data)
        if header != f'data:{mime};base64':
            raise ValueError('Image content does not match its MIME type')
        name = str(upload.get('name') or 'image').replace('\\', '/').split('/')[-1][:200]
        meta = dict(id=f'{hashlib.sha256(data).hexdigest()}.{suffix}', name=name, mime_type=mime,
                    size=len(data), width=width, height=height)
        decoded.append((meta, data))
    if not decoded:
        return []
    directory = asset_directory(store)
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or getattr(directory.lstat(), 'st_file_attributes', 0) & 0x400:
        raise ValueError('Image asset directory cannot be a link')
    for meta, data in decoded:
        target = directory / meta['id']
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, prefix='.image-', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
            os.replace(temporary, target)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    return [meta for meta, _ in decoded]


def read_image(store, image_id: str):
    if not IMAGE_ID.fullmatch(image_id):
        raise ValueError('Invalid image id')
    directory = asset_directory(store)
    path = directory / image_id
    if directory.is_symlink() or path.is_symlink() or any(
        getattr(item.lstat(), 'st_file_attributes', 0) & 0x400 for item in (directory, path)
    ):
        raise ValueError('Image assets cannot be links')
    with path.open('rb') as stream:
        data = stream.read(MAX_TOOL_IMAGE_BYTES + 1)
    if len(data) > MAX_TOOL_IMAGE_BYTES or hashlib.sha256(data).hexdigest() != image_id.split('.')[0]:
        raise ValueError('Invalid image asset')
    mime, suffix, *_ = image_info(data, tool=True)
    if not image_id.endswith('.' + suffix):
        raise ValueError('Invalid image asset type')
    return data, mime


def save_tool_image(store, block, name):
    """Persist a processed tool image; only asset metadata enters journals/events."""
    data = base64.b64decode(block['data'], validate=True)
    mime, suffix, width, height = image_info(data, tool=True)
    if mime != block['mimeType'] or len(data) > MAX_TOOL_IMAGE_BYTES:
        raise ValueError('Invalid processed tool image')
    meta = dict(id=f'{hashlib.sha256(data).hexdigest()}.{suffix}', name=Path(name).name,
                mime_type=mime, size=len(data), width=width, height=height)
    directory = asset_directory(store)
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or getattr(directory.lstat(), 'st_file_attributes', 0) & 0x400:
        raise ValueError('Image asset directory cannot be a link')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix='.image-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        os.replace(temporary, directory / meta['id'])
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return meta


def image_data_url(store, image):
    data, mime = read_image(store, image['id'])
    return f'data:{mime};base64,' + base64.b64encode(data).decode('ascii')


def user_message(text, images=()):
    return {'role': 'user', 'content': text, **({'_images': images} if images else {})}


def user_event(text, images=()):
    return {'message': text, **({'images': images} if images else {})}


def expand_message(message, store):
    images = message.get('_images')
    if not images:
        return message
    if store is None:
        raise ValueError('Image message has no session store')
    content = [{'type': 'text', 'text': message['content']}] if message.get('content') else []
    content += [{'type': 'image_url', 'image_url': {'url': image_data_url(store, image)}} for image in images]
    return {**message, 'content': content}
