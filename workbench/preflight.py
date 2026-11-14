"""Measured, source-aware figure preflight. A file hash is not authenticity proof.

Checks original raster pixels and raster images embedded in vector containers.
All findings refer to assets/panels; unsupported inspection is a warning, not pass.
"""
from __future__ import annotations

import hashlib
import math
from functools import lru_cache
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np

from .imaging import display_bounds, load_image_array, retained_segments
from .figure_geometry import annotation_display_bounds, panel_display_bounds
from .storage import safe_svg

RESOURCE_FONTS = Path(__file__).resolve().parents[1] / 'resources' / 'fonts'


@lru_cache(maxsize=8)
def _font_codepoints(path_string: str, size: int, mtime: int, font_number: int):
    from fontTools.ttLib import TTFont
    font = TTFont(path_string, fontNumber=font_number, lazy=True)
    try:
        characters = set()
        for table in font['cmap'].tables:
            if table.isUnicode():
                characters.update(table.cmap)
        return frozenset(characters)
    finally:
        font.close()


def _font_coverage():
    paths = [(RESOURCE_FONTS / 'DejaVuSans.ttf', 0), (RESOURCE_FONTS / 'NotoSansCJK-Regular.ttc', 2)]
    points, names, failures = set(), [], []
    for path, number in paths:
        if not path.is_file():
            failures.append(path.name)
            continue
        try:
            stat = path.stat()
            points.update(_font_codepoints(str(path), stat.st_size, stat.st_mtime_ns, number))
            names.append(path.name)
        except Exception:
            failures.append(path.name)
    return points, names, failures


def _hash_file(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def _numeric(value, default=None):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (ValueError, TypeError):
        return default


def _bounds(rect, width, height):
    x,y,w,h = rect
    return x >= -0.001 and y >= -0.001 and x+w <= width+0.001 and y+h <= height+0.001


def _intersection(a, b):
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[2], b[2]), min(a[3], b[3])
    return right > left and bottom > top


