# Mixins del llenç

Responsabilitats d'`AstroCanvas`:

- projecció i adaptació als renderitzadors;
- Sol, Lluna, planetes i eclipsis;
- interacció, picking i eines;
- selecció, traces i orquestració de l'esdeveniment de pintat.

Cada mòdul importa les seves dependències reals. No existeix cap `sky_runtime`
ni cap ruta de renderització alternativa silenciosa.
