"""Composition documents stay portable while rejecting unusable settings."""
from copy import deepcopy
import io

from PIL import Image
from pydantic import ValidationError
import pytest

from workbench.models import (CompositionSettings, Panel, ProjectDocument,
                              validate_schema)
from workbench.storage import ProjectStore


def panel(**changes):
    return {
        'id': 'panel_1', 'asset_id': 'a' * 64,
        'x_mm': 6, 'y_mm': 12, 'width_mm': 40, 'height_mm': 30,
        'label': 'Actin', **changes,
    }


def project(**changes):
    return {
        'schema_version': 1, 'id': 'legacy_project', 'title': 'Example',
        'revision': 0, 'created_at': '2026-01-01', 'updated_at': '2026-01-01',
        'figure': {'width_mm': 180, 'height_mm': 120},
        'panels': [panel()], **changes,
    }


def composition():
    return {
        'layout': {'preset': 'hero-left', 'columns': 3, 'rows': 2,
                   'margin_mm': 6, 'gap_x_mm': 3.5, 'gap_y_mm': 2},
        'numbering': {'style': 'lower', 'order': 'visual', 'enabled': True},
        'resize_mode': 'fit', 'auto_import': True,
        'journal_preset_id': 'nature-single',
        'journal_preset_snapshot': {
            'id': 'nature-single', 'title': 'Nature single column',
            'journal': 'Nature', 'category': 'journal',
            'verification_status': 'verified', 'scope': 'main-figure-final-artwork',
            'width_semantics': 'print-column', 'width_mm': 89,
            'height_mm': None, 'max_height_mm': 170, 'min_ppi': None,
            'resolution_by_type': {'photograph': {'min_ppi': 300,
                                                  'applies_at': 'maximum-print-size'}},
            'label_style': 'lower', 'label_font_size_pt': 8,
            'body_font_size_pt': {'min': 5, 'max': 7},
            'editable_vector_required': True,
            'source_url': 'https://www.nature.com/nature/for-authors/final-submission',
            'source_urls': ['https://www.nature.com/nature/for-authors/final-submission'],
            'checked_at': '2026-11-12',
            'verified_fields': ['width_mm', 'label_style'],
            'field_sources': {'width_mm': 'https://example.org/guide'},
            'notes': ['Saved provenance; enforcement uses the current catalogue.'],
        },
    }


def replace_path(value, path, replacement):
    for key in path[:-1]:
        value = value[key]
    value[path[-1]] = replacement


def test_old_documents_keep_labels_and_do_not_gain_fields_on_validation():
    payload = project()
    before = deepcopy(payload)
    validate_schema(payload)
    model = ProjectDocument.model_validate(payload)
    assert payload == before
    assert model.model_dump(exclude_unset=True) == before
    assert model.panels[0].label == 'Actin'
    assert model.panels[0].label_mode is None
    assert model.panels[0].label_font_size_pt == 10
    assert model.panels[0].title_font_size_pt == 8
    assert model.panels[0].group_id is None
    assert model.settings.composition.layout.columns == 0
    assert model.settings.composition.numbering.enabled is True


def test_draft_layout_can_be_saved_before_it_has_space_for_current_panels():
    payload = project(
        panels=[panel(id=f'panel_{index}') for index in range(6)],
        settings={'composition': {
            'layout': {'preset': 'hero-top', 'columns': 1, 'rows': 1,
                       'margin_mm': 1000, 'gap_x_mm': 1000, 'gap_y_mm': 1000},
        }},
    )
    validate_schema(payload)
    # The planner, not document validation, decides whether applying it fits.
    assert ProjectDocument.model_validate(payload).settings.composition.layout.rows == 1


@pytest.mark.parametrize('order', ['visual', 'source', 'array'])
@pytest.mark.parametrize('style', ['upper', 'lower', 'numeric'])
def test_numbering_orders_and_styles_round_trip_without_normalization(order, style):
    payload = {'numbering': {'style': style, 'order': order, 'enabled': False}}
    assert CompositionSettings.model_validate(payload).model_dump(exclude_unset=True) == payload


@pytest.mark.parametrize('field', ['columns', 'rows'])
@pytest.mark.parametrize('value', [-1, 1001, 1.5, 2.0, True, '2', None,
                                  float('nan'), float('inf')])
def test_grid_counts_are_bounded_integers(field, value):
    with pytest.raises(ValidationError):
        CompositionSettings.model_validate({'layout': {field: value}})


@pytest.mark.parametrize('field', ['margin_mm', 'gap_x_mm', 'gap_y_mm'])
@pytest.mark.parametrize('value', [-0.01, 1000.01, float('nan'), float('inf'),
                                  -float('inf'), True, '6', None])
def test_layout_distances_are_finite_numeric_and_bounded(field, value):
    with pytest.raises(ValidationError):
        CompositionSettings.model_validate({'layout': {field: value}})


def test_zero_spacing_and_automatic_grid_and_maximum_counts_are_accepted():
    for count in (0, 1000):
        model = CompositionSettings.model_validate({
            'layout': {'columns': count, 'rows': count, 'margin_mm': 0,
                       'gap_x_mm': 0, 'gap_y_mm': 0},
        })
        assert model.layout.columns == count
        assert model.layout.gap_x_mm == 0