def check_project(project: dict, asset_resolver, min_ppi=300) -> dict:
    issues, panel_metrics, asset_metrics = [], [], []
    min_ppi = _numeric(min_ppi)
    if min_ppi is None or not 20 <= min_ppi <= 2400:
        raise ValueError('最低有效分辨率必须在 20–2400 PPI 之间。')

    def issue(severity, code, message, panel_id=None, asset_id=None, details=None):
        item = {'severity':severity, 'code':code, 'message':message}
        if panel_id is not None:
            item['panel_id'] = panel_id
        if asset_id is not None:
            item['asset_id'] = asset_id
        if details is not None:
            item['details'] = details
        issues.append(item)

    figure = project.get('figure', {})
    canvas_w = _numeric(figure.get('width_mm'))
    canvas_h = _numeric(figure.get('height_mm'))
    if not canvas_w or not canvas_h or min(canvas_w,canvas_h) <= 0:
        issue('error','INVALID_CANVAS','画布物理尺寸无效。')
        canvas_w, canvas_h = 0,0
    assets = project.get('assets', [])
    by_id = {asset.get('id'): asset for asset in assets}
    from .channels import FilePlaneReader
    channel_reader = FilePlaneReader(assets, asset_resolver)
    if len(by_id) != len(assets):
        issue('error','DUPLICATE_ASSET_ID','素材列表包含重复身份。')
    panels = project.get('panels', [])
    if not panels:
        issue('warning','EMPTY_FIGURE','图版尚未放入任何面板。')

    sources = {}
    verified_hashes = 0
    for asset in assets:
        aid = asset.get('id')
        try:
            path = Path(asset_resolver(aid))
            if not path.is_file():
                raise ValueError('原始文件不存在')
            digest = _hash_file(path)
            expected = asset.get('sha256')
            if not expected:
                issue('warning','SOURCE_HASH_MISSING','素材未记录来源哈希，无法核对字节一致性。',asset_id=aid)
            elif digest != expected:
                issue('error','SOURCE_HASH_MISMATCH','原始文件与工程记录的 SHA-256 不一致。请恢复原文件。',asset_id=aid)
            else:
                verified_hashes += 1
            sources[aid] = path
            asset_metrics.append({'asset_id':aid,'size_bytes':path.stat().st_size,'sha256_matches':digest==expected})
        except Exception as exc:
            issue('error','SOURCE_MISSING',f'无法取得完整原始素材：{str(exc)[:160]}',asset_id=aid)

    # Project measurements and panel derivations must retain every original.
    referenced = set()
    def visit(node):
        if isinstance(node, dict):
            for key,value in node.items():
                if key=='asset_id' and isinstance(value,str):
                    referenced.add(value)
                elif key in {'source_asset_ids','asset_ids'} and isinstance(value,list):
                    referenced.update(str(item) for item in value)
                visit(value)
        elif isinstance(node,list):
            for value in node:
                visit(value)
    visit(project)
    for aid in sorted(referenced - set(sources)):
        issue('error','SOURCE_COVERAGE_INCOMPLETE','组图或分析记录引用的原始素材未完整保留。',asset_id=aid)

    labels = {}
    text_items = []
    groups = {}
    for panel in panels:
        if panel.get('hidden'):
            continue
        pid, aid = panel.get('id'),panel.get('asset_id')
        asset = by_id.get(aid)
        if asset is None:
            issue('error','PANEL_ASSET_MISSING','面板所引用的素材不存在。',pid,aid)
            continue
        label = '' if panel.get('label_mode') == 'none' else str(panel.get('label') or '').strip()
        if label:
            labels.setdefault(label,[]).append(pid)
            text_items.append((label,pid,_numeric(panel.get('label_font_size_pt'),10)))
        title = str(panel.get('title') or '')
        if title:
            text_items.append((title,pid,_numeric(panel.get('title_font_size_pt'),8)))
        channel_view = panel.get('channel_view') or {}
        if channel_view.get('mode') == 'split' and panel.get('show_channel_labels', True):
            text_items.extend((str(channel.get('name') or ''), pid, 7) for channel in channel_view.get('channels', [])
                              if channel.get('visible', True))
        gel = panel.get('gel') or {}
        for text in gel.get('lane_names',[]):
            text_items.append((str(text),pid,_numeric(gel.get('font_size_pt'),8)))
        for marker in gel.get('marker_points',[]):
            text_items.append((str(marker.get('label') or marker.get('size') or marker.get('value') or ''),pid,_numeric(gel.get('font_size_pt'),8)))
        if aid not in sources:
            continue
        x,y,w,h = (_numeric(panel.get(key)) for key in ('x_mm','y_mm','width_mm','height_mm'))
        if any(v is None for v in (x,y,w,h)) or min(w or 0,h or 0)<=0:
            issue('error','PANEL_SIZE_INVALID','面板位置或物理尺寸无效。',pid,aid)
            continue
        rotation = _numeric(panel.get('rotation',0))
        if rotation is None:
            issue('error','ROTATION_INVALID','旋转角度不是有限数值。',pid,aid)
            rotation=0
        radians=math.radians(rotation)
        extent_w=abs(w*math.cos(radians))+abs(h*math.sin(radians))
        extent_h=abs(w*math.sin(radians))+abs(h*math.cos(radians))
        bounds = {'image': (x+(w-extent_w)/2,y+(h-extent_h)/2,extent_w,extent_h)}
        try:
            bounds = panel_display_bounds({**panel, 'rotation': rotation})
        except (TypeError, ValueError):
            issue('error','FONT_SIZE_INVALID','面板编号或标题字号无效，无法检查完整显示边界。',pid,aid)
        outside = [part for part, rectangle in bounds.items() if not _bounds(rectangle,canvas_w,canvas_h)]
        if outside:
            names = {'image':'图像（含旋转）', 'label':'编号', 'title':'标题'}
            issue('warning','PANEL_OUTSIDE_CANVAS',
                  '面板的'+'、'.join(names[part] for part in outside)+'超出画布，导出时可能被裁掉。',pid,aid,
                  {'outside_parts': outside, 'bounds_mm': bounds})
        if asset.get('kind')=='table':
            issue('error','TABLE_AS_FIGURE','数据表不能直接作为图像面板；请从真实分析结果生成图表。',pid,aid)
            continue
        display = panel.get('display') or {}
        gamma=_numeric(display.get('gamma',1))
        if gamma is None or not .1<=gamma<=5:
            issue('error','DISPLAY_GAMMA_INVALID','显示 Gamma 超出支持范围 0.1–5。',pid,aid)
        elif gamma!=1:
            issue('warning','NONLINEAR_DISPLAY','面板使用了非线性 Gamma 调整；请确认适用性并保留处理说明。',pid,aid,{'gamma':gamma})
        if panel.get('channel_view'):
            _check_channel_panel(panel, channel_reader, by_id, min_ppi, panel_metrics, issue)
        else:
            groups.setdefault(aid,[]).append(panel)

    for label,ids in labels.items():
        if len(ids)>1:
            for pid in ids:
                issue('warning','DUPLICATE_PANEL_LABEL',f'图版编号「{label}」重复，请核对正文引用。',panel_id=pid)

    # Decode each source at most once for its panels. Do not retain a project-sized array cache.
    for aid, source_panels in groups.items():
        asset, path = by_id[aid], sources[aid]
        kind=asset.get('kind')
        if kind=='image':
            planes = {}
            for panel in source_panels:
                try:
                    frame = panel.get('frame') if panel.get('frame') is not None else panel.get('page', 0)
                    plane = (int(frame), panel.get('channel'), int(panel.get('series', 0)))
                    planes.setdefault(plane, []).append(panel)
                except (ValueError, TypeError):
                    issue('error','IMAGE_PLANE_INVALID','图像平面索引无效。',panel.get('id'),aid)
            for (page_index, channel, series_index), plane_panels in planes.items():
                try:
                    array=load_image_array(path, page=page_index, channel=channel, series=series_index)
                    source_h, source_w=array.shape[:2]
                    source_details={'width_px':source_w,'height_px':source_h,'dtype':str(array.dtype),'bit_depth':array.dtype.itemsize*8}
                    claimed=(asset.get('width_px'),asset.get('height_px'))
                    if claimed!=(source_w,source_h):
                        issue('warning','SOURCE_METADATA_DIFFERS','素材记录的尺寸与实际解码尺寸不同；本次检查使用实际像素。',asset_id=aid,details=source_details)
                    for panel in plane_panels:
                        _check_raster_panel(panel,asset,array,min_ppi,panel_metrics,issue)
                    del array
                except Exception as exc:
                    for panel in plane_panels:
                        issue('warning','RASTER_INSPECTION_UNAVAILABLE',f'无法完整评估此原图的像素、饱和与分辨率：{str(exc)[:160]}',panel.get('id'),aid)
        elif kind in {'svg','pdf'}:
            try:
                import fitz
                if kind=='svg':
                    payload=safe_svg(path.read_bytes())
                    from .vectors import open_svg
                    document=open_svg(payload)
                else:
                    document=fitz.open(path)
                try:
                    for panel in source_panels:
                        page_index=int(panel.get('page',0))
                        if page_index<0 or page_index>=document.page_count:
                            issue('error','PDF_PAGE_INVALID','面板引用的 PDF 页码超出文件范围。',panel.get('id'),aid)
                            continue
                        page=document[page_index]
                        _check_vector_panel(panel,asset,document,page,min_ppi,panel_metrics,issue,text_items)
                        if kind=='svg':
                            nodes=ET.fromstring(payload).iter()
                            embedded=sum(node.tag.split('}')[-1].lower()=='image' for node in nodes)
                            if embedded and not page.get_image_info():
                                issue('warning','SVG_RASTER_NOT_DECODED','SVG 包含图像节点，但解码器未读取到内部栅格；不能据此认定全部为矢量。',panel.get('id'),aid)
                finally:
                    document.close()
            except Exception as exc:
                for panel in source_panels:
                    issue('warning','VECTOR_INSPECTION_UNAVAILABLE',f'无法完整检查容器内的图像与字体：{str(exc)[:160]}',panel.get('id'),aid)
        else:
            for panel in source_panels:
                issue('warning','SOURCE_KIND_UNSUPPORTED','此素材类型暂不能进行完整投稿检查。',panel.get('id'),aid)

    for annotation in project.get('annotations',[]):
        if annotation.get('hidden'):
            continue
        text=str(annotation.get('text') or '')
        size=_numeric(annotation.get('font_size_pt',8))
        if text:
            text_items.append((text,None,size))
        x,y,w,h=(_numeric(annotation.get(key,0)) for key in ('x_mm','y_mm','width_mm','height_mm'))
        if any(v is None for v in (x,y,w,h)):
            issue('error','ANNOTATION_GEOMETRY_INVALID','文字或图形标注含有无效坐标。',details={'annotation_id':annotation.get('id')})
        else:
            try:
                bounds = annotation_display_bounds(annotation)
            except (TypeError, ValueError):
                issue('error','ANNOTATION_GEOMETRY_INVALID','标注字号或线宽无效，无法检查完整显示边界。',details={'annotation_id':annotation.get('id')})
            else:
                if not _bounds(bounds,canvas_w,canvas_h):
                    issue('warning','ANNOTATION_OUTSIDE_CANVAS','标注的文字或图形超出画布，导出时可能被裁掉。',
                          details={'annotation_id':annotation.get('id'), 'bounds_mm':bounds})

    points,font_names,font_failures=_font_coverage()
    if text_items and font_failures:
        issue('warning','FONT_RESOURCE_UNAVAILABLE','部分导出字体资源无法读取，缺字检查不完整。',details={'fonts':font_failures})
    settings=project.get('settings') or {}
    family=str(settings.get('font_family','DejaVu Sans'))
    if family not in {'DejaVu Sans','DejaVuSans','Noto Sans CJK SC','NotoSansCJK'}:
        issue('warning','FONT_SUBSTITUTION',f'字体「{family}」未包含在本地交付字体中；导出将使用 DejaVu Sans / Noto Sans CJK。')
    missing = {}
    for text,pid,size in text_items:
        if size is None or size<=0:
            issue('error','FONT_SIZE_INVALID','文字字号无效。',panel_id=pid)
        elif size<6:
            issue('warning','SMALL_TEXT',f'最终文字约 {size:.1f} pt，缩印后可能难以辨认。',panel_id=pid)
        if points:
            unavailable={char for char in text if not char.isspace() and ord(char) not in points}
            if unavailable:
                missing.setdefault(pid,set()).update(unavailable)
    for pid,characters in missing.items():
        issue('error','MISSING_GLYPHS','交付字体缺少部分字符：'+''.join(sorted(characters))[:40],panel_id=pid,
              details={'codepoints':[f'U+{ord(char):04X}' for char in sorted(characters)[:40]]})
    if project.get('settings',{}).get('synthetic_demo'):
        issue('info','SYNTHETIC_DEMO','当前工程包含明确标记的合成示例数据，不是实验结果。')
    issue('info','PROVENANCE_LIMIT','原图哈希核对仅证明当前字节与工程记录一致，不能证明仪器来源或实验真实性。')
    numeric_ppi=[item['effective_ppi'] for item in panel_metrics if isinstance(item.get('effective_ppi'),(int,float))]
    metrics={'panel_count':len(panels),'asset_count':len(assets),'originals_available':len(sources),
             'source_hashes_verified':verified_hashes,'minimum_required_ppi':min_ppi,
             'min_effective_ppi':min(numeric_ppi) if numeric_ppi else None,
             'panels':panel_metrics,'assets':asset_metrics,'fonts_checked':font_names,
             'error_count':sum(item['severity']=='error' for item in issues),
             'warning_count':sum(item['severity']=='warning' for item in issues)}
    incomplete_codes = {'RASTER_INSPECTION_UNAVAILABLE', 'VECTOR_INSPECTION_UNAVAILABLE', 'EMBEDDED_RASTER_UNRESOLVED', 'SVG_RASTER_NOT_DECODED', 'FONT_RESOURCE_UNAVAILABLE', 'VECTOR_TEXT_INSPECTION_UNAVAILABLE', 'SOURCE_KIND_UNSUPPORTED', 'NO_VISIBLE_CHANNELS'}
    complete = not any(item['code'] in incomplete_codes for item in issues)
    metrics['inspection_complete'] = complete
    return {'ok':complete and not any(item['severity']=='error' for item in issues), 'complete':complete, 'issues':issues,'metrics':metrics}


