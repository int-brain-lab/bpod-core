"""
Sphinx extension that hides doctest-only comment nodes from the ``llms-markdown``
Markdown build.

``sphinx.ext.doctest`` always renders ``testsetup``/``testcleanup`` blocks, and any
``testcode``/``doctest``/``testoutput`` block marked ``:hide:``, as
``docutils.nodes.comment`` nodes tagged with a ``testnodetype`` attribute. The HTML
writer skips comment nodes entirely, but ``sphinx_markdown_builder``'s translator
(used by sphinx-llm's ``llms-markdown`` builder) renders them as literal ``<!-- -->``
text, leaking setup code into the generated Markdown and ``llms.txt``/
``llms-full.txt``. This extension removes those nodes -- and only those nodes --
before the ``llms-markdown`` builder writes its output, leaving the ``doctest``
builder, the HTML/dirhtml build, and any plain authored RST comments unaffected.
"""

from docutils import nodes


def _strip_doctest_comments(app, doctree, _docname):
    """Remove doctest-only comment nodes when building with ``llms-markdown``."""
    if app.builder.name != 'llms-markdown':
        return
    for node in list(doctree.findall(nodes.comment)):
        if node.get('testnodetype'):
            node.parent.remove(node)


def setup(app):
    """Register the ``doctree-resolved`` handler."""
    app.connect('doctree-resolved', _strip_doctest_comments)
    return {
        'version': '0.1',
        'parallel_read_safe': True,
        'parallel_write_safe': True,
    }
