# Evidència visual de la fase 05

Els tests automàtics comparen el buffer ARGB premultiplicat complet entre
`legacy` i `scene` per dos estats deterministes: wide field i scope. També es
comparen els índexs de pick, coordenades projectades i counters de selecció.
Tots els bytes coincideixen.

La comprovació manual continua necessària perquè el test offscreen no pot
avaluar perceptivament la continuïtat de pan/zoom, el canvi de catàleg ni la
lectura de halos sota configuracions d'instrument reals.