@pytest.mark.parametrize('path,value', [
    (('layout', 'preset'), 'masonry'),
    (('numbering', 'style'), 'roman'),
    (('numbering', 'order'), 'random'),
    (('resize_mode',), 'stretch'),
    (('auto_import',), 1),
    (('auto_import',), 'false'),
    (('auto_import',), None),
    (('numbering', 'enabled'), 0),
    (('numbering', 'enabled'), 'true'),
    (('numbering', 'enabled'), None),
    (('journal_preset_id',), ''),
    (('journal_preset_id',), 123),
    (('journal_preset_id',), 'a' * 121),
    (('journal_preset_snapshot',), []),
])
def test_invalid_composition_choices_are_rejected_at_the_specific_field(path, value):
    payload = composition()
    replace_path(payload, path, value)
    with pytest.raises(ValidationError) as caught:
        CompositionSettings.model_validate(payload)
    assert any(error['loc'][:len(path)] == path for error in caught.value.errors())


@pytest.mark.parametrize('path,value', [
    (('width_mm',), float('nan')),
    (('max_height_mm',), float('inf')),
    (('min_width_mm',), -1),
    (('min_ppi',), 0),
    (('label_font_size_pt',), -1),
    (('label_style',), 'roman'),
    (('editable_vector_required',), 'false'),
    (('body_font_size_pt', 'max'), 4),
    (('body_font_size_pt', 'min'), float('inf')),
    (('resolution_by_type', 'photograph', 'min_ppi'), -300),
])
def test_journal_snapshot_known_quantities_are_validated(path, value):
    payload = composition()
    replace_path(payload['journal_preset_snapshot'], path, value)
    with pytest.raises(ValidationError):
        CompositionSettings.model_validate(payload)


@pytest.mark.parametrize('field,value', [
    ('label_mode', 'automatic'), ('label_mode', True),
    ('group_id', ''), ('group_id', '../another'), ('group_id', 'g' * 101),
    ('label_font_size_pt', 0), ('label_font_size_pt', float('nan')),
    ('title_font_size_pt', -1), ('title_font_size_pt', float('inf')),
])
def test_panel_composition_metadata_rejects_invalid_values(field, value):
    with pytest.raises(ValidationError):
        Panel.model_validate(panel(**{field: value}))


@pytest.mark.parametrize('mode', ['auto', 'manual', 'none', None])
def test_panel_label_intent_and_group_identity_are_preserved(mode):
    payload = panel(label_mode=mode, group_id='group_123',
                    label_font_size_pt=12, title_font_size_pt=7.5)
    assert Panel.model_validate(payload).model_dump(exclude_unset=True) == payload


def test_future_extensions_survive_at_every_composition_level():
    settings = composition()
    settings['future_extension'] = {'version': 2, 'enabled': False}
    settings['layout']['alignment'] = {'vertical': 'center'}
    settings['numbering']['prefix'] = 'S'
    snapshot = settings['journal_preset_snapshot']
    snapshot['new_journal_rule'] = {'profile': 'future'}
    snapshot['body_font_size_pt']['unit_note'] = 'physical-points'
    snapshot['resolution_by_type']['photograph']['new_rule'] = [1, 2, 3]
    settings['journal_preset_id'] = 'future-journal-custom'
    snapshot['id'] = 'future-journal-custom'
    payload = project(settings={'composition': settings, 'future_settings': [1, 2]},
                      panels=[panel(label_mode='manual', plugin_panel={'custom': True})])
    before = deepcopy(payload)
    validate_schema(payload)
    assert payload == before
    assert ProjectDocument.model_validate(payload).model_dump(exclude_unset=True) == before


def test_composition_round_trip_through_portable_archive(tmp_path):
    original = ProjectStore(tmp_path / 'original')
    image = io.BytesIO()
    Image.new('L', (12, 8), color=127).save(image, format='PNG')
    asset = original.add_files([('original.png', image.getvalue())])[0]
    payload = original.get_project()
    payload['settings']['composition'] = composition()
    payload['settings']['composition']['numbering']['order'] = 'array'
    payload['settings']['composition']['layout']['future_balance'] = 'weighted'
    payload['panels'] = [
        panel(asset_id=asset['id'], label='Custom A', label_mode='manual',
              group_id='group_123', label_font_size_pt=9),
        panel(id='panel_2', asset_id=asset['id'], label='b', label_mode='auto',
              group_id='group_123', x_mm=70),
    ]
    original.update_project(payload)
    restored = ProjectStore(tmp_path / 'restored').open_archive(original.export_archive())
    assert restored['settings']['composition'] == payload['settings']['composition']
    assert restored['panels'] == payload['panels']


def test_schema_rejection_keeps_the_last_saved_project(tmp_path):
    store = ProjectStore(tmp_path)
    before = store.get_project()
    invalid = deepcopy(before)
    invalid['settings']['composition'] = {'numbering': {'enabled': 'false'}}
    with pytest.raises(ValueError):
        store.update_project(invalid)
    assert store.get_project() == before
