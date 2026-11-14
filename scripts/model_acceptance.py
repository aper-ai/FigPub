"""Stable JUnit identities required for actual official Cellpose inference."""
from __future__ import annotations

from collections import Counter
from xml.etree.ElementTree import Element


OFFICIAL_MODEL_TEST = 'test_real_official_model_inference_preserves_input'
OFFICIAL_MODEL_CASES = frozenset({
    f'{OFFICIAL_MODEL_TEST}[nuclei]',
    f'{OFFICIAL_MODEL_TEST}[cyto3]',
})


def verified_official_model_cases(report: Element, *, only_models: bool = False) -> bool:
    """Require one successful nuclei case and one successful cyto3 case.

    The full Python report contains unrelated regressions. The portable report
    is deliberately restricted to these two cases. Counting names preserves
    multiplicity, so duplicate executions cannot replace a missing model.
    """
    cases = report.findall('.//testcase')
    selected = cases if only_models else [case for case in cases
        if case.get('name', '').split('[', 1)[0] == OFFICIAL_MODEL_TEST]
    return (Counter(case.get('name') for case in selected) == Counter(OFFICIAL_MODEL_CASES)
            and all(not any(case.find(tag) is not None for tag in ('failure', 'error', 'skipped'))
                    for case in selected))
