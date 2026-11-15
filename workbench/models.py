"""Canonical, unit-explicit project schema. Unknown extension fields round-trip.

Geometry in panels/crops is always original pixels; canvas geometry is mm.
Intensity numbers retain the source's numeric scale. No Qt/UI dependencies.
"""
from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StrictBool, StrictInt, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra='allow', allow_inf_nan=False)


class SourceAsset(Record):
    id: str = Field(pattern=r'^[a-f0-9]{64}$')
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    name: str = Field(min_length=1, max_length=240)
    kind: Literal['image', 'svg', 'pdf', 'table']
    width_px: int = Field(ge=0, le=60_000_000)
    height_px: int = Field(ge=0, le=60_000_000)
    bit_depth: int = Field(ge=0, le=128)
    dtype: str
    channels: int = Field(ge=0, le=4096)
    frames: int = Field(ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    preview_url: str = ''


class FigureGeometry(Record):
    width_mm: FiniteFloat = Field(gt=0, le=2000)
    height_mm: FiniteFloat = Field(gt=0, le=2000)
    background: str = '#ffffff'


class SourceCrop(Record):
    x: FiniteFloat = Field(ge=0)
    y: FiniteFloat = Field(ge=0)
    width: FiniteFloat = Field(gt=0)
    height: FiniteFloat = Field(gt=0)


class DisplaySettings(Record):
    low: FiniteFloat | None = None
    high: FiniteFloat | None = None
    gamma: FiniteFloat = Field(default=1.0, ge=0.1, le=5)
    invert: bool = False


class ChannelSource(BaseModel):
    """A scalar original plane: frame is flattened over non-Y/X/C/S axes."""
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    asset_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    series: StrictInt = Field(default=0, ge=0, le=127)
    frame: StrictInt = Field(default=0, ge=0)
    channel: StrictInt = Field(default=0, ge=0, le=4095)


class ChannelStyle(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    id: str = Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')
    source: ChannelSource
    name: str = Field(default='', max_length=120)
    color: str = Field(default='#FFFFFF', pattern=r'^#[0-9a-fA-F]{6}$')
    black: FiniteFloat | None = None
    white: FiniteFloat | None = None
    gamma: FiniteFloat = Field(default=1, ge=0.1, le=5)
    visible: StrictBool = True

    @model_validator(mode='after')
    def ordered_levels(self):
        if self.black is not None and self.white is not None and self.white <= self.black:
            raise ValueError('Channel white must be greater than black in original intensity units.')
        return self


class ChannelSplit(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    columns: StrictInt = Field(default=0, ge=0, le=32)
    gap_px: StrictInt = Field(default=8, ge=0, le=1024)
    include_merge: StrictBool = False


class ChannelView(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    version: StrictInt = Field(default=1, ge=1, le=1)
    mode: Literal['merge', 'single', 'split'] = 'merge'
    active_channel: str | None = None
    split: ChannelSplit = Field(default_factory=ChannelSplit)
    channels: list[ChannelStyle] = Field(min_length=1, max_length=32)

    @model_validator(mode='after')
    def coherent_channel_identity(self):
        identifiers = [channel.id for channel in self.channels]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError('Channel ids must be unique within a view.')
        if self.active_channel is None:
            self.active_channel = identifiers[0]
        if self.active_channel not in identifiers:
            raise ValueError('Active channel is missing from this view.')
        return self


class GelSettings(Record):
    lane_centers: list[FiniteFloat] = Field(default_factory=list)
    lane_names: list[str] = Field(default_factory=list)
    marker_points: list[dict[str, Any]] = Field(default_factory=list)
    marker_unit: str = 'kDa'
    hidden_lanes: list[Any] = Field(default_factory=list)


class ImageEdit(Record):
    """One source-image recipe shape shared by panels and asset edits."""
    crop: SourceCrop | None = None
    rotation: FiniteFloat = Field(default=0, ge=-36000, le=36000)
    flip_h: bool = False
    flip_v: bool = False
    display: DisplaySettings = Field(default_factory=DisplaySettings)
    gel: GelSettings = Field(default_factory=GelSettings)
    page: StrictInt = Field(default=0, ge=0)
    frame: StrictInt | None = Field(default=None, ge=0)
    series: StrictInt = Field(default=0, ge=0, le=127)
    channel: StrictInt | None = Field(default=None, ge=0, le=4095)
    channel_view: ChannelView | None = None

    @model_validator(mode='after')
    def supported_image_composition(self):
        if self.channel_view is not None and self.channel_view.mode == 'split' and self.gel.hidden_lanes:
            raise ValueError('Split channel layouts cannot also hide gel lanes; use separate single/merge panels.')
        return self


class PanelLabelAnchor(Record):
    """Upright caption anchor in fractions of displayed image dimensions.

    The physical 1.8 mm baseline offset remains unchanged by image resizing.
    This is layout metadata; source crops and image pixels are unaffected.
    """
    x_ratio: FiniteFloat = Field(strict=True, ge=-400000, le=400000)
    y_ratio: FiniteFloat = Field(strict=True, ge=-400000, le=400000)


class Panel(ImageEdit):
    id: str = Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')
    asset_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    x_mm: FiniteFloat = Field(ge=-2000, le=4000)
    y_mm: FiniteFloat = Field(ge=-2000, le=4000)
    width_mm: FiniteFloat = Field(gt=0, le=4000)
    height_mm: FiniteFloat = Field(gt=0, le=4000)
    label: str = ''
    title: str = ''
    # None means an older document: the editor preserves existing custom labels
    # before deciding which empty labels can become automatic.
    label_mode: Literal['auto', 'manual', 'none'] | None = None
    label_anchor: PanelLabelAnchor | None = None
    label_font_size_pt: FiniteFloat = Field(default=10, gt=0, le=1000)
    title_font_size_pt: FiniteFloat = Field(default=8, gt=0, le=1000)
    group_id: str | None = Field(default=None, min_length=1, max_length=100,
                                pattern=r'^[A-Za-z0-9_-]+$')


class Annotation(Record):
    id: str = Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')
    type: Literal['text', 'line', 'rect']
    x_mm: FiniteFloat
    y_mm: FiniteFloat
    width_mm: FiniteFloat = 0
    height_mm: FiniteFloat = 0
    text: str = ''
    font_size_pt: FiniteFloat = Field(default=8, gt=0, le=1000)
    color: str = '#222222'


class CompositionLayout(Record):
    """Saved layout choices; zero rows/columns let the planner choose a grid.

    Capacity and available space are checked only when a layout is applied.
    A document may store a draft grid that does not yet fit its panel count.
    """
    preset: Literal['grid', 'hero-left', 'hero-top'] = 'grid'
    columns: StrictInt = Field(default=0, ge=0, le=1000)
    rows: StrictInt = Field(default=0, ge=0, le=1000)
    margin_mm: FiniteFloat = Field(default=6, strict=True, ge=0, le=1000)
    gap_x_mm: FiniteFloat = Field(default=3, strict=True, ge=0, le=1000)
    gap_y_mm: FiniteFloat = Field(default=3, strict=True, ge=0, le=1000)


class CompositionNumbering(Record):
    style: Literal['upper', 'lower', 'numeric'] = 'upper'
    # 'array' is a legacy spelling of source order, preserved on round-trip.
    order: Literal['visual', 'source', 'array'] = 'visual'
    enabled: StrictBool = True


class JournalFontRange(Record):
    min: FiniteFloat | None = Field(default=None, strict=True, gt=0, le=1000)
    max: FiniteFloat | None = Field(default=None, strict=True, gt=0, le=1000)

    @model_validator(mode='after')
    def ordered_font_sizes(self):
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError('Journal minimum font size cannot exceed its maximum.')
        return self


class JournalResolutionRule(Record):
    min_ppi: FiniteFloat | None = Field(default=None, strict=True, gt=0, le=100000)
    applies_at: str | None = None


class JournalPresetSnapshot(Record):
    """Portable provenance, not an authority for enforcing journal rules.

    Catalogue identifiers and descriptive metadata stay open to new journals.
    Known physical quantities remain finite and explicitly measured.
    """
    id: str | None = Field(default=None, min_length=1, max_length=120)
    title: str | None = Field(default=None, max_length=500)
    journal: str | None = Field(default=None, max_length=240)
    category: str | None = None
    verification_status: str | None = None
    scope: str | None = None
    width_semantics: str | None = None
    width_mm: FiniteFloat | None = Field(default=None, strict=True, gt=0, le=2000)
    height_mm: FiniteFloat | None = Field(default=None, strict=True, gt=0, le=2000)
    min_width_mm: FiniteFloat | None = Field(default=None, strict=True, gt=0, le=2000)
    max_width_mm: FiniteFloat | None = Field(default=None, strict=True, gt=0, le=2000)
    max_height_mm: FiniteFloat | None = Field(default=None, strict=True, gt=0, le=2000)
    min_ppi: FiniteFloat | None = Field(default=None, strict=True, gt=0, le=100000)
    resolution_scope: str | None = None
    resolution_by_type: dict[str, JournalResolutionRule] = Field(default_factory=dict)
    label_style: Literal['upper', 'lower', 'numeric'] | None = None
    label_font_size_pt: FiniteFloat | None = Field(default=None, strict=True, gt=0, le=1000)
    body_font_size_pt: JournalFontRange | None = None
    editable_vector_required: StrictBool | None = None
    source_url: str | None = None
    source_urls: list[str] = Field(default_factory=list)
    source_snapshot_url: str | None = None
    source_date: str | None = None
    checked_at: str | None = None
    verified_fields: list[str] = Field(default_factory=list)
    field_sources: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class CompositionSettings(Record):
    layout: CompositionLayout = Field(default_factory=CompositionLayout)
    numbering: CompositionNumbering = Field(default_factory=CompositionNumbering)
    resize_mode: Literal['fit', 'reflow', 'canvas'] = 'fit'
    auto_import: StrictBool = True
    # Unknown IDs are portable. The current UI catalog reports whether they
    # can be checked; saving a project must not silently substitute a preset.
    journal_preset_id: str | None = Field(default=None, min_length=1, max_length=120)
    journal_preset_snapshot: JournalPresetSnapshot | None = None


class ProjectSettings(Record):
    asset_edits: dict[str, ImageEdit] = Field(default_factory=dict)
    analysis_rois: list[dict[str, Any]] = Field(default_factory=list, max_length=10000)
    composition: CompositionSettings = Field(default_factory=CompositionSettings)


class ProjectDocument(Record):
    schema_version: Literal[1]
    id: str = Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')
    title: str = Field(max_length=240)
    revision: int = Field(ge=0)
    created_at: str
    updated_at: str
    assets: list[SourceAsset] = Field(default_factory=list, max_length=256)
    figure: FigureGeometry
    panels: list[Panel] = Field(default_factory=list, max_length=1000)
    annotations: list[Annotation] = Field(default_factory=list, max_length=5000)
    measurements: list[dict[str, Any]] = Field(default_factory=list, max_length=2000)
    audit: list[dict[str, Any]] = Field(default_factory=list, max_length=20_000)
    settings: ProjectSettings = Field(default_factory=ProjectSettings)
    # Older documents stored this extension at the top level. It retains the
    # identical ImageEdit type; current UI persistence uses settings.asset_edits.
    asset_edits: dict[str, ImageEdit] = Field(default_factory=dict)


def validate_schema(payload: dict) -> None:
    # Validation does not replace the supplied document with a coerced dump:
    # extension fields, precise numbers and future plugin data stay intact.
    ProjectDocument.model_validate(payload)
