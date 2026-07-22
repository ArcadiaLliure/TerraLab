# Alcance de visibilidad topográfica

`TerrainRangeSettings` y `resolve_visibility_range()` son la fuente única de verdad. La UI
persiste esa configuración, el job la serializa explícitamente y el subproceso la resuelve una
sola vez cuando conoce la elevación DEM del observador. El radio resuelto se reutiliza en bandas,
raycasting, contaminación lumínica, malla y metadatos de `HorizonProfile`.

En modo automático se usa `sqrt(2 R h + h²)` para el observador y para la elevación objetivo, y
se suman ambas distancias. `R` es el radio terrestre o, con refracción, `7/6 R`; el factor aproxima
la curvatura descendente de los rayos en una atmósfera estándar. El valor objetivo predeterminado
es 8.849 m y puede sustituirse por el máximo conocido de la fuente DEM. El resultado respeta un
mínimo operativo de 25 km y el máximo de seguridad configurable y validado de 530 km. En modo
manual el radio se valida dentro del mismo intervalo.

La malla divide el alcance en 0–5, 5–25, 25–100, 100–250 km y una zona de silueta final. Sus
presupuestos son 150, 100, 75, 45 y 25 anillos (395 como máximo), distribuidos geométricamente.
Así el último anillo siempre coincide con el radio resuelto sin extender resolución cercana a
530 km.

`prepare_region()` conserva una precarga inmediata separada y configurable (25 km por defecto),
porque los proveedores actuales pueden cargar ventanas completas. El resto se obtiene mediante
su muestreo/caché diferidos durante el raycast. Esta separación no reduce el alcance calculado;
evita materializar de antemano un disco completo a resolución nativa. La ventana de contaminación
lumínica, de resolución mucho menor, sí recibe el radio completo.

Los perfiles nuevos almacenan `resolved_radius_m`. `covers_radius()` rechaza perfiles antiguos
sin ese metadato y perfiles cuya cobertura sea menor que la solicitada, evitando reutilizaciones
incompatibles aunque se mantenga la lectura del formato antiguo.
