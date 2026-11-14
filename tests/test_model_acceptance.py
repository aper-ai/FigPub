"""Official-model evidence cannot be replaced by arbitrary successful test counts."""
import xml.etree.ElementTree as ET

import pytest

from scripts.model_acceptance import verified_official_model_cases


def report(models, *, problem=None):
    root = ET.Element('testsuites')
    suite = ET.SubElement(root, 'testsuite')
    for model in models:
        case = ET.SubElement(suite, 'testcase',
            name=f'test_real_official_model_inference_preserves_input[{model}]')
        if problem and model == 'cyto3':
            ET.SubElement(case, problem)
    return root


def test_each_named_official_model_is_required_independently_of_order():
    assert verified_official_model_cases(report(['cyto3', 'nuclei']), only_models=True)
    assert not verified_official_model_cases(report(['nuclei', 'nuclei']), only_models=True)
    assert not verified_official_model_cases(report(['nuclei', 'cyto3', 'cyto3']), only_models=True)


@pytest.mark.parametrize('problem', ['failure', 'error', 'skipped'])
@pytest.mark.parametrize('only_models', [False, True])
def test_named_models_with_unsuccessful_execution_are_rejected(problem, only_models):
    assert not verified_official_model_cases(report(['nuclei', 'cyto3'], problem=problem), only_models=only_models)
