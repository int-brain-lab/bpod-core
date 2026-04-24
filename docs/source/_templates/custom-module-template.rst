{{ fullname | escape | underline}}

.. currentmodule:: {{ fullname }}

.. automodule:: {{ fullname }}

{% block modules %}
{%- if modules %}
{{ _('Submodules') | underline('-') }}
.. autosummary::
   :nosignatures:
   :toctree:
   :template: custom-module-template.rst
   :recursive:
{% for item in modules %}
   {{ item }}
{%- endfor %}
{% endif %}
{%- endblock %}

{%- block functions %}
{%- if functions %}
{{ _('Functions') | underline('-') }}
.. autosummary::
   :nosignatures:
   :toctree:
   :template: custom-member-template.rst
{% for item in functions %}
   {{ item }}
{%- endfor %}
{% endif %}
{%- endblock %}

{%- block classes %}
{%- if classes %}
{{ _('Classes') | underline('-') }}
.. autosummary::
   :nosignatures:
   :toctree:
   :template: custom-class-template.rst
{% for item in classes %}
{%- if '[' not in item %}
   {{ item }}
{%- endif %}
{%- endfor %}
{% endif %}
{%- endblock %}

{%- block exceptions %}
{%- if exceptions %}
{{ _('Exceptions') | underline('-') }}
{% for item in exceptions %}
.. autoexception:: {{ item }}
{%- endfor %}
{% endif %}
{%- endblock %}

{% block attributes %}
{%- if attributes %}
{{ _('Attributes') | underline('-') }}
{% for item in attributes %}
.. autodata:: {{ item }}
{% endfor %}
{% endif %}
{%- endblock %}

