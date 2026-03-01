import os
import sys
import numpy as np
import math

# --- COMENTARIOS DE USO ---
# Este script escanea los alrededores de una ubicación para encontrar las cúpulas de luz
# (light domes) más brillantes en el horizonte. Útil para verificar si los núcleos
# urbanos cercanos están siendo detectados por el sampler de GeoTIFF.
#
# EJECUCIÓN:
# python scripts/check_lp_domes.py
# --------------------------

# Añadir el raíz del proyecto al path
base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(base_path)

try:
    from pyproj import Transformer
    from TerraLab.terrain.light_pollution_sampler import LightPollutionSampler
except ImportError:
    print("Error: Asegúrate de instalar las dependencias con 'pip install -r requirements.txt'")
    sys.exit(1)

def main():
    # Ubicación de prueba (Àger / Montsec)
    obs_lat, obs_lon = 41.18979501145245, 1.2100584319384848
    
    tiff_path = os.path.join(base_path, "TerraLab", "data", "light_pollution", "C_DVNL 2022.tif")
    if not os.path.exists(tiff_path):
        print(f"Error: No se encuentra el archivo DVNL en {tiff_path}")
        return

    sampler = LightPollutionSampler(tiff_path)
    
    print(f"--- ESCANEO DE CONTAMINACIÓN ---")
    print(f"Observador: {obs_lat}, {obs_lon}")
    
    # Preparar región (50km de radio para el escaneo)
    sampler.prepare_region(obs_lat, obs_lon, 50.0)
    
    # Transformador para movernos en metros (UTM 31N para Cataluña/Aragón)
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:32631", always_xy=True)
    x_utm, y_utm = transformer.transform(obs_lon, obs_lat)
    inv_transformer = Transformer.from_crs("EPSG:32631", "EPSG:4326", always_xy=True)

    peaks = []
    print("\nEscaneando 360 grados...")
    
    for az_test in range(0, 360, 10):
        max_rad = 0
        peak_d = 0
        for d in range(2000, 50000, 2000): # De 2km a 50km
            # Calcular punto remoto en UTM
            px = x_utm + d * math.sin(math.radians(az_test))
            py = y_utm + d * math.cos(math.radians(az_test))
            
            # Convertir a lat/lon para el sampler
            plon, plat = inv_transformer.transform(px, py)
            rad = sampler.get_radiance(plat, plon)
            
            if rad > max_rad:
                max_rad = rad
                peak_d = d
        
        if max_rad > 0.1:
            peaks.append((az_test, peak_d, max_rad))
    
    peaks.sort(key=lambda x: x[2], reverse=True)
    print("\nPrincipales focos de luz detectados:")
    for p in peaks[:8]:
        print(f"Azimut: {p[0]:3d}° | Distancia: {p[1]/1000:4.1f}km | Radiancia: {p[2]:.2f}")

if __name__ == "__main__":
    main()
