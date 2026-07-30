# Manifiesto de parches

| Parche | Tamaño | SHA-256 |
|---|---:|---|
| `refactor_closure_complete.patch` | 100174 bytes | `029710EE61DE6B13634AB40F1244CA0732622F9D58F45B899A9B046E150D7592` |
| `refactor_closure_complete_tracked_only.patch` | 72288 bytes | `4B64532C6578455963EA129E07B635F6380F4B0EDD802221CFE9EF5137AC5049` |
| `refactor_closure_staged.patch` | 167 bytes | `9B6AB18A1DA7C6E3D3E7BC68979BAD21804F3403649F3BC7B83C2133108F6B0D` |
| `refactor_closure_unstaged.patch` | 72121 bytes | `C99E205A9C8894D245B19BFB7EB9A5E89F73692A2130068D2775C74649517CC5` |

`refactor_closure_complete.patch` contiene 30 rutas: 18 ficheros existentes
modificados, la renombración de capitalización y 11 ficheros públicos nuevos.
Se aplicó sobre un worktree separado creado desde `98204d4`; el diff staged
resultante tuvo el mismo SHA-256 que el parche.

Quedan expresamente fuera los `TODO_*`, `TerraLab/NOTES.md`, los CSV
personales `ES.csv`, `FR.csv` y `PT.csv`, `plans/`, informes generados ajenos
al producto, caches y datasets. Durante la preparación mediante índice
temporal también se detectaron y excluyeron dos binarios locales ocultos por
atributos del índice:

- `TerraLab/assets/horizon_profile.npz`
- `TerraLab/data/light_pollution/C_DVNL 2022.tif`

Los parches no están staged y no deben añadirse a un commit sin una decisión
explícita del usuario.
