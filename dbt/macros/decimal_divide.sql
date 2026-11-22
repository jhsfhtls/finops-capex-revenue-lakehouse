{% macro decimal_divide(numerator, denominator, scale=6) -%}
  {{ return(adapter.dispatch('decimal_divide', 'finops_capex_revenue_lakehouse')(numerator, denominator, scale)) }}
{%- endmacro %}

{% macro duckdb__decimal_divide(numerator, denominator, scale=6) -%}
  CAST(decimal_division({{ numerator }}, {{ denominator }}, {{ scale }}) AS DECIMAL(12, {{ scale }}))
{%- endmacro %}

{% macro default__decimal_divide(numerator, denominator, scale=6) -%}
  CAST(({{ numerator }}) / ({{ denominator }}) AS DECIMAL(12, {{ scale }}))
{%- endmacro %}
