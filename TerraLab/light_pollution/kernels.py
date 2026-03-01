"""
kernels.py

Implements distance-decay radial kernels for light propagation modeling.
Supports Gaussian and Power-law kernels.
"""

import numpy as np

def create_gaussian_kernel(sigma_km: float, max_radius_km: float, res_km: float) -> np.ndarray:
    """
    Creates a 2D Gaussian kernel for simulating the dispersion of light pollution.
    
    Args:
        sigma_km (float): The standard deviation of the Gaussian in kilometers.
        max_radius_km (float): The cutoff radius in kilometers.
        res_km (float): The spatial resolution per pixel in kilometers.
        
    Returns:
        np.ndarray: A normalized 2D kernel array.
    """
    halo = int(np.ceil(max_radius_km / res_km))
    n = 2 * halo + 1
    y, x = np.ogrid[-halo:halo+1, -halo:halo+1]
    
    r_km = np.sqrt(x*x + y*y) * res_km
    
    kernel = np.exp(-(r_km**2) / (2.0 * sigma_km**2))
    kernel[r_km > max_radius_km] = 0.0
    
    sum_k = kernel.sum()
    if sum_k > 0:
        kernel /= sum_k
        
    return kernel

def create_power_law_kernel(p: float, r0_km: float, lambda_km: float, max_radius_km: float, res_km: float) -> np.ndarray:
    """
    Creates a 2D power-law kernel typical for atmospheric scattering.
    
    Args:
        p (float): Power exponent.
        r0_km (float): Regularization radius at the origin.
        lambda_km (float): Exponential decay factor (like an extinction length).
        max_radius_km (float): Cutoff radius in km.
        res_km (float): Spatial resolution per pixel.
        
    Returns:
        np.ndarray: A normalized 2D kernel array.
    """
    halo = int(np.ceil(max_radius_km / res_km))
    n = 2 * halo + 1
    y, x = np.ogrid[-halo:halo+1, -halo:halo+1]
    
    r_km = np.sqrt(x*x + y*y) * res_km
    
    part1_base = r0_km / (r_km + r0_km)
    part1 = part1_base ** p
    part2 = np.exp(-r_km / lambda_km) if lambda_km > 0 else 1.0
    
    kernel = part1 * part2
    kernel[r_km > max_radius_km] = 0.0
    
    sum_k = np.nansum(kernel)
    if sum_k > 0:
        kernel /= sum_k
        
    return kernel
