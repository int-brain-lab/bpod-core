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
        for line in self.content:
            doctest_lines.append(f'   {line}')
        doctest_lines.append('')

        # access Sphinx environment and create a per-build namespace store
        env = self.state.document.settings.env  # type: ignore[attr-defined]
        if not hasattr(env, '_fsm_codeblock_namespaces'):
            env._fsm_codeblock_namespaces = {}
        name_space_store = env._fsm_codeblock_namespaces

        # execute code within the selected namespace and save state diagram to file
        is_doctest = getattr(env, 'app', None) and env.app.builder.name == 'doctest'
        if not is_doctest:
            name_space = name_space_store.setdefault(group, {}) if group else {}
            exec(textwrap.dedent('\n'.join(self.content)), name_space)
            fsm = name_space.get('fsm')
            if fsm and 'filename' in self.options:
                source_path = Path(self.state.document['source']).parent
                fsm.to_file(source_path / self.options['filename'], True)

        container = nodes.Element()
        self.state.nested_parse(
            StringList(doctest_lines), self.content_offset, container
        )
        nodes_list.extend(list(container.children))

        return nodes_list


def setup(app):
    app.add_directive('fsm_codeblock', FSMCodeBlock)
    return {'version': '0.1'}
