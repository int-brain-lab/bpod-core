{{ fullname | escape | underline}}

.. testsetup::

   from {{ module }} import {{ objname }}

.. currentmodule:: {{ module }}

{% if objname in ["RemoteBpod", "Bpod"] %}
.. autoclass:: {{ objname }}
   :members:
   :undoc-members:
   :show-inheritance:
   :member-order: groupwise
   :inherited-members:
{% else %}
.. autoclass:: {{ objname }}
   :members:
   :undoc-members:
   :show-inheritance:
   :member-order: groupwise
{% endif %}
