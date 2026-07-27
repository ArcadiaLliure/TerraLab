# Rendiment comú

Política de memòria i execució per a tasques voluminoses.

- `budget.py`: límits derivats de la memòria física.
- `flags.py`: commutadors d'implementació llegits de l'entorn.
- `memory.py`: mesurament portable de memòria.

Els consumidors importen del mòdul fulla corresponent; aquest paquet no és un
agregador dinàmic de noms.
