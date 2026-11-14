"""Explicit biological-unit comparisons shared by WB and qPCR.

Technical repeats have already been collapsed by the caller. Families never
cross experiments or measured targets. Pairing uses explicit biological IDs,
not row positions. Undefined estimates stay null instead of becoming stars.
"""
from __future__ import annotations

from collections import defaultdict
from itertools import combinations
import math
import warnings

import numpy as np
from scipy import stats


TESTS = frozenset({'none', 'welch_t', 'student_t', 'paired_t', 'anova_tukey'})


def _finite(value):
    return float(value) if value is not None and math.isfinite(float(value)) else None


def _holm(comparisons):
    """Step-down Holm adjustment over every planned comparison in the family."""
    ordered = sorted((row for row in comparisons if row['p_value_raw'] is not None),
                     key=lambda row: row['p_value_raw'])
    previous = 0.0
    for index, row in enumerate(ordered):
        previous = min(1.0, max(previous, row['p_value_raw'] * (len(comparisons) - index)))
        row['p_value'] = previous


def compare_biological_groups(rows, *, feature, value, control_group='', test='none', basis='biological_value'):
    if test not in TESTS:
        raise ValueError('统计检验支持 none、welch_t、student_t、paired_t 或 anova_tukey。')
    if test == 'none':
        return {'comparisons': [], 'omnibus': [], 'warnings': []}
    if test != 'anova_tukey' and not control_group:
        raise ValueError('成对组间检验需要明确指定 control_group。')
    families = defaultdict(lambda: defaultdict(list))
    for row in rows:
        number = _finite(row.get(value))
        if number is not None:
            families[(str(row.get('experiment_id', 'experiment-1')), str(row[feature]))][str(row['group'])].append(
                (str(row['biological_replicate']), number))
    comparisons, omnibus, messages = [], [], []
    for (experiment_id, target), groups in sorted(families.items()):
        context = {'experiment_id': experiment_id, feature: target, 'basis': basis}
        names = sorted(groups)
        family = []
        if test == 'anova_tukey':
            samples = [np.asarray([entry[1] for entry in groups[name]], dtype=np.float64) for name in names]
            note, result, posthoc = '', None, None
            if len(names) < 2 or any(len(sample) < 2 for sample in samples):
                note = 'insufficient_independent_replicates'
            elif all(np.var(sample) == 0 for sample in samples):
                note = 'zero_within_group_variance'
            else:
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', RuntimeWarning)
                    result = stats.f_oneway(*samples)
                    posthoc = stats.tukey_hsd(*samples)
                if _finite(result.pvalue) is None:
                    note = 'undefined_variance'
            omnibus.append({**context, 'test': 'one_way_anova', 'groups': names,
                            'n_biological': [len(sample) for sample in samples],
                            'statistic': _finite(result.statistic) if result else None,
                            'p_value': _finite(result.pvalue) if result else None, 'note': note,
                            'assumptions': ['independent_biological_units', 'normal_errors', 'equal_variance']})
            for i, j in combinations(range(len(names)), 2):
                p_value = _finite(posthoc.pvalue[i, j]) if posthoc is not None and not note else None
                family.append({**context, 'group1': names[i], 'group2': names[j], 'test': 'tukey_hsd',
                               'n1': len(samples[i]), 'n2': len(samples[j]),
                               'statistic': _finite(posthoc.statistic[i, j]) if posthoc is not None and not note else None,
                               'p_value_raw': None, 'p_value': p_value, 'adjustment': 'tukey_hsd',
                               'note': note, 'family_size': len(names) * (len(names) - 1) // 2})
        else:
            control = groups.get(control_group, [])
            for name in names:
                if name == control_group:
                    continue
                other = groups[name]
                row = {**context, 'group1': control_group, 'group2': name, 'test': test,
                       'n1': len(control), 'n2': len(other), 'statistic': None,
                       'p_value_raw': None, 'p_value': None, 'adjustment': 'holm', 'note': ''}
                a, b = np.asarray([entry[1] for entry in control]), np.asarray([entry[1] for entry in other])
                if test == 'paired_t':
                    left, right = dict(control), dict(other)
                    if len(left) != len(control) or len(right) != len(other):
                        raise ValueError('配对检验中每组的 biological_replicate 必须唯一。')
                    paired = sorted(left.keys() & right.keys())
                    a, b = np.asarray([left[key] for key in paired]), np.asarray([right[key] for key in paired])
                    row.update(n_pairs=len(paired), paired_biological_ids=paired,
                               unmatched_group1=sorted(left.keys() - right.keys()),
                               unmatched_group2=sorted(right.keys() - left.keys()))
                    if row['unmatched_group1'] or row['unmatched_group2']:
                        messages.append('配对检验只使用 biological_replicate 相同的完整配对；未匹配身份列在 comparisons 中。')
                if len(a) < 2 or len(b) < 2:
                    row['note'] = 'insufficient_independent_replicates'
                elif (np.var(a - b) == 0 if test == 'paired_t' else np.var(a) == 0 and np.var(b) == 0):
                    row['note'] = 'zero_variance'
                else:
                    with warnings.catch_warnings():
                        warnings.simplefilter('ignore', RuntimeWarning)
                        result = stats.ttest_rel(a, b) if test == 'paired_t' else stats.ttest_ind(a, b, equal_var=test == 'student_t')
                    row.update(statistic=_finite(result.statistic), p_value_raw=_finite(result.pvalue))
                    if row['p_value_raw'] is None:
                        row['note'] = 'undefined_variance'
                family.append(row)
            for row in family:
                row['family_size'] = len(family)
            _holm(family)
        comparisons.extend(family)
    if any(row['p_value'] is None for row in comparisons):
        messages.append('部分检验因独立重复不足、无有效配对或零方差而留空；没有生成显著性结论。')
    if test == 'anova_tukey':
        messages.append('ANOVA 与 Tukey 假设组间独立、误差近似正态且方差相等；多重比较范围为同一实验的同一靶标。')
    elif test == 'student_t':
        messages.append('Student t 检验假设组间独立、近似正态且方差相等；同一实验/靶标对照比较采用 Holm 校正。')
    return {'comparisons': comparisons, 'omnibus': omnibus, 'warnings': list(dict.fromkeys(messages))}
