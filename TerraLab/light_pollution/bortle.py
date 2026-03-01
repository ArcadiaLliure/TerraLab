"""
bortle.py

Maps SQM readings to the Bortle Dark-Sky Scale.
"""

def sqm_to_bortle_class(sqm: float) -> int:
    """
    Converts a Zenith Sky Quality Meter (SQM) reading (mag/arcsec^2) 
    to a Bortle class ranging from 1 to 9.
    
    Args:
        sqm (float): The sky brightness in mag/arcsec^2.
        
    Returns:
        int: The integer Bortle class (1=Excellent, 9=Inner City).
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
