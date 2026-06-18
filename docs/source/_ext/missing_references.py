"""Sphinx extension that resolves cross-references not covered by intersphinx.

Handles the following cases:

- Platform-specific ``serial.Serial`` subclasses are remapped to the public API entry.
- Malformed fragments from comma-split nested generics (unbalanced brackets) are
  suppressed and rendered as plain text.
- Pydantic internal paths (e.g. ``pydantic.root_model.RootModel``) are remapped to the
  public API, stripping any generic parameters.
- Bare names (e.g. ``FieldInfo``, ``NoneType``) are remapped to their fully-qualified
  intersphinx targets.
- Artifacts from mocked modules (e.g. ``serial.threaded.ReaderThread.typing.Self``)
  are remapped to their public API entry.
- A small set of targets absent from all inventories are mapped directly to URLs.
"""

import re

from docutils import nodes
from sphinx.ext.intersphinx import InventoryAdapter

_REFERENCE_URL_MAP = {
    'polars.dataframe.frame.DataFrame': 'https://docs.pola.rs/py-polars/html/reference/dataframe',
    'polars.lazyframe.frame.LazyFrame': 'https://docs.pola.rs/py-polars/html/reference/lazyframe',
    'polars.DataFrame': 'https://docs.pola.rs/py-polars/html/reference/dataframe',
    'polars.LazyFrame': 'https://docs.pola.rs/py-polars/html/reference/lazyframe',
    # 'typing.Annotated': 'https://docs.python.org/3/library/typing.html#typing.Annotated',
    'pydantic_extra_types.color.ColorType': 'https://pydantic.dev/docs/validation/latest/api/pydantic-extra-types/pydantic_extra_types_color/',
    'MinLen': 'https://github.com/annotated-types/annotated-types',
}

_TARGET_REMAP = {
    'FieldInfo': 'pydantic.fields.FieldInfo',
    'NoneType': 'types.NoneType',
}


def _resolve(app, env, node, contnode):
    """Resolve missing cross-references not covered by intersphinx inventories."""

    target = node.get('reftarget', '')

    # Remap serial.tools.list_ports_common.ListPortInfo
    if target == 'serial.tools.list_ports_common.ListPortInfo':
        node['reftarget'] = 'serial.tools.list_ports.ListPortInfo'
        return None

    # Remap bare names to their fully-qualified intersphinx targets
    if target in _TARGET_REMAP:
        node['reftarget'] = _TARGET_REMAP[target]
        return None

    # Fix artifact from the mocked serial module (ReaderThread[Self] annotation)
    if target == 'serial.threaded.ReaderThread.typing.Self':
        contnode[0] = nodes.Text('ReaderThread[Self]')
        node['reftarget'] = 'serial.threaded.ReaderThread'
        return None

    # Remap fully-parameterized ValidatedDict generics
    if re.match(r'^bpod_core\.misc\.ValidatedDict\[', target):
        contnode[0] = nodes.Text('ValidatedDict')
        node['reftarget'] = 'bpod_core.misc.ValidatedDict'
        return env.get_domain('py').resolve_xref(
            env,
            node.get('refdoc', env.docname),
            app.builder,
            node.get('reftype', 'class'),
            'bpod_core.misc.ValidatedDict',
            node,
            contnode,
        )

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
                    ref = nodes.reference('', '', internal=False, refuri=entry.uri)
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
