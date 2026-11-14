"""Exact, read-only raster planes with explicit series/frame/component identity.

No projection, intensity normalization, channel averaging or dtype promotion is
performed here. Frame indices flatten non-Y/X/C/S axes in C order. Components
flatten C then S, so RGB sample components remain explicitly distinguishable
from independent fluorescence channels.
"""
from __future__ import annotations

from io import BytesIO
import math
from pathlib import Path
import re
from xml.etree import ElementTree as ET

import numpy as np
from PIL import Image

MAX_IMAGE_PIXELS = 60_000_000
MAX_DECODE_BYTES = 512 * 1024 * 1024
MAX_SERIES = 128
MAX_COMPONENTS = 4096
PLANE_SCHEMA_VERSION = 1


def _integer(value, name):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)) or value < 0:
        raise ValueError(f'{name} must be a nonnegative integer.')
    return int(value)


def _ome_channels(xml):
    if not xml or len(xml) > 8 * 1024 * 1024 or re.search(r'<!\s*(DOCTYPE|ENTITY)', xml, re.I):
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    result = []
    for image in root:
        if image.tag.rsplit('}', 1)[-1] != 'Image':
            continue
        pixels = next((node for node in image if node.tag.rsplit('}', 1)[-1] == 'Pixels'), None)
        channels = []
        if pixels is not None:
            for channel in pixels:
                if channel.tag.rsplit('}', 1)[-1] != 'Channel':
                    continue
                color = None
                try:
                    # OME Color is a signed 32-bit RGBA value.
                    rgba = int(channel.attrib['Color']) & 0xFFFFFFFF
                    color = f'#{(rgba >> 8):06X}'
                except (KeyError, ValueError, TypeError):
                    pass
                channels.append({'name': str(channel.get('Name') or '')[:120], 'color': color})
        result.append(channels)
    return result


def _series_spec(series, index, ome_channels=None):
    axes, shape, dtype = str(series.axes), tuple(int(n) for n in series.shape), np.dtype(series.dtype)
    if len(axes) != len(shape) or axes.count('Y') != 1 or axes.count('X') != 1 or axes.count('C') > 1 or axes.count('S') > 1:
        raise ValueError('Image axes are ambiguous; explicit unique X/Y/C/S axes are required.')
    if dtype.kind not in 'buif':
        raise ValueError('Only real-valued integer, floating-point, or boolean image planes are supported.')
    if math.prod(shape) * dtype.itemsize > MAX_DECODE_BYTES:
        raise ValueError('TIFF decoded series data exceeds the 512 MB limit.')
    width, height = shape[axes.index('X')], shape[axes.index('Y')]
    if width * height > MAX_IMAGE_PIXELS:
        raise ValueError('Image pixel limit exceeded.')
    logical = shape[axes.index('C')] if 'C' in axes else 1
    samples = shape[axes.index('S')] if 'S' in axes else 1
    components = logical * samples
    if components > MAX_COMPONENTS:
        raise ValueError('Image contains more than 4096 scalar channel components.')
    frame_positions = [i for i, axis in enumerate(axes) if axis not in 'YXCS']
    frame_shape = [shape[i] for i in frame_positions]
    channel_info = []
    photometric = str(getattr(series.keyframe.photometric, 'name', '')).upper()
    true_rgb = photometric == 'RGB'
    source_bits = getattr(series.keyframe, 'bitspersample', dtype.itemsize * 8)
    source_bits = max(source_bits) if isinstance(source_bits, (tuple, list)) else source_bits
    bit_depth = int(source_bits) if isinstance(source_bits, (int, np.integer)) and 0 < source_bits <= dtype.itemsize * 8 else dtype.itemsize * 8
    for component in range(components):
        channel_index, sample_index = divmod(component, samples)
        original = (ome_channels or [])[channel_index] if channel_index < len(ome_channels or []) else {}
        name = original.get('name') or f'Channel {channel_index + 1}'
        role = 'signal'
        color = original.get('color')
        if true_rgb and samples >= 3:
            role = 'alpha' if sample_index == 3 else 'rgb_component'
            suffix = ['Red', 'Green', 'Blue', 'Alpha'][sample_index] if sample_index < 4 else f'Sample {sample_index + 1}'
            name = f'{name} / {suffix}' if logical > 1 else suffix
            color = ['#FF0000', '#00FF00', '#0000FF', '#FFFFFF'][sample_index] if sample_index < 4 else '#FFFFFF'
        elif samples > 1:
            name += f' / Sample {sample_index + 1}'
        channel_info.append({'channel': component, 'logical_channel': channel_index, 'sample': sample_index,
                             'name': name[:120], 'color': color, 'role': role})
    return {'index': index, 'name': str(getattr(series, 'name', '') or f'Series {index + 1}')[:120],
            'axes': axes, 'shape': list(shape), 'width_px': width, 'height_px': height,
            'channels': components, 'logical_channels': logical, 'samples': samples,
            'frames': math.prod(frame_shape) if frame_shape else 1,
            'frame_axes': ''.join(axes[i] for i in frame_positions), 'frame_shape': frame_shape,
            'dtype': str(dtype), 'bit_depth': bit_depth,
            'channel_info': channel_info, 'rgb_samples': true_rgb}


