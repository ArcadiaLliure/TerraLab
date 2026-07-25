# Adaptadores Qt de render

Tipos que conectan el núcleo de escena con `QPainter`.

`context.py` contiene el `RenderContext` canónico. Este paquete puede depender
de Qt; `TerraLab.scene` no puede depender de él ni de la UI.
