# Manifiesto de artefactos

Construcción realizada el 26 de julio de 2026 desde Python 3.13.7, sobre el
commit base `98204d4cfd2e16fa6ff39075c72c573d4800cde8` y el parche local
`029710EE61DE6B13634AB40F1244CA0732622F9D58F45B899A9B046E150D7592`.

| Artefacto | Tamaño | SHA-256 | Fecha de construcción |
|---|---:|---|---|
| `dist-modern/terralab-0.1.0-py3-none-any.whl` | 800624 bytes | `03C91C3B291FC6111BAE70875E442EDA64A161800A92F72E854D3372BBE92E51` | 2026-07-26 01:49:00 +02:00 |
| `dist-modern/terralab-0.1.0.tar.gz` | 819663 bytes | `15B470905130E20B8E18D8B41955CCF1398FD661FC303E51F9899A669A929CAA` | 2026-07-26 01:48:52 +02:00 |

## Wheel

- 230 entradas.
- Incluye `terralab-0.1.0.dist-info/licenses/LICENSE`, `METADATA`,
  `entry_points.txt`, `WHEEL` y `RECORD`.
- Metadato de licencia: `License-Expression: MIT`.
- Registra exactamente siete comandos:
  `terralab`, `terralab-query-elevation`, `terralab-gaia`,
  `terralab-dvnl-preprocess`, `terralab-dvnl-convolve`,
  `terralab-predict-sqm` y `terralab-calibrate-sqm`.
- No contiene `tests`, `tools`, `benchmarks`, cachés, `de421.bsp`, GeoTIFF,
  NPZ, CSV personales, notas privadas, rutas absolutas del checkout ni
  entradas de más de 10 MiB.

## Sdist

- 326 entradas.
- Contiene fuentes de `TerraLab`, pruebas, `README.md`, `LICENSE`,
  `pyproject.toml` y metadatos de setuptools.
- No contiene datasets, `de421.bsp`, GeoTIFF, NPZ, CSV personales,
  `TODO_*`, `NOTES.md`, `plans`, cachés ni rutas absolutas.
- No existe ni se usa `setup.py`, `requirements.txt` o `environment.yml`.
  El `setup.cfg` presente en el sdist es metadato generado por setuptools
  durante la construcción.

La salida íntegra de la construcción está en
`validation/10_build.{meta,stdout,stderr}.txt`. La instalación desde cero y
los siete `--help` están en `external_install/`.
