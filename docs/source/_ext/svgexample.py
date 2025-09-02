import os
import textwrap
from pathlib import Path

from docutils import nodes
from docutils.parsers.rst import Directive, directives
from sphinx.util.osutil import ensuredir


class SvgExample(Directive):

    has_content = True  # directive body will contain Python code
    option_spec = { 'filename': directives.unchanged }


    def run(self):
        env = self.state.document.settings.env
        app = env.app

        code = textwrap.dedent('\n'.join(self.content))

        # Output directory: _build/html/_images
        path_images = Path(app.outdir) / '_images'
        ensuredir(path_images)

        # Unique filename (docname + line number)
        filename_opt = self.options.get('filename')
        if filename_opt:
            basename = os.path.basename(filename_opt)
            if not basename.lower().endswith('.svg'):
                basename += '.svg'
        else:
            basename = f'{env.docname.replace("/", "_")}_{self.lineno}.svg'

        path_image = path_images / basename

        # Execute code in isolated namespace
        from bpod_core.fsm import StateMachine

        fsm = StateMachine()
        ns = {"fsm": fsm}
        exec(code, ns)

        fsm = ns.get('fsm')
        fsm.to_file(path_image, overwrite=True)

        # Code block (literal Python)
        literal = nodes.literal_block(code, code)
        literal['language'] = 'python'

        # Image node (relative to _images)
        image = nodes.image(uri=f'_images/{basename}')

        return [literal, image]


def setup(app):
    app.add_directive('svg-example', SvgExample)
    return {'version': '0.1'}
