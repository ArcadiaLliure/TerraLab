# Catàlegs astronòmics

Descobriment, càrrega i memòria cau de catàlegs d'estrelles.

`constants.py` és l'únic propietari dels límits compartits. `star_catalog.py`
transforma i fusiona dades; `scope_cache.py` conserva paquets `mmap` per al mode
telescòpic. No es permet `allow_pickle=True`.
