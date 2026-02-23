import math
import os
import random
from PyQt5.QtCore import Qt, QPointF, QRectF, QTimer, QObject, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QBrush, QPen, QRadialGradient, QPolygonF, QLinearGradient

try:
    from TerraLab.terrain.engine import load_profile, HorizonProfile
    HORIZON_ENGINE_AVAILABLE = True
except ImportError:
    HORIZON_ENGINE_AVAILABLE = False

# --- CONFIGURATION ---
VILLAGE_SCALE = 0.6  # Adjust this to resize all village objects (houses, trees, lanterns)


class VillageTheme:
    def __init__(self):
        # Palette Definition - SOFT PASTEL GHIBLI STYLE
        # Day
        self.day_sky = QColor(160, 210, 230) # Softer Blue
        self.day_ground = QColor(120, 180, 100) # Muted Pastel Green
        self.day_mtn_1 = QColor(100, 150, 120) # Soft Green/Blue
        self.day_mtn_2 = QColor(130, 160, 180) # Atmospheric Blue-Grey
        
        self.day_house_wall = QColor(245, 235, 215) # Warm Cream/Paper
        self.day_house_wood = QColor(110, 90, 70)   # Light Warm Wood
        self.day_house_roof = QColor(90, 80, 75)   # Muted Thatch
        
        # Night
        self.night_sky = QColor(20, 25, 40) # Deep Blue-Black
        self.night_ground = QColor(30, 45, 40) 
        self.night_mtn_1 = QColor(5, 8, 10) # Pitch black silhouette for near
        self.night_mtn_2 = QColor(18, 22, 35) # Faint mist/blue for far (almost sky color)
        
        self.night_house_wall = QColor(100, 100, 110)
        self.night_house_wood = QColor(40, 35, 30)
        self.night_house_roof = QColor(50, 40, 40)
        
        # Lights
        self.light_warm_core = QColor(255, 230, 180)
        self.light_warm_glow = QColor(255, 180, 100, 0)

        # Nature
        self.water_day = QColor(130, 200, 240, 200) # Semi-transparent Pastel Blue
        self.water_night = QColor(30, 50, 80, 200)
        self.tree_day = QColor(80, 160, 90) # Soft/Round Green
        self.tree_night = QColor(25, 40, 30)
        
        # Linework
        self.line_day = QColor(60, 50, 40, 180) # Brownish-Grey ink
        self.line_night = QColor(10, 10, 15, 180) # Dark ink
        
    def get_color(self, name, t_night):
        def lerp_c(c1, c2, t):
            r = c1.red() + (c2.red() - c1.red()) * t
            g = c1.green() + (c2.green() - c1.green()) * t
            b = c1.blue() + (c2.blue() - c1.blue()) * t
            return QColor(int(r), int(g), int(b), int(c1.alpha() + (c2.alpha()-c1.alpha())*t))

        if name == 'ground': return lerp_c(self.day_ground, self.night_ground, t_night)
        if name == 'mtn_near': return lerp_c(self.day_mtn_1, self.night_mtn_1, t_night)
        if name == 'mtn_far': return lerp_c(self.day_mtn_2, self.night_mtn_2, t_night)
        
        if name == 'wall': return lerp_c(self.day_house_wall, self.night_house_wall, t_night)
        if name == 'wood': return lerp_c(self.day_house_wood, self.night_house_wood, t_night)
        if name == 'roof': return lerp_c(self.day_house_roof, self.night_house_roof, t_night)
        
        if name == 'water': return lerp_c(self.water_day, self.water_night, t_night)
        if name == 'tree': return lerp_c(self.tree_day, self.tree_night, t_night)
        if name == 'line': return lerp_c(self.line_day, self.line_night, t_night)
        
        if name == 'window': 
            # Warm yellow light for windows at night
            c_lit = QColor(255, 220, 150)
            c_unlit = QColor(60, 70, 80) # Dark blue-grey for day glass/paper
            if t_night < 0.3: return c_unlit
            return lerp_c(c_unlit, c_lit, t_night)
            
        if name == 'line': return lerp_c(self.line_day, self.line_night, t_night)
        
        return Qt.red

class MountainParams:
    def __init__(self, seed, base_height, roughness, layer_id):
        self.seed = seed
        self.base_height = base_height
        self.roughness = roughness
        self.layer_id = layer_id
        self.points = self._generate()
        
    def _generate(self):
        rng = random.Random(self.seed)
        pts = []
        steps = 180 # 2 degree steps
        
        for i in range(steps):
            az = i * (360 / steps)
            rad = math.radians(az)
            
            # Ghibli-esque Rolling Hills (Restored)
            # Not jagged, but not flat. High amplitude, low frequency.
            if self.layer_id == 0:
                # Large distinct hills
                n = math.sin(rad * 3.0) * 3.5 + math.sin(rad * 1.5) * 2.0
                h = (7.0 + n) * self.base_height
            else:
                # Foreground 
                # Gentle slopes matching the image style
                n = math.sin(rad * 2.0) * 1.5 + math.sin(rad * 5.0) * 0.5
                h = (2.0 + n) * self.base_height
            
            # SUNRISE/SUNSET CLEARANCE (Dip at 90 and 270)
            # Create a "dipping" mask. 1.0 everywhere, 0.0 at 90/270.
            # Az is 0..360.
            # dist to 90
            d_east = abs(az - 90)
            d_west = abs(az - 270)
            
            # Gaussian dip width ~ 20 degrees
            dip_width = 25.0
            
            dip_factor = 1.0
            if d_east < dip_width:
                dip_factor = min(dip_factor, (d_east / dip_width))
            if d_west < dip_width:
                dip_factor = min(dip_factor, (d_west / dip_width))
                
            # Apply smooth dip
            h = h * dip_factor 
            if h < 0.5: h = 0.5 # Min ground level
            
            pts.append((az, h))
        return pts


