# TerraLab performance baselines

Run the reproducible CPU and bounded-memory suite from the repository parent:

```powershell
$env:PYTHONPATH='.'
python benchmarks/benchmark_optimization.py `
  --output benchmarks/performance_current.json
```

`performance_baseline.json` records the historical measurements. The generated
`performance_current.json` records the vectorized 30 m, 5 m and 1 m terrain
cases plus streamed catalog workloads of 62k, 1M, 43.6M and 157.7M rows. A
real HEALPix store can be measured with `--catalog <catalog-or-directory>`;
the first query is the process-cold measurement and the last repeat is warm.
Synthetic cases run three times by default and report the median elapsed time
plus every raw timing in `repeat_elapsed_s`; use `--repeats` to change it.

The optimized runtime is controlled by independent rollback variables, all
enabled by default:

- `TERRALAB_RASTER_BATCH`
- `TERRALAB_RAYCAST_VECTORIZED`
- `TERRALAB_RELIEF_CACHED`
- `TERRALAB_GAIA_OUT_OF_CORE`

Set any variable to `0`, `false`, `no`, or `off` before starting TerraLab to
disable only that backend. An internal raycast process pool is intentionally
not enabled: the 5 m pure-CPU benchmark is below the 2 s gate, so another pool
would not meet the required 20% improvement/memory criterion.

Runtime phase metrics are appended as JSONL to `logs/terralab_perf.log` and
include sample counts, elapsed time, raster bytes read and block-cache hits and
misses. Renderer diagnostics include the final star draw-call count.
