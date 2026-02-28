import os
import rasterio
import math
import numpy as np
from typing import Optional

class LightPollutionSampler:
    """
    Reads the VIIRS Light Pollution map (GeoTIFF) to compute the perceived 
    radiance and estimate Bortle classes or Light Dome intensities.
    Robust version: avoids pyproj to prevent crashes on shared DLLs on Windows.
    Uses linear local approximation for ROI sampling directly from UTM coords.
    """
    def __init__(self, tiff_path: str):
        self.tiff_path = tiff_path
        self.dataset = None
        
        # ROI Caching
        self.cached_data = None
        self.cached_window = None
        self.cached_transform = None
        self.cached_inv_transform = None
        
        # Local linear approximation params (UTM 31N -> Native)
        self.center_x_utm = 0.0
        self.center_y_utm = 0.0
        self.native_cx = 0.0
        self.native_cy = 0.0
        self.kx_utm = 0.0 # dNativeX / dUTMX
        self.ky_utm = 0.0 # dNativeY / dUTMY

    def initialize(self):
        if not os.path.exists(self.tiff_path):
            raise FileNotFoundError(f"Light pollution dataset not found: {self.tiff_path}")
        print(f"[LightPollutionSampler] Opening {self.tiff_path}...", flush=True)
        self.dataset = rasterio.open(self.tiff_path)
        print(f"[LightPollutionSampler] Dataset CRS: {self.dataset.crs}", flush=True)

    def close(self):
        if self.dataset:
            self.dataset.close()
            self.dataset = None
        self.cached_data = None

    def prepare_region(self, lat: float, lon: float, radius_m: float, x_utm: float, y_utm: float):
        """
        Calculates and loads a Region of Interest (ROI) into memory.
        Uses a local linear approximation for fast UTM -> (x,y) mapping in the loop.
        """
        if not self.dataset:
            return
            
        try:
            from rasterio.warp import transform
            print(f"[LightPollutionSampler] Pre-loading ROI for UTM ({x_utm:.1f}, {y_utm:.1f}) r={radius_m/1000}km...", flush=True)
            
            self.center_x_utm = x_utm
            self.center_y_utm = y_utm
            
            # 1. Exact transform of center and two offset points to get local scaling from UTM 31N
            utms_x = [x_utm, x_utm + 1000.0, x_utm]
            utms_y = [y_utm, y_utm, y_utm + 1000.0]
            
            # We assume UTM Zone 31N (EPSG:32631) or matching the observer's UTM
            xs, ys = transform("EPSG:32631", self.dataset.crs, utms_x, utms_y)
            
            self.native_cx = xs[0]
            self.native_cy = ys[0]
            self.kx_utm = (xs[1] - xs[0]) / 1000.0
            self.ky_utm = (ys[2] - ys[0]) / 1000.0
            
            print(f"[LightPollutionSampler]   - Scale Native/UTM: kx={self.kx_utm:.4f}, ky={self.ky_utm:.4f}", flush=True)
            
            # 2. Determine bounds for the window (using the corners in native CRS)
            # Find bounds in WGS84 first to be safe about coverage
            dlat = radius_m / 111320.0
            dlon = radius_m / (111320.0 * math.cos(math.radians(max(-85, min(85, lat)))))
            c_lons = [lon - dlon, lon + dlon]
            c_lats = [lat - dlat, lat + dlat]
            c_xs, c_ys = transform("EPSG:4326", self.dataset.crs, c_lons, c_lats)
            
            left, right = min(c_xs), max(c_xs)
            bottom, top = min(c_ys), max(c_ys)
            
            from rasterio.windows import from_bounds
            window = from_bounds(left, bottom, right, top, transform=self.dataset.transform)
            
            # Intersect with dataset
            ds_win = rasterio.windows.Window(0, 0, self.dataset.width, self.dataset.height)
            window = window.intersection(ds_win).round_lengths().round_offsets()
            
            if window.width > 0 and window.height > 0:
                print(f"[LightPollutionSampler]   - Reading {window.width}x{window.height} window...", flush=True)
                self.cached_data = self.dataset.read(1, window=window)
                self.cached_transform = self.dataset.window_transform(window)
                self.cached_inv_transform = ~self.cached_transform
                self.cached_window = window
                print(f"[LightPollutionSampler] ROI Cached successfully.", flush=True)
            else:
                print("[LightPollutionSampler] ROI is outside dataset bounds.", flush=True)
                self.cached_data = None

        except Exception as e:
            print(f"[LightPollutionSampler] Error in prepare_region: {e}", flush=True)
            import traceback
            traceback.print_exc()
            self.cached_data = None

    def get_radiance_utm(self, x: float, y: float) -> float:
        """Ultrafast sampling using linear approximation relative to UTM center."""
        if self.cached_data is None:
            return 0.0
        try:
            dx = x - self.center_x_utm
            dy = y - self.center_y_utm
            
            nx = self.native_cx + dx * self.kx_utm
            ny = self.native_cy + dy * self.ky_utm
            
            # Transform to local array (row, col)
            # The ~affine transform is a fast multiplication
            col_f, row_f = self.cached_inv_transform * (nx, ny)
            r, c = int(row_f), int(col_f)
            
            rows, cols = self.cached_data.shape
            if 0 <= r < rows and 0 <= c < cols:
                val = float(self.cached_data[r, c])
                return val if val >= 0 else 0.0
            return 0.0
        except Exception:
            return 0.0

    def get_radiance(self, lat: float, lon: float) -> Optional[float]:
        """Slow fallback or UI lookup. Uses rasterio.warp.transform directly."""
        if not self.dataset:
            return None
        try:
            from rasterio.warp import transform
            xs, ys = transform("EPSG:4326", self.dataset.crs, [lon], [lat])
            row, col = self.dataset.index(xs[0], ys[0])
            if 0 <= row < self.dataset.height and 0 <= col < self.dataset.width:
                window = rasterio.windows.Window(col, row, 1, 1)
                data = self.dataset.read(1, window=window)
                if data is not None and data.size > 0:
                    val = float(data[0, 0])
                    return val if val >= 0 else 0.0
            return 0.0
        except Exception:
            return 0.0

    # TODO: Revisar esta fórmula de cálculo del índice de Bortle.
    # Los valores de radiancia (nW/cm2/sr) obtenidos del satélite (VIIRS) indican la luz emitida hacia arriba,
    # y la conversión empírica que se hace a clase de Bortle (brillo del cielo observado desde el suelo)
    # produce valores inexactos.
    # Por ejemplo, en las coordenadas (41.9792, 0.750917) se calcula un Bortle de 5, 
    # mientras que según mapas más precisos (ej: lightpollutionmap.info, atlas 2015 Falchi) es de clase 3.
    # Habría que ajustar los umbrales o usar otro modelo de conversión.
    # TODO: Metodo obsoleto. Las estimadas por pixel individual ignoran la cúpula de luz.
    # Se preserva para retrocompatibilidad, pero se recomienda usar estimate_bortle_from_location.
    def estimate_bortle_class(self, radiance: float) -> int:
        if radiance is None or radiance <= 0.03:
            return 2
        elif radiance <= 0.4:
            return 3
        elif radiance <= 1.5:
            return 4
        elif radiance <= 5.0:
            return 5
        elif radiance <= 15.0:
            return 6
        elif radiance <= 30.0:
            return 7
        elif radiance <= 50.0:
            return 8
        else:
            return 9

    def get_effective_radiance(self, lat: float, lon: float, T: float = 1.0) -> float:
        """
        Calcula la radiancia efectiva como convolución espacial en un radio de 100km.
        R_eff(x) = sum_{y in 100km} R(y) * e^{-d(x,y)/L}
        donde:
        - R(y): radiancia de píxel individual del VIIRS.
        - d(x,y): distancia geodésica en km.
        - L = 18 * T: factor de atenuación exponencial (T = transparencia atmosférica).
        Este enfoque permite capturar cúpulas de luz remotas y evita que localizaciones rurales
        alejadas de núcleos urbanos se marquen como brillantes si sólo tienen un halo tenue,
        y al revés, evita falsos rurales oscuros cerca de grandes ciudades.
        """
        if not self.dataset:
            return 0.0
            
        radius_m = 100000.0 # 100 km interpolación para la "cúpula"
        # Se ha ajustado L empíricamente para reflejar mejor que el skyglow
        # decae mucho más agresivamente que una exponencial pura de 18km (se ha reducido a 4.0km).
        # Esto previene que una ciudad lejana a 50km envíe un 6% de su luz (sobre-iluminando las zonas rurales).
        L = 4.0 * T      # Parámetro L en km
        
        try:
            from rasterio.warp import transform
            from rasterio.windows import from_bounds
            import rasterio
            
            # 1. Definir la ventana basada en un radio de 60km (suficiente con un L mucho menor)
            radius_m = 60000.0
            dlat = radius_m / 111320.0
            dlon = radius_m / (111320.0 * math.cos(math.radians(max(-85.0, min(85.0, lat)))))
            
            c_lons = [lon - dlon, lon + dlon]
            c_lats = [lat - dlat, lat + dlat]
            c_xs, c_ys = transform("EPSG:4326", self.dataset.crs, c_lons, c_lats)
            
            left, right = min(c_xs), max(c_xs)
            bottom, top = min(c_ys), max(c_ys)
            
            ds_win = rasterio.windows.Window(0, 0, self.dataset.width, self.dataset.height)
            window = from_bounds(left, bottom, right, top, transform=self.dataset.transform)
            window = window.intersection(ds_win).round_lengths().round_offsets()
            
            if window.width <= 0 or window.height <= 0:
                return 0.0
                
            # 2. Extraer datos con la ventana
            data = self.dataset.read(1, window=window)
            transform_win = self.dataset.window_transform(window)
            
            # 3. Crear malla paramétrica de coordenadas
            rows, cols = data.shape
            c_grid = np.arange(cols)
            r_grid = np.arange(rows)
            C, R = np.meshgrid(c_grid, r_grid)
            
            # Proyectar malla de celdas a coordenadas origen
            X, Y = transform_win * (C, R)
            
            # Centro en el map
            cx, cy = transform("EPSG:4326", self.dataset.crs, [lon], [lat])
            cx, cy = cx[0], cy[0]
            
            # 4. Calcular kernel de distancia para todos los pixeles
            if self.dataset.crs.is_geographic:
                dx = (X - cx) * 111.32 * math.cos(math.radians(lat))
                dy = (Y - cy) * 111.32
                dist_km = np.sqrt(dx**2 + dy**2)
            else:
                scale_factor = math.cos(math.radians(lat)) if self.dataset.crs.to_epsg() == 3857 else 1.0
                dist_km = np.sqrt((X - cx)**2 + (Y - cy)**2) * scale_factor / 1000.0
                
            # Transformar radiancia descartando el piso de ruido natural (airglow/VIRS noise limit ~ 0.40 nW/cm2/sr)
            noise_floor = 0.40
            data_clean = np.maximum(0.0, data - noise_floor)
            
            # Solo píxeles válidos del interior del buffer
            valid_mask = (dist_km <= 60.0) & (data_clean > 0)
            
            if not np.any(valid_mask):
                return 0.0
                
            # 5. Aplicar kernel Exponencial y sumar (Convolución Discreta)
            kernel = np.exp(-dist_km[valid_mask] / L)
            R_sum = np.sum(data_clean[valid_mask] * kernel)
            
            # 6. Factor de calibración geométrica
            # Ajustado para mapear exactamente las variaciones
            calibration_factor = 60.0
            
            R_eff = R_sum / calibration_factor
            
            return float(R_eff)
            
        except Exception as e:
            print(f"[LightPollutionSampler] Error durante el procesamiento de contribución regional: {e}", flush=True)
            return getattr(self, "get_radiance")(lat, lon) or 0.0

    def estimate_bortle_from_location(self, lat: float, lon: float, T: float = 1.0) -> int:
        """
        Estima la clase Bortle usando el modelo coherente de cúpula de luz regional.
        - Transforma R_eff en S (brillo del cielo)
        - Usa una sigmoide (suavizada) para saltar rangos Bortle en vez de if duros
        """
        # 1. Obtención de influencia regional luminosa
        R_eff = self.get_effective_radiance(lat, lon, T)
        
        # 2. Transformación a brillo de cielo visual aparente en magnitudes por arcosegundo cuadrado.
        # Parámetros calibrados base dados del Atlas Mundial del Brillo del Cielo Nocturno (Falchi et al.)
        a = 22.0
        b = 1.9
        eps = 0.01
        
        S = a - b * np.log10(R_eff + eps)
        
        # 3. Transición suavizada y continua entre clases usando un modelo de probabilidades
        # t_k son los umbrales de mag/arcsec2 para saltar desde la clase k a la k+1
        t_k = [21.9, 21.7, 21.5, 21.3, 20.8, 20.3, 19.5, 18.5]
        w = 0.1 # Factor de dispersión/suavizado de la transición sigmoidal (ajustado para caber en rangos de 0.2)
        
        probs = np.zeros(9)
        
        def sigmoid(x):
            # Normalización rápida para evitar warnings de overflow de np.exp
            x = np.clip(x, -50, 50)
            return 1.0 / (1.0 + np.exp(-x))
            
        p_prev = 0.0
        for i, threshold in enumerate(t_k):
            # P(B <= i + 1)
            # Dado que si S aumenta el cielo es más oscuro y por ende la clase disminuye,
            # el operando evalúa (S - tk). 
            prob_cum = sigmoid((S - threshold) / w)
            probs[i] = prob_cum - p_prev
            p_prev = prob_cum
            
        probs[8] = 1.0 - p_prev # P(B = 9) = 1.0 - P(B <= 8)
        
        # Seleccionamos el bin (clase) ganadora añadiendo +1 ya que el indice base es 0
        best_bortle = np.argmax(probs) + 1
        return int(best_bortle)
