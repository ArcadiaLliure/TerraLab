"""
Weather System for Astronomical Widget
GeneraciÃ³n procedural de clima optimizada con proyecciÃ³n esfÃ©rica y cachÃ©.
"""

import math
import random
import time
from datetime import datetime, timedelta
from PyQt5.QtCore import Qt, QPointF, QRectF
from PyQt5.QtGui import QPainter, QColor, QPen, QBrush, QPixmap, QLinearGradient, QRadialGradient, QImage
from TerraLab.weather.metno_provider import MetNoWeatherProvider

# --- CONFIG ---
DEBUG_CLOUD_DENSITY = 1.0 # Multiplicador de cantidad de nubes (1.0 = normal)

# ============================================================================
# WEATHER PALETTE GENERATOR
# ============================================================================

class WeatherPalette:
    """
    Genera y gestiona paletas de clima para todo el aÃ±o.
    24 horas Ã— 365 dÃ­as = 8760 registros
    """
    
    def __init__(self):
        self.data = []  # Lista de diccionarios con datos horarios
        
    def generate_random(self, 
                       clear_nights=0.7,      # 0.0-1.0: mÃ¡s claro = mÃ¡s estrellas
                       clear_days=0.6,
                       rainy_tendency=0.2,    # 0.0-1.0: probabilidad de lluvia
                       snowy_tendency=0.1):   # 0.0-1.0: probabilidad de nieve
        """Genera una paleta procedural suave para todo el aÃ±o"""
        self.data = []
        
        # Generadores de ruido suave (Perlin simplificado)
        def smooth_noise(x, octaves=3):
            """Ruido suave entre 0 y 1"""
            value = 0
            amplitude = 1.0
            frequency = 1.0
            max_value = 0
            
            for _ in range(octaves):
                value += amplitude * (math.sin(x * frequency * 0.01) * 0.5 + 0.5)
                max_value += amplitude
                amplitude *= 0.5
                frequency *= 2.0
                
            return value / max_value
        
        # Generar datos
        for day in range(365):
            season_factor = math.sin((day / 365.0) * 2 * math.pi)
            winter_factor = max(0, -season_factor)
            
            for hour in range(24):
                idx = day * 24 + hour
                base_noise = smooth_noise(idx, octaves=4)
                
                is_night = hour < 6 or hour > 20
                cloud_cover = (1.0 - (clear_nights if is_night else clear_days)) * base_noise
                
                precip_roll = smooth_noise(idx + 1000, octaves=2)
                p_type = 'none'
                p_int = 0.0
                
                if precip_roll < rainy_tendency:
                    if winter_factor > 0.5 and precip_roll < snowy_tendency:
                        p_type = 'snow'
                        p_int = winter_factor * smooth_noise(idx + 2000)
                    else:
                        p_type = 'rain'
                        p_int = smooth_noise(idx + 3000, octaves=3)
                
                t_prob = 0.0
                if p_type == 'rain' and p_int > 0.6:
                    t_prob = (p_int - 0.6) * 2.5
                
                # FORCE LOGIC: Rain implies Clouds
                if p_type != 'none' and p_int > 0:
                    cloud_cover = max(cloud_cover, 0.8 + p_int * 0.2)
                
                self.data.append({
                    'cloud_cover': min(1.0, cloud_cover),
                    'precipitation_type': p_type,
                    'precipitation_intensity': min(1.0, p_int),
                    'thunder_probability': min(1.0, t_prob)
                })
    
    def get_weather(self, day, hour):
        """Obtiene el clima para un dÃ­a y hora especÃ­ficos"""
        day = int(day) % 365
        hour = int(hour) % 24
        idx = day * 24 + hour
        if idx < len(self.data):
            return self.data[idx]
        return {'cloud_cover': 0.0, 'precipitation_type': 'none', 'precipitation_intensity': 0.0, 'thunder_probability': 0.0}

# ============================================================================
# CLOUD (CACHED & SPHERICAL)
# ============================================================================

