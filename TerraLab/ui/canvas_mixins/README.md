# Mixins del lienzo

Responsabilidades de `AstroCanvas`:

- proyección y adaptación a renderizadores;
- Sol, Luna, planetas y eclipses;
- interacción, picking y herramientas;
- selección, trazas y orquestación del paint event.

Cada módulo importa sus dependencias reales. No existe un `sky_runtime` ni una
ruta de render alternativa silenciosa.
