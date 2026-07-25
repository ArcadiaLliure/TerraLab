# Rendimiento común

Política de memoria y ejecución para trabajos voluminosos.

- `budget.py`: límites derivados de la memoria física.
- `flags.py`: conmutadores de implementación leídos del entorno.
- `memory.py`: medición portable de memoria.

Los consumidores importan del módulo hoja correspondiente; este paquete no es
un agregador dinámico de nombres.