class Cloud:
    """
    Nube orgÃ¡nica dispersa (Estilo Fractal-like).
    MÃ¡s pequeÃ±a, mÃ¡s irregular, sin "base plana" caricaturesca.
    """
    
    def __init__(self, azimuth, altitude, size_deg, puffiness=1.0):
        self.azimuth = azimuth % 360.0
        self.altitude = altitude 
        self.size_deg = size_deg 
        
        # Reduced texture size to keep frame-time stable during overcast.
        scale_factor = 14 
        w = int(size_deg * scale_factor * 4.0) # Much wider canvas to avoid clipping
        h = int(size_deg * scale_factor * 3.0)
        self.pixmap = QPixmap(w, h)
        self.pixmap.fill(Qt.transparent)
        
        cx, cy = w/2, h/2
        
        painter = QPainter(self.pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        
        # --- "SMOKE" ALGORITHM (Accumulated Transparency) ---
        # Instead of solid bubbles, we draw hundreds of faint, distorted ellipses
        # to build up volume. This hides the "circular" edges.
        
        # 1. Seeds (The "Skeleton" of the cloud)
        seeds = []
        num_cores = random.randint(4, 8)
        
        # Spread seeds horizontally
        for i in range(num_cores):
            sx = (random.random() - 0.5) * w * 0.4 # Keep seeds more central
            sy = (random.random() - 0.5) * h * 0.15
            sr = w * 0.15 * random.uniform(0.8, 1.5)
            seeds.append((cx + sx, cy + sy, sr))
            
        # 2. Particles ( The "Flesh")
        # Higher count because soft edges make them fainter effectively
        total_particles = int(240 * puffiness) 
        
        for _ in range(total_particles):
            # Pick a seed
            parent_x, parent_y, parent_r = random.choice(seeds)
            
            # Scatter
            angle = random.uniform(0, 6.28)
            dist = parent_r * (random.random() ** 0.5) * 1.3 # Slightly wider spread
            
            px = parent_x + math.cos(angle) * dist
            py = parent_y + math.sin(angle) * dist
            
            # Texture/Shading Logic
            rel_y = (py - (cy - h*0.2)) / (h * 0.5)
            rel_y = max(0.0, min(1.0, rel_y))
            
            # Base Colors (Higher alpha because gradients will fade them out)
            if rel_y < 0.3:
                c = QColor(255, 255, 255, 25) 
            elif rel_y < 0.7:
                c = QColor(230, 235, 245, 20)
            else:
                c = QColor(200, 210, 220, 15)
            
            # Random size
            pr = parent_r * random.uniform(0.3, 0.7)
            
            # Distortion
            sx = random.uniform(0.8, 1.6)
            sy = random.uniform(0.7, 1.2)
            rot = random.uniform(0, 360)
            
            painter.save()
            painter.translate(px, py)
            painter.rotate(rot)
            painter.scale(sx, sy)
            
            # SOFT EDGE MAGIC: Radial Gradient instead of Solid Brush
            # Center: Color, Edge: Transparent
            grad = QRadialGradient(0, 0, pr)
            grad.setColorAt(0.0, c)
            grad.setColorAt(0.4, c) # Core is solid-ish
            grad.setColorAt(1.0, QColor(c.red(), c.green(), c.blue(), 0)) # Edge fades to 0
            
            painter.setBrush(QBrush(grad))
            painter.drawEllipse(QPointF(0, 0), pr, pr)
            
            painter.restore()
            
        painter.end()
        
    def move_with_wind(self, dx, dy):
        """
        Mueve la nube en un plano imaginario "techo" y recalcula Az/Alt.
        Esto permite que las nubes se muevan hacia el horizonte o hacia el cÃ©nit
        con la perspectiva correcta.
        """
        # 1. Proyectar (Az, Alt) -> (x, y) en plano a altura H
        # Usamos Alt=90 como centro (distancia 0)
        # Distancia desde el cÃ©nit 'r'
        # Cuanto mÃ¡s bajo en el horizonte (Alt->0), mÃ¡s lejos (r->grande)
        # Mapping simple: r = (90 - Alt) * k
        # Pero queremos perspectiva real tangencial: r = H / tan(Alt)
        # Evitamos divisiÃ³n por cero para Alt=0
        
        eff_alt = max(0.1, self.altitude)
        # Radio proyectado desde el eje Z (zenit)
        # Usamos una escala arbitraria donde el horizonte visual estÃ¡ a R=1000
        # tan(alitutde) = H / D  => D = H / tan(alt)
        
        H = 1000.0
        rad_alt = math.radians(eff_alt)
        dist = H / math.tan(rad_alt)
        
        rad_az = math.radians(self.azimuth)
        
        # Coordenadas en el plano del techo (Norte/Sur/Este/Oeste)
        cx = dist * math.sin(rad_az)
        cy = dist * math.cos(rad_az)
        
        # 2. Aplicar Viento
        cx += dx
        cy += dy
        
        # 3. Recalcular (Az, Alt)
        new_dist = math.sqrt(cx*cx + cy*cy)
        
        # Azimuth
        # atan2(x, y) devuelve Ã¡ngulo desde eje Y (Norte) si invertimos o rotamos
        # Standard atan2(y, x) es desde X.
        # Ajustamos a nuestra convenciÃ³n anterior
        new_az_rad = math.atan2(cx, cy) 
        self.azimuth = math.degrees(new_az_rad) % 360.0
        
        # Altitude
        # D = H / tan(new_alt) => tan(new_alt) = H / D => new_alt = atan(H/D)
        new_alt_rad = math.atan(H / new_dist)
        self.altitude = math.degrees(new_alt_rad)
        
        # Clip horizon
        if self.altitude < 0: self.altitude = 0

# ============================================================================
# PARTICLE SYSTEM (SCREEN SPACE)
# ============================================================================

class Particle:
    """PartÃ­cula simple para lluvia/nieve (Espacio de pantalla)"""
    def __init__(self, x, y, type='rain'):
        self.x = x
        self.y = y
        self.type = type
        self.life = 1.0
        self.vy = random.uniform(15, 25) if type == 'rain' else random.uniform(2, 5)
        self.vx = random.uniform(-1, 1) if type == 'rain' else random.uniform(-2, 2)
        self.size = random.uniform(2, 4)

    def update(self, dt=0.016):
        self.x += self.vx * dt * 60
        self.y += self.vy * dt * 60
        self.life -= dt * 0.5
        
    def draw(self, painter):
        alpha = int(self.life * 150) # Semi-transparente
        if self.type == 'rain':
            c = QColor(200, 220, 255, alpha)
            painter.setPen(QPen(c, 2))
            painter.drawLine(int(self.x), int(self.y), int(self.x + self.vx*0.5), int(self.y + self.vy*0.5))
        else:
            c = QColor(255, 255, 255, alpha)
            painter.setBrush(c)
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QPointF(self.x, self.y), self.size, self.size)

# ============================================================================
# WEATHER SYSTEM (MAIN)
# ============================================================================

