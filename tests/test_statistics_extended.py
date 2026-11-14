"""Regression cases for identity, actual sample size and exclusion mistakes."""
import math

import numpy as np
import pytest

from workbench.analysis import analyze_wb
from workbench.qpcr import analyze_qpcr, read_qpcr_table
from workbench.statistics import _holm, compare_biological_groups


def grouped(values, *, group, experiment='e1', feature='x'):
    return [{'experiment_id': experiment, 'target': feature, 'group': group,
             'biological_replicate': str(index), 'value': value}
            for index, value in enumerate(values)]


def test_holm_is_monotone_and_keeps_planned_family_size():
    records = [{'p_value_raw': value, 'p_value': None} for value in [.03, .001, .04, None]]
    _holm(records)
    assert [row['p_value'] for row in records[:3]] == pytest.approx([.09, .004, .09])
    assert records[3]['p_value'] is None


def test_pairing_matches_identity_not_row_order_and_reports_unmatched():
    left = grouped([1, 3, 5, 100], group='Control')
    right = list(reversed(grouped([2, 6, 7], group='Treatment')))
    result = compare_biological_groups(left + right, feature='target', value='value', control_group='Control', test='paired_t')
    row = result['comparisons'][0]
    assert row['n_pairs'] == 3 and row['paired_biological_ids'] == ['0', '1', '2']
    assert row['unmatched_group1'] == ['3']
    # Differences are [-1,-3,-2]: mean -2, sample SD 1, t=-sqrt(12).
    assert row['statistic'] == pytest.approx(-math.sqrt(12))
    assert row['p_value_raw'] == pytest.approx(.07417990022744853)
    assert row['p_value'] == row['p_value_raw']


def test_experiments_and_targets_never_share_test_family():
    rows = grouped([1, 2, 4], group='Control') + grouped([3, 6, 8], group='A') + grouped([2, 5, 9], group='B')
    rows += grouped([1, 3], group='Control', experiment='e2') + grouped([3, 8], group='A', experiment='e2')
    rows += grouped([2, 4], group='Control', feature='y') + grouped([8, 9], group='A', feature='y')
    result = compare_biological_groups(rows, feature='target', value='value', control_group='Control', test='welch_t')
    assert len(result['comparisons']) == 4
    for row in result['comparisons']:
        assert row['family_size'] == (2 if row['experiment_id'] == 'e1' and row['target'] == 'x' else 1)


def test_anova_tukey_reports_omnibus_and_all_three_comparisons():
    rows = grouped([1, 2, 3], group='A') + grouped([3, 4, 5], group='B') + grouped([5, 6, 7], group='C')
    result = compare_biological_groups(rows, feature='target', value='value', test='anova_tukey')
    assert result['omnibus'][0]['statistic'] == pytest.approx(12)
    assert len(result['comparisons']) == 3
    assert all(row['adjustment'] == 'tukey_hsd' and 0 <= row['p_value'] <= 1 for row in result['comparisons'])
    assert min(result['comparisons'], key=lambda row: row['p_value'])['group2'] == 'C'


@pytest.mark.parametrize('test', ['welch_t', 'student_t', 'paired_t', 'anova_tukey'])
def test_degenerate_replicates_have_no_manufactured_pvalue(test):
    rows = grouped([1, 1, 1], group='Control') + grouped([3, 3, 3], group='Treatment')
    result = compare_biological_groups(rows, feature='target', value='value', control_group='Control', test=test)
    assert result['comparisons'][0]['p_value'] is None


def test_qpcr_text_false_and_explicit_source_row_exclusion(tmp_path):
    source = tmp_path / 'ct.csv'
    source.write_text('Sample,Target,Ct,Group,included\nc1,Ref,20,C,true\nc1,X,22,C,true\nc1,X,99,C,false\nc1,X,30,C,true\nt1,Ref,20,T,true\nt1,X,21,T,true\n')
    table = read_qpcr_table(source)
    result = analyze_qpcr(table['rows'], {'reference_gene': 'Ref', 'control_group': 'C', 'excluded_source_rows': [5]})
    control = next(row for row in result['rows'] if row['group'] == 'C')
    assert control['delta_ct'] == 2
    assert {row['source_row'] for row in result['source_rows'] if not row['included']} == {4, 5}
    assert all('excluded_by_user' in row['qc_flags'] for row in result['source_rows'] if not row['included'])


def test_qpcr_group_override_uses_experiment_and_sample_identity():
    rows = []
    for experiment in ['e1', 'e2']:
        for sample, target_ct in [('same', 22), ('other', 21)]:
            rows.extend([{'sample': sample, 'target': target, 'ct': ct, 'experiment_id': experiment}
                         for target, ct in [('Ref', 20), ('X', target_ct)]])
    result = analyze_qpcr(rows, {'reference_gene': 'Ref', 'control_group': 'C', 'group_mapping': {
        'e1::same': 'C', 'e1::other': 'T', 'e2::same': 'T', 'e2::other': 'C'}})
    assert {(r['experiment_id'], r['group'], r['delta_ct']) for r in result['rows']} == {
        ('e1', 'C', 2), ('e1', 'T', 1), ('e2', 'T', 2), ('e2', 'C', 1)}


def test_wb_identical_geometry_on_distinct_channels_can_normalize():
    aid = 'a' * 64
    target, control = np.full((8, 8), 100, np.uint16), np.full((8, 8), 40, np.uint16)
    base = {'asset_id': aid, 'x': 1, 'y': 1, 'width': 5, 'height': 5,
            'sample_id': 's', 'group': 'C', 'biological_replicate': '1'}
    rois = [{**base, 'id': 'target', 'role': 'target', 'protein': 'X',
             'source': {'asset_id': aid, 'series': 0, 'frame': 2, 'channel': 1}},
            {**base, 'id': 'control', 'role': 'control', 'protein': 'Ref',
             'source': {'asset_id': aid, 'series': 0, 'frame': 2, 'channel': 2}}]
    result = analyze_wb({(aid, 0, 2, 1): target, (aid, 0, 2, 2): control},
                        {'rois': rois, 'polarity': 'bright', 'background': 'none', 'metric': 'normalized'})
    assert result['rows'][0]['normalized'] == 2.5
    assert 'control_is_target_source_roi' not in result['rows'][0]['qc_flags']