def _png_depth(path):
    if path.suffix.lower() == '.png':
        with path.open('rb') as source:
            header = source.read(29)
        if header[:8] == b'\x89PNG\r\n\x1a\n' and len(header) >= 26:
            return int(header[24])
    return None


def _pil_info(path):
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        width, height = image.size
        if width * height > MAX_IMAGE_PIXELS:
            raise ValueError('Image pixel limit exceeded.')
        original_mode = image.mode
        mode = original_mode
        if mode == 'P':
            mode = 'RGBA' if 'transparency' in image.info else 'RGB'
        elif mode in {'CMYK', 'YCbCr'}:
            mode = 'RGB'
        elif mode == 'LA':
            mode = 'RGBA'
        channels = {'RGB': 3, 'RGBA': 4}.get(mode, 1)
        depth = _png_depth(path)
        if depth == 16:
            dtype = np.dtype('uint16')
        elif mode == 'I':
            dtype = np.dtype('int32')
        elif mode == 'F':
            dtype = np.dtype('float32')
        elif mode == '1':
            dtype = np.dtype('bool')
        else:
            dtype = np.dtype('uint8')
        if width * height * channels * dtype.itemsize > MAX_DECODE_BYTES:
            raise ValueError('Decoded image exceeds the 512 MB limit.')
        rgb = mode in {'RGB', 'RGBA'}
        info = []
        for channel in range(channels):
            role = ('alpha' if channel == 3 else 'rgb_component') if rgb else 'signal'
            name = ['Red', 'Green', 'Blue', 'Alpha'][channel] if rgb else 'Channel 1'
            color = ['#FF0000', '#00FF00', '#0000FF', '#FFFFFF'][channel] if rgb else None
            info.append({'channel': channel, 'logical_channel': 0, 'sample': channel,
                         'name': name, 'color': color, 'role': role})
        frames = int(getattr(image, 'n_frames', 1))
        spec = {'index': 0, 'name': 'Image', 'axes': ('FYXS' if rgb else 'FYX') if frames > 1 else ('YXS' if rgb else 'YX'),
                'shape': ([frames] if frames > 1 else []) + [height, width] + ([channels] if rgb else []),
                'width_px': width, 'height_px': height, 'channels': channels, 'logical_channels': 1,
                'samples': channels, 'frames': frames, 'frame_axes': 'F' if frames > 1 else '',
                'frame_shape': [frames] if frames > 1 else [], 'dtype': str(dtype),
                'bit_depth': 1 if dtype.kind == 'b' else dtype.itemsize * 8, 'channel_info': info, 'rgb_samples': rgb}
        return spec, {'format': image.format, 'mode': original_mode,
                      'original_numeric_data': image.format != 'JPEG' and original_mode not in {'P', 'CMYK', 'YCbCr', 'LA'},
                      'decoded_color_conversion': original_mode != mode}


def describe_raster(path: str | Path) -> dict:
    """Inspect dimensions/axis identities without decoding complete TIFF arrays."""
    path = Path(path)
    if path.suffix.lower() in {'.tif', '.tiff'}:
        import tifffile
        with tifffile.TiffFile(path) as tif:
            if not tif.series or len(tif.series) > MAX_SERIES:
                raise ValueError('TIFF must contain between 1 and 128 image series.')
            names = _ome_channels(tif.ome_metadata)
            series = [_series_spec(item, i, names[i] if i < len(names) else None) for i, item in enumerate(tif.series)]
        extra = {'format': 'TIFF', 'original_numeric_data': True}
    else:
        first, extra = _pil_info(path)
        series = [first]
    first = series[0]
    return {'width_px': first['width_px'], 'height_px': first['height_px'], 'channels': first['channels'],
            'frames': first['frames'], 'bit_depth': first['bit_depth'], 'dtype': first['dtype'],
            'metadata': {**extra, 'axes': first['axes'], 'shape': first['shape'],
                         'plane_schema_version': PLANE_SCHEMA_VERSION, 'series_count': len(series), 'series': series}}


def frame_coordinates(spec: dict, frame: int) -> list[dict]:
    frame = _integer(frame, 'Frame')
    if frame >= spec['frames']:
        raise ValueError('Image frame is outside its source range.')
    if not spec['frame_shape']:
        return []
    indices = np.unravel_index(frame, tuple(spec['frame_shape']))
    return [{'axis': axis, 'position': position, 'index': int(index), 'size': int(size)}
            for position, (axis, size, index) in enumerate(zip(spec['frame_axes'], spec['frame_shape'], indices))]


def _coordinates(spec, frame, component):
    frame_coordinates(spec, frame)
    component = _integer(component, 'Channel')
    if component >= spec['channels']:
        raise ValueError('Image channel is outside its source range.')
    indices = iter(np.unravel_index(frame, tuple(spec['frame_shape'])) if spec['frame_shape'] else [])
    logical, sample = divmod(component, spec['samples'])
    return [slice(None) if axis in 'YX' else logical if axis == 'C' else sample if axis == 'S' else int(next(indices))
            for axis in spec['axes']]


