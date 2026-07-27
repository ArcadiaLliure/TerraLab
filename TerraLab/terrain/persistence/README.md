# Persistència del terreny

Serialització versionada de perfils i malles.

`profile_npz.py` valida l'esquema i carrega matrius amb `allow_pickle=False`.
Les escriptures es publiquen de manera atòmica per no deixar perfils parcials
després d'una cancel·lació o una fallada.
