"""Multichannel display recipes over immutable scalar source planes.

Display-only LUTs never enter a quantitative image array. Independently stored
channels keep their own dtypes and identities instead of being promoted into a
new synthetic raw stack. The same layout and rendering functions serve preview,
figure export and preflight.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
import math
from pathlib import Path
import tempfile
from typing import Callable

import numpy as np
from PIL import Image

from .models import ChannelSource, ChannelView
from .planes import describe_raster, frame_coordinates, read_raster

PALETTE = ('#FF0000', '#00FF00', '#0000FF', '#00FFFF', '#FF00FF', '#FFFF00', '#FFFFFF', '#FF8000')
MAX_DISPLAY_CHANNELS = 32
MAX_RENDER_PIXELS = 100_000_000
MAX_RANGE_SAMPLES = 262_144
MAX_LUT_PIXELS = 262_144
MEMORY_TILE_PIXELS = 8_000_000


def normalize_view(view: dict) -> dict:
    """Return canonical typed settings, retaining intensity units explicitly."""
    normalized = ChannelView.model_validate(view).model_dump()
    for index, channel in enumerate(normalized['channels']):
        channel['name'] = channel['name'] or f'Channel {index + 1}'
        channel['color'] = channel['color'].upper()
    return normalized


def _asset(provider, asset_id):
    if hasattr(provider, 'public_asset'):
        return provider.public_asset(asset_id)
    record = provider.get(asset_id) if isinstance(provider, dict) else None
    if record is None:
        raise ValueError('Channel view references a missing original asset.')
    return record


def source_spec(provider, source: dict) -> tuple[dict, dict]:
    identity = ChannelSource.model_validate(source).model_dump()
    asset = _asset(provider, identity['asset_id'])
    if asset.get('kind') != 'image':
        raise ValueError('Channel sources must be original raster images, not rendered PDF/SVG/table assets.')
    series_list = asset.get('metadata', {}).get('series')
    if series_list:
        if identity['series'] >= len(series_list):
            raise ValueError('Channel source series is outside its original range.')
        spec = series_list[identity['series']]
    else:
        if identity['series'] != 0:
            raise ValueError('Refresh this source metadata before choosing another series.')
        spec = {'index': 0, 'width_px': asset['width_px'], 'height_px': asset['height_px'],
                'channels': asset.get('channels', 1), 'frames': asset.get('frames', 1),
                'frame_axes': '', 'frame_shape': [], 'dtype': asset['dtype'], 'bit_depth': asset['bit_depth'],
                'channel_info': [], 'rgb_samples': False}
    if identity['frame'] >= spec['frames'] or identity['channel'] >= spec['channels']:
        raise ValueError('Channel source frame or component is outside its original range.')
    return asset, spec


def validate_view(view: dict, registry_or_store) -> dict:
    normalized = normalize_view(view)
    dimensions = None
    for channel in normalized['channels']:
        _, spec = source_spec(registry_or_store, channel['source'])
        shape = (int(spec['width_px']), int(spec['height_px']))
        if dimensions is None:
            dimensions = shape
        elif dimensions != shape:
            raise ValueError('All channel planes must have identical original dimensions; automatic resampling/alignment is not performed.')
    return {'view': normalized, 'width_px': dimensions[0], 'height_px': dimensions[1],
            'source_ids': sorted({channel['source']['asset_id'] for channel in normalized['channels']})}


def plane_statistics(plane: np.ndarray, percentiles=None) -> dict:
    if plane.ndim != 2 or plane.dtype.kind not in 'buif':
        raise ValueError('Channel statistics require a scalar real-valued source plane.')
    if plane.dtype.kind in 'bui':
        minimum, maximum = int(plane.min()), int(plane.max())
        finite_count = plane.size
    else:
        finite = np.isfinite(plane)
        finite_count = int(np.count_nonzero(finite))
        minimum = float(np.min(plane, where=finite, initial=np.inf)) if finite_count else None
        maximum = float(np.max(plane, where=finite, initial=-np.inf)) if finite_count else None
    stats = {'minimum': minimum, 'maximum': maximum, 'finite_count': int(finite_count),
             'nonfinite_count': int(plane.size - finite_count), 'dtype': str(plane.dtype),
             'bit_depth': plane.dtype.itemsize * 8}
    if percentiles is not None:
        if not isinstance(percentiles, (list, tuple)) or len(percentiles) != 2:
            raise ValueError('Supply exactly two ordered percentiles.')
        lower, upper = (float(value) for value in percentiles)
        if not math.isfinite(lower) or not math.isfinite(upper) or not 0 <= lower < upper <= 100:
            raise ValueError('Percentiles must satisfy 0 <= lower < upper <= 100.')
        step = max(1, math.ceil(plane.size / MAX_RANGE_SAMPLES))
        samples = plane.reshape(-1)[::step]
        samples = samples[np.isfinite(samples)]
        values = np.percentile(samples.astype(np.float64), [lower, upper]).tolist() if samples.size else [None, None]
        stats.update(percentiles={'requested': [lower, upper], 'values': values},
                     sampled=step > 1, sample_count=int(samples.size), sampling='deterministic_flat_stride',
                     sample_stride=step)
    return stats


def _limits(plane, channel, *, stats=None, component=None):
    stats = stats or plane_statistics(plane)
    if stats['finite_count'] == 0:
        raise ValueError('Selected channel contains no finite source intensities.')
    black, white = channel.get('black'), channel.get('white')
    if component and component.get('role') in {'rgb_component', 'alpha'} and plane.dtype.kind in 'bui':
        lower, upper = (0, 1) if plane.dtype.kind == 'b' else (int(np.iinfo(plane.dtype).min), int(np.iinfo(plane.dtype).max))
    else:
        lower, upper = stats['minimum'], stats['maximum']
    lower, upper = (float(lower) if black is None else float(black)), (float(upper) if white is None else float(white))
    if not math.isfinite(lower) or not math.isfinite(upper):
        raise ValueError('Channel display bounds must be finite.')
    if upper <= lower:
        if black is not None or white is not None:
            raise ValueError('Channel white must exceed black; automatic source bound would make this range empty.')
        upper = max(lower + 1, float(np.nextafter(lower, np.inf)))
    return lower, upper


def describe_channels(store, asset_id, series=0, frame=0, percentiles=None, channel_indices=None):
    asset = store.public_asset(asset_id)
    if asset.get('kind') != 'image':
        raise ValueError('Select a raster image to inspect its original channels.')
    metadata = asset.get('metadata', {})
    series_list = metadata.get('series')
    if not series_list:
        metadata = describe_raster(store.resolve(asset_id))['metadata']
        series_list = metadata['series']
    identity = ChannelSource(asset_id=asset_id, series=series, frame=frame, channel=0).model_dump()
    if series >= len(series_list):
        raise ValueError('Image series is outside its source range.')
    spec = series_list[series]
    coordinates = frame_coordinates(spec, frame)
    total = spec['channels']
    selected = list(range(min(total, MAX_DISPLAY_CHANNELS))) if channel_indices is None else list(channel_indices)
    if not selected or len(selected) > MAX_DISPLAY_CHANNELS or len(set(selected)) != len(selected):
        raise ValueError('Choose between 1 and 32 distinct channel component indices.')
    result, config, warnings = [], [], []
    for index in selected:
        source = {**identity, 'channel': index}
        source = ChannelSource.model_validate(source).model_dump()
        if index >= total:
            raise ValueError('Channel component is outside its original range.')
        plane = store.read_plane(source)
        detail = spec['channel_info'][index] if index < len(spec.get('channel_info', [])) else {'name': f'Channel {index + 1}', 'role': 'signal'}
        stats = plane_statistics(plane, percentiles)
        channel = {'id': f'c{index}', 'source': source, 'name': detail['name'],
                   'color': detail.get('color') or ('#FFFFFF' if total == 1 else PALETTE[index % len(PALETTE)]), 'black': None, 'white': None,
                   'gamma': 1, 'visible': detail.get('role') != 'alpha'}
        # RGB components start with their actual native range, so enabling a
        # channel recipe does not auto-stretch an ordinary color photograph.
        if detail.get('role') in {'rgb_component', 'alpha'} and plane.dtype.kind in 'bui':
            channel['black'], channel['white'] = (0, 1) if plane.dtype.kind == 'b' else (int(np.iinfo(plane.dtype).min), int(np.iinfo(plane.dtype).max))
        config.append(channel)
        result.append({**detail, **stats, 'bit_depth': spec['bit_depth'], 'id': channel['id'], 'source': source,
                       'color': channel['color']})
    if total > len(selected):
        warnings.append(f'此系列共有 {total} 个标量分量；本次返回 {len(selected)} 个。每个显示配方最多 32 个通道，可明确选择其他索引。')
    if spec.get('rgb_samples'):
        warnings.append('RGB/Alpha 分量是原文件颜色/覆盖分量，不能据此推定独立荧光染料或独立生物学测量。')
    if not asset.get('metadata', {}).get('original_numeric_data', True):
        warnings.append('此文件是有损图像或经颜色解码的图像，不能替代仪器原始强度数据。')
    default_view = normalize_view({'channels': config, 'mode': 'merge' if len(config) > 1 else 'single'})
    return {'asset_id': asset_id, 'series': series, 'frame': frame, 'width_px': spec['width_px'],
            'height_px': spec['height_px'], 'series_list': copy.deepcopy(series_list),
            'frame_axes': spec['frame_axes'], 'frame_shape': spec['frame_shape'], 'frame_coordinates': coordinates,
            'channels': result, 'default_view': default_view, 'total_channels': total,
            'returned_channels': len(result), 'truncated': total > len(result), 'warnings': warnings}


def _crop_rectangle(crop, width, height):
    if crop is None:
        return {'x': 0, 'y': 0, 'width': int(width), 'height': int(height)}
    from .models import SourceCrop
    numbers = SourceCrop.model_validate(crop).model_dump()
    if numbers['x'] + numbers['width'] > width + 1e-8 or numbers['y'] + numbers['height'] > height + 1e-8:
        raise ValueError('Multichannel crop extends beyond the original source pixels.')
    left, top = math.floor(numbers['x']), math.floor(numbers['y'])
    right, bottom = min(width, math.ceil(numbers['x'] + numbers['width'])), min(height, math.ceil(numbers['y'] + numbers['height']))
    return {'x': left, 'y': top, 'width': int(right - left), 'height': int(bottom - top)}


def tile_layout(view: dict, width_px: int, height_px: int, crop=None) -> dict:
    normalized = normalize_view(view)
    if width_px <= 0 or height_px <= 0:
        raise ValueError('Original channel dimensions must be positive.')
    rectangle = _crop_rectangle(crop, width_px, height_px)
    visible = [item for item in normalized['channels'] if item['visible']]
    mode = normalized['mode']
    if mode == 'single':
        selected = next(item for item in normalized['channels'] if item['id'] == normalized['active_channel'])
        entries = [('single', [selected])]
    elif mode == 'merge':
        entries = [('merge', visible)]
    else:
        entries = [('single', [item]) for item in visible]
        if normalized['split']['include_merge'] and visible:
            entries.append(('merge', visible))
    if not entries:
        raise ValueError('No visible channel is available for a split layout.')
    columns = 1 if mode != 'split' else min(len(entries), normalized['split']['columns'] or math.ceil(math.sqrt(len(entries))))
    rows = math.ceil(len(entries) / columns)
    gap = normalized['split']['gap_px'] if mode == 'split' else 0
    tiles = []
    for index, (tile_mode, selected) in enumerate(entries):
        channel = selected[0] if tile_mode == 'single' else None
        tiles.append({'id': channel['id'] if channel else 'merge', 'channel_id': channel['id'] if channel else None,
                      'channel_ids': [item['id'] for item in selected], 'mode': tile_mode,
                      'name': channel['name'] if channel else 'Merge',
                      'x_px': (index % columns) * (rectangle['width'] + gap),
                      'y_px': (index // columns) * (rectangle['height'] + gap),
                      'width_px': rectangle['width'], 'height_px': rectangle['height'],
                      **({'source': copy.deepcopy(channel['source'])} if channel else {})})
    return {'width_px': columns * rectangle['width'] + (columns - 1) * gap,
            'height_px': rows * rectangle['height'] + (rows - 1) * gap,
            'source_width_px': int(width_px), 'source_height_px': int(height_px),
            'crop': rectangle, 'mode': mode, 'columns': columns, 'rows': rows, 'gap_px': gap, 'tiles': tiles}


def _lut_plane(plane, channel, lower, upper):
    # Float64 subtraction protects small signals on a large raw offset, before
    # the normalized display buffer is reduced to float32. Sources remain read-only.
    values = plane.astype(np.float64, copy=True)
    values -= lower
    values /= upper - lower
    np.nan_to_num(values, copy=False, nan=0, posinf=1, neginf=0)
    np.clip(values, 0, 1, out=values)
    if channel['gamma'] != 1:
        np.power(values, 1.0 / channel['gamma'], out=values)
    return values.astype(np.float32)


@contextmanager
def _accumulator(height, width):
    """Bound the native export heap while keeping additive LUT math exact.

    A large RGB accumulator is a temporary mapped file, never another original
    or persistent project asset. It is unlinked on close, also on render errors.
    Preview buffers and ordinary figure panels stay in memory.
    """
    if width * height <= MEMORY_TILE_PIXELS:
        yield np.zeros((height, width, 3), dtype=np.float32)
        return
    with tempfile.TemporaryFile(prefix='figwork-channel-render-') as temporary:
        temporary.truncate(height * width * 3 * np.dtype(np.float32).itemsize)
        pixels = np.memmap(temporary, dtype=np.float32, mode='r+', shape=(height, width, 3))
        try:
            # A newly truncated temporary file reads as zeroes. Explicitly
            # touching the entire mapping here would defeat bounded working RAM.
            yield pixels
        finally:
            pixels._mmap.close()


def render_view(store, view: dict, crop=None, max_size=None) -> tuple[Image.Image, dict]:
    validated = validate_view(view, store)
    normalized = validated['view']
    manifest = tile_layout(normalized, validated['width_px'], validated['height_px'], crop)
    width, height = manifest['width_px'], manifest['height_px']
    if max_size is not None:
        if isinstance(max_size, bool) or not isinstance(max_size, int) or not 1 <= max_size <= 8192:
            raise ValueError('Preview maximum size must be an integer between 1 and 8192.')
        scale = min(1.0, max_size / max(width, height))
    else:
        scale = 1.0
    target_width, target_height = max(1, round(width * scale)), max(1, round(height * scale))
    if target_width * target_height > MAX_RENDER_PIXELS:
        raise ValueError('Multichannel rendering exceeds 100 million output pixels; reduce the crop or preview size.')
    canvas = Image.new('RGB', (target_width, target_height), '#ffffff' if normalized['mode'] == 'split' else '#000000')
    channels = {item['id']: item for item in normalized['channels']}
    needed = {identifier for tile in manifest['tiles'] for identifier in tile['channel_ids']}
    rectangle = manifest['crop']
    tile_width, tile_height = max(1, round(rectangle['width'] * scale)), max(1, round(rectangle['height'] * scale))
    # Read and normalize one channel at a time. Retaining one float image per
    # selected channel scales memory with channel count and makes large 16-bit
    # acquisitions unusable. Only tiny level records survive between tiles.
    level_map = {}
    for tile in manifest['tiles']:
        with _accumulator(tile_height, tile_width) as pixels:
            rows_per_chunk = max(1, MAX_LUT_PIXELS // tile_width)
            for identifier in tile['channel_ids']:
                channel = channels[identifier]
                plane = store.read_plane(channel['source'])
                if plane.shape != (validated['height_px'], validated['width_px']):
                    raise ValueError('Decoded original dimensions disagree with the channel metadata.')
                if identifier not in level_map:
                    _, spec = source_spec(store, channel['source'])
                    component_index = channel['source']['channel']
                    info = spec.get('channel_info', [])
                    component = info[component_index] if component_index < len(info) else None
                    lower, upper = _limits(plane, channel, component=component)
                    level_map[identifier] = {'channel_id': identifier, 'source': channel['source'],
                                            'black': lower, 'white': upper, 'gamma': channel['gamma'],
                                            'original_dtype': str(plane.dtype)}
                level = level_map[identifier]
                source_crop = plane[rectangle['y']:rectangle['y'] + rectangle['height'],
                                    rectangle['x']:rectangle['x'] + rectangle['width']]
                color = channel['color']
                rgb = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
                if scale < 1:
                    # Bounds come from the full original, before crop or
                    # deterministic preview sampling. Only previews resample.
                    stride = max(1, int(max(source_crop.shape) // (max(tile_width, tile_height) * 2)))
                    sampled = source_crop[::stride, ::stride]
                    intensity = np.empty(sampled.shape, dtype=np.float32)
                    sample_rows = max(1, MAX_LUT_PIXELS // sampled.shape[1])
                    for row in range(0, sampled.shape[0], sample_rows):
                        intensity[row:row + sample_rows] = _lut_plane(
                            sampled[row:row + sample_rows], channel, level['black'], level['white'])
                    if intensity.shape != (tile_height, tile_width):
                        intensity = np.asarray(Image.fromarray(intensity).resize(
                            (tile_width, tile_height), Image.Resampling.BILINEAR), dtype=np.float32)
                for top in range(0, tile_height, rows_per_chunk):
                    bottom = min(tile_height, top + rows_per_chunk)
                    values = intensity[top:bottom] if scale < 1 else _lut_plane(
                        source_crop[top:bottom], channel, level['black'], level['white'])
                    for component in range(3):
                        if rgb[component]:
                            pixels[top:bottom, :, component] += values * rgb[component]
                # Do not keep full normalized thumbnails for every channel.
                if scale < 1:
                    del intensity
            for top in range(0, tile_height, rows_per_chunk):
                bottom = min(tile_height, top + rows_per_chunk)
                strip = pixels[top:bottom]
                np.clip(strip, 0, 255, out=strip)
                np.rint(strip, out=strip)
                canvas.paste(Image.fromarray(strip.astype(np.uint8)),
                             (round(tile['x_px'] * scale), round(tile['y_px'] * scale) + top))
    levels = [level_map[item['id']] for item in normalized['channels'] if item['id'] in needed]
    manifest.update(preview_width_px=target_width, preview_height_px=target_height,
                    scale=scale, levels=levels, source_asset_ids=validated['source_ids'])
    return canvas, manifest


def auto_range(store, view: dict, percentiles=(1, 99.5), channel_ids=None) -> dict:
    normalized = validate_view(view, store)['view']
    selected = {item['id'] for item in normalized['channels']} if channel_ids is None else set(channel_ids)
    if not selected <= {item['id'] for item in normalized['channels']}:
        raise ValueError('Auto-range channel selection is not present in this view.')
    levels, warnings = [], []
    for channel in normalized['channels']:
        if channel['id'] not in selected:
            continue
        plane = store.read_plane(channel['source'])
        stats = plane_statistics(plane, percentiles)
        if not stats['finite_count']:
            raise ValueError('Cannot choose display levels for a channel without finite intensities.')
        lower, upper = stats['percentiles']['values']
        if lower is None or upper is None or upper <= lower:
            lower, upper = float(stats['minimum']), float(stats['maximum'])
            if upper <= lower:
                upper = max(lower + 1, float(np.nextafter(lower, np.inf)))
            warnings.append(f'{channel["name"]} 的百分位范围为空，使用全幅范围或常数图像的非零显示区间。')
        channel['black'], channel['white'] = lower, upper
        levels.append({'channel_id': channel['id'], 'source': channel['source'], 'black': lower, 'white': upper, **stats})
    return {'channel_view': normalized, 'levels': levels, 'warnings': warnings}


class FilePlaneReader:
    """Exporter/preflight adapter; metadata is inspected from the actual files."""
    def __init__(self, assets: list[dict] | dict, resolver: Callable[[str], Path]):
        self.assets = {item['id']: item for item in assets} if isinstance(assets, list) else dict(assets)
        self.resolver = resolver
        self.metadata = {}
        self.last_key = None
        self.last_plane = None

    def resolve(self, asset_id):
        if asset_id not in self.assets:
            raise ValueError('Channel source is absent from this project.')
        return Path(self.resolver(asset_id))

    def public_asset(self, asset_id):
        if asset_id not in self.metadata:
            asset = self.assets.get(asset_id)
            if asset is None or asset.get('kind') != 'image':
                raise ValueError('Channel source must be an original raster in the project.')
            self.metadata[asset_id] = {**asset, **describe_raster(self.resolve(asset_id))}
        return self.metadata[asset_id]

    def read_plane(self, source):
        identity = ChannelSource.model_validate(source).model_dump()
        source_spec(self, identity)
        key = tuple(identity[name] for name in ('asset_id', 'series', 'frame', 'channel'))
        if key == self.last_key:
            return self.last_plane
        plane = read_raster(self.resolve(identity['asset_id']), series=identity['series'],
                            frame=identity['frame'], channel=identity['channel'])
        # One plane only, and never retain a large frame across unrelated panels.
        if plane.nbytes <= 96 * 1024 * 1024:
            self.last_key, self.last_plane = key, plane
        else:
            self.last_key = self.last_plane = None
        return plane
