# Workers de la UI

Adaptadors `QObject` per a la càrrega asíncrona de catàlegs i efemèrides.

Els workers emeten resultats mitjançant senyals i no construeixen ginys. NumPy
i Skyfield són dependències obligatòries declarades a `pyproject.toml`; els
errors d'instal·lació no s'oculten amb alternatives incompletes.
