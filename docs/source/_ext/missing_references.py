"""Sphinx extension that resolves cross-references not covered by intersphinx.

Handles three cases:

- Platform-specific ``serial.Serial`` subclasses are remapped to the public API entry.
- Generic subscripts (e.g. ``List[int]``) are stripped so intersphinx can resolve the
  base type.
- A small set of targets absent from all inventories are mapped directly to URLs.
"""

import re

from docutils import nodes
from sphinx.ext.intersphinx import InventoryAdapter

_REFERENCE_URL_MAP = {
    'polars.DataFrame': 'https://docs.pola.rs/py-polars/html/reference/dataframe',
    'polars.LazyFrame': 'https://docs.pola.rs/py-polars/html/reference/lazyframe',
    'typing.Annotated': 'https://docs.python.org/3/library/typing.html#typing.Annotated',
    'numpy.uint8': 'https://numpy.org/doc/stable/reference/arrays.scalars.html#numpy.uint8',
    'pydantic_extra_types.color.ColorType': 'https://pydantic.dev/docs/validation/latest/api/pydantic-extra-types/pydantic_extra_types_color/',
}


def _resolve(_, env, node, contnode):
    """Resolve missing cross-references not covered by intersphinx inventories."""

    target = node.get('reftarget', '')

    # Remap platform-specific serial.Serial subclasses to the public API entry
    if re.match(r'^serial\.serial\w+\.Serial$', target):
        node['reftarget'] = 'serial.Serial'
        return None

    # # Remap fully-parameterized ValidatedDict generics to typing.Annotated
    # if re.match(r'^bpod_core\.misc\.ValidatedDict\[', target):
    #     target = node['reftarget'] = 'typing.Annotated'

    # Strip generic subscripts that intersphinx can't resolve
    if '[' in target:
        node['reftarget'] = target[: target.index('[')]
        text = contnode.astext()
        if '[' in text:
            contnode[0] = nodes.Text(text[: text.index('[')])
        return None

    # Direct URL fallbacks for targets absent from all inventories
    if target in _REFERENCE_URL_MAP:
        ref = nodes.reference('', '', internal=False, refuri=_REFERENCE_URL_MAP[target])
        ref += contnode
        return ref

    # For failed py:class lookups, retry as py:data or py:attribute
    if node.get('refdomain') == 'py' and node.get('reftype') == 'class':
        for inv in InventoryAdapter(env).named_inventory.values():
            for alt in ('py:data', 'py:attribute'):
                entry = inv.get(alt, {}).get(target)
                if entry:
                    _proj, _ver, location, _display = entry
                    ref = nodes.reference('', '', internal=False, refuri=location)
                    ref += contnode
                    return ref

    return None


def setup(app):
    app.connect('missing-reference', _resolve, priority=400)
    return {
        'version': '0.1',
        'parallel_read_safe': True,
        'parallel_write_safe': True,
    }