class WeatherSystem:
    def __init__(self, width, height, latitude=0.0, longitude=0.0, use_remote_weather=True, cache_enabled=True):
        self.width = width
        self.height = height
        self.enabled = True
        self.palette = WeatherPalette()
        self.palette.generate_random()
        self.provider = MetNoWeatherProvider(
            latitude=latitude,
            longitude=longitude,
            use_remote=bool(use_remote_weather),
            cache_enabled=bool(cache_enabled),
        )
        
        self.clouds = []
        self.particles = []
        
        # Viento Global
        # DirecciÃ³n 0-360, Velocidad 
        self.wind_dir = random.uniform(0, 360)
        self.wind_speed = random.uniform(2.0, 5.0) # Unidades arbitrarias por frame
        
        # Precalcule vector
        rad = math.radians(self.wind_dir)
        self.wind_dx = math.sin(rad) * self.wind_speed
        self.wind_dy = math.cos(rad) * self.wind_speed
        
        # State
        self.current_cover = 0.0
        self.current_cover_low = 0.0
        self.current_cover_mid = 0.0
        self.current_cover_high = 0.0
        self.fog_cover = 0.0
        self.humidity = 0.0
        self.precip_type = 'none'
        self.precip_int = 0.0
        self.precip_rate_mm_h = 0.0
        self.thunder_prob = 0.0
        self.flash_val = 0.0
        self.bortle = 4 # Current Bortle class (1-9)
        self.last_weather_source = "fallback"
        self.last_weather_reason = "init"
        self._has_layer_cloud_data = False
        self._last_update_monotonic = time.monotonic()
        self._delta_seconds = 0.05
        self._last_weather_slot = None
        self._slot_changed_recent = False
        self._stratus_phase_x = 0.0
        self._stratus_phase_y = 0.0
        # Two periodic textures with different scales reduce visible tiling artifacts.
        self._stratus_texture = self._build_stratus_texture(256, seed=137)
        self._stratus_detail_texture = self._build_stratus_texture(160, seed=811)

    @staticmethod
    def _smooth_alpha(base_step_50ms, delta_seconds):
        """
        Normalitza el suavitzat perquè no depengui del FPS.
        base_step_50ms: guany de suavitzat dissenyat per cicle de 50ms.
        """
        base = max(0.0001, min(0.9999, float(base_step_50ms)))
        dt = max(0.01, min(0.50, float(delta_seconds)))
        return 1.0 - ((1.0 - base) ** (dt / 0.05))

    @staticmethod
    def _cloud_optical_depth(coverage):
        """
        Converteix coberta [0..1] en profunditat òptica de núvol.
        Model continu (sense umbrals durs) per a un comportament més físic.
        """
        c = max(0.0, min(1.0, float(coverage)))
        # Slightly stronger mid/high response:
        # this avoids excessive blue bleed-through when forecast reports
        # fragmented-but-dense cloud decks around ~0.55-0.85 coverage.
        return 0.05 + 2.75 * (c ** 1.15)

    @staticmethod
    def _lerp(a, b, t):
        return float(a) + (float(b) - float(a)) * float(t)

    @staticmethod
    def _lerp_angle_deg(a, b, t):
        a = float(a) % 360.0
        b = float(b) % 360.0
        d = ((b - a + 180.0) % 360.0) - 180.0
        return (a + d * float(t)) % 360.0

    @staticmethod
    def _blend_color(c_a, c_b, t):
        tt = max(0.0, min(1.0, float(t)))
        if c_a is None:
            return c_b
        if c_b is None:
            return c_a
        return QColor(
            int(WeatherSystem._lerp(c_a.red(), c_b.red(), tt)),
            int(WeatherSystem._lerp(c_a.green(), c_b.green(), tt)),
            int(WeatherSystem._lerp(c_a.blue(), c_b.blue(), tt)),
            int(WeatherSystem._lerp(c_a.alpha(), c_b.alpha(), tt)),
        )

    @staticmethod
    def _slot_from_year_day_hour(year, day, hour_value):
        dt = datetime(int(year), 1, 1) + timedelta(days=int(day), hours=float(hour_value))
        day_idx = (dt.date() - datetime(dt.year, 1, 1).date()).days
        return int(dt.year), int(day_idx), int(dt.hour)

    def _blend_weather_slots(self, w0, w1, t):
        if not isinstance(w0, dict) and not isinstance(w1, dict):
            return None
        if not isinstance(w0, dict):
            return dict(w1)
        if not isinstance(w1, dict):
            return dict(w0)

        out = {}
        num_keys = (
            "cloud_cover",
            "cloud_low",
            "cloud_mid",
            "cloud_high",
            "fog_cover",
            "humidity",
            "wind_speed_ms",
            "pressure_hpa",
            "temperature_c",
            "precip_rate_mm_h",
            "precipitation_intensity",
            "thunder_probability",
        )
        for k in num_keys:
            v0 = float(w0.get(k, 0.0))
            v1 = float(w1.get(k, v0))
            out[k] = self._lerp(v0, v1, t)

        out["wind_direction_deg"] = self._lerp_angle_deg(
            w0.get("wind_direction_deg", 0.0),
            w1.get("wind_direction_deg", w0.get("wind_direction_deg", 0.0)),
            t,
        )
        # Categorical fields choose nearest slot.
        nearest = w1 if float(t) >= 0.5 else w0
        out["precipitation_type"] = str(nearest.get("precipitation_type", "none"))
        return out

    def _build_stratus_texture(self, size, seed=None):
        size_i = max(64, int(size))
        rng = random.Random(int(seed) if seed is not None else (size_i * 37 + 11))
        image = QImage(size_i, size_i, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)

        # Periodic harmonics make the texture tile seamlessly in X/Y.
        harmonics = []
        amplitude = 1.0
        for octave in range(4):
            freq = float(2 ** octave)
            phase_x = rng.uniform(0.0, math.tau)
            phase_y = rng.uniform(0.0, math.tau)
            harmonics.append((freq, phase_x, phase_y, amplitude))
            amplitude *= 0.55
        max_amp = max(1e-6, sum(h[3] for h in harmonics))

        for y in range(size_i):
            ny = math.tau * (float(y) / float(size_i))
            for x in range(size_i):
                nx = math.tau * (float(x) / float(size_i))
                accum = 0.0
                for freq, phase_x, phase_y, amp in harmonics:
                    wave_1 = math.sin(nx * freq + phase_x)
                    wave_2 = math.cos(ny * freq + phase_y)
                    wave_3 = math.sin((nx + ny) * freq + phase_x - phase_y)
                    accum += (0.50 * wave_1 + 0.35 * wave_2 + 0.25 * wave_3) * amp

                v = 0.5 + 0.5 * (accum / (max_amp * 1.10))
                v = max(0.0, min(1.0, v))
                density = v ** 1.70
                alpha = int(5 + density * 74)
                c = int(202 + 44 * (v ** 0.78))
                image.setPixelColor(x, y, QColor(c, min(255, c + 4), min(255, c + 12), alpha))

        return QPixmap.fromImage(image)

    def shutdown(self):
        try:
            self.provider.shutdown()
        except Exception:
            pass

    def __del__(self):
        self.shutdown()

    def set_location(self, latitude, longitude):
        try:
            self.provider.set_location(latitude, longitude)
            self.last_weather_source = "fallback"
            self.last_weather_reason = "location_changed"
        except Exception:
            pass

    def get_cache_path(self):
        try:
            return self.provider.get_cache_path()
        except Exception:
            return None

    def set_remote_weather_enabled(self, enabled: bool):
        try:
            self.provider.set_remote_enabled(bool(enabled))
            if not bool(enabled):
                self.last_weather_source = "fallback"
                self.last_weather_reason = "remote_disabled"
        except Exception:
            pass

    def set_cache_enabled(self, enabled: bool):
        try:
            self.provider.set_cache_enabled(bool(enabled))
            self.last_weather_reason = self.provider.get_last_status()
        except Exception:
            pass

    def set_remote_user_agent(self, user_agent: str):
        try:
            self.provider.set_user_agent(str(user_agent or "").strip())
            self.last_weather_reason = self.provider.get_last_status()
        except Exception:
            pass

    def get_runtime_status(self):
        requires_user_agent = False
        try:
            requires_user_agent = not bool(str(getattr(self.provider, "user_agent", "") or "").strip())
        except Exception:
            requires_user_agent = True
        return {
            "source": str(getattr(self, "last_weather_source", "fallback") or "fallback"),
            "reason": str(getattr(self, "last_weather_reason", "unknown") or "unknown"),
            "requires_user_agent": bool(requires_user_agent),
        }
        
    def set_bortle(self, bortle_class: int):
        """Updates the current light pollution level."""
        self.bortle = max(1, min(9, int(bortle_class)))
        
    def resize(self, w, h):
        self.width = w
        self.height = h
        
    def update_weather(self, day, hour, year=None):
        if not self.enabled:
            self.clouds = []
            self.last_weather_source = "disabled"
            self.last_weather_reason = "weather_disabled"
            return

        now_m = time.monotonic()
        self._delta_seconds = max(0.01, min(0.50, now_m - float(getattr(self, "_last_update_monotonic", now_m))))
        self._last_update_monotonic = now_m

        if year is None:
            year = datetime.utcnow().year
        hour_float = float(hour) % 24.0
        hour_floor = int(math.floor(hour_float))
        frac_hour = max(0.0, min(1.0, hour_float - hour_floor))

        y0, d0, h0 = self._slot_from_year_day_hour(year, day, hour_floor)
        y1, d1, h1 = self._slot_from_year_day_hour(year, day, hour_floor + 1)
        slot = (int(y0), int(d0), int(h0))
        slot_changed = slot != self._last_weather_slot
        self._last_weather_slot = slot
        self._slot_changed_recent = bool(slot_changed)

        # Prefer MET Norway data when available in coverage/cache.
        # Fallback to procedural palette when day is out of remote range or cache is empty.
        w = None
        provider_status = "unknown"
        try:
            w0 = self.provider.get_weather(y0, d0, h0)
            status0 = self.provider.get_last_status()
            w1 = self.provider.get_weather(y1, d1, h1)
            status1 = self.provider.get_last_status()
            w = self._blend_weather_slots(w0, w1, frac_hour)
            provider_status = status0 if isinstance(w0, dict) else status1
        except Exception:
            w = None
            provider_status = "provider_exception"
        if not isinstance(w, dict):
            p0 = self.palette.get_weather(d0 % 365, h0)
            p1 = self.palette.get_weather(d1 % 365, h1)
            w = self._blend_weather_slots(p0, p1, frac_hour) or self.palette.get_weather(d0 % 365, h0)
            self.last_weather_source = "fallback"
            self.last_weather_reason = provider_status
        else:
            self.last_weather_source = "remote"
            self.last_weather_reason = provider_status

        # Mapping meteo -> render:
        # - cloud_* are area fractions [0..1] used as target densities by layer.
        # - precipitation_intensity/precip_rate_mm_h control particle emission.
        # - fog/humidity drive horizon haze.
        # - wind fields steer cloud drift direction/speed.
        #
        # Values are smoothed to avoid hard jumps between hourly forecast slots.
        cloud_cover = max(0.0, min(1.0, float(w.get('cloud_cover', 0.0))))
        self._has_layer_cloud_data = all(k in w for k in ('cloud_low', 'cloud_mid', 'cloud_high'))
        if self._has_layer_cloud_data:
            cloud_low = max(0.0, min(1.0, float(w.get('cloud_low', 0.0))))
            cloud_mid = max(0.0, min(1.0, float(w.get('cloud_mid', 0.0))))
            cloud_high = max(0.0, min(1.0, float(w.get('cloud_high', 0.0))))
        else:
            # Si la font no dona capes, la densitat global es calcula per cloud_cover
            # i el repartiment low/mid/high es fa més avall a _manage_clouds.
            cloud_low = 0.0
            cloud_mid = 0.0
            cloud_high = 0.0

        alpha_cloud = self._smooth_alpha(0.05, self._delta_seconds)
        alpha_haze = self._smooth_alpha(0.04, self._delta_seconds)
        alpha_precip = self._smooth_alpha(0.06, self._delta_seconds)
        self.current_cover += (cloud_cover - self.current_cover) * alpha_cloud
        self.current_cover_low += (cloud_low - self.current_cover_low) * alpha_cloud
        self.current_cover_mid += (cloud_mid - self.current_cover_mid) * alpha_cloud
        self.current_cover_high += (cloud_high - self.current_cover_high) * alpha_cloud
        self.fog_cover += (float(w.get('fog_cover', 0.0)) - self.fog_cover) * alpha_haze
        self.humidity += (float(w.get('humidity', 0.0)) - self.humidity) * alpha_haze
        self.precip_rate_mm_h += (float(w.get('precip_rate_mm_h', 0.0)) - self.precip_rate_mm_h) * alpha_precip
        self.precip_int += (float(w.get('precipitation_intensity', 0.0)) - self.precip_int) * alpha_precip
        self.precip_type = str(w.get('precipitation_type', 'none'))
        self.thunder_prob = float(w.get('thunder_probability', 0.0))

        # Fast convergence when user jumps to another hour/day slot.
        if slot_changed:
            fast = 0.75
            self.current_cover += (cloud_cover - self.current_cover) * fast
            self.current_cover_low += (cloud_low - self.current_cover_low) * fast
            self.current_cover_mid += (cloud_mid - self.current_cover_mid) * fast
            self.current_cover_high += (cloud_high - self.current_cover_high) * fast
            self.precip_rate_mm_h += (float(w.get('precip_rate_mm_h', 0.0)) - self.precip_rate_mm_h) * fast
            self.precip_int += (float(w.get('precipitation_intensity', 0.0)) - self.precip_int) * fast

        # Real wind from forecast.
        # Si no arriba vent, fem servir una velocitat suau per no tenir desplaçaments exagerats.
        target_speed = max(0.0, float(w.get('wind_speed_ms', 1.2)))
        target_dir = float(w.get('wind_direction_deg', self.wind_dir))
        alpha_wind = self._smooth_alpha(0.08, self._delta_seconds)
        self.wind_speed += (target_speed - self.wind_speed) * alpha_wind
        dir_delta = ((target_dir - self.wind_dir + 180.0) % 360.0) - 180.0
        self.wind_dir = (self.wind_dir + dir_delta * alpha_wind) % 360.0
        rad = math.radians(self.wind_dir)
        self.wind_dx = math.sin(rad) * self.wind_speed
        self.wind_dy = math.cos(rad) * self.wind_speed

        self._manage_clouds()
        self._manage_precip()

    def _manage_clouds(self):
        # Cloud synthesis model:
        # 1) Convert low/mid/high cloud fractions to target object counts.
        # 2) Spawn/kill clouds per layer to converge to that target.
        # 3) Move all clouds with forecast-driven wind vector.
        coverage = max(0.0, min(1.0, float(self.current_cover)))
        # Balanced with performance and overcast realism.
        target_density = int((14.0 + 30.0 * (coverage ** 1.15)) * DEBUG_CLOUD_DENSITY)

        if self._has_layer_cloud_data:
            total = int(coverage * target_density)
            w_low = max(0.0, float(self.current_cover_low))
            w_mid = max(0.0, float(self.current_cover_mid))
            w_high = max(0.0, float(self.current_cover_high))
            w_sum = w_low + w_mid + w_high
            if w_sum > 1e-6:
                layer_targets = {
                    'low': int(total * (w_low / w_sum)),
                    'mid': int(total * (w_mid / w_sum)),
                    'high': int(total * (w_high / w_sum)),
                }
            else:
                layer_targets = {
                    'low': int(total * 0.50),
                    'mid': int(total * 0.32),
                    'high': int(total * 0.18),
                }
        else:
            # Sense capes explícites, fem servir cloud_cover global.
            total = int(coverage * target_density)
            layer_targets = {
                'low': int(total * 0.50),
                'mid': int(total * 0.32),
                'high': int(total * 0.18),
            }

        layer_counts = {'low': 0, 'mid': 0, 'high': 0}
        for c in self.clouds:
            layer = getattr(c, 'layer', 'mid')
            if layer not in layer_counts:
                layer = 'mid'
            layer_counts[layer] += 1

        # Spawn per layer.
        for layer in ('low', 'mid', 'high'):
            deficit = max(0, layer_targets[layer] - layer_counts[layer])
            if deficit <= 0:
                continue
            slot_boost = 0.25 if self._slot_changed_recent else 0.0
            spawn_chance = 0.20 + slot_boost + min(0.55, deficit * 0.02)
            spawn_chance = min(0.95, spawn_chance)
            spawn_attempts = 1 + int(deficit > 8) + int(deficit > 18) + (2 if self._slot_changed_recent else 0)
            for _ in range(spawn_attempts):
                if random.random() < spawn_chance:
                    self._spawn_cloud(layer)

        # Remove excess by layer.
        if len(self.clouds) > sum(layer_targets.values()) and random.random() < (0.28 if self._slot_changed_recent else 0.18):
            removals = 1 + int(len(self.clouds) > sum(layer_targets.values()) + 10) + (2 if self._slot_changed_recent else 0)
            for _ in range(removals):
                for layer in ('high', 'mid', 'low'):
                    if layer_counts.get(layer, 0) > layer_targets.get(layer, 0):
                        for i, cloud in enumerate(self.clouds):
                            if getattr(cloud, 'layer', 'mid') == layer:
                                self.clouds.pop(i)
                                layer_counts[layer] -= 1
                                break
                        break

        # Remove clouds too close to horizon and move all.
        self.clouds = [c for c in self.clouds if c.altitude > 2.0]
        total_target = max(0, sum(layer_targets.values()))
        # Hard cap to avoid frame-time spikes under overcast.
        hard_cap = max(total_target + 8, int(34 + 56 * coverage))
        if len(self.clouds) > hard_cap:
            self.clouds = self.clouds[-hard_cap:]
        delta_s = max(0.01, min(0.25, float(getattr(self, "_delta_seconds", 0.05))))
        wind_visual_scale = 0.65
        wind_step_x = self.wind_dx * wind_visual_scale * delta_s
        wind_step_y = self.wind_dy * wind_visual_scale * delta_s
        self._stratus_phase_x += wind_step_x * 5.0
        self._stratus_phase_y += wind_step_y * 2.5
        for c in self.clouds:
            c.move_with_wind(wind_step_x, wind_step_y)
        self._slot_changed_recent = False

    def _altitude_range_for_layer(self, layer):
        if layer == 'low':
            return 0.5, 22.0
        if layer == 'high':
            return 58.0, 85.0
        return 28.0, 58.0

    def _spawn_cloud(self, layer):
        az = random.uniform(0, 360)
        min_alt, max_alt = self._altitude_range_for_layer(layer)
        alt = random.uniform(min_alt, max_alt)
        coverage = max(0.0, min(1.0, float(self.current_cover)))
        if layer == 'low':
            size = random.uniform(7, 12) + 2.5 * coverage
        elif layer == 'high':
            size = random.uniform(4, 8) + 1.5 * coverage
        else:
            size = random.uniform(5, 10) + 2.0 * coverage
        cloud = Cloud(az, alt, size)
        cloud.layer = layer
        self.clouds.append(cloud)

    def _manage_precip(self):
        # Precipitation synthesis model:
        # - rain/snow particle flux depends on mm/h and normalized intensity.
        # - we cap total particles for stable frame-time.
        if self.current_cover < 0.55 or self.precip_int < 0.05:
            self.particles = []
            return

        base_count = self.precip_rate_mm_h * (14.0 if self.precip_type == 'rain' else 10.0)
        intensity_count = self.precip_int * (20.0 if self.precip_type == 'rain' else 14.0)
        count = int(max(0.0, base_count + intensity_count))

        for _ in range(count):
            self.particles.append(Particle(random.uniform(0, self.width), -10, self.precip_type))
        if len(self.particles) > 900:
            self.particles = self.particles[-900:]

        for p in self.particles[:]:
            p.update()
            if p.y > self.height or p.life <= 0:
                self.particles.remove(p)

    def update_thunder(self):
        if self.thunder_prob > 0 and random.random() < self.thunder_prob * 0.01:
            self.flash_val = 1.0
        if self.flash_val > 0:
            self.flash_val *= 0.85
            
    def draw(self, painter, sun_alt, view_az, view_alt, current_fov=100, eclipse_dimming=1.0, project_fn=None):
        if not self.enabled: return
        
        ppd = self.width / current_fov 
        
        # Painter-time weather model:
        # - `sun_alt` comes from the astronomical pipeline for the current simulation time.
        # - day/twilight/night tint is derived from `sun_alt`.
        # - cloud geometry is projected with `view_az`, `view_alt` and `current_fov`.
        # - eclipse_dimming modulates final luminance independently of weather.
        eff_sun_alt = sun_alt
        
        # --- Tinte ---
        tint_gradient = False
        base_tint = None

        rain_tint = None
        if self.precip_int > 0.05:
            # Rain/snow clouds are grey but should not collapse to pure black.
            pt = max(0.0, min(1.0, (self.precip_int - 0.05) / 0.95))
            rain_tint = QColor(
                int(self._lerp(206, 128, pt)),
                int(self._lerp(208, 132, pt)),
                int(self._lerp(216, 140, pt)),
                int(self._lerp(74, 178, pt)),
            )

        if eff_sun_alt >= 10.0:
            base_tint = rain_tint
        elif eff_sun_alt >= -6.0:
            # Continuous dusk transition (no hard branch between 6 and 10 deg).
            tint_gradient = True
            dusk_t = max(0.0, min(1.0, (10.0 - eff_sun_alt) / 16.0))
            warm_tint = QColor(
                int(self._lerp(255, 245, dusk_t)),
                int(self._lerp(186, 128, dusk_t)),
                int(self._lerp(118, 78, dusk_t)),
                int(self._lerp(28, 188, dusk_t)),
            )
            if rain_tint is None:
                base_tint = warm_tint
            else:
                # During precipitation we keep the grey mass and only a residual warm cast.
                mix = max(0.48, min(0.92, 0.48 + 0.44 * self.precip_int))
                base_tint = self._blend_color(warm_tint, rain_tint, mix)
        else:
            # NIGHT MODE: Must be VERY DARK to hide white clouds
            # But influenced by Light Pollution (Bortle)
            glow_intensity = (self.bortle - 1) * 0.1
            r = int(5 + 25 * glow_intensity)
            g = int(5 + 15 * glow_intensity)
            b = int(15 + 5 * glow_intensity)
            base_tint = QColor(r, g, b, 235)
            if rain_tint is not None:
                base_tint = self._blend_color(base_tint, rain_tint, 0.55)
            # Cloud Illumination (Bottom-up)
            if self.bortle > 3:
                tint_gradient = True

        # --- GLOBAL ECLIPSE DARKENING ---
        # Apply to ANY state (Day/Sunset/Night)
        
        # 1. TOTAL ECLIPSE (Deep Darkness) -> Force NIGHT MODE
        if eclipse_dimming < 0.2:
            # Completely opaque black/dark blue, IDENTICAL to Night Mode
            base_tint = QColor(5, 5, 15, 255)
            
        # 2. PARTIAL ECLIPSE -> Transition
        elif eclipse_dimming < 0.95:
            # Darkness factor (0.05 to 1.0)
            darkness = 1.0 - eclipse_dimming 
            
            # Target color is Night Mode (5, 5, 15, 255)
            target_r, target_g, target_b, target_a = 5, 5, 15, 255
            
            if base_tint is None:
                # Transition from White/Transparent to Black/Opaque
                # Alpha grows with darkness
                alpha_val = int(min(255, darkness * 300)) # Ramp faster to opaque
                base_tint = QColor(target_r, target_g, target_b, alpha_val)
            else:
                # Transition from Existing Tint to Black/Opaque
                # Interpolate RGB towards (5, 5, 15)
                # Interpolate Alpha towards 255
                
                # Factor 0.0 (No Eclipse) -> 1.0 (Total Eclipse)
                # darkness is approx 0.05 -> 0.8 here.
                # Re-map 0.95->0.2 range to 0.0->1.0 progress
                progress = (0.95 - eclipse_dimming) / (0.95 - 0.2)
                progress = max(0.0, min(1.0, progress))
                
                r = int(base_tint.red() * (1.0 - progress) + target_r * progress)
                g = int(base_tint.green() * (1.0 - progress) + target_g * progress)
                b = int(base_tint.blue() * (1.0 - progress) + target_b * progress)
                a = int(base_tint.alpha() * (1.0 - progress) + target_a * progress)
                
                base_tint = QColor(r, g, b, a)

        # Overcast deck:
        # Continuous Beer-Lambert style attenuation from zenith to horizon.
        if eff_sun_alt >= -2.0:
            coverage = max(0.0, min(1.0, float(self.current_cover)))
            precip_dark = max(0.0, min(1.0, float(self.precip_int)))
            tau_cloud = self._cloud_optical_depth(coverage)
            t_top = math.exp(-tau_cloud * 0.70)
            t_mid = math.exp(-tau_cloud * 1.20)
            t_bottom = math.exp(-tau_cloud * 1.95)
            alpha_top = max(0, min(255, int((1.0 - t_top) * 210.0 + 24.0 * precip_dark)))
            alpha_mid = max(0, min(255, int((1.0 - t_mid) * 225.0 + 28.0 * precip_dark)))
            alpha_bottom = max(0, min(255, int((1.0 - t_bottom) * 242.0 + 36.0 * precip_dark)))
            gray = int(232 - 66 * (coverage ** 1.12) - 44 * precip_dark)
            gray = max(118, min(236, gray))
            painter.save()
            grad_cov = QLinearGradient(0, 0, 0, self.height)
            grad_cov.setColorAt(0.0, QColor(gray + 6, gray + 8, min(255, gray + 14), alpha_top))
            grad_cov.setColorAt(0.55, QColor(gray + 2, gray + 4, min(255, gray + 10), alpha_mid))
            grad_cov.setColorAt(1.0, QColor(gray - 8, gray - 6, min(255, gray + 2), alpha_bottom))
            painter.fillRect(0, 0, self.width, self.height, QBrush(grad_cov))

            # Extra overcast veil for medium-high cloud cover to reduce blue "holes".
            if coverage > 0.45:
                over_t = ((coverage - 0.45) / 0.55)
                over_t = max(0.0, min(1.0, over_t))
                over_alpha = int(34 + 118 * (over_t ** 1.12))
                painter.fillRect(0, 0, self.width, self.height, QColor(gray - 5, gray - 3, min(255, gray + 4), over_alpha))

            if self._stratus_texture is not None:
                tw = max(1, self._stratus_texture.width())
                th = max(1, self._stratus_texture.height())
                ox = int(self._stratus_phase_x) % tw
                oy = int(self._stratus_phase_y) % th
                tex_opacity = max(0.10, min(0.88, 0.14 + (1.0 - t_mid) * 0.92))
                painter.setOpacity(tex_opacity * 0.88)
                painter.drawTiledPixmap(QRectF(-ox, -oy, self.width + tw, self.height + th), self._stratus_texture)
            if self._stratus_detail_texture is not None:
                dw = max(1, self._stratus_detail_texture.width())
                dh = max(1, self._stratus_detail_texture.height())
                dox = int(self._stratus_phase_x * 1.7 + 41.0) % dw
                doy = int(self._stratus_phase_y * 1.4 + 29.0) % dh
                detail_opacity = max(0.06, min(0.48, 0.10 + (1.0 - t_bottom) * 0.35))
                painter.setOpacity(detail_opacity * 0.95)
                painter.drawTiledPixmap(QRectF(-dox, -doy, self.width + dw, self.height + dh), self._stratus_detail_texture)
            painter.restore()
                
        # Ordenar nubes por altitud para pintar primero las cercanas al horizonte (fondo)
        # y luego las del zenit (frente) ? 
        # En una bÃ³veda, lo mÃ¡s lejano es el horizonte.
        # Pintamos de horizonte (0) a zenit (90)? 
        # SÃ­, normalmente painter order.
        
        sorted_clouds = sorted(self.clouds, key=lambda c: c.altitude)
        
        for c in sorted_clouds:
            if project_fn is not None:
                pt = project_fn(c.altitude, c.azimuth)
                if not pt:
                    continue
                screen_x, screen_y = float(pt[0]), float(pt[1])
                if screen_x < -320 or screen_x > (self.width + 320) or screen_y < -220 or screen_y > (self.height + 260):
                    continue
            else:
                diff_az = (c.azimuth - view_az + 180) % 360 - 180
                diff_alt = c.altitude - view_alt
                if not (abs(diff_az) < (current_fov/2 + 30) and abs(diff_alt) < (current_fov/2 + 30)):
                    continue
                screen_x = (self.width / 2) + (diff_az * ppd)
                screen_y = (self.height / 2) - (diff_alt * ppd)

            # PERSPECTIVA 3D SIMULADA
            # Nubes en el horizonte (alt=0) se ven mÃ¡s pequeÃ±as y aplastadas.
            # Nubes en el zenit (alt=90) se ven grandes (estÃ¡n mÃ¡s cerca, h=height).
            # Factor de escala por altitud:
            # Horizon: 0.4x, Zenith: 1.5x
            alt_factor = max(0.1, math.sin(math.radians(c.altitude))) # 0 a 1
            perspective_scale = 0.4 + (alt_factor * 1.5)

            w_px = c.pixmap.width()
            h_px = c.pixmap.height()

            # Escala final combinando FOV (zoom) y Perspectiva
            final_scale = (ppd / 12.0) * perspective_scale

            # --- OPTIMIZATION: Direct Draw if no Tint ---
            if base_tint is None:
                 painter.save()
                 painter.translate(screen_x, screen_y)
                 painter.scale(final_scale, final_scale)

                 if c.altitude < 20:
                     flatten = 0.5 + (c.altitude / 20.0) * 0.5
                     painter.scale(1.0, flatten)

                 day_cloud_opacity = 0.58 + 0.34 * max(0.0, min(1.0, self.current_cover))
                 painter.setOpacity(day_cloud_opacity)
                 # Use one extra pass under high cover to increase cloud body opacity.
                 draw_passes = 2 + (1 if self.current_cover > 0.62 else 0)
                 for _ in range(draw_passes):
                     painter.drawPixmap(QPointF(-w_px/2, -h_px/2), c.pixmap)

                 painter.restore()
                 continue

            # --- OFFSCREEN COMPOSITION (Only when Tinting is needed) ---
            # We compose the cloud + tint into a separate buffer so we don't
            # paint the tint on the sky background.
            temp_cloud = QPixmap(w_px, h_px)
            temp_cloud.fill(Qt.transparent)

            pt = QPainter(temp_cloud)
            pt.setRenderHint(QPainter.Antialiasing)

            # 1. Select Source Mapping
            source_pixmap = c.pixmap # Always use the soft texture

            # 2. HOMOGENIZED RENDERING (Fix used shape changing)
            # Always use the SAME number of passes to define the cloud "Volume/Shape".
            CONST_PASSES = 2

            # Default opacity for "Day/Wispy"
            master_opacity = 0.60

            if base_tint:
                # Map tint alpha to solidity
                # Night (Black, Alpha 255) -> Opacity 1.0 (Solid)
                t_alpha = base_tint.alpha() / 255.0
                if eff_sun_alt < -8.0:
                    master_opacity = 0.66 + (t_alpha * 0.28)
                else:
                    # Keep rain/twilight clouds textured and avoid near-black blobs.
                    master_opacity = 0.50 + (t_alpha * 0.26)

            pt.setOpacity(master_opacity)

            # 3. Draw Source Stack (Constant Geometry)
            for _ in range(CONST_PASSES):
                 pt.drawPixmap(0, 0, source_pixmap)

            # Restore opacity for tinting operations
            pt.setOpacity(1.0)

            # 3. Apply Tint (SourceAtop = Tint only where Alpha > 0)
            if base_tint:
                pt.setCompositionMode(QPainter.CompositionMode_SourceAtop)

                if tint_gradient:
                     grad = QLinearGradient(0, h_px/2, 0, -h_px/2)
                     c_bottom = base_tint

                     # Dynamic Top Color for Eclipse Homogenization
                     # If eclipsing, top should also darken towards black, not clear white
                     if eclipse_dimming < 0.95:
                         darkness = 1.0 - eclipse_dimming
                         alpha_val = int(min(255, darkness * 255))
                         c_top = QColor(5, 5, 15, alpha_val) # Fade to Black
                     else:
                         c_top = QColor(255, 255, 255, 0)

                     grad.setColorAt(0.0, c_bottom)
                     # MIDDLE: Slightly tinted towards light pollution hue
                     mid_alpha = max(110, c_top.alpha())
                     grad.setColorAt(0.3, QColor(base_tint.red(), base_tint.green(), int(min(255, base_tint.blue()*1.2)), mid_alpha))
                     # TOP: Gaussian-like decay to atmospheric black
                     grad.setColorAt(1.0, c_top)
                     pt.fillRect(QRectF(0, 0, w_px, h_px), QBrush(grad))
                else:
                    # Uniform tint
                    pt.fillRect(QRectF(0, 0, w_px, h_px), base_tint)

            # 4. SAFETY FADE (Erode edges to prevent square artifacts)
            # This ensures that even if particles hit the edge of the buffer,
            # they fade out softy instead of cutting off.
            pt.setCompositionMode(QPainter.CompositionMode_DestinationIn)
            mask_grad = QRadialGradient(0.5, 0.5, 0.5)
            mask_grad.setCoordinateMode(QRadialGradient.ObjectBoundingMode)
            mask_grad.setColorAt(0.0, QColor(0, 0, 0, 255))   # Center: Opaque
            mask_grad.setColorAt(0.7, QColor(0, 0, 0, 255))   # 70% of radius: Opaque
            mask_grad.setColorAt(1.0, QColor(0, 0, 0, 0))     # Edge: Transparent
            pt.fillRect(QRectF(0, 0, w_px, h_px), QBrush(mask_grad))
            pt.end() # Finish composition

            # --- DRAW TO SCREEN ---
            painter.save()
            painter.translate(screen_x, screen_y)
            painter.scale(final_scale, final_scale)

            # Aplastar nubes en el horizonte para efecto 3D
            if c.altitude < 20:
                flatten = 0.5 + (c.altitude / 20.0) * 0.5
                painter.scale(1.0, flatten)

            painter.drawPixmap(QPointF(-w_px/2, -h_px/2), temp_cloud)
            painter.restore()
                
        # Horizon fog veil from forecast fog fraction + humidity.
        if self.fog_cover > 0.01 or self.humidity > 0.75:
            fog_strength = min(1.0, self.fog_cover * 1.15 + max(0.0, self.humidity - 0.75) * 1.6)
            if fog_strength > 0.01:
                fog_alpha_bottom = int(140 * fog_strength)
                fog_alpha_top = int(20 * fog_strength)
                fog = QLinearGradient(0, self.height, 0, self.height * 0.35)
                fog.setColorAt(0.0, QColor(210, 220, 235, fog_alpha_bottom))
                fog.setColorAt(1.0, QColor(210, 220, 235, fog_alpha_top))
                painter.fillRect(0, 0, self.width, self.height, QBrush(fog))

        # ... (Precip & Lightning logic) ...
        if self.precip_int > 0.1:
            for p in self.particles:
                p.draw(painter)
        if self.flash_val > 0.01:
            painter.fillRect(0, 0, self.width, self.height, QColor(255, 255, 255, int(self.flash_val * 128)))

# ============================================================================
# CONTROL WIDGET
# ============================================================================
class WeatherControlWidget:
    @staticmethod
    def create_controls(parent, system):
        from PyQt5.QtWidgets import QWidget, QVBoxLayout, QCheckBox, QPushButton
        w = QWidget()
        l = QVBoxLayout(w)
        c = QCheckBox("Activar Clima")
        c.setChecked(system.enabled)
        c.toggled.connect(lambda x: setattr(system, 'enabled', x))
        l.addWidget(c)
        b = QPushButton("Regenerar")
        b.clicked.connect(lambda: system.palette.generate_random())
        l.addWidget(b)
        return w