class RealMountainProfile:
    """
    Wraps a HorizonProfile band into the same (az, h_deg) points format
    that draw_hybrid_strip expects, replacing procedural MountainParams.
    """

    def __init__(self, profile, band_id, height_scale=1.0, min_height=0.3, void_height=-0.2):
        """
        Args:
            profile: HorizonProfile instance
            band_id: which band to use ('far' or 'near')
            height_scale: multiplier for the elevation angles
            min_height: minimum height in degrees (ground level)
        """
        self.profile = profile
        self.band_id = band_id
        self.height_scale = height_scale
        self.min_height = min_height
        self.void_height = void_height
        self.points = self._build_points()

    def _build_points(self):
        raw = self.profile.get_band_points(self.band_id)
        if not raw:
            return [(i * 2, 1.0) for i in range(180)]  # Fallback flat

        pts = []
        for az, elev_deg in raw:
            # Check for void (approx -90 deg or -1.57 rad)
            if elev_deg < -80.0:
                h = self.void_height
            else:
                # Clamp: low real terrain
                h = max(self.min_height, elev_deg * self.height_scale)
            pts.append((az, h))
        return pts


class MinkaHouse:
    def __init__(self, seed, az, alt):
        self.az = az
        self.alt = alt # Base altitude (Ground level)
        self.seed = seed
        rng = random.Random(seed)
        
        # Procedural Geometry
        self.main_w = rng.uniform(4.0, 6.0)
        self.main_h = rng.uniform(2.5, 3.5)
        
        # Rotation: How much we see of the "Side"
        # 0 = Front facing view
        # +/- 30 = 3/4 view
        self.rotation = rng.uniform(-45, 45) 
        
        # L-Shape wing?
        self.has_wing = rng.random() > 0.5
        self.wing_w = rng.uniform(2.0, 3.5)
        self.wing_h = self.main_h * 0.8
        self.wing_side = rng.choice([-1, 1]) # Left or Right
        
        self.has_second_floor = rng.random() > 0.7
        if self.has_second_floor: self.main_h += 1.5

        # Roof Type
        self.roof_type = rng.choice(['hip', 'gable'])

    def draw(self, painter, projection_fn, theme, t_night, view_az, fov):
        # 1. Scale Calculation
        if hasattr(self, 'custom_scale'):
             scale_factor = self.custom_scale * VILLAGE_SCALE
        else:
             scale_factor = (1.0 - (self.alt * 0.08)) * VILLAGE_SCALE 
             
        if scale_factor < 0.05: return 
        if scale_factor > 2.5: scale_factor = 2.5

        # 2. Colors
        c_wall = theme.get_color('wall', t_night)
        c_wood = theme.get_color('wood', t_night)
        c_roof = theme.get_color('roof', t_night)
        c_win = theme.get_color('window', t_night)
        c_line = theme.get_color('line', t_night)
        
        # -- DRAWING GEOMETRY --
        w = self.main_w * scale_factor
        h = self.main_h * scale_factor
        d = 3.0 * scale_factor # Depth
        
        # Material Colors
        c_sides = c_wall.darker(110)
        c_stones = QColor(60, 60, 65) if t_night < 0.5 else QColor(20, 20, 25)

        # Generic Box Drawer
        def draw_cube(cx, cz, bw, bh, bd, is_stone=False):
            # 1. Rotate Center and vectors
            rad = math.radians(self.rotation)
            rc = math.cos(rad); rs = math.sin(rad)
            
            # Local vectors
            vx_x, vx_z = bw * rc, bw * rs
            vz_x, vz_z = -bd * rs, bd * rc
            
            # Center offset
            off_x, off_z = cx * rc - cz * rs, cx * rs + cz * rc
            
            # Generate Screen Vertices
            verts = {}
            for sx in [-1, 1]:
                for sy in [0, 1]:
                    for sz in [-1, 1]:
                        # Pos relative to center
                        # Note: we are in 'pixel' linear space relative to anchor?
                        # No, the 'projection_fn' passed here is actually 'obj_projector'
                        # which takes (alt_offset, az_offset_in_deg).
                        # We need to convert our linear box dimensions to angular offsets?
                        
                        # WAIT. The refactor changed the projection_fn contract.
                        # It now maps (a, z) -> screen_pt.
                        # (a, z) are offsets from house center in degrees?
                        
                        # Let's assume (alt, az) passed to obj_projector are Absolute coords?
                        # No, looking at `obj_projector` definition in `VillageOverlay.draw_with_projection`:
                        # It calculates offsets `d_az`, `d_alt` from `obj.az`, `obj.alt`.
                        # So we pass Absolute Coordinates.
                        
                        # We need to convert box meters/pixels to Degrees roughly.
                        # House width ~ 4-6 units.
                        # Scale factor applied.
                        
                        # Local offsets (px approx):
                        lx = off_x + sx * (bw/2.0)
                        lz = off_z + sz * (bd/2.0) # Depth is Z
                        ly = bh * sy # Height
                        
                        # Convert to Degrees Offset (Approx)
                        # Pixel scale is `px_per_deg`. Can we recover it?
                        # No. But we know standard sizes.
                        # Let's assume raw units are 'Degrees' in the local cluster?
                        # No, self.main_w is 4.0 ~ 4 degrees?
                        # Yes, standard house size ~ 5 deg width is reasonable.
                        
                        d_az = lx # degrees
                        d_alt = ly # degrees
                        
                        # Depth (Z) implies parallax x-shift?
                        # Parallax handled by 3D rotation in logical space?
                        # Standard stereographic maps lat/lon. "Depth" is just radius.
                        # We just map flat on the surface for now, ignore true depth Z except for draw order.
                        # Or we treat Z as modifier to Az/Alt?
                        # Let's keep it 2.5D billboard style: Z modifies X slightly -> perspective.
                        
                        # Simple Rotate 3D points *before* projection?
                        # We already did Local Rotation (rc, rs).
                        # lx, lz is position on "Ground Plane" relative to center.
                        
                        # Map (lx, lz) to (d_az, d_alt_ground_depth?)
                        # Stereo proj is 2D surface.
                        # We just map lx -> az_offset, lz -> nothing (collapsed)??
                        # No, that flattens depth.
                        # We need Perspective from Camera.
                        
                        # Simplified:
                        # Map ground footprint (lx, lz) to Az/Alt offsets
                        # If I walk "depth" away, my Az doesn't change much, my Alt might due to globe curve?
                        # Let's just treat box as flat footprint on ground.
                        
                        p_az = self.az + lx
                        p_alt = self.alt - lz * 0.1 # Fake depth tilt?
                        
                        # Top of wall
                        if sy == 1:
                            p_alt += ly
                            
                        # Project
                        verts[(sx, sy, sz)] = projection_fn(p_alt, p_az)
            
            # Filter None
            if not all(verts.values()): return

            # Polygons
            # Side Face (Right x=1)
            # Visibility based on 'lx' range vs View?
            # Or just Vector math?
            
            # Let's just draw all sides back-to-front?
            # Or Z-sort faces.
            
            # Simple Sort:
            # Side X=1 (Right) if rotation makes it visible.
            # Vector from center to camera is ~ (0, -1) in Z?
            # House fwd is (0, 1).
            # Rotated house fwd = (rs, rc).
            # If (rs) > 0, we see Left?
            
            # Just draw side then front.
            # Side
            side_x = 1 if math.sin(rad) < 0 else -1
            sv = [verts.get((side_x, 0, -1)), verts.get((side_x, 0, 1)), verts.get((side_x, 1, 1)), verts.get((side_x, 1, -1))]
            
            # Front Face (z=1) 
            fv = [verts.get((-1, 0, 1)), verts.get((1, 0, 1)), verts.get((1, 1, 1)), verts.get((-1, 1, 1))]
            
            # -- SHADOW --
            # Draw a flat shadow on the ground to anchor the house
            if all(fv) and all(sv):
                shad_poly = [
                    verts.get((side_x, 0, -1)), # Back Corner
                    verts.get((side_x, 0, 1)),  # Front Corner (Side)
                    verts.get((-side_x, 0, 1)), # Front Corner (Other)
                    verts.get((-side_x, 0, -1)) # Back Corner (Other)
                ]
                # Check valid
                shad_pts = []
                for p in shad_poly:
                    if p: shad_pts.append(QPointF(*p))
                
                if len(shad_pts) == 4:
                     painter.setBrush(QColor(0, 0, 0, 80)) # Softer shadow
                     painter.setPen(Qt.NoPen)
                     painter.drawPolygon(QPolygonF(shad_pts))
            
            # -- ENGAWA (Wooden Veranda) --
            # Skirt around the base
            # Draw before main body? No, outside it.
            # Just a small lip at the bottom of the visible faces.
            
            # Draw Sides
            # Make side significantly darker for depth
            painter.setBrush(c_sides.darker(150))
            painter.setPen(QPen(c_line, 1.0)) # OUTLINE
            painter.drawPolygon(QPolygonF([QPointF(*p) for p in sv]))
            
            # Draw Front
            if is_stone:
                painter.setBrush(c_stones)
            else:
                # Gradient Wall
                # We need bounding rect of fv for gradient
                f_poly = QPolygonF([QPointF(*p) for p in fv])
                br = f_poly.boundingRect()
                grad = QLinearGradient(br.topLeft(), br.bottomLeft())
                grad.setColorAt(0.0, c_wall)
                grad.setColorAt(1.0, c_wall.darker(110))
                painter.setBrush(QBrush(grad))
                
            painter.setPen(QPen(c_line, 1.0)) # OUTLINE
            painter.drawPolygon(QPolygonF([QPointF(*p) for p in fv]))
            
            # TIMBER FRAMING (Vertical Beams)
            if not is_stone:
                 # Draw lines at corners and mid points
                 painter.setPen(QPen(c_wood.darker(150), 2))
                 # Map normalized coords to face projections
                 # Left edge
                 l_top = verts.get((-1, 1, 1))
                 l_bot = verts.get((-1, 0, 1))
                 if l_top and l_bot: painter.drawLine(QPointF(*l_top), QPointF(*l_bot))
                 
                 # Right edge
                 r_top = verts.get((1, 1, 1))
                 r_bot = verts.get((1, 0, 1))
                 if r_top and r_bot: painter.drawLine(QPointF(*r_top), QPointF(*r_bot))
                 
                 # Top horizontal beam
                 if l_top and r_top: painter.drawLine(QPointF(*l_top), QPointF(*r_top))
            
            if is_stone: return # No windows on stone base
            
            if is_stone: return # No windows on stone base
            
            # -- HOUSE DETAILS --
            if all(fv):
                # We need to map 2D layout on the face to the 3D projected vertices.
                # Bilinear Interpolation helper
                def map_face(u, v, quad):
                     p0, p1, p2, p3 = quad
                     pt_u0 = p0 * (1-u) + p1 * u
                     pt_u1 = p3 * (1-u) + p2 * u
                     return pt_u0 * (1-v) + pt_u1 * v

                fv_pts = [QPointF(*p) for p in fv]
                
                # Engawa (Base Veranda)
                # Strip at bottom 0.0-0.15
                e_bl = map_face(0.0, -0.05, fv_pts) # Stick out slightly down/side?
                e_br = map_face(1.0, -0.05, fv_pts)
                e_tr = map_face(1.0, 0.15, fv_pts)
                e_tl = map_face(0.0, 0.15, fv_pts)
                
                painter.setBrush(c_wood)
                painter.drawPolygon(QPolygonF([e_bl, e_br, e_tr, e_tl]))
                
                # 1. Door (Bottom Center)
                # u from 0.4 to 0.6. v from 0.15 to 0.6
                d_bl = map_face(0.4, 0.15, fv_pts)
                d_br = map_face(0.6, 0.15, fv_pts)
                d_tr = map_face(0.6, 0.6, fv_pts)
                d_tl = map_face(0.4, 0.6, fv_pts)
                
                painter.setBrush(c_wood.darker(120))
                painter.drawPolygon(QPolygonF([d_bl, d_br, d_tr, d_tl]))
                
                # 2. Windows (Left and Right of door)
                win_color = theme.get_color('window', t_night)
                
                def draw_shoji_window(u_min, u_max, v_min, v_max):
                    w_poly = [
                        map_face(u_min, v_min, fv_pts),
                        map_face(u_max, v_min, fv_pts),
                        map_face(u_max, v_max, fv_pts),
                        map_face(u_min, v_max, fv_pts)
                    ]
                    painter.setBrush(win_color)
                    painter.setPen(Qt.NoPen)
                    painter.drawPolygon(QPolygonF(w_poly))
                    
                    # Grid (Muntins)
                    painter.setPen(QPen(c_wood.darker(130), 1))
                    # Mid Vertical
                    top = map_face((u_min+u_max)/2, v_max, fv_pts)
                    bot = map_face((u_min+u_max)/2, v_min, fv_pts)
                    painter.drawLine(top, bot)
                    # Mid Horizontal
                    left = map_face(u_min, (v_min+v_max)/2, fv_pts)
                    right = map_face(u_max, (v_min+v_max)/2, fv_pts)
                    painter.drawLine(left, right)
                    
                    # Frame rect
                    painter.setBrush(Qt.NoBrush)
                    painter.drawPolygon(QPolygonF(w_poly))

                # Left Window
                draw_shoji_window(0.1, 0.35, 0.3, 0.7)
                # Right Window
                draw_shoji_window(0.65, 0.9, 0.3, 0.7)

            # Roof
            # Peak
            # Make roof prominent overhang
            overhang = 1.2
            p_peak = projection_fn(self.alt + bh + 1.5, self.az + off_x)
            
            # Eaves points (approximate by expanding the top quad)
            # Actually, just drawing the triangle is simpler and robust.
            
            if p_peak:
                 painter.setBrush(c_roof)
                 painter.setPen(QPen(c_line, 1.0)) # Outline Roof
                 peak = QPointF(*p_peak)
                 
                 # Draw Main Slope (Front)
                 top_l = QPointF(*fv[3])
                 top_r = QPointF(*fv[2])
                 
                 # Overhang hack: extend bottom corners of roof slightly out
                 v_l = top_l - top_r
                 v_r = top_r - top_l
                 # Normalize? Nah, just simplistic extension
                 ext_l = top_l + QPointF(-10, 0) # Screen space hack? Bad.
                 # Let's trust proper verts.
                 
                 poly_roof = QPolygonF([top_l, top_r, peak])
                 painter.drawPolygon(poly_roof)
                 
                 # Side Slope (if Right visible)
                 if side_x == 1:
                     top_br = QPointF(*sv[2]) # FrontTop of side
                     top_bk_r = QPointF(*sv[3]) # BackTop of side
                     # Connect to peak?
                     # The peak is centered. The side slope goes from (FrontTop, BackTop) to Peak.
                     painter.setBrush(c_roof.darker(110))
                     painter.drawPolygon(QPolygonF([top_br, top_bk_r, peak]))
                    
        # 1. Wing (Behind logic sorted? No, draw Wing then Main if Wing is behind?)
        # Simple Z sort: Wing Z is positive (forward).
        # Draw Main then Side Wing.
        draw_cube(0, 0, w, h, d)
        
        if self.has_wing:
             wx = (w/2 + self.wing_w/2 - 0.5) * self.wing_side
             # Wing sticks out front (z+)
             wz = d/2 + self.wing_w/2 - 0.5 
             # For visuals, we just draw it. Painter's algo might fail for self-intersection overlap
             # but "Organic" usually means messy is ok.
             
             # Stone Base for Wing
             draw_cube(wx, wz, self.wing_w, 0.4*scale_factor, self.wing_w, is_stone=True)
             draw_cube(wx, wz, self.wing_w, self.wing_h, self.wing_w)
             
             
        # Stone Base for Main
        draw_cube(0, 0, w, 0.5*scale_factor, d, is_stone=True)
        # Windows?
        # Only on main block front
        if t_night > 0.4:
            # Simple glow rect in middle of main block
            # Re-calc center front
            pt_c = projection_fn(self.alt + h/2, self.az)
            if pt_c:
                painter.setBrush(c_win)
                #painter.drawEllipse(QPointF(*pt_c), 5, 5)




