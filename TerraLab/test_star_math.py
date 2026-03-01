import numpy as np
import math

def simulate_stars(bortle=1, mag_limit=6.0, elev=0.0):
    # Create fake stars evenly distributed
    idx_list = []
    f_alt = []
    f_az = []
    f_mag = []
    
    for alt in range(0, 90, 5):
        for az in range(0, 360, 5):
            idx_list.append(len(f_alt))
            f_alt.append(alt)
            f_az.append(az)
            f_mag.append(4.0) # Bright star

    f_alt = np.array(f_alt, dtype=np.float32)
    f_az = np.array(f_az, dtype=np.float32)
    f_mag = np.array(f_mag, dtype=np.float32)
    
    # 2. Filtering
    sun_alt = -30.0 # Deep night
    
    local_limit = np.full(len(f_alt), mag_limit)
    
    limit_buffer = local_limit + 1.0 
    mask = f_mag < limit_buffer
    
    f_alt = f_alt[mask]
    f_az = f_az[mask]
    f_mag = f_mag[mask]
    f_ll = local_limit[mask]

    # Projection
    azimuth_offset = 180.0
    alt_rad = np.radians(f_alt)
    az_rel_deg = f_az - azimuth_offset
    az_rel_rad = np.radians(az_rel_deg)
    
    cos_alt = np.cos(alt_rad)
    sin_alt = np.sin(alt_rad)
    cos_az = np.cos(az_rel_rad)
    sin_az = np.sin(az_rel_rad)
    
    denom = 1.0 + cos_alt * cos_az
    mask_proj = denom > 1e-6
    
    denom = denom[mask_proj]
    cos_alt = cos_alt[mask_proj]
    sin_alt = sin_alt[mask_proj]
    sin_az = sin_az[mask_proj]
    f_alt = f_alt[mask_proj]
    f_az = f_az[mask_proj]
    f_mag = f_mag[mask_proj]
    f_ll = f_ll[mask_proj]
    
    k = 2.0 / denom
    x = k * cos_alt * sin_az
    y = k * sin_alt
    
    w, h = 1920, 1080
    zoom_level = 1.0
    scale_h = h / 2.0 * zoom_level
    cx = w/2.0
    cy_base = h/2.0
    elev_rad = math.radians(elev)
    y_center_val = 2.0 * math.tan(elev_rad / 2.0)
    
    sx = cx + x * scale_h
    sy = cy_base - (y - y_center_val) * scale_h
    
    mask_screen = (sx > -50) & (sx < w+50) & (sy > -50) & (sy < h+50)
    
    f_alt_on = f_alt[mask_screen]
    sx_on = sx[mask_screen]
    sy_on = sy[mask_screen]
    f_ll_on = f_ll[mask_screen]
    f_mag_on = f_mag[mask_screen]
    
    diff = f_ll_on - f_mag_on
    fade_in = np.clip(diff * 2.0, 0.0, 1.0)
    eff_alpha = np.where(f_mag_on < 2.0, np.sqrt(fade_in), fade_in)
    mask_vis = eff_alpha > 0.01
    
    print(f"Total initially: {len(mask)}")
    print(f"On screen (elev={elev}): {len(sx_on)}")
    print(f"Visible (Alpha > 0): {np.sum(mask_vis)}")
    
    # Check what altitudes are visible on screen
    if len(f_alt_on[mask_vis]) > 0:
        print(f"Visible altitudes: Min={np.min(f_alt_on[mask_vis]):.1f}, Max={np.max(f_alt_on[mask_vis]):.1f}")
        # Count stars below 15 degrees
        low_stars = np.sum(f_alt_on[mask_vis] < 15.0)
        print(f"Stars below 15deg (horizon): {low_stars}")

print("Simulation looking at Horizon:")
simulate_stars(elev=0.0)

print("\nSimulation looking at Zenith:")
simulate_stars(elev=90.0)
