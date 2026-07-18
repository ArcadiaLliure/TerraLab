"""
bortle.py

Mapeja lectures d'SQM a l'escala de cel fosc de Bortle.
"""


def sqm_to_bortle_class(sqm: float) -> int:
    """
    Converteix una lectura del Sky Quality Meter zenital (mag/arcsec^2)
    a una classe Bortle entre 1 i 9.

    Paràmetres:
    - sqm (float): Brillantor del cel en mag/arcsec^2.

    Retorna:
    - int: Classe Bortle sencera (1 = excel·lent, 9 = centre urbà).
    """
    if sqm >= 21.99:
        return 1
    elif sqm >= 21.89:
        return 2
    elif sqm >= 21.69:
        return 3
    elif sqm >= 20.49:
        return 4
    elif sqm >= 19.50:
        return 5
    elif sqm >= 18.94:
        return 6
    elif sqm >= 18.38:
        return 7
    elif sqm >= 17.80:
        return 8
    else:
        return 9
