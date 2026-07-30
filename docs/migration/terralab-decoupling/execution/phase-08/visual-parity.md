# Validacio visual manual - Fase 08

1. Amb el valor per defecte `TERRALAB_OVERLAY_RENDERING_PIPELINE=scene`, activa
   i desactiva Grid; fes pan 360 graus, incloent RA 0/360, horitzo i pols. No
   hi ha d'haver salts de segment ni grid persistent quan esta desactivat.
2. Rota la camera a 0, 90, 180 i 270 graus. La bruixola i els seus ticks han
   de seguir l'horitzo sense labels duplicades, tallades o desplaçades.
3. En una escena nocturna densa, prova planetes i NGC. Les labels prioritaries
   han de romandre llegibles, sense solapament; click/pick de cossos i NGC no
   pot canviar.
4. Comprova HUD visible/amagat, normal, scope i debug. Ha d'existir una sola
   caixa HUD, amb pipelines correctes en debug i sense text residual en amagar.
5. Fes resize rapid i prova DPR 1, 1.25, 1.5 i 2. La tipografia, el clip i la
   posicio no poden tremolar ni bloquejar la UI.
6. Reinicia amb una font configurada absent si es possible; Qt ha de fer
   fallback i el frame ha de continuar mostrant labels.
7. Repeteix grid, bruixola, labels i HUD amb
   `TERRALAB_OVERLAY_RENDERING_PIPELINE=legacy`; el rollback ha de renderitzar
   i el HUD no pot quedar duplicat ni congelar la pantalla.
