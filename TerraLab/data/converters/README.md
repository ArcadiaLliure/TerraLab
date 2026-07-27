# Convertidors de dades

Convertidors explícits de productes científics a formats d'execució.

Les dependències pesants, com Astropy per a FITS, es carreguen quan s'invoca la
conversió i no quan s'importa `TerraLab`. Els resultats han de ser
deterministes, versionats i segurs de llegir sense pickle.
