.. meta::
   :description: API reference for {{ fullname }}.

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

{%- block exceptions %}
{%- if exceptions %}
{{ _('Exceptions') | underline('-') }}
{% for item in exceptions %}
.. autoexception:: {{ fullname }}.{{ item }}
{% endfor %}
{% endif %}
{%- endblock %}

{%- block classes %}
{%- if classes %}
{{ _('Classes') | underline('-') }}
{% for item in classes %}
.. autoclass:: {{ fullname }}.{{ item }}
   :members:
{%- if item in ["RemoteBpod", "Bpod"] %}
   :inherited-members:
{% elif item in ["SerialDevice"] %}
   :private-members: _serial, _port_info, _serial_device_name, _rename_serial_device
{%- endif %}
{% endfor %}
{% endif %}
{%- endblock %}

{%- block functions %}
{%- if functions %}
{{ _('Functions') | underline('-') }}
{% for item in functions %}
.. autofunction:: {{ fullname }}.{{ item }}
{% endfor %}
{% endif %}
{%- endblock %}

{%- block attributes %}
{%- if attributes %}
{{ _('Attributes') | underline('-') }}
{% for item in attributes %}
.. autodata:: {{ fullname }}.{{ item }}
{% endfor %}
{% endif %}
{%- endblock %}
