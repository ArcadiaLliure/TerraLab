# Contaminación lumínica

Modos, modelos y procesamiento de luminosidad nocturna.

- `modes.py`: valores canónicos y resolución de Bortle.
- `kernels.py`: núcleos espaciales.
- `processing.py`: operaciones por lotes reutilizadas por la CLI.

La selección automática se recalcula al cambiar la posición; los modos
manuales conservan el valor elegido por el usuario.
