{{ objname | escape | underline}}

.. testsetup::

   from {{ module }} import {{ objname }}

.. currentmodule:: {{ module }}

{% if objname in ["RemoteBpod", "Bpod"] %}
.. autoclass:: {{ objname }}
   :members:
   :inherited-members:
{% else %}
.. autoclass:: {{ objname }}
   :members:
{% endif %}
