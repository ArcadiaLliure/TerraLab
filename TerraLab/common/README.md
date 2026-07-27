# Infraestructura comuna

Elements primitius compartits que no pertanyen a un domini científic concret.
Utilitats transversals, configuració i classes base compartides per tota l'aplicació.

- `cache.py`: única implementació de `ByteLRU`.
- `cancellation.py`: generacions i cancel·lació cooperativa.
- `data_library.py`: arrel i disposició de la biblioteca de l'usuari.
- `app_paths.py`: resolució de rutes sense escriptures en importar.
- `performance/`: pressupost, flags i memòria.
- `exception_reporting.py`: diagnòstic d'operacions best-effort no fatals.
Aquest paquet agrupa la lògica que no pertany a un domini científic o d'interfície específic, però que és fonamental per al funcionament coordinat de la resta de mòduls.

Eviteu convertir aquest paquet en un contenidor de dependències de la UI.
## Responsabilitats clau

- **Configuració**: Lectura i escriptura de la configuració global de l'aplicació (`config.py`, `utils.py`).
- **Gestió de rutes**: Localització de la biblioteca de dades i altres directoris clau (`data_library.py`).
- **Classes base de la UI**: `CustomWidgetBase` (`custom_widget_base.py`) proporciona la funcionalitat comuna per a tots els ginys flotants, incloent-hi el sistema de temes, el redimensionament, l'arrossegament i els controls de finestra.
- **Traduccions**: Càrrega i accés al sistema d'internacionalització (`utils.py`).
- **Cancel·lació**: Mecanismes per a la cancel·lació cooperativa de tasques en segon pla.
- **Informe d'excepcions**: Utilitats per registrar errors de manera segura sense interrompre l'aplicació (`exception_reporting.py`).

## Principis de disseny

El codi de `common` ha de ser agnòstic respecte als detalls d'implementació dels paquets que l'utilitzen. Per exemple, `CustomWidgetBase` no coneix la lògica interna dels ginys d'astronomia o terreny; simplement els proporciona un marc de finestra.