class VillageLantern:
    """Tōrō - Traditional Japanese Stone Lantern"""
    def __init__(self, rng, az_range, alt_range):
        self.az = rng.uniform(*az_range)
        self.alt = rng.uniform(*alt_range)
        self.seed = rng.randint(0, 10000)
        # Lantern style variation
        self.style = rng.choice(['ikekomi', 'tachi', 'yukimi'])  # ground, standing, snow-viewing
        
    def draw(self, painter, projection_fn, theme, t_night, view_az, fov):
        # Visibility check
        rel_az = (self.az - view_az + 180) % 360 - 180
        if abs(rel_az) > fov * 0.8: return
        
        # Project center
        pt = projection_fn(self.alt, self.az)
        if not pt: return 
        
        base = QPointF(*pt)
        
        # Scale based on FOV and altitude (similar to houses)
        if hasattr(self, 'custom_scale'):
            alt_scale = self.custom_scale 
        else:
            alt_scale = 1.0 - (self.alt * 0.06)
            
        if alt_scale < 0.1: return 
        if alt_scale > 2.0: alt_scale = 2.0
        
        # FOV scale factor
        fov_scale = 100.0 / fov 
        
        scale = alt_scale * fov_scale * VILLAGE_SCALE
        
        # Stone colors (grey with slight variation)
        rng = random.Random(self.seed)
        grey_base = 70 + rng.randint(-10, 10)
        c_stone = QColor(grey_base, grey_base - 5, grey_base - 10)
        c_stone_dark = c_stone.darker(130)
        c_stone_light = c_stone.lighter(110)
        
        painter.setPen(Qt.NoPen)
        
        # === STRUCTURE (bottom to top) ===
        # Base dimensions in pixels (will be scaled)
        
        # 1. Base (Kiso) - wide foundation
        base_w = 14 * scale
        base_h = 4 * scale
        painter.setBrush(c_stone_dark)
        painter.drawRect(QRectF(
            base.x() - base_w/2, base.y() - base_h,
            base_w, base_h
        ))
        
        # 2. Column/Pillar (Sao)
        col_w = 5 * scale
        col_h = 22 * scale
        col_top = base.y() - base_h - col_h
        painter.setBrush(c_stone)
        painter.drawRect(QRectF(
            base.x() - col_w/2, col_top,
            col_w, col_h
        ))
        
        # 3. Light Chamber Base (Chudai)
        chudai_w = 16 * scale
        chudai_h = 4 * scale
        chudai_top = col_top - chudai_h
        painter.setBrush(c_stone_dark)
        painter.drawRect(QRectF(
            base.x() - chudai_w/2, chudai_top,
            chudai_w, chudai_h
        ))
        
        # 4. Light Chamber (Hibukuro) - the actual lantern box
        hibukuro_w = 14 * scale
        hibukuro_h = 12 * scale
        hibukuro_top = chudai_top - hibukuro_h
        
        # Draw outer stone frame
        painter.setBrush(c_stone)
        painter.drawRect(QRectF(
            base.x() - hibukuro_w/2, hibukuro_top,
            hibukuro_w, hibukuro_h
        ))
        
        # Inner light window (paper/glass)
        window_margin = 2 * scale
        window_rect = QRectF(
            base.x() - hibukuro_w/2 + window_margin,
            hibukuro_top + window_margin,
            hibukuro_w - window_margin*2,
            hibukuro_h - window_margin*2
        )
        
        # Window color depends on night
        if t_night > 0.2:
            # Lit at night - warm glow
            alpha = int(200 * min(1.0, t_night * 1.5))
            c_window = QColor(255, 240, 200, alpha)
            painter.setBrush(c_window)
        else:
            # Day - dark interior
            painter.setBrush(QColor(40, 35, 30))
        painter.drawRect(window_rect)
        
        # 5. Roof (Kasa) - pagoda style with slight curve
        roof_w = 20 * scale
        roof_h = 7 * scale
        roof_top = hibukuro_top - roof_h
        
        # Trapezoid roof shape
        roof_poly = QPolygonF([
            QPointF(base.x() - roof_w/2, hibukuro_top),      # bottom left
            QPointF(base.x() + roof_w/2, hibukuro_top),      # bottom right
            QPointF(base.x() + roof_w*0.35, roof_top),       # top right
            QPointF(base.x() - roof_w*0.35, roof_top),       # top left
        ])
        painter.setBrush(c_stone_dark)
        painter.drawPolygon(roof_poly)
        
        # 6. Finial (Hōju) - decorative top
        hoju_r = 3 * scale
        painter.setBrush(c_stone_light)
        painter.drawEllipse(QPointF(base.x(), roof_top - hoju_r), hoju_r, hoju_r)
        
        # === LIGHT GLOW (Night only) ===
        if t_night > 0.15:
            alpha = int(180 * t_night)
            glow_radius = 30 * scale
            
            glow_center = window_rect.center()
            
            # Soft warm glow
            grad = QRadialGradient(glow_center, glow_radius)
            grad.setColorAt(0.0, QColor(255, 220, 150, alpha))
            grad.setColorAt(0.4, QColor(255, 180, 100, int(alpha * 0.4)))
            grad.setColorAt(1.0, QColor(255, 140, 50, 0))
            
            painter.setBrush(grad)
            painter.drawEllipse(glow_center, glow_radius, glow_radius)

