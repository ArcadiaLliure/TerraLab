# TerraLab

TerraLab es una aplicación científica de escritorio que combina un cielo
astronómico interactivo con terreno real, cobertura del suelo y estimaciones
de contaminación lumínica. El observador se sitúa en coordenadas geográficas
concretas; desde ahí la aplicación calcula el horizonte visible a partir de un
modelo digital de elevaciones y compone el resultado con catálogos y
efemérides astronómicas.

> Estado del proyecto: desarrollo activo. Las interfaces de datos y los
> formatos de caché pueden evolucionar antes de la primera versión estable.

## Funciones principales

- Horizonte de 360 grados y relieve proyectado a partir de DEM.
- Superficie independiente de la geometría: ortofoto RGB o cobertura del
  suelo categórica.
- Cielo con Gaia, objetos de cielo profundo, planetas, Sol, Luna y satélites.
- Modos visual y telescópico con magnitud límite configurable.
- Estimación automática de contaminación lumínica mediante rásteres DVNL/SQM.
- Descarga o enlace de recursos científicos desde una biblioteca elegida por
  el usuario.
- Procesamiento por lotes, cachés acotadas por bytes y cancelación cooperativa
  para conjuntos de datos grandes.

## Requisitos

- Python 3.10, 3.11, 3.12 o 3.13.
- Un entorno de escritorio compatible con Qt 5.
- Espacio adicional para DEM, Gaia, ortofotos y productos derivados. Estos
  datos no forman parte del paquete Python.

Las dependencias de ejecución están declaradas únicamente en
[`pyproject.toml`](pyproject.toml).

## Instalación para desarrollo

```bash
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

En Linux o macOS, la activación equivalente es:

```bash
source .venv/bin/activate
```

## Ejecución

```bash
python -m TerraLab
```

La instalación también registra el comando:

```bash
terralab
```

En el primer arranque, el asistente permite elegir una biblioteca de datos y
configurar los recursos disponibles. También se puede fijar la raíz antes de
arrancar:

```powershell
$env:TERRALAB_DATA_ROOT = "D:\TerraLabData"
python -m TerraLab
```

## Biblioteca de datos

TerraLab separa el código de los datos científicos. La biblioteca contiene el
catálogo tipado de fuentes, descargas reanudables, datos administrados y
cachés derivadas:

```text
<biblioteca>/
├── config/data_sources.json
├── data/earth/elevation/
├── data/earth/surface/
├── data/earth/light-pollution/
├── data/gaia/
├── data/sky/
├── downloads/
├── cache/
└── logs/
```

Los archivos enlazados desde fuera de la biblioteca siguen siendo propiedad
del usuario. Las operaciones de borrado distinguen entre datos administrados y
fuentes externas.

## Arquitectura

```text
TerraLab/
├── astro/              cálculos y búsqueda astronómica
├── common/             configuración, caché, cancelación y utilidades
├── data/               biblioteca, recursos y catálogos
├── scene/              estado y proyección independientes de Qt
├── render/             renderizadores del cielo y adaptadores Qt
├── terrain/            dominio, proveedores, raycast, superficie y render
├── ui/                 composición de widgets, diálogos y workers
├── widgets/            controles interactivos reutilizables
└── cli/                comandos no gráficos
```

Los límites relevantes se verifican automáticamente: el dominio de terreno no
depende de Qt, los módulos de datos no dependen de la UI, el grafo interno no
tiene ciclos y existe una única propiedad del worker de horizonte. Las
decisiones se documentan en
[`docs/architecture/decisions`](docs/architecture/decisions).

## Herramientas de línea de comandos

Tras instalar el proyecto están disponibles:

- `terralab-query-elevation`
- `terralab-gaia`
- `terralab-dvnl-preprocess`
- `terralab-dvnl-convolve`
- `terralab-predict-sqm`
- `terralab-calibrate-sqm`

Las herramientas de desarrollo e inventario viven en [`tools`](tools) y los
benchmarks reproducibles en [`benchmarks`](benchmarks).

## Calidad

```bash
python -m ruff check TerraLab scripts tests benchmarks tools/dev
python -m pytest
python tools/dev/check_pyright_baseline.py
python -m vulture TerraLab scripts --min-confidence 70
python tools/dev/code_inventory.py
```

Las pruebas de arquitectura se encuentran en
[`tests/architecture`](tests/architecture).
El chequeo de Pyright es incremental y cualquier aumento respecto al baseline
vigente hace fallar el comando.

## Documentación

- [Paquete Python](TerraLab/README.md)
- [Subsistema de terreno](TerraLab/terrain/README.md)
- [Pipeline de superficie categórica](docs/categorical-surface-pipeline.md)
- [Roadmap](docs/roadmap.md)
- [Changelog](CHANGELOG.md)

## Fuentes y atribución de datos

TerraLab puede trabajar con datos de Gaia/ESA, efemérides JPL, OpenNGC,
EU-DEM y Copernicus, S2GLC, productos de luces nocturnas y servicios de MET
Norway o CAMS. Cada recurso conserva su procedencia, atribución y nota de
licencia en el manifiesto de la biblioteca. La licencia MIT de este repositorio
no sustituye las condiciones de los conjuntos de datos incorporados por el
usuario.

## Contribución

Antes de proponer un cambio:

1. Mantén las dependencias en `pyproject.toml`.
2. No añadas binarios científicos ni cachés generadas al repositorio.
3. Respeta las fronteras verificadas en `tests/architecture`.
4. Añade pruebas de regresión para cambios funcionales.
5. Actualiza el README del paquete afectado y el changelog.

## Licencia

El código de TerraLab se distribuye bajo la licencia
[MIT](LICENSE). Los datos científicos mantienen sus licencias originales.
