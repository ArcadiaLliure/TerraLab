# Migració de TerraLab cap a MVC i render desacoblat

Aquest directori és el paquet de direcció tècnica per migrar TerraLab sense
introduir canvis funcionals en el moment de l'auditoria. No conté cap
implementació. Està ancorat a l'arbre de `origin/refactor`
(`4352e35f588a75b1e0fea8b71e3fce6801be53bf`), idèntic al merge
`1fbcf088a0bfc1f832fc0f2a8ba2808e3e783a7d`, observat el 28 de juliol de
2026. Durant l'auditoria el checkout va canviar externament de `refactor` a
`fromcpu_togpu`; no es va canviar cap branca com a part d'aquest treball.

Ordre de lectura i ús:

1. [`architecture.md`](architecture.md): auditoria basada en el codi,
   arquitectura objectiu, contractes, decisions i política de rendiment.
2. [`mvc-file-map.md`](mvc-file-map.md): classificació normativa i inequívoca
   de cada fitxer com a Model, Vista, Controlador o infraestructura fora de MVC.
3. [`migration-plan.md`](migration-plan.md): seqüència completa de vint-i-dues fases,
   dependències, fitxers, riscos, proves i criteris d'acceptació.
4. [`prompts/00_ORCHESTRATOR.md`](prompts/00_ORCHESTRATOR.md): prompt mestre
   que governa l'execució i impedeix avançar sense evidència.
5. `prompts/01_*.md` fins a `prompts/22_*.md`: prompts autònoms per executar
   una sola fase cada vegada.

## Regles d'ús

- El prompt orquestrador és l'únic que pot autoritzar el pas de fase.
- Cada fase s'executa en una branca o commit identificable i ha d'acabar amb
  TerraLab instal·lable, executable i verificable visualment.
- Un prompt de fase no hereta context conversacional: abans d'actuar torna a
  inspeccionar el repositori, el `HEAD`, l'estat del working tree i els
  prerequisits.
- Els fitxers previs de l'usuari no es netegen, mouen ni reescriuen.
- Cada fitxer de producte ha de tenir exactament un rol al mapa MVC. Els
  fitxers tècnics es marquen `OUTSIDE_MVC` amb subrol concret; `MIXED` és deute
  transitori i ha d'arribar a zero. Cap fitxer pot quedar sense classificar.
- La línia base actual no és verda: hi ha una prova d'arquitectura fallida i
  quinze errors de Ruff. La fase 01 els ha de caracteritzar i resoldre o
  registrar com a excepció temporal aprovada; cap extracció comença abans.
- El canvi estable de backend es fa amb una selecció única de composició
  (`qpainter`, `threejs`, `opengl`, etc.). Els flags per capa només són
  bastides reversibles de migració i s'eliminen al tancament.

## Resultat esperat

En l'estat final, el Model genera dades científiques i estructures d'escena
tipades sense importar Qt ni cap API gràfica. El Controlador coordina casos
d'ús i publica frames d'escena. La Vista tradueix entrades d'usuari i presenta
sortides. Els backends implementen el port de render propietat de la capa
d'aplicació. Substituir QPainter per Three.js no modifica cap càlcul
astronòmic, fotomètric, geoespacial, de càmera, raycast o terreny.
