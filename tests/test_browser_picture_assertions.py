"""The browser must compare each container pair irrespective of DOM order."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('picture_assertions', Path(__file__).parents[1] / 'tests_ha/render/picture_assertions.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

ENTITIES = {'_container_alpha_one': 'switch.alpha', '_update_alpha_one': 'update.alpha',
            '_container_beta': 'switch.beta', '_update_beta': 'update.beta'}


def pictures():
    return [{'entity': entity, 'src': source, 'width': 20, 'visible': True} for entity, source in (
        ('switch.alpha', '/local/alpha.png'), ('update.alpha', '/local/alpha.png'),
        ('switch.beta', '/local/beta.png'), ('update.beta', '/local/beta.png'))]


@pytest.mark.parametrize('pair', ['alpha', 'beta'])
def test_each_mismatched_container_pair_is_rejected(pair):
    rows = pictures()
    next(row for row in rows if row['entity'] == 'update.' + pair)['src'] = '/local/different.png'
    with pytest.raises(AssertionError):
        module.assert_container_pictures(rows, ENTITIES)


def test_valid_pairs_accept_interleaved_dom_order():
    rows = pictures()
    module.assert_container_pictures([rows[3], rows[0], rows[2], rows[1]], ENTITIES)
