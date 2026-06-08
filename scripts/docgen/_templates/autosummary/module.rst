{# Modules listed here only expose the named classes in the left nav. Every
   class is still listed in the on-page table below and gets its own page (the
   non-nav ones are generated as orphans by _generate_class_pages in conf.py).
   Keep in sync with NAV_ONLY in conf.py. #}
{% set nav_only = {
   "baseten.client.managementapi": ["ApiClient", "AsyncApiClient"],
   "baseten.client.inferenceapi": ["ApiClient", "AsyncApiClient"],
   "baseten.client.modelconfig": ["ModelConfig"],
} %}
{{ fullname | escape | underline }}

.. automodule:: {{ fullname }}
   :no-members:

   {% block classes %}
   {% if classes %}
   .. rubric:: Classes

   .. autosummary::
      {% if fullname not in nav_only %}:toctree:
      {% endif %}:nosignatures:
   {% for item in classes %}
      {{ item }}
   {%- endfor %}
   {% endif %}
   {% endblock %}

   {% block functions %}
   {% if functions %}
   .. rubric:: Functions

   .. autosummary::
      :toctree:
   {% for item in functions %}
      {{ item }}
   {%- endfor %}
   {% endif %}
   {% endblock %}

{% if fullname in nav_only %}
.. toctree::
   :hidden:
{% for item in nav_only[fullname] %}
   {{ fullname }}.{{ item }}
{%- endfor %}
{% endif %}

{% block modules %}
{% if modules %}
.. rubric:: Modules

.. autosummary::
   :toctree:
   :recursive:
{% for item in modules %}
   {{ item }}
{%- endfor %}
{% endif %}
{% endblock %}
