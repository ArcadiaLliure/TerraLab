"""
Weather System for Astronomical Widget
Generación procedural de clima optimizada con proyección esférica y caché.
"""

import math
import random
import csv
from PyQt5.QtCore import Qt, QPointF, QRectF
from PyQt5.QtGui import QPainter, QColor, QPen, QBrush, QPainterPath, QPixmap, QLinearGradient, QRadialGradient

# --- CONFIG ---
DEBUG_CLOUD_DENSITY = 3.0 # Multiplicador de cantidad de nubes (1.0 = normal)

# ============================================================================
# WEATHER PALETTE GENERATOR
# ============================================================================

class WeatherPalette:
    """
    Genera y gestiona paletas de clima para todo el año.
    24 horas × 365 días = 8760 registros
    """
    
    def __init__(self):
        self.data = []  # Lista de diccionarios con datos horarios
        
    def generate_random(self, 
                       clear_nights=0.7,      # 0.0-1.0: más claro = más estrellas
                       clear_days=0.6,
                       rainy_tendency=0.2,    # 0.0-1.0: probabilidad de lluvia
                       snowy_tendency=0.1):   # 0.0-1.0: probabilidad de nieve
        """Genera una paleta procedural suave para todo el año"""
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
        """Obtiene el clima para un día y hora específicos"""
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
    Nube orgánica dispersa (Estilo Fractal-like).
    Más pequeña, más irregular, sin "base plana" caricaturesca.
    """
    
    def __init__(self, azimuth, altitude, size_deg, puffiness=1.0):
        self.azimuth = azimuth % 360.0
        self.altitude = altitude 
        self.size_deg = size_deg 
        
        # High resolution for softness
        scale_factor = 20 
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
        total_particles = int(600 * puffiness) 
        
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
        
    def move(self, d_az):
        # Deprecated simple move
        self.azimuth = (self.azimuth + d_az) % 360.0

    def move_with_wind(self, dx, dy):
        """
        Mueve la nube en un plano imaginario "techo" y recalcula Az/Alt.
        Esto permite que las nubes se muevan hacia el horizonte o hacia el cénit
        con la perspectiva correcta.
        """
        # 1. Proyectar (Az, Alt) -> (x, y) en plano a altura H
        # Usamos Alt=90 como centro (distancia 0)
        # Distancia desde el cénit 'r'
        # Cuanto más bajo en el horizonte (Alt->0), más lejos (r->grande)
        # Mapping simple: r = (90 - Alt) * k
        # Pero queremos perspectiva real tangencial: r = H / tan(Alt)
        # Evitamos división por cero para Alt=0
        
        eff_alt = max(0.1, self.altitude)
        # Radio proyectado desde el eje Z (zenit)
        # Usamos una escala arbitraria donde el horizonte visual está a R=1000
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
        # atan2(x, y) devuelve ángulo desde eje Y (Norte) si invertimos o rotamos
        # Standard atan2(y, x) es desde X.
        # Ajustamos a nuestra convención anterior
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
    """Partícula simple para lluvia/nieve (Espacio de pantalla)"""
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
    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.enabled = True
        self.palette = WeatherPalette()
        self.palette.generate_random()
        
        self.clouds = []
        self.particles = []
        
        # Viento Global
        # Dirección 0-360, Velocidad 
        self.wind_dir = random.uniform(0, 360)
        self.wind_speed = random.uniform(2.0, 5.0) # Unidades arbitrarias por frame
        
        # Precalcule vector
        rad = math.radians(self.wind_dir)
        self.wind_dx = math.sin(rad) * self.wind_speed
        self.wind_dy = math.cos(rad) * self.wind_speed
        
        # State
        self.current_cover = 0.0
        self.precip_type = 'none'
        self.precip_int = 0.0
        self.thunder_prob = 0.0
        self.flash_val = 0.0
        
    def resize(self, w, h):
        self.width = w
        self.height = h
        
    def update_weather(self, day, hour):
        if not self.enabled: 
            self.clouds = []
            return
            
        w = self.palette.get_weather(day, hour)
        
        # Interpolación suave
        self.current_cover += (w['cloud_cover'] - self.current_cover) * 0.05
        self.precip_int += (w['precipitation_intensity'] - self.precip_int) * 0.05
        self.precip_type = w['precipitation_type']
        self.thunder_prob = w['thunder_probability']
        
        self._manage_clouds()
        self._manage_precip()
        
    def _manage_clouds(self):
        # MÁS nubes, pero más pequeñas
        target_density = 50 
        target = int(self.current_cover * target_density * DEBUG_CLOUD_DENSITY)
        
        # Spawning
        # Spawneamos en el horizonte "upwind" para que entren en escena
        # O aleatorio al principio
        if len(self.clouds) < target and random.random() < 0.3:
            
            # Spawn logic: prefer the side where wind comes from?
            # Or just random and let them flow. Random is easier to fill sky.
            az = random.uniform(0, 360)
            alt = random.uniform(5, 85) 
            
            # Tamaño variado
            size = random.uniform(5, 12) 
            self.clouds.append(Cloud(az, alt, size))
            
        # Kill logic
        # Si bajan demasiado al horizonte, las borramos
        # O si hay demasiadas
        if len(self.clouds) > target and random.random() < 0.05:
            self.clouds.pop(0)
            
        # Remove low altitude
        self.clouds = [c for c in self.clouds if c.altitude > 2] # Kill at horizon
        
        # Move logic
        for c in self.clouds:
            c.move_with_wind(self.wind_dx, self.wind_dy)
            
    def _manage_precip(self):
        # Don't rain if clouds aren't gathered (Visual Consistency)
        if self.current_cover < 0.6: 
            self.particles = []
            return

        if self.precip_int < 0.1: 
            self.particles = []
            return
        count = int(self.precip_int * 10)
        for _ in range(count):
            self.particles.append(Particle(random.uniform(0, self.width), -10, self.precip_type))
        for p in self.particles[:]:
            p.update()
            if p.y > self.height or p.life <= 0:
                self.particles.remove(p)

    def update_thunder(self):
        if self.thunder_prob > 0 and random.random() < self.thunder_prob * 0.01:
            self.flash_val = 1.0
        if self.flash_val > 0:
            self.flash_val *= 0.85
            
    def draw(self, painter, sun_alt, view_az, view_alt, current_fov=100, eclipse_dimming=1.0):
        if not self.enabled: return
        
        ppd = self.width / current_fov 
        
        # Use Sun Alt normally
        eff_sun_alt = sun_alt
        
        # --- Tinte ---
        tint_gradient = False
        base_tint = None
        
        if -3.0 < eff_sun_alt < 6.0: 
            tint_gradient = True
            base_tint = QColor(255, 140, 60, 180) 
        elif eff_sun_alt >= 10: 
            # Check Rain/Storm
            if self.precip_int > 0.1:
                # Gradual Grey Tint depending on intensity
                # Intensity 0.1 -> Light Grey (200, 200, 210, 100)
                # Intensity 1.0 -> Dark Grey (60, 60, 70, 220)
                t = min(1.0, self.precip_int)
                r = int(200 * (1-t) + 60 * t)
                g = int(200 * (1-t) + 60 * t)
                b = int(210 * (1-t) + 70 * t)
                a = int(100 * (1-t) + 220 * t)
                base_tint = QColor(r, g, b, a)
            else:
                base_tint = None
        else: 
            # NIGHT MODE: Must be VERY DARK to hide white clouds
            # But influenced by Light Pollution (Bortle)
            bortle = 4 # Default if not passed
            glow_intensity = (bortle - 1) * 0.1
            r = int(5 + 25 * glow_intensity)
            g = int(5 + 15 * glow_intensity)
            b = int(15 + 5 * glow_intensity)
            base_tint = QColor(r, g, b, 255)
            # Cloud Illumination (Bottom-up)
            if bortle > 3:
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
                
        # Ordenar nubes por altitud para pintar primero las cercanas al horizonte (fondo)
        # y luego las del zenit (frente) ? 
        # En una bóveda, lo más lejano es el horizonte.
        # Pintamos de horizonte (0) a zenit (90)? 
        # Sí, normalmente painter order.
        
        sorted_clouds = sorted(self.clouds, key=lambda c: c.altitude)
        
        for c in sorted_clouds:
            diff_az = (c.azimuth - view_az + 180) % 360 - 180
            diff_alt = c.altitude - view_alt
            
            if abs(diff_az) < (current_fov/2 + 30) and abs(diff_alt) < (current_fov/2 + 30):
                
                screen_x = (self.width / 2) + (diff_az * ppd)
                screen_y = (self.height / 2) - (diff_alt * ppd)
                
                # PERSPECTIVA 3D SIMULADA
                # Nubes en el horizonte (alt=0) se ven más pequeñas y aplastadas.
                # Nubes en el zenit (alt=90) se ven grandes (están más cerca, h=height).
                # Factor de escala por altitud:
                # Horizon: 0.4x, Zenith: 1.5x
                
                alt_factor = max(0.1, math.sin(math.radians(c.altitude))) # 0 a 1
                perspective_scale = 0.4 + (alt_factor * 1.5)
                
                w_px = c.pixmap.width()
                h_px = c.pixmap.height()
                
                # Escala final combinando FOV (zoom) y Perspectiva
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
                     
                     painter.setOpacity(0.60) # Master opacity
                     # Draw 3 passes directly
                     painter.drawPixmap(QPointF(-w_px/2, -h_px/2), c.pixmap)
                     painter.drawPixmap(QPointF(-w_px/2, -h_px/2), c.pixmap)
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
                # CONST_PASSES = 3 (Aggressively reduced to maintain "soft/fractal" edges)
                CONST_PASSES = 3
                
                # Default opacity for "Day/Wispy"
                # With only 3 passes, we need high opacity to see the cloud body.
                master_opacity = 0.60
                
                if base_tint:
                    # Map tint alpha to solidity
                    # Night (Black, Alpha 255) -> Opacity 1.0 (Solid)
                    t_alpha = base_tint.alpha() / 255.0
                    
                    # Interpolate 0.60 -> 1.0
                    master_opacity = 0.60 + (t_alpha * 0.40)
                
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
                
                # Gradient relative to bounding rect (0,0 to 1,1)
                # This creates an elliptical mask that fits the cloud canvas
                # Radius 0.5 touches the nearest edge.
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