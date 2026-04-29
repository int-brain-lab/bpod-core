"""
Sphinx extension providing the ``dark-light-figure`` directive.

Renders a ``<stem>__light`` / ``<stem>__dark`` image pair as a captioned figure
that switches based on the active color mode.
"""

from docutils import nodes
from docutils.parsers.rst import Directive
from docutils.statemachine import StringList


class DarkLightFigure(Directive):
    """Directive that renders light and dark image variants as a captioned figure."""

    required_arguments = 1
    optional_arguments = 0
    has_content = True

    def run(self):
        stem = self.arguments[0]
        caption = '\n   '.join(self.content)

        strings = [
            '.. container:: light-only',
            '',
            f'   .. figure:: {stem}__light.svg',
            '',
            f'      {caption}',
            '',
            '.. container:: dark-only',
            '',
            f'   .. figure:: {stem}__dark.svg',
            '',
            f'      {caption}',
            '',
        ]

        container = nodes.Element()
        self.state.nested_parse(StringList(strings), self.content_offset, container)
        return list(container.children)


def setup(app):
    """Register the ``dark-light-figure`` directive."""
    app.add_directive('dark-light-figure', DarkLightFigure)
    return {
        'version': '0.1',
        'parallel_read_safe': True,
        'parallel_write_safe': True,
    }