def _readonly_plane(array):
    result = np.ascontiguousarray(array)
    if result.ndim != 2:
        raise ValueError('The selected image identity does not resolve to a scalar Y/X plane.')
    result.setflags(write=False)
    return result


def _tiff_plane(path, series, spec, frame, channel, mapped, page_cache):
    coordinates = _coordinates(spec, frame, channel)
    if mapped is not None:
        selected = mapped[tuple(coordinates)]
    else:
        page = series.keyframe
        page_axes = str(page.axes)
        suffix_count = len(page.shape)
        prefix_count = len(spec['shape']) - suffix_count
        can_select_page = (prefix_count >= 0 and spec['axes'].endswith(page_axes) and
                           tuple(spec['shape'][-suffix_count:]) == tuple(page.shape) and
                           all(isinstance(index, int) for index in coordinates[:prefix_count]))
        try:
            if can_select_page:
                page_index = int(np.ravel_multi_index(tuple(coordinates[:prefix_count]), tuple(spec['shape'][:prefix_count]))) if prefix_count else 0
                if page_index not in page_cache:
                    page_cache[page_index] = series.pages[page_index].asarray()
                selected = page_cache[page_index][tuple(coordinates[prefix_count:])]
            else:
                # Uncommon tiled/planar storage can differ from logical axes.
                # Its complete decode remains bounded by describe_raster's cap.
                if 'whole' not in page_cache:
                    page_cache['whole'] = series.asarray()
                selected = page_cache['whole'][tuple(coordinates)]
        except (ValueError, RuntimeError, OSError, IndexError) as exc:
            raise ValueError('TIFF plane decoding failed; compressed TIFF requires imagecodecs. ' + str(exc)[:160]) from exc
    remaining = [axis for axis in spec['axes'] if axis in 'YX']
    if remaining == ['X', 'Y']:
        selected = selected.T
    return _readonly_plane(selected)


def _pil_frame(path, frame):
    with Image.open(path) as image:
        if image.width * image.height > MAX_IMAGE_PIXELS:
            raise ValueError('Image pixel limit exceeded.')
        if not 0 <= frame < getattr(image, 'n_frames', 1):
            raise ValueError('Image frame is outside its source range.')
        image.seek(frame)
        if _png_depth(path) == 16 and image.mode in {'RGB', 'RGBA'}:
            if frame != 0:
                raise ValueError('16-bit animated PNG frames cannot be decoded without losing source precision.')
            import imagecodecs
            array = imagecodecs.png_decode(path.read_bytes())
        else:
            if image.mode == 'P':
                image = image.convert('RGBA' if 'transparency' in image.info else 'RGB')
            elif image.mode in {'CMYK', 'YCbCr'}:
                image = image.convert('RGB')
            elif image.mode == 'LA':
                image = image.convert('RGBA')
            array = np.array(image)
            if _png_depth(path) == 16 and array.ndim == 2:
                array = array.astype(np.uint16, copy=False)
    array = np.ascontiguousarray(array)
    array.setflags(write=False)
    return array


def read_raster(path: str | Path, *, series: int = 0, frame: int = 0, channel: int | None = None) -> np.ndarray:
    """Read owned YX or YXC data; channel=None explicitly retains components.

    Returned arrays never keep the source file mapped. A scalar TIFF plane can
    be contiguous inside a much larger mapped series, so ascontiguousarray alone
    is not enough to detach it. Only that selected plane is copied when needed.
    """
    path = Path(path)
    series_index, frame = _integer(series, 'Series'), _integer(frame, 'Frame')
    if channel is not None:
        channel = _integer(channel, 'Channel')
    if path.suffix.lower() not in {'.tif', '.tiff'}:
        if series_index != 0:
            raise ValueError('This raster format contains only one image series.')
        array = _pil_frame(path, frame)
        channels = array.shape[-1] if array.ndim == 3 else 1
        if channel is not None:
            if channel >= channels:
                raise ValueError('Image channel is outside its source range.')
            return _readonly_plane(array[..., channel] if array.ndim == 3 else array)
        return array
    import tifffile
    with tifffile.TiffFile(path) as tif:
        if series_index >= len(tif.series):
            raise ValueError('TIFF series is outside its source range.')
        selected_series = tif.series[series_index]
        spec = _series_spec(selected_series, series_index)
        frame_coordinates(spec, frame)
        try:
            mapped = tifffile.memmap(path, series=series_index, mode='r')
        except (ValueError, OSError):
            mapped = None
        try:
            page_cache = {}
            if channel is not None or spec['channels'] == 1:
                array = _tiff_plane(path, selected_series, spec, frame, channel or 0, mapped, page_cache)
                if mapped is not None and np.shares_memory(array, mapped):
                    array = array.copy(order='C')
            else:
                planes = [_tiff_plane(path, selected_series, spec, frame, index, mapped, page_cache)
                          for index in range(spec['channels'])]
                array = np.stack(planes, axis=-1)
            array.setflags(write=False)
            return array
        finally:
            if mapped is not None:
                mapped._mmap.close()