def _crop(panel, width, height, issue, aid):
    raw=panel.get('crop') or {'x':0,'y':0,'width':width,'height':height}
    x,y,w,h=(_numeric(raw.get(key)) for key in ('x','y','width','height'))
    if any(value is None for value in (x,y,w,h)) or w<=0 or h<=0:
        issue('error','INVALID_CROP','裁切范围无效。',panel.get('id'),aid)
        return None
    if x<0 or y<0 or x+w>width+.01 or y+h>height+.01:
        issue('error','CROP_OUTSIDE_SOURCE','裁切范围超出了原始素材；请返回原图重新定位。',panel.get('id'),aid,
              {'source_width':width,'source_height':height,'crop':raw})
        return None
    return x,y,w,h


def _check_channel_panel(panel, reader, assets, min_ppi, metrics, issue):
    """Inspect original planes at each tile's actual printed size, excluding gaps."""
    from .channels import validate_view, tile_layout
    pid, aid = panel.get('id'), panel.get('asset_id')
    try:
        validated = validate_view(panel['channel_view'], reader)
        view = validated['view']
        layout = tile_layout(view, validated['width_px'], validated['height_px'], crop=panel.get('crop'))
        if view['mode'] == 'split' and (panel.get('gel') or {}).get('hidden_lanes'):
            raise ValueError('分屏面板不能同时拼接隐藏泳道；请拆为独立通道面板。')
        channels = {channel['id']: channel for channel in view['channels']}
        channel_metrics = []
        if not any(tile['channel_ids'] for tile in layout['tiles']):
            issue('warning', 'NO_VISIBLE_CHANNELS', '所有通道均隐藏，当前合并图为纯黑色，未检查可见原始像素。', pid, aid)
        for tile in layout['tiles']:
            for cid in tile['channel_ids']:
                channel = channels[cid]
                source = channel['source']
                original = reader.read_plane(source)
                crop = layout['crop']
                region = original[crop['y']:crop['y'] + crop['height'], crop['x']:crop['x'] + crop['width']]
                if not np.isfinite(region).any():
                    issue('error', 'NO_FINITE_CHANNEL_PIXELS', f"{channel['name']}：裁切区域没有有限原始像素。", pid, source['asset_id'], {'source': source})
                tile_panel = {**panel, 'asset_id': source['asset_id'],
                              'crop': layout['crop'],
                              'width_mm': float(panel['width_mm']) * tile['width_px'] / layout['width_px'],
                              'height_mm': float(panel['height_mm']) * tile['height_px'] / layout['height_px'],
                              'display': {'low': channel.get('black'), 'high': channel.get('white'), 'gamma': channel['gamma']}}
                start = len(channel_metrics)
                def channel_issue(severity, code, message, panel_id=None, asset_id=None, details=None):
                    issue(severity, code, f"{channel['name']}：{message}", panel_id or pid,
                          asset_id or source['asset_id'], {**(details or {}), 'source': source, 'channel_id': cid, 'tile_id': tile['id']})
                _check_raster_panel(tile_panel, assets[source['asset_id']], original, min_ppi, channel_metrics, channel_issue)
                for item in channel_metrics[start:]:
                    item.update(channel_id=cid, tile_id=tile['id'], source=source)
                if channel['gamma'] != 1:
                    channel_issue('warning', 'NONLINEAR_DISPLAY', '使用了非线性 Gamma 调整，原始定量不受显示设置影响。', details={'gamma': channel['gamma']})
        ppies = [item['effective_ppi'] for item in channel_metrics]
        metrics.append({'panel_id': pid, 'asset_id': aid, 'kind': 'multichannel', 'mode': view['mode'],
                        'effective_ppi': min(ppies) if ppies else None, 'layout': layout, 'channels': channel_metrics})
    except Exception as exc:
        issue('error', 'CHANNEL_VIEW_INVALID', f'多通道来源、布局或显示配置无效：{str(exc)[:200]}', pid, aid)


