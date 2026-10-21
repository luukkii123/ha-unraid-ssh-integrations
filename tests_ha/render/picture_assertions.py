"""Identity-based picture checks shared by browser evidence and regressions."""


def assert_container_pictures(pictures, entities):
    by_entity = {}
    for suffix in ('_container_alpha_one', '_update_alpha_one', '_container_beta', '_update_beta'):
        matching = [item for item in pictures if item['entity'] == entities[suffix]]
        assert len(matching) == 1 and matching[0]['width'] > 0 and matching[0]['visible'], (suffix, pictures)
        by_entity[entities[suffix]] = matching[0]
    for container in ('alpha_one', 'beta'):
        switch = by_entity[entities['_container_' + container]]
        update = by_entity[entities['_update_' + container]]
        assert switch['src'] == update['src'], (container, switch, update)
