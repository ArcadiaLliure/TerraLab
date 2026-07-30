# Regresiones de superficie

| Caso | Estado inicial | Acción | Estado final y objetos activos | Resultado gráfico | Hit-test | Resultado |
|---|---|---|---|---|---|---|
| Desmarcado completo | Superficie visible, material y caché publicados | Desmarcar `Mostrar` | Superficie deshabilitada; se eliminan imagen, geometría, raster, materiales, caché activa, caché de categorías y referencia publicada; se cancela el muestreo y se oculta el tooltip | Sin textura ni relleno | Devuelve `None`; no queda superficie invisible consultable | PASS |
| Relieve 3D desactivado | Superficie visible con malla disponible | Desmarcar relieve 3D | La superficie sigue habilitada; se usan bandas/perfil y muestras de material 2D; la malla deja de ser el camino de render | Material superficial pintado sobre la proyección 2D | El tooltip y la selección categórica siguen operativos | PASS |

Pruebas independientes ejecutadas:

- `test_disabling_surface_layer_hides_material_and_does_not_start_sampling`
- `test_terrain_3d_toggle_switches_between_mesh_and_all_distance_silhouettes`
- `test_profile_mode_paints_sampled_surface_material`
- `test_standard_tooltip_event_shows_cached_categorical_description`
- `test_mouse_move_shows_categorical_tooltip_without_waiting_for_qt_delay`
- `test_profile_category_hit_uses_top_visible_polygon_and_nearest_sample`

Resultado: `6 passed in 1.49s`. Evidencia íntegra en
`validation/11_surface_regressions.*.txt`.

Durante la validación apareció un delta tardío que forzaba el relieve 3D al
activar la superficie y deshabilitaba su control. Se reprodujo la
contradicción con este contrato y se retiró exclusivamente ese delta; no forma
parte del parche final.