def _check_raster_panel(panel,asset,array,min_ppi,metrics,issue):
    aid,pid=asset['id'],panel.get('id')
    height,width=array.shape[:2]
    crop=_crop(panel,width,height,issue,aid)
    if crop is None:
        return
    from .analysis import PixelRegion
    try:
        rounded = PixelRegion.parse(dict(zip(('x', 'y', 'width', 'height'), crop)), (height, width))
    except ValueError as exc:
        issue('error', 'CROP_OUTSIDE_SOURCE', str(exc), pid, aid)
        return
    panel = {**panel, 'crop': rounded.to_dict()}
    x,y,cw,ch = rounded.x, rounded.y, rounded.width, rounded.height
    physical_w,physical_h=float(panel['width_mm']),float(panel['height_mm'])
    try:
        segments=retained_segments(panel,width,height)
    except ValueError as exc:
        issue('error','SPLICE_GEOMETRY_INVALID',str(exc),pid,aid)
        return
    hidden=(panel.get('gel') or {}).get('hidden_lanes',[])
    if hidden:
        centers=(panel.get('gel') or {}).get('lane_centers',[])
        if any(not isinstance(index,int) or index<0 or index>=len(centers) for index in hidden):
            issue('error','HIDDEN_LANE_INVALID','隐藏泳道索引与源泳道列表不匹配。',pid,aid)
        issue('warning','LANES_REMOVED','有泳道被隐藏。导出保留可见分隔间隙；图注仍需声明去除位置及原因。',pid,aid,
              {'hidden_lane_indices':hidden,'visible_segments':len(segments),'gap_fraction':0.025})
    ppi_x=min(segment.width/(physical_w*fraction/25.4) for segment,_,fraction in segments)
    ppi_y=ch/(physical_h/25.4)
    effective=min(ppi_x,ppi_y)
    metrics.append({'panel_id':pid,'asset_id':aid,'kind':'image','ppi_x':ppi_x,'ppi_y':ppi_y,'effective_ppi':effective,
                    'source_width_px':width,'source_height_px':height,'crop_width_px':cw,'crop_height_px':ch})
    if effective+1e-6<min_ppi:
        issue('warning','LOW_EFFECTIVE_PPI',f'此面板有效分辨率约 {effective:.0f} PPI，低于 {min_ppi:g}。提高导出 DPI 不会补回原图细节。',pid,aid,
              {'ppi_x':ppi_x,'ppi_y':ppi_y,'maximum_width_mm_at_target':sum(segment.width for segment,_,_ in segments)/min_ppi*25.4})
    if abs(math.log(ppi_x/ppi_y))>.03:
        issue('warning','ASPECT_DISTORTION','图像在横向与纵向的缩放比例不同，条带或细胞形状可能被拉伸。',pid,aid,
              {'horizontal_to_vertical_scale':ppi_y/ppi_x})
    settings=panel.get('display') or {}
    try:
        low,high=display_bounds(array,settings)
    except ValueError as exc:
        issue('error','DISPLAY_RANGE_INVALID',str(exc),pid,aid)
        return
    region=array[int(y):math.ceil(y+ch),int(x):math.ceil(x+cw)]
    if not region.size:
        issue('error','EMPTY_CROP','裁切后没有有效原图像素。',pid,aid)
        return
    finite_mask=np.isfinite(region)
    if not finite_mask.all():
        issue('warning','NONFINITE_SOURCE_VALUES','原始像素中存在 NaN/Infinity；显示可替代，但测量应逐项核对。',pid,aid,
              {'count':int(region.size-np.count_nonzero(finite_mask))})
    if settings.get('low') is not None or settings.get('high') is not None:
        clipped=int(np.count_nonzero((region<low)|(region>high)))
        if clipped:
            issue('warning','DISPLAY_CLIPPING',f'当前显示范围截断了裁切区域中 {clipped/region.size:.1%} 的像素；原始数值仍保留。',pid,aid,
                  {'clipped_fraction':clipped/region.size,'low':low,'high':high})
    if np.issubdtype(region.dtype,np.integer):
        maximum=np.iinfo(region.dtype).max
        saturated=int(np.count_nonzero(region==maximum))
        if saturated:
            issue('warning','RAW_MAXIMUM_PIXELS',f'原始区域有 {saturated} 个通道值达到 {maximum}。请结合采集位深和背景判断是否过曝，不能用显示调整恢复。',pid,aid,
                  {'fraction':saturated/region.size,'dtype':str(region.dtype),'maximum':int(maximum)})


