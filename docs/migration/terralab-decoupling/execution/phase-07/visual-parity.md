# Validacio visual manual - Fase 07

1. Amb `TERRALAB_MILKYWAY_RENDERING_PIPELINE=scene` i
   `TERRALAB_DEEP_SKY_RENDERING_PIPELINE=scene`, comprova Bortle 1 nocturn:
   Via Lactia visible, centre galactic orientat correctament i NGC variats.
2. Recorre RA 0/360, els dos pols, offset 180 graus i els flips de latitud i
   longitud; no hi ha costura, salt ni crash.
3. Comprova dia, crepuscle, Bortle 1/5/9 i magnitud manual. La politica ha de
   ocultar o atenuar la Via Lactia sense ocultar indegudament la UI.
4. Activa/desactiva pols Planck; prova un asset absent o invalid i una recarrega
   d'asset. Ha de conservar el frame, amb fallback explicit i sense bloqueig.
5. Confirma formes G/GC/PN, labels sense solapaments molestos, click/pick i
   cerca/goto per NGC/alias.
6. Fes pan/zoom/scope, resize i canvis rapid de temps; el frame calent no ha de
   recalcular la textura ni mostrar flicker.
7. Repeteix la mateixa escena amb un sol rollback a la vegada:
   `TERRALAB_MILKYWAY_RENDERING_PIPELINE=legacy` i, separadament,
   `TERRALAB_DEEP_SKY_RENDERING_PIPELINE=legacy`.
