import os
import sys
import numpy as np

# Add TerraLab to path
sys.path.append(r'e:\Desarrollo\TerraLab')

from TerraLab.terrain.engine import HorizonBaker, TileIndex, TileCache, DemSampler
from TerraLab.terrain.light_pollution_sampler import LightPollutionSampler
from pyproj import Transformer
import math

def main():
    obs_lat, obs_lon = 41.979239, 0.750917
    tiles_dir = r"E:\Desarrollo\TerraLab\data\dem"
    tiff_path = r"e:\Desarrollo\TerraLab\TerraLab\data\light_pollution\C_DVNL 2022.tif"
    
    sampler = LightPollutionSampler(tiff_path)
    sampler.initialize()
    
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:25831", always_xy=True)
    x_utm, y_utm = transformer.transform(obs_lon, obs_lat)
    
    sampler.prepare_region(obs_lat, obs_lon, 100000.0, x_utm, y_utm)
    
    class MockProvider:
        def __init__(self, sampler):
            self.sampler = sampler
        def get_elevation(self, x, y):
            val = self.sampler.sample(x, y)
            return val if val is not None else 200.0

    # Build DEM system
    idx = TileIndex(tiles_dir)
    cache = TileCache(capacity=100)
    dem_sampler = DemSampler(idx, cache)
    provider = MockProvider(dem_sampler)
    
    baker = HorizonBaker(provider, eye_height=1.7)
    
    ground_h = provider.get_elevation(x_utm, y_utm)
    if ground_h is None:
        ground_h = 200.0

        
    print(f"Baking with Observer at {x_utm:.1f}, {y_utm:.1f}, elevation {ground_h:.1f}m...")
    
    azimuths, bands, light_domes, dists = baker.bake(
        obs_x=x_utm, obs_y=y_utm, obs_h_ground=ground_h,
        step_m=50, d_max=100000, delta_az_deg=0.5,
        light_sampler=sampler
    )
    
    with open(r"e:\Desarrollo\TerraLab\TerraLab\clean_peaks.txt", "w", encoding="utf-8") as f:
        f.write("Overlay Clustering Simulator:\n")
        
        pending_domes = []
        n = len(light_domes)
        for i in range(n):
            val = light_domes[i]
            is_peak = True
            for j in range(-20, 21):
                if light_domes[(i + j) % n] > val:
                    is_peak = False
                    break
            if is_peak and val > 10.0:
                pending_domes.append({'idx': i, 'val': val, 'dist': dists[i]})
                
        f.write(f"Raw Peak Count: {len(pending_domes)}\n")
        
        if pending_domes:
            clustered = []
            sorted_by_intensity = sorted(pending_domes, key=lambda x: light_domes[x['idx']], reverse=True)
            used_indices = set()
            
            for d in sorted_by_intensity:
                if d['idx'] in used_indices: continue
                center_az = azimuths[d['idx']]
                clustered.append(d)
                used_indices.add(d['idx'])
                
                for other in sorted_by_intensity:
                    if other['idx'] in used_indices: continue
                    other_az = azimuths[other['idx']]
                    diff = abs(other_az - center_az) % 360
                    if diff > 180: diff = 360 - diff
                    if diff < 15.0:
                        used_indices.add(other['idx'])
            
            pending_domes = sorted(clustered, key=lambda x: x['dist'], reverse=True)
            
        f.write(f"Clustered Peak Count: {len(pending_domes)}\n\n")
        
        for p in pending_domes:
            az = azimuths[p['idx']]
            rad_val = light_domes[p['idx']]
            dist = p['dist']
            f.write(f"Azimuth: {az:.1f}, Radiance Sum: {rad_val:.2f}, Peak Dist: {dist:.0f}m\n")
            
        # Check max horizon elevation angle
        angles = []
        for b in bands:
            angles.append(b['angles'])
        max_angles = np.max(angles, axis=0) # [n_az]
        max_deg = np.degrees(np.max(max_angles))
        mean_deg = np.degrees(np.mean(max_angles))
        
        f.write(f"\nTerrain Elevation Stats:\n")
        f.write(f"Max Horizon Angle: {max_deg:.2f} deg\n")
        f.write(f"Mean Horizon Angle: {mean_deg:.2f} deg\n")
        
        # Check angle at azimuths 0, 90, 180, 270 (N, E, S, W)
        for az in [0, 90, 180, 270]:
            idx = int((az / 360.0) * len(azimuths))
            f.write(f"Horizon Angle at {az} deg: {np.degrees(max_angles[idx]):.2f} deg\n")
            
        sampler.close()

if __name__ == "__main__":
    main()