class VillageTree:
    def __init__(self, rng, az_range, alt_range):
        self.az = rng.uniform(*az_range)
        self.alt = rng.uniform(*alt_range)
        self.height_mod = rng.uniform(0.8, 1.2)
        self.seed = rng.randint(0, 1000)
        self.is_sakura = False

    def draw(self, painter, projection_fn, theme, t_night, view_az, fov):
        # Projection check
        pt_base = projection_fn(self.alt, self.az)
        if not pt_base: return
        pt_base = QPointF(*pt_base)

        # Scale based on altitude (similar to houses)
        if hasattr(self, 'custom_scale'):
            alt_scale = self.custom_scale 
        else:
            alt_scale = 1.0 - (self.alt * 0.06)
            
        if alt_scale < 0.05: return 
        if alt_scale > 2.0: alt_scale = 2.0
        
        # FOV scale factor - ensures trees scale with zoom like houses
        fov_scale = 100.0 / fov
        
        scale = alt_scale * fov_scale * VILLAGE_SCALE
        
        # Use seeded RNG for consistent appearance (no flickering)
        rng = random.Random(self.seed)
        
        # Colors
        if self.is_sakura:
             # Pink/White
             c_leaf_base = QColor(255, 192, 203) if t_night < 0.5 else QColor(100, 60, 70)
             # Consistent variation based on seed (no flickering)
             if rng.random() > 0.5: c_leaf_base = c_leaf_base.lighter(110)
        else:
             c_leaf_base = theme.get_color('tree', t_night)
             
        c_trunk = theme.get_color('wood', t_night).darker(150)
        
        # Trunk with texture
        w_trunk = 4 * scale
        h_trunk = 25 * scale * self.height_mod
        
        painter.setPen(Qt.NoPen)
        
        # Trunk gradient for volume
        trunk_gradient = QLinearGradient(pt_base.x() - w_trunk/2, pt_base.y(), 
                                         pt_base.x() + w_trunk/2, pt_base.y())
        trunk_gradient.setColorAt(0.0, c_trunk.darker(130))
        trunk_gradient.setColorAt(0.5, c_trunk)
        trunk_gradient.setColorAt(1.0, c_trunk.darker(120))
        painter.setBrush(QBrush(trunk_gradient))
        
        # Tapered trunk
        path_trunk = QPainterPath()
        path_trunk.moveTo(pt_base + QPointF(-w_trunk/2, 0))
        path_trunk.lineTo(pt_base + QPointF(w_trunk/2, 0))
        path_trunk.lineTo(pt_base + QPointF(w_trunk*0.3, -h_trunk))
        path_trunk.lineTo(pt_base + QPointF(-w_trunk*0.3, -h_trunk))
        path_trunk.closeSubpath()
        painter.drawPath(path_trunk)
        
        # Small branches
        branch_color = c_trunk.darker(110)
        painter.setPen(QPen(branch_color, max(1, 1.5 * scale)))
        num_branches = rng.randint(3, 5)
        for i in range(num_branches):
            branch_y = -h_trunk * rng.uniform(0.4, 0.9)
            branch_len = rng.uniform(8, 15) * scale
            branch_angle = rng.uniform(-45, 45)
            dx = branch_len * math.sin(math.radians(branch_angle))
            dy = -abs(branch_len * 0.3)
            
            painter.drawLine(pt_base + QPointF(0, branch_y),
                           pt_base + QPointF(dx, branch_y + dy))
        
        painter.setPen(Qt.NoPen)
        
        # ORGANIC FOLIAGE - Multiple layers with irregular shapes
        center_leaf = pt_base + QPointF(0, -h_trunk * 0.8)
        
        # Layer 1: Background soft blob (largest, most transparent)
        for i in range(3):
            offset_x = rng.uniform(-18, 18) * scale
            offset_y = rng.uniform(-20, 10) * scale
            blob_center = center_leaf + QPointF(offset_x, offset_y)
            
            blob_radius = rng.uniform(20, 28) * scale
            
            # Radial gradient for volume
            gradient = QRadialGradient(blob_center, blob_radius)
            c_light = c_leaf_base.lighter(rng.randint(115, 135))
            c_dark = c_leaf_base.darker(rng.randint(110, 130))
            c_light.setAlpha(100)
            c_dark.setAlpha(60)
            gradient.setColorAt(0.0, c_light)
            gradient.setColorAt(0.7, c_leaf_base)
            gradient.setColorAt(1.0, c_dark)
            painter.setBrush(QBrush(gradient))
            
            # Irregular organic shape
            blob_path = QPainterPath()
            num_points = rng.randint(12, 18)
            for j in range(num_points):
                angle = (j / num_points) * 2 * math.pi
                # Add noise to radius for organic feel
                r_var = blob_radius * rng.uniform(0.7, 1.0)
                px = blob_center.x() + r_var * math.cos(angle)
                py = blob_center.y() + r_var * math.sin(angle)
                
                if j == 0:
                    blob_path.moveTo(px, py)
                else:
                    # Use quadratic curves for smoother edges
                    prev_angle = ((j-1) / num_points) * 2 * math.pi
                    ctrl_r = blob_radius * rng.uniform(0.8, 1.1)
                    ctrl_angle = (angle + prev_angle) / 2
                    ctrl_x = blob_center.x() + ctrl_r * math.cos(ctrl_angle)
                    ctrl_y = blob_center.y() + ctrl_r * math.sin(ctrl_angle)
                    blob_path.quadTo(ctrl_x, ctrl_y, px, py)
            
            blob_path.closeSubpath()
            painter.drawPath(blob_path)
        
        # Layer 2: Mid-layer clusters (medium size, more opaque)
        for i in range(4):
            offset_x = rng.uniform(-12, 12) * scale
            offset_y = rng.uniform(-18, 8) * scale
            cluster_center = center_leaf + QPointF(offset_x, offset_y)
            cluster_radius = rng.uniform(12, 18) * scale
            
            gradient = QRadialGradient(cluster_center, cluster_radius)
            c_mid = c_leaf_base.lighter(rng.randint(105, 120))
            gradient.setColorAt(0.0, c_mid)
            gradient.setColorAt(0.6, c_leaf_base)
            gradient.setColorAt(1.0, c_leaf_base.darker(120))
            painter.setBrush(QBrush(gradient))
            
            # Smaller irregular shape
            cluster_path = QPainterPath()
            num_points = rng.randint(8, 12)
            for j in range(num_points):
                angle = (j / num_points) * 2 * math.pi
                r_var = cluster_radius * rng.uniform(0.75, 1.0)
                px = cluster_center.x() + r_var * math.cos(angle)
                py = cluster_center.y() + r_var * math.sin(angle)
                
                if j == 0:
                    cluster_path.moveTo(px, py)
                else:
                    cluster_path.lineTo(px, py)
            
            cluster_path.closeSubpath()
            painter.drawPath(cluster_path)
        
        # Layer 3: Highlight details (small, bright accents)
        for i in range(5):
            offset_x = rng.uniform(-10, 10) * scale
            offset_y = rng.uniform(-15, 5) * scale
            detail_center = center_leaf + QPointF(offset_x, offset_y)
            detail_radius = rng.uniform(5, 9) * scale
            
            # Bright highlights
            c_highlight = c_leaf_base.lighter(rng.randint(130, 150))
            c_highlight.setAlpha(180)
            
            gradient = QRadialGradient(detail_center, detail_radius)
            gradient.setColorAt(0.0, c_highlight)
            gradient.setColorAt(1.0, QColor(c_highlight.red(), c_highlight.green(), 
                                           c_highlight.blue(), 0))
            painter.setBrush(QBrush(gradient))
            painter.drawEllipse(detail_center, detail_radius, detail_radius)

