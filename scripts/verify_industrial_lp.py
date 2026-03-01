import os
import sys

# --- COMENTARIOS DE USO ---
# Este script verifica que la ubicación de El Morell (Tarragona) sea detectada
# correctamente como una zona de alta contaminación lumínica (Bortle 8-9).
# Sirve como test de regresión para el Sampler de GeoTIFF y la calibración SQM.
#
# EJECUCIÓN:
# python scripts/verify_industrial_lp.py
# --------------------------

base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(base_path)

try:
    from TerraLab.terrain.light_pollution_sampler import LightPollutionSampler
except ImportError:
    print("Error: Instala las dependencias con 'pip install -r requirements.txt'")
    sys.exit(1)

def main():
    tif_path = os.path.join(base_path, "TerraLab", "data", "light_pollution", "C_DVNL 2022.tif")
    if not os.path.exists(tif_path):
        print(f"Buscando archivo en: {tif_path}")
        print("ERROR: No se encuentra el archivo de contaminación lumínica.")
        return

    # Coordenadas de El Morell (Refinería / Industrial)
    lat, lon = 41.1897, 1.2100
    sampler = LightPollutionSampler(tif_path)
    
    # Simular carga de región completa (como hace el worker)
    sampler.prepare_region(lat, lon, 10.0) # 10km a la redonda
    
    # Calcular SQM y Bortle usando el modelo calibrado
    sqm, bortle = sampler.estimate_zenith_sqm(lat, lon)
    
    print("\n--- RESULTADOS DE VERIFICACIÓN INDUSTRIAL ---")
    print(f"Ubicación: El Morell ({lat}, {lon})")
    print(f"SQM Estimado: {sqm:.2f} mag/arcsec^2")
    print(f"Clase de Bortle: {bortle} (9 = Cielo Urbano Intenso)")
    
    if bortle >= 8:
        print("\n[V] ÉXITO: Tarragona/El Morell identificado correctamente.")
    else:
        print("\n[X] ERROR: El cálculo sigue siendo erróneo (> Bortle 7 esperado).")

if __name__ == "__main__":
    main()
