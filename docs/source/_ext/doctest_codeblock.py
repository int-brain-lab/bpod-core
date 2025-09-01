from __future__ import annotations

from docutils import nodes
from docutils.parsers.rst import Directive, directives
from docutils.statemachine import StringList
from sphinx.directives.code import CodeBlock
from sphinx.ext.doctest import DoctestDirective, TestcodeDirective

spec_codeblock = CodeBlock.option_spec
spec_doctest = DoctestDirective.option_spec
spec_testcode = TestcodeDirective.option_spec


class BaseCodeBlockDirective(Directive):
    required_arguments = 0
    optional_arguments = 1  # language, optional
    final_argument_whitespace = False
    has_content = True

    def _generate_block(self, opts: dict[str, str | list[str] | None]) -> list[str]:
        output = []
        for k, v in opts.items():
            value = '' if v is None else ' '.join(v) if isinstance(v, list) else v
            output.append(f'   :{k}: {value}')
        output.append('')
        for line in self.content:
            output.append(f'   {line}')
        output.append('')
        return output

    def _run(self, test_type: str):
        options = self.options
        group = options.pop('group', '')
        language = options.pop('language', None) or next(iter(self.arguments), 'pycon')

        codeblock_opts = {k: v for k, v in options.items() if k in spec_codeblock}
        doctest_opts = {k: v for k, v in options.items() if k not in spec_codeblock}
        doctest_opts['hide'] = None

        strings = [f'.. {test_type}:: {group}']
        strings.extend(self._generate_block(doctest_opts))
        strings.append(f'.. code-block:: {language}')
        strings.extend(self._generate_block(codeblock_opts))

        container = nodes.Element()
        self.state.nested_parse(StringList(strings), self.content_offset, container)
        return list(container.children)


class DoctestCodeBlockDirective(BaseCodeBlockDirective):
    """Directive to run doctests in a code block."""

    option_spec = spec_codeblock | spec_doctest | {'group': directives.unchanged}

    def run(self) -> list[nodes.Node]:
        return self._run('doctest')


class TestcodeCodeBlockDirective(BaseCodeBlockDirective):
    """Directive to run testcode in a code block."""

    option_spec = spec_codeblock | spec_testcode | {'group': directives.unchanged}

    def run(self) -> list[nodes.Node]:
        return self._run('testcode')


def setup(app):
    app.add_directive('doctest-code-block', DoctestCodeBlockDirective)
    app.add_directive('testcode-code-block', DoctestCodeBlockDirective)
    return {
        'version': '1.0',
        'parallel_read_safe': True,
        'parallel_write_safe': True,
    }