class VillageOverlay(QObject):
    request_update = pyqtSignal()
    
    def __init__(self, parent=None, layer_id=0, seed=42):
        super().__init__(parent)
        self.layer_id = layer_id
        self.seed = seed
        self.theme = VillageTheme()
        self.profile = None
        
        self.houses = []
        self.trees = []
        self.lanterns = []
        self.paths = []
        self.paddies = []

    def set_profile(self, profile):
        """Receive new horizon profile and regenerate village on the terrain."""
        print(f"[VillageOverlay] Profile updated. Regenerating village...")
        self.profile = profile
        #self._generate() #TODO: Molt millorable i poc madur. es desactiva temporalment
        self.request_update.emit()

    def _generate(self):
        rng = random.Random(self.seed + 1)
        self.houses = []
        self.trees = []
        self.lanterns = []
        self.paths = []
        self.paddies = []

        if not self.profile:
            return

        # Define bands to populate and their distance scales
        # Closer = Larger Scale
        pop_bands = [
            ("gnd_0_250",    2.00,  0.05), # ID, Scale, Density(prob)
            ("gnd_250_500",  1.50,  0.10),
            ("gnd_500_1k",   1.00,  0.20),
            ("near_1_1.5",   0.70,  0.30),
            ("near_1.5_2",   0.50,  0.30),
            ("near_2_3",     0.40,  0.40),
            ("near_3_4",     0.30,  0.40),
            ("near_4_5",     0.25,  0.30),
            ("mid_5_7",      0.15,  0.20), # Very far houses
        ]

        # Iterate through bands
        for band_id, scale_base, density in pop_bands:
            # Get valid points from this band
            pts = self.profile.get_band_points(band_id) 
            if not pts: continue
            
            # Subsample points based on density
            # pts is list of (az, elev)
            # We shuffle and pick N
            
            # How many objects for this band?
            # Based on circumference roughly... just use heuristic count
            target_count = int(15 * density) 
            
            candidates = [p for p in pts if p[1] > -20.0] # Valid points only
            if not candidates: continue
            
            # Shuffle candidates
            # (Use rng logic manually to avoid full shuffle overhead if large)
            chosen = []
            for _ in range(target_count):
                if not candidates: break
                idx = rng.randint(0, len(candidates)-1)
                chosen.append(candidates.pop(idx))
            
            for az, elev in chosen:
                # Place House?
                if rng.random() > 0.6:
                    h = MinkaHouse(rng.randint(0, 99999), az, elev)
                    h.rotation = rng.uniform(-20, 20)
                    # Store custom scale on the house object
                    h.custom_scale = scale_base * rng.uniform(0.8, 1.2)
                    self.houses.append(h)
                    
                    # Tree near house
                    if rng.random() > 0.3:
                        t_az = az + rng.uniform(-1.0, 1.0) * (5.0 / (scale_base * 10))
                        t = VillageTree(rng, (t_az, t_az), (elev, elev))
                        t.custom_scale = scale_base * rng.uniform(0.8, 1.2)
                        if rng.random() > 0.8: t.is_sakura = True
                        self.trees.append(t)
                        
                # Just Tree?
                elif rng.random() > 0.3:
                    t = VillageTree(rng, (az, az), (elev, elev))
                    t.custom_scale = scale_base * rng.uniform(0.9, 1.4)
                    if rng.random() > 0.9: t.is_sakura = True
                    self.trees.append(t)


    def draw_with_projection(self, painter, projection_fn, width, height, current_hour, current_azimuth, zoom_level, elevation_angle):
        # HYBRID MODE
        # 1. Use projection_fn to find the "Ground Point" (Anchor) on the curved horizon.
        # 2. Draw objects linearly relative to that anchor (Billboards) to avoid distortion.
        
        # FOV Calc for Scale
        fov_deg = 100.0 / zoom_level
        px_per_deg = width / fov_deg
        
        # Time
        val = math.cos((current_hour / 24.0) * 2 * math.pi) 
        t_night = (val + 1) / 2.0
        t_night = t_night * t_night * (3 - 2 * t_night)
        t_night = max(0.0, min(1.0, t_night)) 
        
        painter.setRenderHint(QPainter.Antialiasing)

        # === PARALLAX for village objects ===
        # We now use the profile, so objects are "baked" into a specific band.
        # But for parallax, we should ideally use the parallax factor OF THAT BAND.
        # Since we don't store the band ID on the object easily right now, 
        # let's assume a generic parallax for "village layer" or derive it?
        # Actually, simpler: Objects at different distances should move differently.
        # But for now, we'll keep it at 1.0 to ensure zero drift relative to Stars/Compass.
        parallax_objects = 1.0 # 1.0 = Perfectly aligned with coordinate system.
        
        def make_parallax_projection(base_proj_fn, parallax_factor):
            def parallax_proj(alt, az):
                parallax_offset = (parallax_factor - 1.0) * current_azimuth
                adjusted_az = az + parallax_offset
                return base_proj_fn(alt, adjusted_az)
            return parallax_proj
        
        proj_objects = make_parallax_projection(projection_fn, parallax_objects)

        # 3. Objects (Lanterns -> Houses -> Trees)
        all_objects = []
        if elevation_angle < 75.0:
            for h in self.houses: all_objects.append(('house', h))
            for t in self.trees: all_objects.append(('tree', t))
            for l in self.lanterns: all_objects.append(('lantern', l))
            
            # Sort by distance (scale) then altitude?
            # Actually, sort by scale (smaller = further = draw first)
            # Or assume altitude correlation.
            # Best: draw small scales first.
            all_objects.sort(key=lambda x: getattr(x[1], 'custom_scale', 1.0)) # Smallest (Far) first
 
        
        for type_str, obj in all_objects:
             # Make individual projector
             center_az = obj.az
             center_alt = obj.alt
             
             # Get Screen Center of object (Anchor)
             pt_center = proj_objects(center_alt, center_az)
             if not pt_center: continue
             
             cx, cy = pt_center
             
             # Rotation Logic
             pt_up = proj_objects(center_alt + 1.0, center_az)
             current_scale = getattr(obj, 'custom_scale', 1.0)

             angle_rad = 0.0
             if pt_up:
                 ux, uy = pt_up
                 dx, dy = ux - cx, uy - cy
                 curr_angle = math.atan2(dy, dx)
                 target_angle = -math.pi / 2
                 angle_rad = curr_angle - target_angle
             
             sin_a = math.sin(angle_rad)
             cos_a = math.cos(angle_rad)
             
             def obj_projector(a, z):
                 # a, z are absolute coordinates requested by house logic.
                 d_az = (z - center_az + 180) % 360 - 180
                 d_alt = a - center_alt
                 
                 # Apply Custom Scale here!
                 # The object drawing logic uses 'scale' internally but based on 'alt'.
                 # We override/multiply by our distance-based scale.
                 
                 # Linear pixel offset (Unrotated)
                 raw_dx = d_az * px_per_deg
                 raw_dy = -d_alt * px_per_deg
                 
                 rot_dx = raw_dx * cos_a - raw_dy * sin_a
                 rot_dy = raw_dx * sin_a + raw_dy * cos_a
                 
                 return cx + rot_dx, cy + rot_dy
                 
             # We need to inject the custom scale into the draw method?
             # The draw methods calculate 'scale' based on 'alt' and 'fov'.
             # We should patch them or modify them?
             # Let's modify the draw calls below to use the custom_scale if present.
             
             # ACTUALLY: MinkaHouse.draw uses 'self.alt' to calc scale.
             # We should monkey-patch 'VILLAGE_SCALE' globally? No.
             # Better: Modify MinkaHouse.draw and Tree.draw to accept an override scale.
             # Or... update the object's logic.
             # For now, let's rely on the fact that MinkaHouse uses 'self.alt' to scale.
             # BUT we are setting 'self.alt' to the horizon height (e.g. -2 deg).
             # The original logic: lower alt (-5) = closer = bigger.
             # Our new alts are real (-2 to -0.5).
             # We need to decoupled Position from Scale.
             pass 

             # HACK: Pass custom_scale via a temporary attribute and modify draw methods separately?
             # Yes, I will update MinkaHouse.draw and VillageTree.draw in the next step to look for 'custom_scale'.
             
             obj.draw(painter, obj_projector, self.theme, t_night, current_azimuth, fov_deg)

    def _set_linear_rendering(self, enabled):
        self.use_linear = enabled
        
    def handle_click_projection(self, click_az, click_alt, view_az, fov):
        for h in self.houses:
            az_diff = abs((h.az - click_az + 180)%360 - 180)
            alt_diff = abs(h.alt - click_alt)
            if az_diff < 3.0 and alt_diff < 4.0:
                print(f"Clicked House at Az {h.az}")
                return h.az
        return None
