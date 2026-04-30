"""
Sphinx extension providing the ``fsm_codeblock`` directive.

Renders code as a highlighted ``.. code-block::`` and a hidden ``.. testcode::`` block
(for the ``doctest`` builder), and also executes the code immediately so that the
resulting :class:`~bpod_core.fsm.StateMachine` is rendered into light and dark SVG
diagrams alongside the document.
"""

import textwrap
from pathlib import Path

from docutils import nodes
from docutils.parsers.rst import directives
from docutils.statemachine import StringList
from sphinx.directives.code import CodeBlock


class FSMCodeBlock(CodeBlock):
    option_spec = CodeBlock.option_spec.copy()
    option_spec['filename'] = directives.unchanged
    option_spec['group'] = directives.unchanged

    def run(self):
        original_content = self.content
        filtered_lines = [line for line in self.content if '# hide' not in line]
        self.content = StringList(filtered_lines)
        nodes_list = super().run()
        self.content = original_content

        # produce a hidden testcode block
        group = self.options.get('group', self.options.get('filename', ''))
        doctest_lines = [f'.. testcode:: {group}'.rstrip(), '   :hide:', '']
        doctest_lines.extend(f'   {line}' for line in self.content)
        doctest_lines.append('')

        # access Sphinx environment and create a per-build namespace store
        env = self.state.document.settings.env  # type: ignore[attr-defined]
        if not hasattr(env, '_fsm_codeblock_namespaces'):
            env._fsm_codeblock_namespaces = {}
        name_space_store = env._fsm_codeblock_namespaces

        # execute code and render light/dark SVG diagrams
        is_doctest = getattr(env, 'app', None) and env.app.builder.name == 'doctest'
        if not is_doctest:
            name_space = name_space_store.setdefault(group, {}) if group else {}
            exec(textwrap.dedent('\n'.join(self.content)), name_space)
            fsm = name_space.get('fsm')
            if fsm and 'filename' in self.options:
                source_path = Path(self.state.document['source']).parent
                filepath = source_path / self.options['filename']
                config = env.app.config
                digraph_light = fsm.to_digraph(**config.fsm_light_colors)
                digraph_dark = fsm.to_digraph(**config.fsm_dark_colors)
                digraph_light.render(
                    outfile=filepath.with_stem(filepath.stem + '__light'),
                    cleanup=True,
                    quiet=True,
                )
                digraph_dark.render(
                    outfile=filepath.with_stem(filepath.stem + '__dark'),
                    cleanup=True,
                    quiet=True,
                )

        container = nodes.Element()
        self.state.nested_parse(
            StringList(doctest_lines), self.content_offset, container
        )
        nodes_list.extend(list(container.children))

        return nodes_list


def setup(app):
    """Register the ``fsm_codeblock`` directive."""
    app.add_directive('fsm_codeblock', FSMCodeBlock)
    return {
        'version': '0.1',
        'parallel_read_safe': False,
        'parallel_write_safe': True,
    }
