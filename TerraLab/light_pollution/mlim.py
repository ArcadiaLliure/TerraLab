"""
mlim.py

Calculates limiting magnitude given an observer's Bortle class and the
altitude angle of the observed object.
"""

import numpy as np

def calculate_mlim(bortle_class: int, h_deg: float, k: float = 0.25) -> float:
    """
    Calculates the visual limiting magnitude for an observer, adjusted 
    for the altitude angle of the observation.
    
    Args:
        bortle_class (int): Computed Bortle class (1-9).
        h_deg (float): Altitude angle above the horizon in degrees.
        k (float): Atmospheric extinction coefficient (typically 0.15 - 0.45).
                   Default is 0.25 for typical conditions.
                   
    Returns:
        float: Estimated limiting magnitude at that altitude.
    """
    # Ensure numerical stability at horizons
    h_deg = np.clip(h_deg, 10.0, 90.0)
    
    B_index = bortle_class - 1
    m_lim_zenith = 7.6 - 0.5 * B_index
    
    h_rad = np.radians(h_deg)
    m_lim_h = m_lim_zenith - k * (1.0 / np.sin(h_rad) - 1.0)
    
    return float(m_lim_h)

def calculate_mlim_from_sqm(sqm: float, h_deg: float, k: float = 0.25) -> float:
    """
    Convenience wrapper converting SQM to Limiting Magnitude at altitude h.
    
    Args:
        sqm (float): Sky brightness in mag/arcsec^2.
        h_deg (float): Altitude angle in degrees.
        k (float): Atmospheric extinction coefficient.
        
    Returns:
        float: Estimated limiting magnitude.
    """
    from .bortle import sqm_to_bortle_class
    b = sqm_to_bortle_class(sqm)
    return calculate_mlim(b, h_deg, k)