def _check_vector_panel(panel,asset,document,page,min_ppi,metrics,issue,text_items):
    aid,pid=asset['id'],panel.get('id')
    rect=page.rect
    crop=_crop(panel,rect.width,rect.height,issue,aid)
    if crop is None:
        return
    x,y,cw,ch=crop
    sx=float(panel['width_mm'])/cw
    sy=float(panel['height_mm'])/ch
    if abs(math.log(sx/sy))>.03:
        issue('warning','ASPECT_DISTORTION','矢量容器在横向与纵向的缩放比例不同。',pid,aid,{'horizontal_to_vertical_scale':sx/sy})
    raster_metrics=[]
    images=page.get_image_info()
    for index,image in enumerate(images):
        bbox=image.get('bbox')
        if not bbox or not _intersection(bbox,(x,y,x+cw,y+ch)):
            continue
        iw,ih=int(image.get('width',0)),int(image.get('height',0))
        transform=image.get('transform')
        if not iw or not ih or not transform or len(transform)<6:
            issue('warning','EMBEDDED_RASTER_UNRESOLVED','容器内图像缺少像素或变换信息，无法计算其有效分辨率。',pid,aid)
            continue
        a,b,c,d,e,f=map(float,transform)
        mapping=np.array([[a*sx/iw,c*sx/ih],[b*sy/iw,d*sy/ih]])
        largest=float(np.linalg.svd(mapping,compute_uv=False).max())
        if not math.isfinite(largest) or largest<=0:
            issue('warning','EMBEDDED_RASTER_UNRESOLVED','容器内图像变换无效，分辨率检查未完成。',pid,aid)
            continue
        ppi=25.4/largest
        item={'index':index,'width_px':iw,'height_px':ih,'effective_ppi':ppi,'source_bbox':list(bbox)}
        raster_metrics.append(item)
        if ppi+1e-6<min_ppi:
            issue('warning','EMBEDDED_RASTER_LOW_PPI',f'{asset["kind"].upper()} 内第 {index+1} 张栅格图有效分辨率约 {ppi:.0f} PPI，低于 {min_ppi:g}。',pid,aid,item)
    effective=min(item['effective_ppi'] for item in raster_metrics) if raster_metrics else None
    metrics.append({'panel_id':pid,'asset_id':aid,'kind':asset['kind'],'effective_ppi':effective,
                    'embedded_rasters':raster_metrics,'vector_content':True})
    if not raster_metrics:
        issue('info','VECTOR_RESOLUTION','此可见区域未发现可解码栅格图像；矢量文字和线条没有固定 PPI。',pid,aid)
    if document.is_pdf:
        unembedded=[]
        for font in page.get_fonts(full=True):
            xref=font[0]
            try:
                buffer=document.extract_font(xref)[3] if xref>0 else b''
            except Exception:
                buffer=b''
            if not buffer:
                unembedded.append(str(font[3]))
        if unembedded:
            issue('warning','SOURCE_PDF_FONTS_UNEMBEDDED','源 PDF 的部分字体未嵌入；转交编辑前须核对字形和字体依赖。',pid,aid,{'fonts':sorted(set(unembedded))})
    try:
        for block in page.get_text('dict').get('blocks',[]):
            for line in block.get('lines',[]):
                for span in line.get('spans',[]):
                    bbox=span.get('bbox')
                    if bbox and _intersection(bbox,(x,y,x+cw,y+ch)):
                        effective_size=float(span.get('size',8))*min(sx,sy)*72/25.4
                        # Internal vector glyphs may use fonts other than the app;
                        # size is measurable, but don't certify their font coverage.
                        if effective_size<6:
                            issue('warning','SMALL_EMBEDDED_TEXT',f'容器内文字缩印后约 {effective_size:.1f} pt，建议检查可读性。',pid,aid)
                            return
    except Exception:
        issue('warning','VECTOR_TEXT_INSPECTION_UNAVAILABLE','容器内文字尺寸未能完整检查。',pid,aid)
