# Persistencia de terreno

Serialización versionada de perfiles y mallas.

`profile_npz.py` valida el esquema y carga arrays con `allow_pickle=False`.
Las escrituras se publican de forma atómica para no dejar perfiles parciales
tras una cancelación o un fallo.
